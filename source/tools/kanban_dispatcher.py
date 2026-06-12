"""The kanban operation dispatcher — the single authority chokepoint for
AGENT-originated board operations (humans use the page's bulk save, which is
a different, version-guarded surface).

docs/kanban-design.md: "Models propose, code disposes." A model (or the code
driving it) proposes an op dict {op, taskId, ...}; this module validates the
shape (malformed input raises, it is never silently dropped), resolves the
agent's authority from the agent_config registry, and only then routes to
the db.kanban_* primitives:

    observe  reads; append_event with kind 'comment'/'suggestion' only
    work     observe + claim/claim_next/renew/release/append_event(any)/complete
    shape    everything incl. move — human-only: no registry entry grants it

complete() carries the Review-before-Done policy: an UNVERIFIED agent
(kanban_verified missing/False) gets review=True, so its successful work
routes to a Review-named column when the board has one. KanbanConflict and
KanbanError from the db layer propagate unchanged."""

from typing import Any
from uuid import UUID

import db

OBSERVE_EVENT_KINDS = frozenset({"comment", "suggestion"})
_RANK = {"observe": 0, "work": 1, "shape": 2}


class KanbanDispatchError(ValueError):
    """Malformed op: unknown name, missing field, or unparseable uuid."""


class KanbanAuthorityError(PermissionError):
    """The agent's authority does not permit this op (a ledger signal —
    callers should record it as a 'permission-denied' task event)."""


def _registry_entry(agent_uuid: UUID) -> dict[str, Any] | None:
    from agents.config import agent_config

    for entry in agent_config.values():
        if entry["uuid"] == agent_uuid:
            return dict(entry)
    return None


def kanban_authority_for(agent_uuid: UUID) -> str:
    """The agent's kanban authority; unknown agents and entries without the
    field are 'observe' (read + comment/suggestion only)."""
    entry = _registry_entry(agent_uuid)
    return (entry or {}).get("kanban_authority", "observe")


def kanban_is_verified(agent_uuid: UUID) -> bool:
    """Whether the agent's ok=true is ground truth (workspace_shell's exit
    codes). Unverified agents complete into Review, not Done."""
    entry = _registry_entry(agent_uuid)
    return bool((entry or {}).get("kanban_verified"))


def _uuid_field(op: dict[str, Any], key: str, *, required: bool = True) -> UUID | None:
    raw = op.get(key)
    if raw is None or raw == "":
        if required:
            raise KanbanDispatchError(f"op {op.get('op')!r} requires {key}")
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise KanbanDispatchError(f"{key} is not a uuid: {raw!r}") from exc


def _require_uuid(op: dict[str, Any], key: str) -> UUID:
    """Like _uuid_field(required=True) but narrows the return type to UUID
    (never None) so Pyright sees a guaranteed non-optional value."""
    val = _uuid_field(op, key, required=True)
    assert val is not None
    return val


def _require(authority: str, needed: str, op_name: str) -> None:
    # Use .get() so an unrecognised authority string ranks below observe (-1)
    # instead of raising a KeyError.
    if _RANK.get(authority, -1) < _RANK[needed]:
        raise KanbanAuthorityError(
            f"op {op_name!r} requires {needed!r} authority, agent has {authority!r}")


def kanban_dispatch(agent_uuid: UUID, op: dict[str, Any]) -> dict[str, Any] | None:
    """Validate, authorize, and execute one agent operation. Returns the db
    function's result (task brief dict, or None for a vanished task)."""
    if not isinstance(op, dict):
        raise KanbanDispatchError(f"op must be a dict, got {type(op).__name__}")
    name = op.get("op")
    authority = kanban_authority_for(agent_uuid)
    actor = str(agent_uuid)

    if name == "claim":
        _require(authority, "work", name)
        return db.kanban_claim_task(_require_uuid(op, "taskId"), agent_uuid)
    if name == "claim_next":
        _require(authority, "work", name)
        return db.kanban_claim_next(
            agent_uuid,
            board_uuid=_uuid_field(op, "boardId", required=False),
            include_unassigned=bool(op.get("includeUnassigned", True)))
    if name == "renew":
        _require(authority, "work", name)
        return db.kanban_renew_claim(_require_uuid(op, "taskId"), agent_uuid)
    if name == "release":
        _require(authority, "work", name)
        return db.kanban_release_task(_require_uuid(op, "taskId"), agent_uuid)
    if name == "append_event":
        kind = str(op.get("kind") or "").strip()
        if not kind:
            raise KanbanDispatchError("append_event requires a non-empty kind")
        if kind not in OBSERVE_EVENT_KINDS:
            _require(authority, "work", f"append_event[{kind}]")
        return db.kanban_append_event(
            _require_uuid(op, "taskId"), kind, actor=actor,
            detail=str(op.get("detail") or ""))
    if name == "complete":
        _require(authority, "work", name)
        if not isinstance(op.get("ok"), bool):
            raise KanbanDispatchError("complete requires a boolean 'ok'")
        return db.kanban_complete_task(
            _require_uuid(op, "taskId"), op["ok"], actor=actor,
            detail=str(op.get("detail") or ""),
            review=not kanban_is_verified(agent_uuid))
    if name == "move":
        _require(authority, "shape", name)
        return db.kanban_move_task(
            _require_uuid(op, "taskId"), _require_uuid(op, "columnId"), actor=actor)
    raise KanbanDispatchError(f"unknown op: {name!r}")
