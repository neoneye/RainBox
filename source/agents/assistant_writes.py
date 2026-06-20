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
        cap = CAPABILITIES[AssistantActionName(intent.capability_name)]
    except (KeyError, ValueError):
        db.set_write_intent_state(intent, "failed", error="unknown capability")
        return AssistantObservation(ok=False, text="unknown capability for intent")
    if cap.action is None or not cap.write:
        db.set_write_intent_state(intent, "failed", error="capability is not an executable write")
        return AssistantObservation(ok=False, text="capability is not an executable write")

    db.set_write_intent_state(intent, "confirmed", confirmed_by_uuid=confirmed_by_uuid)
    db.set_write_intent_state(intent, "executing")
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=intent.room_uuid, agent_uuid=intent.agent_uuid,
        step_index=intent.step_index,
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
