"""Durable assistant trace: assistant_run / assistant_step persistence.

The trace tables are the source of truth for an assistant turn. The loop calls
exactly three helpers — `start_assistant_run`, `append_assistant_step`,
`finish_run` — plus `list_assistant_steps` for readers. Re-exported from `db`.

`append_assistant_step` commits the step row first, then (at the step's first
transition) posts a thin `debug-assistant` chat row carrying only the
run_id/step_index pointer, so the trace renders inline without putting the
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


def append_assistant_step(
    *,
    run_id: int,
    step_index: int,
    phase: StepPhase,
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    observation_preview: str | None = None,
    error: str | None = None,
    model_group_uuid: UUID | None = None,
    model_uuid: UUID | None = None,
) -> AssistantStep:
    """Append one step-transition row (the structured source of truth) and, on the
    step's *terminal* transition (observed/failed/final), post a `debug-assistant`
    chat row whose `text` IS the full readable trace (action / reason / args /
    observation). Self-contained on purpose: the chat message text == what's shown
    == what copy-to-clipboard yields, with no pointer indirection to resolve.

    Anchored at the terminal phase (not `planned`) so the observation already
    exists when the row is posted. Redaction v1: no secret-carrying actions exist
    yet, so `args` persist verbatim into both the step row and this trace text; a
    later capability that sets secrets=true must redact before calling this helper.
    """
    step = AssistantStep(
        run_id=run_id,
        step_index=step_index,
        phase=phase,
        action=action,
        reason=reason,
        args=args or {},
        observation_preview=observation_preview,
        error=error,
        model_group_uuid=model_group_uuid,
        model_uuid=model_uuid,
    )
    db.session.add(step)
    db.session.flush()  # commit the step row before anything else this txn

    if phase in ("observed", "failed", "final"):
        run = db.session.get(AssistantRun, run_id)
        if run is not None:
            state: dict[str, Any] = {
                "step": step_index,
                "phase": phase,
                "action": action,
                "reason": reason,
                "args": args or {},
            }
            if phase == "observed":
                state["observation"] = observation_preview
            elif phase == "failed":
                state["error"] = error or observation_preview
            elif phase == "final":
                state["result"] = "replied to the user"
            post_chat_message(
                run.room_uuid, run.agent_uuid, json.dumps(state, indent=2),
                content_type="json", kind="debug-assistant",
            )  # commits the txn (including the step row above)
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


def get_assistant_run(run_id: int) -> AssistantRun | None:
    """One run row by id, or None."""
    return db.session.get(AssistantRun, run_id)


def list_assistant_steps(run_id: int) -> list[AssistantStep]:
    """All step rows for a run, in commit order (id ascending)."""
    return (
        db.session.query(AssistantStep)
        .filter(AssistantStep.run_id == run_id)
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
    run_id: int,
    step_index: int,
    capability_name: str,
    payload: dict[str, Any],
    preview_text: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    state: str = "proposed",
    result: dict[str, Any] | None = None,
) -> AssistantWriteIntent:
    """Open a write intent. Defaults to a `proposed` confirm-tier proposal; a
    log-and-undo recorder passes `state="completed"` with a `result` so the row
    is never confirmable as `proposed` (no double-execute window)."""
    intent = AssistantWriteIntent(
        run_id=run_id,
        step_index=step_index,
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
    run_id: int,
    command: str,
    payload: dict[str, Any] | None = None,
    requested_by_uuid: UUID | None = None,
    note: str | None = None,
) -> "AssistantControl":
    """Insert a pending steering command (stop/redirect) for a run."""
    control = AssistantControl(
        run_id=run_id, command=command, payload=payload or {},
        state="pending", requested_by_uuid=requested_by_uuid, note=note,
    )
    db.session.add(control)
    db.session.commit()
    return control


def list_pending_controls(run_id: int) -> list["AssistantControl"]:
    """Pending controls for a run, oldest first (the order the loop applies them)."""
    return (
        db.session.query(AssistantControl)
        .filter(AssistantControl.run_id == run_id, AssistantControl.state == "pending")
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


def request_run_stop(run_id: int) -> bool:
    """Signal an intent to stop a still-running run (status -> 'stopping'). The
    loop performs the actual clean stop at its next step boundary. Returns False
    for an unknown run; a no-op for an already-terminal run."""
    run = db.session.get(AssistantRun, run_id)
    if run is None:
        return False
    if run.status == "running":
        run.status = "stopping"
        db.session.add(run)
        db.session.commit()
    return True
