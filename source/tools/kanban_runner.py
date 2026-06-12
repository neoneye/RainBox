"""The kanban task-execution adapter (milestone 3).

One place owns the board protocol, so individual agents don't each reimplement
it: an agent that receives a kanban inbox payload ({task_uuid, board_uuid,
source: "kanban"} — produced by db.kanban_enqueue_task) hands it to
`run_kanban_task` together with a `work` callback that does the agent's actual
job. The adapter then runs the canonical loop from docs/kanban-design.md:

    claim (lease) → 'started' event → work() → complete(ok) / fail

Every board write goes through tools/kanban_dispatcher.kanban_dispatch, so
authority (observe/work/shape) and Review-before-Done (kanban_verified) are
enforced here for code-originated and model-originated operations alike. An
authority denial is recorded as a 'permission-denied' task event — a ledger
signal, not a silent skip.

- A claim conflict (another agent holds a live lease) is a clean SKIP, not an
  error — someone is already working it.
- A vanished task (deleted between enqueue and execution) is a clean skip too.
- An exception from `work` fails the task with the error recorded; either
  outcome releases the lease, so a failed task is immediately claimable again.

`work(task, board_markdown)` receives the claimed task brief (incl. title +
description) and the board's focus=in-progress markdown (lease state + recent
events inline — the worker's resumable memory) as LLM/context input, and
returns `(ok: bool, detail: str)`. Progress along the way goes through the
dispatcher's append_event — see the workspace_shell consumer in
workspace_shell_chat.py for the reference implementation."""

import logging
from typing import Any, Callable
from uuid import UUID

import db

from .kanban_dispatcher import KanbanAuthorityError, kanban_dispatch

logger = logging.getLogger(__name__)

WorkFn = Callable[[dict[str, Any], str], tuple[bool, str]]


def run_kanban_task(
    agent_uuid: UUID, payload: dict[str, Any], work: WorkFn,
) -> dict[str, Any]:
    """Execute one kanban task through the claim → work → complete protocol.
    Returns the agent's journal result dict."""
    try:
        task_uuid = UUID(str(payload.get("task_uuid")))
    except (ValueError, TypeError):
        return {"ok": False, "error": f"bad task_uuid: {payload.get('task_uuid')!r}"}

    try:
        task = kanban_dispatch(agent_uuid, {"op": "claim", "taskId": str(task_uuid)})
    except db.KanbanConflict as exc:
        logger.info("kanban task %s skipped: %s", task_uuid, exc)
        return {"ok": True, "skipped": str(exc)}
    except KanbanAuthorityError as exc:
        logger.warning("kanban task %s refused: %s", task_uuid, exc)
        # Bypasses the dispatcher on purpose: the claim was just refused, so the
        # ledger event must be written directly — the violation must be recorded.
        db.kanban_append_event(task_uuid, "permission-denied",
                               actor=str(agent_uuid), detail=str(exc))
        return {"ok": False, "error": str(exc)}
    if task is None:
        logger.info("kanban task %s skipped: task no longer exists", task_uuid)
        return {"ok": True, "skipped": "task no longer exists"}

    kanban_dispatch(agent_uuid, {"op": "append_event", "taskId": str(task_uuid),
                                 "kind": "started", "detail": ""})
    context_md = db.kanban_board_markdown(
        UUID(task["boardUuid"]), focus="in-progress") or ""
    try:
        ok, detail = work(task, context_md)
    except Exception as exc:  # noqa: BLE001 — a crashed work fn must release the lease
        logger.exception("kanban task %s work crashed", task_uuid)
        kanban_dispatch(agent_uuid, {"op": "complete", "taskId": str(task_uuid),
                                     "ok": False, "detail": f"crashed: {exc}"})
        return {"ok": False, "task_uuid": str(task_uuid), "error": str(exc)}
    kanban_dispatch(agent_uuid, {"op": "complete", "taskId": str(task_uuid),
                                 "ok": ok, "detail": detail})
    return {"ok": True, "task_uuid": str(task_uuid), "task_ok": ok, "detail": detail}
