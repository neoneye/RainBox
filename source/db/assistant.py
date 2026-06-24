"""Durable assistant trace: assistant_run / assistant_step persistence.

The trace tables are the source of truth for an assistant turn. The loop calls
exactly three helpers — `start_assistant_run`, `append_assistant_step`,
`finish_run` — plus `list_assistant_steps` for readers. Re-exported from `db`.

`append_assistant_step` commits the step row first, then (at the step's first
transition) posts a thin `debug-assistant` chat row carrying only the
run_uuid/step_index pointer, so the trace renders inline without putting the
payload in chat. The pointer is never `kind="progress"` (those get reaped on a
terminal reply) and never carries the step args/observation.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from db.chat import post_chat_message
from db.models import (
    AssistantControl,
    AssistantRun,
    AssistantStep,
    AssistantWriteIntent,
    ChatMessage,
    ChatUser,
    db,
)

StepPhase = Literal["planned", "running", "observed", "failed", "final", "control"]


def start_assistant_run(
    journal_id: UUID,
    room_uuid: UUID,
    agent_uuid: UUID,
    step_limit: int = 6,
) -> AssistantRun:
    """Open a run row (status 'running') and return it."""
    run = AssistantRun(
        journal_id=journal_id,
        room_uuid=room_uuid,
        agent_uuid=agent_uuid,
        status="running",
        step_limit=step_limit,
    )
    db.session.add(run)
    db.session.commit()
    return run


_TERMINAL_PHASES = ("observed", "failed", "final")


def _post_terminal_trace(step: AssistantStep) -> None:
    """Post the self-contained `debug-assistant` chat row for a step that has
    reached a terminal phase (observed/failed/final). The chat text IS the full
    readable trace (action / reason / args / observation) — what's shown ==
    what's copied, no pointer indirection. Anchored at the terminal phase so the
    observation already exists. Commits the surrounding txn (including the step
    row). No-op for non-terminal phases or a missing run.

    Redaction v1: no secret-carrying actions exist yet, so `args` persist verbatim
    into both the step row and this trace text; a later capability that sets
    secrets=true must redact before this is called.
    """
    if step.phase not in _TERMINAL_PHASES:
        db.session.commit()
        return
    run = db.session.get(AssistantRun, step.run_uuid)
    if run is None:
        db.session.commit()
        return
    state: dict[str, Any] = {
        "step": step.step_index,
        "phase": step.phase,
        "action": step.action,
        "reason": step.reason,
        "args": step.args or {},
    }
    if step.phase == "observed":
        state["observation"] = step.observation_preview
    elif step.phase == "failed":
        state["error"] = step.error or step.observation_preview
    elif step.phase == "final":
        state["result"] = "replied to the user"
    post_chat_message(
        run.room_uuid, run.agent_uuid, json.dumps(state, indent=2),
        content_type="json", kind="debug-assistant",
    )  # commits the txn (including the step row)


def open_assistant_step(
    *,
    run_uuid: UUID,
    step_index: int,
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    model_group_uuid: UUID | None = None,
    model_uuid: UUID | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
) -> AssistantStep:
    """Insert a step's single row at phase `running` and commit it before the
    action runs (trace-before-action durability: a kill mid-action leaves this
    row). Returns the row so the caller has its stable `uuid` to bind a
    write-intent to. Posts no chat row — that lands at settle, when the
    observation exists."""
    step = AssistantStep(
        run_uuid=run_uuid,
        step_index=step_index,
        phase="running",
        action=action,
        reason=reason,
        args=args or {},
        model_group_uuid=model_group_uuid,
        model_uuid=model_uuid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
    db.session.add(step)
    db.session.commit()
    return step


def settle_assistant_step(
    step: AssistantStep,
    *,
    phase: StepPhase,
    observation_preview: str | None = None,
    error: str | None = None,
) -> AssistantStep:
    """Settle an open step in place: UPDATE its `running` row to a terminal
    `phase` (observed/failed) with the outcome, then post the terminal
    `debug-assistant` trace row. One row per step — no append."""
    step.phase = phase
    step.observation_preview = observation_preview
    step.error = error
    db.session.add(step)
    db.session.flush()
    _post_terminal_trace(step)
    return step


def append_assistant_step(
    *,
    run_uuid: UUID,
    step_index: int,
    phase: StepPhase,
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    observation_preview: str | None = None,
    error: str | None = None,
    model_group_uuid: UUID | None = None,
    model_uuid: UUID | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
) -> AssistantStep:
    """Record a **single-insert** step row — the terminal-only path for a step
    with no `running`→settle lifecycle: a `failed` validation, the `final` reply,
    and `control` (stop/redirect) events. Inserts the row and, when its `phase`
    is terminal, posts the self-contained `debug-assistant` trace row (see
    `_post_terminal_trace`). Normal action steps use open/settle instead."""
    step = AssistantStep(
        run_uuid=run_uuid,
        step_index=step_index,
        phase=phase,
        action=action,
        reason=reason,
        args=args or {},
        observation_preview=observation_preview,
        error=error,
        model_group_uuid=model_group_uuid,
        model_uuid=model_uuid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
    db.session.add(step)
    db.session.flush()  # commit the step row before anything else this txn
    _post_terminal_trace(step)
    db.session.commit()
    return step


def finish_run(
    run: AssistantRun,
    status: str,
    final_summary: str | None = None,
) -> AssistantRun:
    """Close a run with a terminal status and optional short summary."""
    run.status = status
    run.finished_at = datetime.now(UTC)
    if final_summary is not None:
        run.final_summary = final_summary
    db.session.add(run)
    db.session.commit()
    return run


def set_run_summary(run: AssistantRun, summary: dict[str, Any]) -> AssistantRun:
    """Store the assistant_run_summarizer agent's post-completion digest on a run, stamping
    `summarized_at`. Overwrites any prior summary (the latest summarization wins)."""
    run.summary = {**summary, "summarized_at": datetime.now(UTC).isoformat()}
    db.session.add(run)
    db.session.commit()
    return run


def get_assistant_run(run_uuid: UUID) -> AssistantRun | None:
    """One run row by uuid (the primary key / log-greppable identifier), or None."""
    return db.session.get(AssistantRun, run_uuid)


def list_assistant_runs(limit: int = 50) -> list[AssistantRun]:
    """The most recent runs, newest first — the left pane of the /assistant
    inspector. (uuid is a stable tiebreaker for same-instant rows.)"""
    return (
        db.session.query(AssistantRun)
        .order_by(AssistantRun.started_at.desc(), AssistantRun.uuid.desc())
        .limit(limit)
        .all()
    )


def get_run_trigger_message(run: AssistantRun) -> dict[str, Any] | None:
    """The chat message that initiated a run: the latest human `message` in the
    run's room at or before it started. Best-effort — returns None when none is
    found (e.g. a run seeded outside the chat flow). Returns a small dict
    (uuid/sender_name/text/timestamp) for the /assistant inspector's trigger
    block."""
    row = (
        db.session.query(ChatMessage, ChatUser.name)
        .join(ChatUser, ChatUser.uuid == ChatMessage.sender_uuid)
        .filter(
            ChatMessage.room_uuid == run.room_uuid,
            ChatMessage.kind == "message",
            ChatUser.user_type == "human",
            ChatMessage.created_at <= run.started_at,
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .first()
    )
    if row is None:
        return None
    msg, sender_name = row
    return {
        "id": msg.id,            # the int id the chat DOM anchors on (data-message-id)
        "uuid": str(msg.uuid),
        "sender_name": sender_name,
        "text": msg.text,
        "timestamp": msg.created_at.strftime("%Y-%m-%d %H:%M") if msg.created_at else "",
    }


def list_assistant_steps(run_uuid: UUID) -> list[AssistantStep]:
    """All step rows for a run, in commit order (id ascending)."""
    return (
        db.session.query(AssistantStep)
        .filter(AssistantStep.run_uuid == run_uuid)
        .order_by(AssistantStep.id)
        .all()
    )


# --- confirm-tier write intents (Phase 5) ------------------------------------


def write_intent_payload_hash(capability_name: str, payload: dict[str, Any]) -> str:
    """Stable hash binding a capability to an exact payload. Confirming approves
    this hash; execution re-checks it so a confirmed write can't be mutated."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{capability_name}\n{canonical}".encode()).hexdigest()


