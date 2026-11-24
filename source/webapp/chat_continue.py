"""The Continue button of the chat's Settings sidebar: wake the assistant in a
room without a message from the user, one or more times.

One press is a series: `count` wake-ups, the first at once, each later one
`sleep_seconds` after the reply before it landed, so the machine cools between
them. A wake-up is a `wake` row in the room plus the inbox item a message of
the user's would enqueue (chat/wake.py); the assistant's turn is an ordinary
one from there.

A thread of this process carries the series, and nothing is stored: a webapp
restart ends it after the turn in progress. It ends early when the user
writes in the room (they have taken the conversation back), when a wake-up's
run does not finish, and on Cancel, which stops the wake-ups still to come
and leaves the turn in progress alone.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from flask import Response, abort, jsonify, request

import db
from agents.config import ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE
from chat.wake import WAKE_KIND, continue_meta, continue_notice

from .chat_api import _parse_uuid
from .core import app

logger = logging.getLogger(__name__)

# What the two pickers offer. Validated here, so the page is not the only
# thing standing between a typo and a hundred wake-ups.
CONTINUE_COUNTS: tuple[int, ...] = (1, 2, 3)
SLEEP_SECONDS: tuple[int, ...] = (30, 60, 120)

# How often the series looks at the database while it waits.
POLL_SECONDS: float = 2.0
# No run for a wake-up after this long means no worker took it (the
# supervisor is not running): waking again would only queue more of the same.
RUN_START_TIMEOUT_SECONDS: float = 180.0
# A run still going after this long is not one to wait behind.
RUN_TIMEOUT_SECONDS: float = 1800.0

_RUN_ENDED = frozenset({"finished", "stopped", "failed", "killed"})

# Why a series ended, as the sidebar says it.
ENDED_TEXT: dict[str, str] = {
    "done": "Done.",
    "cancelled": "Cancelled.",
    "user_wrote": "Stopped: you wrote a message.",
    "run_stopped": "Stopped: the turn was stopped.",
    "run_failed": "Stopped: the turn failed.",
    "not_started": "Stopped: no worker took the wake-up. Is the supervisor running?",
    "timed_out": "Stopped: the turn did not finish.",
    "error": "Stopped: an error, see the webapp log.",
}


@dataclass
class ContinueSeries:
    """One press of Continue. Written by its own thread, read by requests;
    every field is a single assignment, so no lock guards them."""

    room_uuid: UUID
    count: int
    sleep_seconds: int
    index: int = 0                      # the wake-up it is on, from 1
    phase: str = "running"              # "running" | "sleeping"
    next_at: datetime | None = None     # when the sleep ends
    ended: str | None = None            # a key of ENDED_TEXT once over
    first_wake_id: int = 0
    wake_uuid: UUID | None = None       # the wake-up it is on
    cancel: threading.Event = field(default_factory=threading.Event)

    def state(self) -> dict[str, Any]:
        left = None
        if self.ended is None and self.phase == "sleeping" and self.next_at:
            left = max(0, round((self.next_at - datetime.now(UTC)).total_seconds()))
        return {
            "active": self.ended is None,
            "index": self.index,
            "count": self.count,
            "phase": self.phase if self.ended is None else None,
            "seconds_left": left,
            "ended": ENDED_TEXT.get(self.ended) if self.ended else None,
        }


# The newest series of each room: the one in progress, or the last one's
# outcome until the next press.
_series: dict[UUID, ContinueSeries] = {}
_series_lock = threading.Lock()


def post_wake(series: ContinueSeries, index: int) -> None:
    """Wake the assistant: the `wake` row, the inbox item naming it, and the
    working bubble a user's message gets."""
    human = db.get_human_user()
    if human is None:
        raise RuntimeError("no human user seeded")
    wake = db.post_chat_message(
        series.room_uuid, human.uuid, continue_notice(index, series.count),
        kind=WAKE_KIND, meta=continue_meta(index, series.count))
    series.index = index
    series.phase = "running"
    series.next_at = None
    series.first_wake_id = series.first_wake_id or wake.id
    series.wake_uuid = wake.uuid
    db.enqueue(ASSISTANT_UUID, {
        "room_uuid": str(series.room_uuid), "message_uuid": str(wake.uuid)})
    db.upsert_progress(
        series.room_uuid, ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE, restart=True)


