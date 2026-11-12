"""The housekeeping role: the work that follows a turn, one job at a time.

When a run ends, the assistant enqueues one item for this role. The worker
expands it into that run's jobs (`housekeeping_job` rows), then runs every job
that is waiting, in order. It starts as soon as nothing else is working, and a
new message kills the model call in progress so the turn has the machine to
itself. The killed job runs again when the system is idle.

Design: notes/housekeeping-design.md.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

import db
from agents.base import Agent
from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID

logger = logging.getLogger(__name__)

PREEMPT_SIGNAL = signal.SIGUSR1

#: Failures a job is retried after. A killed attempt is not a failure.
MAX_RETRIES = 1

#: Chunk jobs queued for a room when one of its runs ends. A turn adds about
#: two messages, so two chunks per turn keeps up with a room and works
#: through an older room's backlog a little at a time.
CHUNK_JOBS_PER_RUN = 2
#: Chunk jobs queued when the operator asks for the rest of a room.
CHUNK_JOBS_PER_REQUEST = 200

#: Model calls in a row before the worker rests, and for how long. The
#: operator's machine takes 10 to 15 calls in a row before it needs a minute
#: to cool, so the burst uses the low end.
BURST_CALLS = 10
COOLDOWN_SECONDS = 60.0


def _is_local_model(model_uuid: UUID) -> bool:
    """Whether a model answers on this machine: a loopback endpoint, and not
    a `:cloud` name, which a local server relays to a remote one."""
    from agents.model_groups import model_base_url
    from diary.embeddings import is_loopback_url

    url = model_base_url(model_uuid)
    if url is None or not is_loopback_url(url):
        return False
    try:
        _provider, name, _args = db.resolved_model_kwargs(model_uuid)
    except Exception:
        return False
    return not (name.endswith(":cloud") or "-cloud" in name)


class Preempted(BaseException):
    """A turn started while a housekeeping call was running.

    A BaseException so that it passes through the model-fallback loop in
    `ModelGroupAgent._structured_completion`, which treats any Exception as
    one model failing and tries the next."""


class TurnWatch:
    """Watches for turn activity while a housekeeping job runs.

    A daemon thread asks `probe()` every `poll_seconds`. When it answers true
    the watch remembers it and, if the main thread is inside an
    `interruptible()` window, signals it. The handler raises `Preempted` there,
    which breaks even a blocked network read (PEP 475), so a call stops while
    a model is still prefilling and has produced nothing.

    The job declares where it may be interrupted. `interruptible()` wraps the
    model call. `shielded()` wraps a database write made from inside that
    call, so the signal never lands in the middle of a commit; a turn seen
    meanwhile raises as the shield ends.

    Must be entered from the main thread, where Python runs signal handlers.
    A `None` probe makes the watch inert.
    """

    def __init__(self, probe: Callable[[], bool] | None, *,
                 poll_seconds: float = 2.0) -> None:
        self._probe = probe
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._seen = threading.Event()
        self._halt = threading.Event()
        self._interruptible = False
        self._thread: threading.Thread | None = None
        self._previous: Any = None
        self._main = threading.main_thread()

    def __enter__(self) -> "TurnWatch":
        if self._probe is None:
            return self
        if threading.current_thread() is not self._main:
            raise RuntimeError("TurnWatch must be entered from the main thread")
        self._previous = signal.signal(PREEMPT_SIGNAL, self._on_signal)
        self._thread = threading.Thread(
            target=self._watch, name="turn-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._probe is None:
            return False
        # No more raising, then no more signalling, and only then the old
        # handler: a late signal under the default disposition would end the
        # process.
        with self._lock:
            self._interruptible = False
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        signal.signal(PREEMPT_SIGNAL, self._previous)
        return False

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        if self._interruptible:
            raise Preempted()

    def _watch(self) -> None:
        assert self._probe is not None
        while not self._halt.wait(self._poll):
            try:
                hit = bool(self._probe())
            except Exception:
                logger.warning("turn watch: probe failed; still watching",
                               exc_info=True)
                continue
            if not hit:
                continue
            with self._lock:
                self._seen.set()
                if self._interruptible and self._main.ident is not None:
                    signal.pthread_kill(self._main.ident, PREEMPT_SIGNAL)
            return

    @contextmanager
    def interruptible(self) -> Generator[None, None, None]:
        """A window in which a turn may interrupt the main thread. Entering
        with a turn already seen raises at once."""
        with self._lock:
            if self._seen.is_set():
                raise Preempted()
            self._interruptible = True
        try:
            yield
        finally:
            with self._lock:
                self._interruptible = False

    @contextmanager
    def shielded(self) -> Generator[None, None, None]:
        """Inside an interruptible window, a stretch that must not be
        interrupted. A turn seen during it raises when it ends."""
        with self._lock:
            was = self._interruptible
            self._interruptible = False
        yield
        with self._lock:
            hit = was and self._seen.is_set()
            self._interruptible = was and not hit
        if hit:
            raise Preempted()


class HousekeepingAgent(Agent):
    """Expand each inbox item into its run's jobs, then run every waiting job."""

    uses_model_group = False

    #: How often the worker asks whether a turn is in progress, both while it
    #: waits to start a job and while a job's call runs.
    POLL_SECONDS: float = 2.0
    #: How often a running call's reply is written to its attempt row.
    PROGRESS_SECONDS: float = 2.0
    BURST_CALLS: int = BURST_CALLS
    COOLDOWN_SECONDS: float = COOLDOWN_SECONDS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Model calls since the worker last rested.
        self._burst = 0

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        # One worker, so anything marked running was left by a dead one.
        db.recover_running_housekeeping_jobs(
            "The housekeeping worker exited while the job was running.")
        self._expand(payload)
        db.sweep_due_housekeeping_jobs()
        return {"ok": True, "jobs": self._drain()}

    def _expand(self, payload: dict[str, Any]) -> None:
        """An item's jobs: the run's summary, and the next chunks of its room.
        An item with `room_chunks` and no run asks for the rest of a room."""
        room_uuid: UUID | None = None
        try:
            if payload.get("room_uuid"):
                room_uuid = UUID(str(payload["room_uuid"]))
            run_uuid = UUID(str(payload["run_uuid"])) if payload.get("run_uuid") else None
        except ValueError:
            logger.warning("housekeeping: malformed uuid in %r", payload)
            return
        if run_uuid is not None:
            job, created = db.ensure_housekeeping_job(
                db.RUN_SUMMARY_JOB, run_uuid=run_uuid, room_uuid=room_uuid)
            if payload.get("force") and not created and job.state == "final":
                db.requeue_housekeeping_job(job)
        if room_uuid is None or payload.get("force"):
            return
        if payload.get("room_chunks"):
            limit = CHUNK_JOBS_PER_REQUEST
        elif run_uuid is not None and db.get_setting("housekeeping.room_summaries"):
            limit = CHUNK_JOBS_PER_RUN
        else:
            return
        from agents.room_chunks import PROMPT_VERSION

        db.ensure_room_chunk_jobs(
            room_uuid, int(db.get_setting("housekeeping.room_chunk_size")),
            int(db.get_setting("housekeeping.room_chunk_overlap")),
            PROMPT_VERSION, limit)

    def _drain(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        while db.next_housekeeping_job() is not None:
            self._cool_down_if_due()
            self._wait_until_idle()
            # Chosen after the wait: a more urgent job may have arrived.
            job = db.next_housekeeping_job()
            if job is None:
                break
            counts[self._run_job(job)] += 1
        return dict(counts)

    def _cool_down_if_due(self) -> None:
        """After BURST_CALLS model calls in a row, rest before the next. A
        turn may run meanwhile: the rest is for the machine, and a turn's own
        calls are the operator's to spend."""
        if self._burst < self.BURST_CALLS:
            return
        logger.info("housekeeping: cooling down %.0f s after %d calls",
                    self.COOLDOWN_SECONDS, self._burst)
        job = db.next_housekeeping_job()
        if job is not None:
            db.settle_housekeeping_job(job, "deferred", deferred_reason="cooldown")
        deadline = time.monotonic() + self.COOLDOWN_SECONDS
        while time.monotonic() < deadline:
            time.sleep(min(self.POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        self._burst = 0

    def _wait_until_idle(self) -> None:
        while db.turn_activity():
            # End the read's transaction, so the wait holds none open.
            db.session.commit()
            time.sleep(self.POLL_SECONDS)
        db.session.commit()

    def _probe(self) -> Callable[[], bool]:
        """What the watch asks, on a session of its own: the watcher thread
        has no app context, and the scoped session is not shared across
        threads."""
        engine = db.db.engine

        def probe() -> bool:
            with Session(bind=engine) as session:
                return db.turn_activity(session=session)
        return probe

    def _run_job(self, job: Any) -> str:
        """One attempt at one job. Returns `done`, `killed` or `failed`."""
        runner = self._RUNNERS.get(job.job_type)
        attempt = db.start_housekeeping_attempt(job)
        outcome: str | None = None
        # Counted before it runs: a killed or failed call heated the machine
        # as much as a finished one.
        self._burst += 1
        try:
            if runner is None:
                raise LookupError(f"no runner for job type {job.job_type!r}")
            with TurnWatch(self._probe(), poll_seconds=self.POLL_SECONDS) as watch:
                outcome = runner(self, job, attempt, watch)
        except Preempted:
            db.session.rollback()
            logger.info("housekeeping: %s for run %s killed by a new turn",
                        job.job_type, job.run_uuid)
            db.finish_housekeeping_attempt(job, attempt, "killed: turn")
            db.settle_housekeeping_job(job, "deferred", deferred_reason="turn")
            return "killed"
        except Exception as e:
            db.session.rollback()
            msg = f"{type(e).__name__}: {e}"
            logger.exception("housekeeping: %s for run %s failed",
                             job.job_type, job.run_uuid)
            db.finish_housekeeping_attempt(job, attempt, "failed", error=msg)
            if job.retries < MAX_RETRIES:
                db.settle_housekeeping_job(job, "waiting", retried=True)
            else:
                db.settle_housekeeping_job(job, "final", outcome=f"failed: {msg}")
            return "failed"
        skipped = (outcome or "").startswith("skipped")
        db.finish_housekeeping_attempt(
            job, attempt, "skipped" if skipped else "done")
        db.settle_housekeeping_job(job, "final", outcome=outcome or "done")
        return "done"

    def _summarize_run(self, job: Any, attempt: Any, watch: TurnWatch) -> str:
        """The run summary: one structured call by the summarizer, on its own
        model slot, interruptible for exactly the length of that call."""
        from agents.assistant_run_summarizer import AssistantRunSummarizerAgent

        run = db.get_assistant_run(job.run_uuid) if job.run_uuid else None
        if run is None:
            return "skipped: run not found"
        summarizer = AssistantRunSummarizerAgent(
            ASSISTANT_RUN_SUMMARIZER_UUID, "assistant.run_summarizer",
            lambda _msg: None)
        summarizer.setup()
        attempt_uuid = attempt.uuid
        last_write = 0.0

        def on_model(model_name: str) -> None:
            with watch.shielded():
                db.checkpoint_housekeeping_attempt(attempt_uuid, model=model_name)

        def on_progress(text: str) -> None:
            nonlocal last_write
            now = time.monotonic()
            if now - last_write < self.PROGRESS_SECONDS:
                return
            last_write = now
            with watch.shielded():
                db.checkpoint_housekeeping_attempt(
                    attempt_uuid, response_preview=text)

        summarizer.call_guard = watch.interruptible
        summarizer.on_model = on_model
        summarizer.on_progress = on_progress
        result = summarizer.handle(self._active_journal_id or job.uuid,
                                   {"run_uuid": str(run.uuid)})
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "summarizer returned no result")
        return "done"

    def _summarize_chunk(self, job: Any, attempt: Any, watch: TurnWatch) -> str:
        """One chunk of a room's messages, summarized by a structured call on
        the `assistant.room_summarizer` slot, then checked part by part by a
        second call on the same models, and written once more when the check
        finds a part's request or result wrong: two calls as a rule, four at
        most. The chunk is the one the job's subject names; its row is
        written once, when the last call returns."""
        from agents import room_chunks
        from agents.config import ASSISTANT_ROOM_SUMMARIZER_UUID
        from agents.model_groups import resolve_assistant_model_uuids, structured_llm_call

        spec = db.parse_chunk_subject(job.subject)
        if spec is None or job.room_uuid is None:
            raise ValueError(f"malformed chunk subject {job.subject!r}")
        size, overlap, seq = spec["size"], spec["overlap"], spec["seq"]
        if spec["version"] != room_chunks.PROMPT_VERSION:
            return "skipped: queued for an earlier prompt"
        earlier = db.list_room_chunks(
            job.room_uuid, size, overlap, room_chunks.PROMPT_VERSION)
        if any(c.seq == seq for c in earlier):
            return "skipped: already summarized"
        if seq != len(earlier):
            # Its predecessor failed, so the series cannot continue past it.
            return "skipped: an earlier chunk of the series is missing"

        messages = db.counted_room_messages(job.room_uuid)
        inside = [i for i, m in enumerate(messages)
                  if spec["first_id"] <= m["id"] <= spec["last_id"]]
        if not inside:
            return "skipped: the chunk's messages are gone"
        first, last = inside[0], inside[-1]
        # Signals for the chunk and its context only: the rest is not shown.
        shown = messages[max(0, first - overlap):last + 1 + overlap]
        db.attach_reply_signals(job.room_uuid, shown)
        chunk = messages[first:last + 1]
        prompt = room_chunks.build_prompt(
            messages[max(0, first - overlap):first], chunk,
            messages[last + 1:last + 1 + overlap], first)

        models, _label = resolve_assistant_model_uuids(ASSISTANT_ROOM_SUMMARIZER_UUID)
        models = [m for m in models or [] if _is_local_model(m)]
        if not models:
            raise RuntimeError(
                "no local model is bound to assistant.room_summarizer or "
                "assistant.default; a room's messages go to local models only")
        attempt_uuid = attempt.uuid
        worker = self

        class Observer:
            last_write = 0.0

            def started(self, model_uuid: UUID, model_name: str,
                        timeout_seconds: float) -> None:
                with watch.shielded():
                    db.checkpoint_housekeeping_attempt(attempt_uuid, model=model_name)

            def progress(self, model_uuid: UUID, model_name: str,
                         reasoning: str | None, response_text: str | None) -> None:
                now = time.monotonic()
                if now - self.last_write < worker.PROGRESS_SECONDS:
                    return
                self.last_write = now
                with watch.shielded():
                    db.checkpoint_housekeeping_attempt(
                        attempt_uuid, response_preview=response_text or reasoning or "")

            def failed(self, *args: Any, **kwargs: Any) -> None:
                pass

        def call(p: dict[str, str], response_model: type, caller: str) -> tuple[Any, UUID]:
            with watch.interruptible():
                return structured_llm_call(
                    caller, models, p["system"], p["user"], response_model,
                    observer=Observer())

        summarizer = db.JOB_CALLERS[db.ROOM_CHUNK_JOB]
        reply, model_uuid = call(prompt, room_chunks.ChunkSummary, summarizer)
        summary, topics, parts, problems = room_chunks.settle(
            reply, first, last, chunk)  # type: ignore[arg-type]

        # The check, and one rewrite when it finds a part's request or result
        # wrong. The rewrite is checked too, so what is stored always carries
        # the check of itself. A check or a rewrite that no model could
        # deliver leaves the answer there is, and says so.
        audit: dict[str, Any] = {"attempts": 1}
        for attempt_no in (1, 2):
            self._burst += 1
            try:
                check, _ = call(room_chunks.build_audit_prompt(prompt, summary, parts),
                                room_chunks.ChunkCheck, room_chunks.AUDIT_CALLER)
            except RuntimeError as e:
                audit["error"] = f"check: {e}"[:300]
                break
            failed, check_problems = room_chunks.settle_audit(
                check, parts)  # type: ignore[arg-type]
            problems += check_problems
            if not failed or attempt_no == 2:
                break
            self._burst += 1
            try:
                reply, model_uuid = call(
                    room_chunks.build_retry_prompt(prompt, summary, parts, failed),
                    room_chunks.ChunkSummary, summarizer)
            except RuntimeError as e:
                audit["error"] = f"rewrite: {e}"[:300]
                break
            audit["rewritten"] = [
                {"first_message": p["first_message"], "last_message": p["last_message"],
                 "summary": p["summary"], "result": p["result"],
                 "reasoning": p["audit"]["reasoning"]} for p in failed]
            summary, topics, parts, problems = room_chunks.settle(
                reply, first, last, chunk)  # type: ignore[arg-type]
            audit["attempts"] = 2
        _provider, model_name, _args = db.resolved_model_kwargs(model_uuid)
        db.store_room_chunk(
            room_uuid=job.room_uuid, chunk_size=size, overlap=overlap, seq=seq,
            first_message_id=chunk[0]["id"], last_message_id=chunk[-1]["id"],
            message_count=len(chunk),
            content_hash=room_chunks.content_hash(chunk),
            summary=summary, topics=topics, parts=parts, problems=problems,
            audit=audit,
            model=model_name, prompt_version=room_chunks.PROMPT_VERSION,
            job_uuid=job.uuid)
        return "done"

    _RUNNERS: dict[str, Callable[..., str]] = {
        db.RUN_SUMMARY_JOB: _summarize_run,
        db.ROOM_CHUNK_JOB: _summarize_chunk,
    }
