"""Confirm-tier write execution: the only path that runs a proposed write.

The assistant proposes a confirm-tier write (an assistant_write_intent in state
`proposed`); it never executes it inline. An operator approves via
`execute_write_intent`, which walks the state machine
(proposed -> confirmed -> executing -> completed/failed) and runs the
capability's executor against the *stored* payload — so the assistant cannot
mutate what was approved. `reject_write_intent` declines a proposal.
"""

import logging
from uuid import UUID

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantObservation,
)

logger = logging.getLogger(__name__)


# Completed intents persist capability names in their payload/undo records, so
# rows written before a capability was renamed still carry its former name.
# Resolution maps those to the current capability instead of refusing to undo.
LEGACY_CAPABILITY_NAMES: dict[str, str] = {
    "remember": "memory_remember",
    "activate_memory": "memory_activate",
    "forget_memory": "memory_forget",
    "reject_memory_candidate": "memory_reject_candidate",
    "reactivate_memory": "memory_reactivate",
    "kanban_move_task": "kanban_task_column",
    "kanban_complete": "kanban_task_complete",
    "kanban_comment": "kanban_task_comment",
    "kanban_create_task": "kanban_task_create",
    "kanban_delete_task": "kanban_task_delete",
    "kanban_create_board": "kanban_board_create",
    "kanban_delete_board": "kanban_board_delete",
}


def _resolve_capability_name(name: str) -> AssistantActionName:
    """Parse a persisted capability name, accepting legacy names. Raises
    ValueError for a name that matches no current or former capability."""
    return AssistantActionName(LEGACY_CAPABILITY_NAMES.get(name, name))


def execute_write_intent(
    intent_uuid: UUID, *, confirmed_by_uuid: UUID | None = None
) -> AssistantObservation:
    """Approve and execute a proposed write intent. Returns the executor's
    observation. Refuses (ok=False) anything not currently in `proposed`, or a
    payload whose hash no longer matches its capability — a confirm-tier write is
    never executed without a matching, approved proposal."""
    intent = db.get_write_intent(intent_uuid)
    if intent is None:
        return AssistantObservation(ok=False, text="no such write intent")
    if intent.state != "proposed":
        return AssistantObservation(
            ok=False, text=f"write intent is not awaiting confirmation (state={intent.state})"
        )
    if intent.payload_hash != db.write_intent_payload_hash(
        intent.capability_name, intent.payload
    ):
        db.set_write_intent_state(intent, "failed", error="payload hash mismatch")
        return AssistantObservation(ok=False, text="payload changed since proposal; refusing")

    try:
        cap = CAPABILITIES[_resolve_capability_name(intent.capability_name)]
    except (KeyError, ValueError):
        db.set_write_intent_state(intent, "failed", error="unknown capability")
        return AssistantObservation(ok=False, text="unknown capability for intent")
    if cap.action is None or not cap.write:
        db.set_write_intent_state(intent, "failed", error="capability is not an executable write")
        return AssistantObservation(ok=False, text="capability is not an executable write")
    if cap.tier != "confirm":
        db.set_write_intent_state(intent, "failed", error="capability is not confirm-tier")
        return AssistantObservation(
            ok=False, text="capability is not confirm-tier; refusing to confirm-execute"
        )

    db.set_write_intent_state(intent, "confirmed", confirmed_by_uuid=confirmed_by_uuid)
    db.set_write_intent_state(intent, "executing")
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=intent.room_uuid, agent_uuid=intent.agent_uuid,
        step_index=0,  # operator-triggered re-dispatch: no loop step
        step_uuid=intent.step_uuid,
    )
    try:
        obs = cap.action(ctx, dict(intent.payload))
    except Exception as e:  # never leave an intent stuck in executing
        db.set_write_intent_state(intent, "failed", error=f"{type(e).__name__}: {e}")
        logger.exception("write intent %s failed during execution", intent_uuid)
        return AssistantObservation(ok=False, text=f"{type(e).__name__}: {e}")

    if obs.ok:
        db.set_write_intent_state(intent, "completed", result=obs.data)
    else:
        db.set_write_intent_state(intent, "failed", error=obs.text)
    return obs


def reject_write_intent(intent_uuid: UUID) -> bool:
    """Decline a proposed write. Returns True if it was proposed and is now
    rejected, False otherwise."""
    intent = db.get_write_intent(intent_uuid)
    if intent is None or intent.state != "proposed":
        return False
    db.set_write_intent_state(intent, "rejected")
    return True


def undo_write_intent(intent_uuid: UUID) -> AssistantObservation:
    """Revert a completed log-and-undo write by replaying its stored inverse op,
    then mark the original intent `undone`. One-shot: only a `completed` intent
    with an `undo` record can be undone (no redo)."""
    intent = db.get_write_intent(intent_uuid)
    if intent is None:
        return AssistantObservation(ok=False, text="no such write intent")
    if intent.state != "completed":
        return AssistantObservation(
            ok=False, text=f"write intent is not undoable (state={intent.state})"
        )
    undo = (intent.result or {}).get("undo")
    if not undo:
        return AssistantObservation(ok=False, text="write intent has no undo record")
    try:
        cap = CAPABILITIES[_resolve_capability_name(undo["capability"])]
    except (KeyError, ValueError):
        return AssistantObservation(ok=False, text="unknown capability for undo")
    if cap.action is None:
        return AssistantObservation(ok=False, text="undo capability has no dispatcher")
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=intent.room_uuid,
        agent_uuid=intent.agent_uuid, step_index=0,  # no loop step for undo
        step_uuid=intent.step_uuid,
    )
    try:
        obs = cap.action(ctx, dict(undo["payload"]))
    except Exception as e:
        logger.exception("undo of write intent %s failed", intent_uuid)
        return AssistantObservation(ok=False, text=f"{type(e).__name__}: {e}")
    if obs.ok:
        db.set_write_intent_state(intent, "undone", result={**intent.result, "undone": True})
    return obs