def _await_run(series: ContinueSeries, wake_uuid: UUID) -> str | None:
    """Wait for the wake-up's run to end. None when it finished and the series
    may go on; otherwise why the series ends."""
    started = time.monotonic()
    while True:
        if series.cancel.wait(POLL_SECONDS):
            return "cancelled"
        run = db.assistant_run_for_request(wake_uuid)
        status = run.status if run is not None else None
        # Each look is its own transaction, so the next one sees new rows.
        db.session.rollback()
        if status in _RUN_ENDED:
            return None if status == "finished" else (
                "run_failed" if status == "failed" else "run_stopped")
        waited = time.monotonic() - started
        if status is None and waited > RUN_START_TIMEOUT_SECONDS:
            return "not_started"
        if waited > RUN_TIMEOUT_SECONDS:
            return "timed_out"


def _sleep(series: ContinueSeries) -> str | None:
    """The pause before the next wake-up. None when it ran its course."""
    series.phase = "sleeping"
    series.next_at = datetime.now(UTC) + timedelta(seconds=series.sleep_seconds)
    while datetime.now(UTC) < series.next_at:
        left = (series.next_at - datetime.now(UTC)).total_seconds()
        if series.cancel.wait(min(POLL_SECONDS, max(left, 0))):
            return "cancelled"
        wrote = db.human_wrote_after(series.room_uuid, series.first_wake_id)
        db.session.rollback()
        if wrote:
            return "user_wrote"
    return None


def run_series(series: ContinueSeries) -> None:
    """The series after its first wake-up, which the request already posted:
    wait for each run, sleep, wake again."""
    try:
        index = 1
        while True:
            ended = _await_run(series, series.wake_uuid)
            if ended is None and index >= series.count:
                ended = "done"
            if ended is None:
                ended = _sleep(series)
            if ended is not None:
                series.ended = ended
                return
            index += 1
            post_wake(series, index)
    except Exception:
        logger.exception("continue series failed in room %s", series.room_uuid)
        series.ended = "error"


def _run_series_thread(series: ContinueSeries) -> None:
    with app.app_context():
        try:
            run_series(series)
        finally:
            # The thread's own scoped session; nothing else will close it.
            db.session.remove()


@app.route("/chat/api/rooms/<room_uuid>/continue",
           methods=["GET", "POST", "DELETE"])
def chat_room_continue(room_uuid: str) -> Response | tuple[Response, int]:
    """GET: the room's series, in progress or last ended. POST `{count,
    sleep_seconds}`: start one — the first wake-up is posted before this
    returns. DELETE: cancel the wake-ups still to come."""
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
        abort(404, "room not found")
    with _series_lock:
        current = _series.get(ruuid)

    if request.method == "GET":
        return jsonify(current.state() if current else {"active": False})

    if request.method == "DELETE":
        if current is not None and current.ended is None:
            current.cancel.set()
        return jsonify({"ok": True})

    if room.room_type == "direct" or ASSISTANT_UUID not in db.get_room_member_uuids(ruuid):
        abort(400, "this room has no assistant to wake")
    data = request.get_json(silent=True) or {}
    count, sleep_seconds = data.get("count"), data.get("sleep_seconds")
    if count not in CONTINUE_COUNTS:
        abort(400, "count must be one of: " + ", ".join(map(str, CONTINUE_COUNTS)))
    if sleep_seconds not in SLEEP_SECONDS:
        abort(400, "sleep_seconds must be one of: "
              + ", ".join(map(str, SLEEP_SECONDS)))
    series = ContinueSeries(ruuid, count, sleep_seconds)
    with _series_lock:
        current = _series.get(ruuid)
        if current is not None and current.ended is None:
            abort(409, "a continue series is already running in this room")
        _series[ruuid] = series
    try:
        post_wake(series, 1)
    except Exception:
        series.ended = "error"
        raise
    threading.Thread(
        target=_run_series_thread, args=(series,), daemon=True,
        name=f"continue-{ruuid}").start()
    return jsonify(series.state()), 202
