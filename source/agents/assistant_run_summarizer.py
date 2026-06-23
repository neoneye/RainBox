"""Assistant-run summarizer — a specialized StructuredLLMAgent.

Runs *after* an `AssistantAgent` turn completes (enqueued by the assistant at
every terminal state), so it never blocks the operator's reply. Given a run uuid,
it reads the run's trigger + step trace and makes one structured call producing a
compact digest — what triggered the run, the obstacles hit across its steps, and
a one-word outcome — which it stores on `assistant_run.summary` for the
`/assistant` inspector. It posts no chat, drives no tools, and enqueues nothing
(so it can never summarize itself).
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

import db
from agents.base import StatusSender, StructuredLLMAgent


class RunSummary(BaseModel):
    trigger: str = Field(
        description="One sentence: what the operator asked for / what kicked off "
        "this run. Plain, concrete, no preamble."
    )
    obstacles: list[str] = Field(
        default_factory=list,
        description="Concrete problems the run hit across its steps — a failed or "
        "blocked action, an error, a retry, a no-op. One short phrase each. Empty "
        "list when the run went smoothly.",
    )
    outcome: Literal["resolved", "partial", "failed"] = Field(
        description='One word: "resolved" if the run fulfilled the request, '
        '"partial" if it only got part way, "failed" if it did not.'
    )


RUN_SUMMARIZER_SYSTEM_PROMPT = """\
You summarize a single completed run of an assistant's reason-act loop, for an \
operator scanning a list of past runs. You are given the message that triggered \
the run and a digest of each step (its action, phase, and any error).

Respond with ONE JSON object matching the `RunSummary` schema and nothing else:
  - `trigger`: one concrete sentence describing what the run was asked to do.
  - `obstacles`: a list of the concrete problems hit across the steps (a `failed` \
phase, an error, a blocked/no-op action, a retry). One short phrase each. Use an \
empty list if the run proceeded without trouble — do NOT invent obstacles.
  - `outcome`: "resolved", "partial", or "failed".

Be terse and factual. Do not add prose, markdown, or fields outside the schema.
"""


class AssistantRunSummarizerAgent(StructuredLLMAgent):
    """Summarize one assistant run by uuid into `assistant_run.summary`."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid, name, send,
            system_prompt=RUN_SUMMARIZER_SYSTEM_PROMPT,
            response_model=RunSummary,
        )

    def _build_prompt(self, run: Any, steps: list, trigger: dict | None) -> str:
        lines = [f"Run status: {run.status}"]
        if trigger:
            lines.append(f"Triggering message from {trigger['sender_name']}: "
                         f"{trigger['text']}")
        else:
            lines.append("Triggering message: (none found)")
        lines.append("")
        lines.append("Steps:")
        if not steps:
            lines.append("  (no steps)")
        for s in steps:
            seg = f"  #{s.step_index} [{s.phase}] {s.action or '-'}"
            if s.error:
                seg += f" — ERROR: {s.error}"
            elif s.observation_preview:
                seg += f" — {s.observation_preview[:200]}"
            lines.append(seg)
        return "\n".join(lines)

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        run = None
        raw = payload.get("run_uuid")
        if raw:
            try:
                run = db.get_assistant_run(UUID(str(raw)))
            except ValueError:
                run = None
        if run is None:
            return {"ok": False, "error": f"assistant run not found: {raw!r}"}

        steps = db.list_assistant_steps(run.uuid)
        trigger = db.get_run_trigger_message(run)
        summary: RunSummary = self._structured_call(  # type: ignore[assignment]
            self._build_prompt(run, steps, trigger)
        )
        db.set_run_summary(run, {
            "trigger": summary.trigger,
            "obstacles": list(summary.obstacles),
            "outcome": summary.outcome,
        })
        return {"ok": True, "response": summary.model_dump()}
