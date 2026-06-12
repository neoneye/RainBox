"""Conversation-run persistence + the concurrency-critical state transitions for
the bounded conversation manager (see
docs/proposals/2026-06-08-persona-prompts-and-agent-conversations.md).

`conversation_run` is the only hot, mutable table the feature adds. The two
compare-and-set helpers (`claim_conversation_tick`, `advance_conversation_if_new`)
are the whole concurrency story: each is a single conditional UPDATE whose
rowcount tells the caller whether it owns the next scheduling decision. Re-exported
from db for `db.*` call sites.
"""

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from db.models import ChatMessage, ChatUser, ConversationRun, db


def _wall_clock_anchor() -> dict[str, float]:
    """The active wall-clock baseline for the budget, as an epoch float in the
    run's `budget` JSONB. Reset on resume so idle time while paused/stopped does
    not count against `max_wall_clock_seconds`."""
    return {"wall_clock_started_at": time.time()}

# A turn (one speaker) is retried this many times before the run is marked
# `failed`. Applies to both an errored turn (failed journal routed back) and a
# stale turn (SIGKILLed speaker, recovered by reconcile). retry_count resets to 0
# on every successful advance.
MAX_TURN_RETRIES: int = 1
# A turn whose speaker was enqueued longer ago than this with no completion is
# considered stale (the child was likely SIGKILLed). Default for reconcile.
STALE_TURN_TIMEOUT_SECONDS: float = 120.0


def create_conversation_run(
    room_uuid: UUID,
    participants: list[dict[str, Any]],
    turn_policy: dict[str, Any],
    last_human_message_id: int | None = None,
) -> ConversationRun:
    run = ConversationRun(
        room_uuid=room_uuid,
        status="running",
        turn=0,
        tick_count=0,
        participants=participants,
        turn_policy=turn_policy,
        last_human_message_id=last_human_message_id,
        budget=_wall_clock_anchor(),
    )
    db.session.add(run)
    db.session.commit()
    return run


def get_conversation_run(run_uuid: UUID) -> ConversationRun | None:
    if not isinstance(run_uuid, UUID):
        run_uuid = UUID(str(run_uuid))
    return db.session.get(ConversationRun, run_uuid)


def current_tick_count(run_uuid: UUID) -> int | None:
    run = get_conversation_run(run_uuid)
    return run.tick_count if run is not None else None


def claim_conversation_tick(run_uuid: UUID, expected_tick_count: int) -> bool:
    """CAS for manual ticks (start / resume / single-step). Returns True iff this
    tick owns the scheduling decision (monotonic tick_count matched)."""
    res = db.session.execute(
        sa.update(ConversationRun)
        .where(
            ConversationRun.id == run_uuid,
            ConversationRun.status == "running",
            ConversationRun.tick_count == expected_tick_count,
        )
        .values(tick_count=ConversationRun.tick_count + 1, updated_at=datetime.now(UTC))
    )
    db.session.commit()
    return res.rowcount > 0


