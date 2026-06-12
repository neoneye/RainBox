"""Queue persistence: agent inbox + journal.

Split out of db.py. Holds the inbox/journal queue operations (enqueue,
take_item, journal_update, fetch_unrouted_completed, mark_routed,
agent_uuids_with_work). Re-exported from db for import compatibility.
"""
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db_models import VALID_STATES, Inbox, Journal, State, db


def enqueue(agent_uuid: UUID, payload: dict[str, Any]) -> None:
    db.session.add(
        Inbox(
            agent_uuid=agent_uuid,
            payload=json.dumps(payload),
        )
    )
    db.session.commit()


def take_item(agent_uuid: UUID) -> tuple[int, dict[str, Any]] | None:
    """Atomically pop the oldest inbox item for this agent and start a journal
    entry in 'processing' state. Returns (journal_id, payload_dict) or None."""
    row = (
        db.session.query(Inbox)
        .filter_by(agent_uuid=agent_uuid)
        .order_by(Inbox.id.asc())
        .first()
    )
    if row is None:
        return None
    inbox_id = row.id
    enqueued_at = row.enqueued_at
    payload_str = row.payload
    db.session.delete(row)
    now = datetime.now(UTC)
    journal = Journal(
        inbox_id=inbox_id,
        agent_uuid=agent_uuid,
        enqueued_at=enqueued_at,
        started_at=now,
        updated_at=now,
        state="processing",
        payload=payload_str,
    )
    db.session.add(journal)
    db.session.commit()
    return journal.id, json.loads(payload_str)


def journal_update(
    journal_id: int,
    state: State,
    result: dict[str, Any] | None = None,
) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; must be one of {VALID_STATES}")
    row = db.session.get(Journal, journal_id)
    if row is None:
        raise LookupError(f"journal row {journal_id} not found")
    row.state = state
    row.updated_at = datetime.now(UTC)
    row.result = json.dumps(result) if result is not None else None
    db.session.commit()


def fetch_unrouted_completed() -> list[dict[str, Any]]:
    """Journal rows that are completed but have not yet been routed to the next agent."""
    rows = (
        db.session.query(Journal)
        .filter(Journal.state == "completed", Journal.routed_at.is_(None))
        .order_by(Journal.id.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "agent_uuid": r.agent_uuid,
            "payload": json.loads(r.payload) if r.payload else None,
            "result": json.loads(r.result) if r.result else None,
        }
        for r in rows
    ]


def fetch_unrouted_terminal() -> list[dict[str, Any]]:
    """Terminal (completed OR failed) journal rows not yet routed, oldest first.

    Carries `state` and `result` so the supervisor can do dynamic return-address
    routing: a completed row may follow static `next`, while a failed row is
    routed only when its result carries an explicit `_routing.return_to_agent_uuid`
    (so a conversation turn that errors still wakes its manager)."""
    rows = (
        db.session.query(Journal)
        .filter(Journal.state.in_(("completed", "failed")), Journal.routed_at.is_(None))
        .order_by(Journal.id.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "agent_uuid": r.agent_uuid,
            "state": r.state,
            "payload": json.loads(r.payload) if r.payload else None,
            "result": json.loads(r.result) if r.result else None,
        }
        for r in rows
    ]


def mark_routed(journal_id: int) -> None:
    row = db.session.get(Journal, journal_id)
    if row is None:
        raise LookupError(f"journal row {journal_id} not found")
    row.routed_at = datetime.now(UTC)
    db.session.commit()


def agent_uuids_with_work() -> set[UUID]:
    """UUIDs whose inbox currently has at least one pending item."""
    rows = db.session.query(Inbox.agent_uuid).distinct().all()
    return {r[0] for r in rows}
