"""Assistant-run summarizer — a specialized StructuredLLMAgent.

Runs *after* an `AssistantAgent` turn completes (enqueued by the assistant at
every terminal state), so it never blocks the operator's reply. Given a run uuid,
it reads the run's trigger + step trace and makes one structured call producing a
compact digest — what triggered the run, the obstacles hit across its steps, and
a one-word outcome — which it stores on `assistant_run.summary` for the
`/assistant` inspector. It posts no chat, drives no tools, and enqueues nothing
(so it can never summarize itself).
"""

import difflib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

import db
from agents.base import StatusSender, StructuredLLMAgent

# Repeated-call detection: two action steps count as the "same call" when they
# invoke the same action with args at least this similar, and a cluster of this
# many such calls is worth flagging to the summarizer as a possible stuck loop.
_DUP_SIMILARITY_THRESHOLD = 0.85
_DUP_MIN_GROUP = 2


def _canonical_args(args: dict | None) -> str:
    """A stable string for an action's args so near-identical calls compare cleanly
    regardless of key order. Falls back to str() for anything not JSON-serializable."""
    if not args:
        return ""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        return str(args)


def _find_repeated_calls(steps: list) -> list[dict]:
    """Group action steps that make the same function call with near-identical args.

    Returns one entry per cluster of >= _DUP_MIN_GROUP near-identical calls:
    ``{action, indices, count, similarity}`` where `similarity` is the lowest
    pairwise ratio binding the cluster (1.0 = byte-identical). Steps without an
    action (control / terminal) are ignored, and a run whose calls are all
    distinct yields an empty list — so no spurious hint is produced.
    """
    calls = [(s.step_index, s.action, _canonical_args(s.args))
             for s in steps if getattr(s, "action", None)]
    groups: list[dict] = []
    used: set[int] = set()
    for i, (_idx_i, act_i, args_i) in enumerate(calls):
        if i in used:
            continue
        cluster = [i]
        sims: list[float] = []
        for j in range(i + 1, len(calls)):
            if j in used or calls[j][1] != act_i:
                continue
            ratio = difflib.SequenceMatcher(None, args_i, calls[j][2]).ratio()
            if ratio >= _DUP_SIMILARITY_THRESHOLD:
                cluster.append(j)
                sims.append(ratio)
        if len(cluster) >= _DUP_MIN_GROUP:
            used.update(cluster)
            groups.append({
                "action": act_i,
                "indices": [calls[k][0] for k in cluster],
                "count": len(cluster),
                "similarity": min(sims) if sims else 1.0,
            })
    return groups


def _repeated_calls_hint(groups: list[dict]) -> list[str]:
    """Prompt lines describing repeated calls, with the similarity score, for the
    summarizer. Empty when there are no repeats (distinct calls get no hint)."""
    if not groups:
        return []
    lines = ["", "Possible repeated calls (near-identical action + args — may "
             "indicate a stuck loop; treat as an obstacle):"]
    for g in groups:
        idxs = ", ".join(f"#{i}" for i in g["indices"])
        pct = round(g["similarity"] * 100)
        lines.append(f"  - {g['action']} called {g['count']}× (steps {idxs}), "
                     f"args ~{pct}% similar")
    return lines


class RunSummary(BaseModel):
    trigger: str = Field(
        description="A short noun phrase naming what the run was asked to do, for "
        "scanning a list. Drop leading verbs (\"Create a new\" → \"New\") and omit "
        "noisy identifiers like UUIDs or board/object IDs. Keep meaningful names. "
        'E.g. \'New kanban task named "Find my phone"\', not \'Create a new kanban '
        'task named "Find my phone" on kanban board 753fc9b3-…\'.'
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


ASSISTANT_RUN_SUMMARIZER_SYSTEM_PROMPT = """\
You summarize a single completed run of an assistant's reason-act loop, for an \
operator scanning a list of past runs. You are given the message that triggered \
the run and a digest of each step (its action, phase, and any error).

Respond with ONE JSON object matching the `RunSummary` schema and nothing else:
  - `trigger`: a short noun phrase naming what the run was asked to do, for an \
operator scanning a list. Drop leading verbs ("Create a new …" → "New …") and \
omit noisy identifiers like UUIDs or board/object IDs; keep meaningful names. \
E.g. 'New kanban task named "Find my phone"', not 'Create a new kanban task \
named "Find my phone" on kanban board 753fc9b3-…'.
  - `obstacles`: a list of the concrete problems hit across the steps (a `failed` \
phase, an error, a blocked/no-op action, a retry). One short phrase each. Use an \
empty list if the run proceeded without trouble — do NOT invent obstacles. If a \
"Possible repeated calls" note appears below the steps, treat that repetition as \
an obstacle (e.g. "repeated kanban_read 6× — stuck loop").
  - `outcome`: "resolved", "partial", or "failed".

Be terse and factual. Do not add prose, markdown, or fields outside the schema.
"""


class AssistantRunSummarizerAgent(StructuredLLMAgent):
    """Summarize one assistant run by uuid into `assistant_run.summary`."""

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(
            agent_uuid, name, send,
            system_prompt=ASSISTANT_RUN_SUMMARIZER_SYSTEM_PROMPT,
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
        lines.extend(_repeated_calls_hint(_find_repeated_calls(steps)))
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