def create_write_intent(
    *,
    run_uuid: UUID,
    capability_name: str,
    payload: dict[str, Any],
    preview_text: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    state: str = "proposed",
    result: dict[str, Any] | None = None,
    step_uuid: UUID | None = None,
) -> AssistantWriteIntent:
    """Open a write intent. Defaults to a `proposed` confirm-tier proposal; a
    log-and-undo recorder passes `state="completed"` with a `result` so the row
    is never confirmable as `proposed` (no double-execute window). `step_uuid`
    binds the intent to the step that produced it (the identity pointer)."""
    intent = AssistantWriteIntent(
        run_uuid=run_uuid,
        step_uuid=step_uuid,
        capability_name=capability_name,
        payload=payload,
        payload_hash=write_intent_payload_hash(capability_name, payload),
        preview_text=preview_text,
        state=state,
        room_uuid=room_uuid,
        agent_uuid=agent_uuid,
        result=result or {},
    )
    db.session.add(intent)
    db.session.commit()
    return intent


def get_write_intent(intent_uuid: UUID) -> AssistantWriteIntent | None:
    return (
        db.session.query(AssistantWriteIntent)
        .filter(AssistantWriteIntent.uuid == intent_uuid)
        .one_or_none()
    )


def list_write_intents_for_run(run_uuid: UUID) -> list[AssistantWriteIntent]:
    """All write intents a run produced, in creation order — the /assistant
    inspector buckets them by `step_uuid` to render each one under its step."""
    return (
        db.session.query(AssistantWriteIntent)
        .filter(AssistantWriteIntent.run_uuid == run_uuid)
        .order_by(AssistantWriteIntent.id)
        .all()
    )


