"""The first LLM kanban worker (docs/kanban-design.md roadmap item 2; spec:
docs/superpowers/specs/2026-06-11-kanban-llm-worker-design.md).

KanbanWorkerAgent executes ONE claimed card per inbox item with ONE
structured-output call (the /benchmark_kanban verdict: markdown context +
structured output). Its work product is TEXT — the deliverable lands in the
task's event trail as a 'progress' event; it never touches files, shells, or
rooms. Completion goes through the authority dispatcher, and because the
worker is UNVERIFIED (no kanban_verified flag), a successful complete routes
to the board's Review column when one exists — a human moves Review → Done.

Readiness is folded into the single call: the model must return
status='unclear' (with a precise reason) when the card has no verifiable
acceptance criteria, which becomes complete(ok=false, 'unclear acceptance
criteria: …') — a quality signal in the ledger, not confident nonsense."""

import logging
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel

from agents.base import StatusSender, StructuredLLMAgent
from tools.kanban_dispatcher import kanban_dispatch
from tools.kanban_runner import run_kanban_task

logger = logging.getLogger(__name__)


class KanbanWorkerReply(BaseModel):
    """The worker's single structured reply per card (the edit_document_v2
    pattern: a status plus a required human-readable explanation)."""
    status: Literal["done", "unclear", "failed"]
    deliverable: str = ""  # the complete work product; required when done
    comment: str = ""      # the reason; required when unclear/failed


KANBAN_WORKER_SYSTEM_PROMPT: str = (
    "You are a kanban worker agent. You are given ONE claimed task from a "
    "kanban board, plus the board's markdown serialization for context. The "
    "task's column shows recent events — that trail is your working memory "
    "if this task was started before.\n\n"
    "Your job: produce the task's deliverable as TEXT (an analysis, draft, "
    "answer, summary, or plan — whatever the task asks for). You cannot run "
    "commands, edit files, or move cards; your text IS the work.\n\n"
    "First decide readiness: can you tell, from the title and description, "
    "what a correct, finished deliverable looks like? If not, reply "
    "status='unclear' with a comment naming exactly what is missing. Never "
    "produce confident filler for an unclear task.\n\n"
    "Reply with exactly one of:\n"
    "- status='done': 'deliverable' contains the COMPLETE work product (not "
    "a promise or a plan to do it later); 'comment' is one short line.\n"
    "- status='unclear': 'comment' names the missing acceptance criteria.\n"
    "- status='failed': 'comment' says precisely why the task cannot be "
    "done.\n"
)


class KanbanWorkerAgent(StructuredLLMAgent):
    """One structured call per card; deliverable into the event trail;
    complete via the authority dispatcher (unverified → Review)."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send,
                         system_prompt=KANBAN_WORKER_SYSTEM_PROMPT,
                         response_model=KanbanWorkerReply)

    # This class overrides handle() entirely and deliberately bypasses the base
    # class's user_prompt(payload) hook (which would otherwise serialize the
    # payload as JSON). The prompt needs the claimed task + focus context, which
    # only exist inside the work() callback.
    @staticmethod
    def _work_user_prompt(task: dict[str, Any], context_md: str) -> str:
        return (
            "Board context (markdown; the ids in backticks are "
            "authoritative):\n\n"
            f"{context_md}\n"
            "Your claimed task:\n"
            f"- taskId: {task['uuid']}\n"
            f"- title: {task['title']}\n"
            f"- description: {task['description'] or '(none)'}\n\n"
            "Produce the deliverable for this task now."
        )

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("source") != "kanban":
            return {"ok": False,
                    "error": "kanban_worker only handles kanban payloads"}

        def work(task: dict[str, Any], context_md: str) -> tuple[bool, str]:
            reply = cast(KanbanWorkerReply,
                         self._structured_call(self._work_user_prompt(task, context_md)))
            if reply.status == "done":
                if not reply.deliverable.strip():
                    return False, ("model claimed done but returned an empty "
                                   "deliverable")
                kanban_dispatch(self.agent_uuid, {
                    "op": "append_event", "taskId": task["uuid"],
                    "kind": "progress", "detail": reply.deliverable})
                return True, reply.comment or "deliverable in event trail"
            if not reply.comment.strip():
                return False, f"model returned status={reply.status} without a reason"
            if reply.status == "unclear":
                return False, f"unclear acceptance criteria: {reply.comment}"
            return False, reply.comment

        return run_kanban_task(self.agent_uuid, payload, work)