def advance_conversation_if_new(
    run_uuid: UUID, src_journal_id: int, completed_turn: int
) -> bool:
    """CAS for a routed speaker-completion. Advances the run by one turn at most
    once per completion. Rejects duplicates and stale older completions via the
    monotonic `last_speaker_journal_id < src` guard. Returns True iff it advanced.
    """
    res = db.session.execute(
        sa.update(ConversationRun)
        .where(
            ConversationRun.id == run_uuid,
            ConversationRun.status == "running",
            ConversationRun.turn == completed_turn,
            ConversationRun.active_turn == completed_turn,
            sa.or_(
                ConversationRun.last_speaker_journal_id.is_(None),
                ConversationRun.last_speaker_journal_id < src_journal_id,
            ),
        )
        .values(
            last_speaker_journal_id=src_journal_id,
            turn=ConversationRun.turn + 1,
            active_turn=None,
            active_speaker_uuid=None,
            active_turn_enqueued_at=None,
            retry_count=0,
            updated_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    return res.rowcount > 0


def mark_conversation_turn_in_flight(
    run_uuid: UUID, turn: int, speaker_uuid: UUID
) -> None:
    db.session.execute(
        sa.update(ConversationRun)
        .where(ConversationRun.id == run_uuid)
        .values(
            active_turn=turn,
            active_speaker_uuid=speaker_uuid,
            active_turn_enqueued_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def finish_conversation(run_uuid: UUID, status: str, reason: str | None = None) -> None:
    db.session.execute(
        sa.update(ConversationRun)
        .where(ConversationRun.id == run_uuid)
        .values(
            status=status,
            reason=reason,
            active_turn=None,
            active_speaker_uuid=None,
            active_turn_enqueued_at=None,
            updated_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def pause_conversation(
    run_uuid: UUID, reason: str | None = None, last_human_message_id: int | None = None
) -> None:
    values: dict[str, Any] = {
        "status": "paused",
        "reason": reason,
        "updated_at": datetime.now(UTC),
    }
    if last_human_message_id is not None:
        values["last_human_message_id"] = last_human_message_id
    db.session.execute(
        sa.update(ConversationRun).where(ConversationRun.id == run_uuid).values(**values)
    )
    db.session.commit()


def request_conversation_stop(run_uuid: UUID) -> None:
    db.session.execute(
        sa.update(ConversationRun)
        .where(ConversationRun.id == run_uuid)
        .values(stop_requested=True, updated_at=datetime.now(UTC))
    )
    db.session.commit()


def list_conversation_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Recent runs (newest first) as plain dicts for the control page."""
    rows = (
        db.session.query(ConversationRun)
        .order_by(ConversationRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "run_uuid": str(r.id),
            "room_uuid": str(r.room_uuid),
            "status": r.status,
            "turn": r.turn,
            "reason": r.reason,
            "stop_requested": r.stop_requested,
            "participants": [p.get("slug") for p in (r.participants or [])],
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
        }
        for r in rows
    ]


def find_human_message_after(
    room_uuid: UUID, last_human_message_id: int | None
) -> ChatMessage | None:
    """The earliest human `message` in the room newer than the watermark, or None.
    Used to detect a human interruption mid-run."""
    watermark = last_human_message_id or 0
    human = db.session.query(ChatUser).filter_by(user_type="human").first()
    if human is None:
        return None
    return (
        db.session.query(ChatMessage)
        .filter(
            ChatMessage.room_uuid == room_uuid,
            ChatMessage.id > watermark,
            ChatMessage.sender_uuid == human.uuid,
            ChatMessage.kind == "message",
        )
        .order_by(ChatMessage.id.asc())
        .first()
    )


def claim_failed_turn(
    run_uuid: UUID, src_journal_id: int, completed_turn: int
) -> int | None:
    """CAS for a routed *failed* speaker turn. Records the journal id and bumps
    retry_count WITHOUT advancing the turn, so the manager can retry the same
    speaker. Returns the new retry_count, or None if this completion was a
    duplicate/stale delivery (or the run is no longer running / not on this turn).
    The monotonic `last_speaker_journal_id < src` guard makes it idempotent."""
    res = db.session.execute(
        sa.update(ConversationRun)
        .where(
            ConversationRun.id == run_uuid,
            ConversationRun.status == "running",
            ConversationRun.turn == completed_turn,
            ConversationRun.active_turn == completed_turn,
            sa.or_(
                ConversationRun.last_speaker_journal_id.is_(None),
                ConversationRun.last_speaker_journal_id < src_journal_id,
            ),
        )
        .values(
            last_speaker_journal_id=src_journal_id,
            retry_count=ConversationRun.retry_count + 1,
            updated_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    if res.rowcount == 0:
        return None
    run = get_conversation_run(run_uuid)
    return run.retry_count if run is not None else None


def stop_conversation(run_uuid: UUID) -> str:
    """Operator stop. A running run gets `stop_requested` set (the manager ends it
    on its next tick); a paused run is transitioned straight to `stopped` here,
    because the manager skips non-running runs and would never see the flag.
    Returns the resulting action: 'stopping' | 'stopped' | a terminal status |
    'missing'."""
    run = get_conversation_run(run_uuid)
    if run is None:
        return "missing"
    if run.status == "paused":
        finish_conversation(run_uuid, status="stopped", reason="operator_stop")
        return "stopped"
    if run.status == "running":
        request_conversation_stop(run_uuid)
        return "stopping"
    return run.status  # already terminal (finished / failed / stopped)


RESUMABLE_STATUSES: tuple[str, ...] = ("paused", "stopped", "failed")


def resume_conversation(run_uuid: UUID) -> dict[str, Any]:
    """Resume a paused, stopped, or failed run (Stop is treated as pause/play, not
    a hard terminal). Clears any stale active-turn fields, moves the human-
    interruption watermark past the current newest human message (so the message
    that paused it doesn't immediately re-pause), clears stop/failure state, and
    sets status back to 'running'. Returns {status, tick_count} so the caller can
    enqueue a manager tick with the right expected_tick_count, or
    {status: 'not_resumable'|'missing'}. `finished` runs are not resumable (they
    reached DONE/max_turns and would immediately re-finish)."""
    run = get_conversation_run(run_uuid)
    if run is None:
        return {"status": "missing"}
    if run.status not in RESUMABLE_STATUSES:
        return {"status": "not_resumable", "current": run.status}
    human = db.session.query(ChatUser).filter_by(user_type="human").first()
    watermark = run.last_human_message_id or 0
    if human is not None:
        newest = db.session.execute(
            sa.select(sa.func.max(ChatMessage.id)).where(
                ChatMessage.room_uuid == run.room_uuid,
                ChatMessage.sender_uuid == human.uuid,
                ChatMessage.kind == "message",
            )
        ).scalar()
        watermark = int(newest or 0)
    # Reset the wall-clock anchor so time spent paused/stopped (possibly hours,
    # or across days) does not instantly exhaust max_wall_clock_seconds on the
    # first post-resume tick.
    new_budget = {**(run.budget or {}), **_wall_clock_anchor()}
    db.session.execute(
        sa.update(ConversationRun)
        .where(ConversationRun.id == run_uuid)
        .values(
            status="running",
            stop_requested=False,
            reason=None,
            active_turn=None,
            active_speaker_uuid=None,
            active_turn_enqueued_at=None,
            last_human_message_id=watermark,
            budget=new_budget,
            updated_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    fresh = get_conversation_run(run_uuid)
    return {"status": "running", "tick_count": fresh.tick_count if fresh else 0}


def reconcile_conversation(
    run_uuid: UUID, timeout_seconds: float = STALE_TURN_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Recover a run whose in-flight turn went stale (its speaker child was
    SIGKILLed, so no completion was ever routed and the manager keeps skipping
    'speaker in flight'). If the active turn is older than `timeout_seconds`,
    either clear it for a retry (bumping retry_count) or, past MAX_TURN_RETRIES,
    mark the run failed. Returns {status: 'noop'|'too_recent'|'retry'|'failed',…}.
    """
    run = get_conversation_run(run_uuid)
    if run is None:
        return {"status": "missing"}
    if run.status != "running" or run.active_turn is None:
        return {"status": "noop"}
    enq = run.active_turn_enqueued_at
    if enq is not None:
        if enq.tzinfo is None:
            enq = enq.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - enq).total_seconds()
        if age < timeout_seconds:
            return {"status": "too_recent", "age_seconds": age}
    new_retry = run.retry_count + 1
    if new_retry > MAX_TURN_RETRIES:
        finish_conversation(run_uuid, status="failed", reason="stale_turn")
        return {"status": "failed", "turn": run.active_turn}
    db.session.execute(
        sa.update(ConversationRun)
        .where(ConversationRun.id == run_uuid)
        .values(
            active_turn=None,
            active_speaker_uuid=None,
            active_turn_enqueued_at=None,
            retry_count=new_retry,
            updated_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    return {"status": "retry", "retry_count": new_retry, "tick_count": run.tick_count}