def set_write_intent_state(
    intent: AssistantWriteIntent,
    state: str,
    *,
    confirmed_by_uuid: UUID | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> AssistantWriteIntent:
    """Transition an intent and stamp the matching timestamp."""
    now = datetime.now(UTC)
    intent.state = state
    if state == "confirmed":
        intent.confirmed_at = now
        if confirmed_by_uuid is not None:
            intent.confirmed_by_uuid = confirmed_by_uuid
    elif state == "executing":
        intent.executed_at = now
    elif state in ("completed", "failed", "rejected", "undone"):
        intent.completed_at = now
    if result is not None:
        intent.result = result
    if error is not None:
        intent.error = error
    db.session.add(intent)
    db.session.commit()
    return intent


# --- control channel (Phase 6) -----------------------------------------------


def create_assistant_control(
    *,
    run_uuid: UUID,
    command: str,
    payload: dict[str, Any] | None = None,
    requested_by_uuid: UUID | None = None,
    note: str | None = None,
) -> "AssistantControl":
    """Insert a pending steering command (stop/redirect) for a run."""
    control = AssistantControl(
        run_uuid=run_uuid, command=command, payload=payload or {},
        state="pending", requested_by_uuid=requested_by_uuid, note=note,
    )
    db.session.add(control)
    db.session.commit()
    return control


def list_pending_controls(run_uuid: UUID) -> list["AssistantControl"]:
    """Pending controls for a run, oldest first (the order the loop applies them)."""
    return (
        db.session.query(AssistantControl)
        .filter(AssistantControl.run_uuid == run_uuid, AssistantControl.state == "pending")
        .order_by(AssistantControl.id)
        .all()
    )


def mark_control_state(
    control: "AssistantControl", state: str, *, note: str | None = None
) -> "AssistantControl":
    """Transition a control to applied/ignored, stamping applied_at."""
    control.state = state
    if state in ("applied", "ignored"):
        control.applied_at = datetime.now(UTC)
    if note is not None:
        control.note = note
    db.session.add(control)
    db.session.commit()
    return control


def request_run_stop(run_uuid: UUID) -> bool:
    """Signal an intent to stop a still-running run (status -> 'stopping'). The
    loop performs the actual clean stop at its next step boundary. Returns False
    for an unknown run; a no-op for an already-terminal run."""
    run = db.session.get(AssistantRun, run_uuid)
    if run is None:
        return False
    if run.status == "running":
        run.status = "stopping"
        db.session.add(run)
        db.session.commit()
    return True
