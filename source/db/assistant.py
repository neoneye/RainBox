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

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from db.chat import post_chat_message
from db.models import AssistantRun, AssistantStep, db

StepPhase = Literal["planned", "running", "observed", "failed", "final", "control"]


def start_assistant_run(
    journal_id: int,
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
    """Append one step-transition row (the source of truth) and, on the step's
    first transition (`planned`), post a thin `debug-assistant` chat pointer so
    the step renders inline in the conversation.

    Redaction v1: PRs 1-4 expose no secret-carrying actions, so `args` persist
    verbatim. A later capability that sets secrets=true must redact before
    calling this helper.
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

    # One inline anchor per step, placed at its first transition. The pointer
    # carries only the locator — readers join back to assistant_step by it.
    if phase == "planned":
        run = db.session.get(AssistantRun, run_id)
        if run is not None:
            post_chat_message(
                run.room_uuid,
                run.agent_uuid,
                json.dumps({"run_id": run_id, "step_index": step_index}),
                content_type="json",
                kind="debug-assistant",
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
