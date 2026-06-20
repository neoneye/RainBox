"""The assistant: a rainbox-owned ReAct loop over a typed action enum.

Layered by PR so each slice stays shippable:

- PR 1 added the *contract* — `AssistantActionName` and `AssistantStepDecision` —
  so the eval harness could drive a deterministic fake model
  (`agents/assistant_fakes.py`) before any live LLM behaviour existed.
- PR 2 (this) adds `AssistantAgent`: the bounded plan -> act -> observe loop. It
  enables only the two terminal actions (`reply`, `ask_clarifying_question`);
  the read-only action dispatcher arrives in PR 4.
- PR 3 makes the per-step trace durable (dedicated tables); here it lives in
  `self._steps`, which `_record_step` is the single seam for.

The loop owns validation, the step cap, terminal posting, and trace boundaries;
the only live-model seam is `_decide_next_step`. See
docs/proposals/2026-06-19-improvements-v2.md ("Loop skeleton", "Step-decision
schema") for the binding rationale. Concrete shapes here are
illustrative-until-promoted: they may be refined by a later PR as long as they
still satisfy the assistant contracts.
"""

import logging
from enum import Enum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field

import db
from agents.base import ModelGroupAgent, StatusSender
from chat.transcript import format_history

logger = logging.getLogger(__name__)


class AssistantActionName(str, Enum):
    """The bounded set of capabilities a single assistant step may request.

    This enum is the primitive capability registry (Phase 4 formalizes it with
    metadata). The model can only ever name an action in this enum; code, not
    prompt text, decides what each one is allowed to do.

    PR 1 ships the full read-only enum so the eval harness can script the
    actions PRs 2-4 will implement, but only the two terminal actions are wired
    in PR 2 and the read actions in PR 4.
    """

    # Terminal actions (PR 2): the loop ends the run and posts a chat message.
    REPLY = "reply"
    ASK_CLARIFYING_QUESTION = "ask_clarifying_question"

    # Read-only actions (PR 4): each performs one bounded read and returns an
    # observation the loop feeds back to the model.
    QUERY_MEMORY = "query_memory"
    QUERY_QA = "query_qa"
    WORKSPACE_READ_COMMAND = "workspace_read_command"
    KANBAN_READ = "kanban_read"


class AssistantStepDecision(BaseModel):
    """One structured decision the model emits per loop step.

    Emitted via the provider's grammar-constrained structured-output mode
    (`as_structured_llm`), not freeform string parsing. The dispatcher owns
    action-specific argument typing for now, so `args` stays an open dict until
    the action surface is large enough to justify a per-action union.
    """

    reason: str = Field(
        description=(
            "Brief operator-facing rationale for this step. This is an audit "
            "note shown in the trace, not hidden chain-of-thought."
        )
    )
    action: AssistantActionName
    args: dict[str, Any] = Field(default_factory=dict)


# Required args per action — the dispatcher's validator rejects a decision whose
# required args are missing/empty before anything runs. Defined for the full
# read-only enum (PR 4 reuses it); PR 2 only *enables* the terminal actions.
_REQUIRED_ARGS: dict[AssistantActionName, tuple[str, ...]] = {
    AssistantActionName.REPLY: ("message",),
    AssistantActionName.ASK_CLARIFYING_QUESTION: ("question",),
    AssistantActionName.QUERY_MEMORY: ("query",),
    AssistantActionName.QUERY_QA: ("query",),
    AssistantActionName.WORKSPACE_READ_COMMAND: ("command",),
    AssistantActionName.KANBAN_READ: (),
}

# One-line prompt help per action, used to render the action catalog.
_ACTION_HELP: dict[AssistantActionName, str] = {
    AssistantActionName.REPLY: (
        'give your final answer to the user; ends the turn. args: {"message": "..."}'
    ),
    AssistantActionName.ASK_CLARIFYING_QUESTION: (
        "ask the user for missing information; ends the turn. "
        'args: {"question": "..."}'
    ),
    AssistantActionName.QUERY_MEMORY: (
        'search remembered facts. args: {"query": "..."}'
    ),
    AssistantActionName.QUERY_QA: (
        "answer from the Q&A knowledge base and read-only handlers. "
        'args: {"query": "..."}'
    ),
    AssistantActionName.WORKSPACE_READ_COMMAND: (
        'run an allowlisted read-only file-inspection command. args: {"command": "..."}'
    ),
    AssistantActionName.KANBAN_READ: (
        'read kanban board/card state. args: optional {"board_uuid"|"task_uuid"}'
    ),
}

# Actions that end the run: the loop posts a chat message and finishes.
_TERMINAL_ACTIONS: frozenset[AssistantActionName] = frozenset(
    {AssistantActionName.REPLY, AssistantActionName.ASK_CLARIFYING_QUESTION}
)


ASSISTANT_SYSTEM_PROMPT: str = """\
You are a personal assistant that works in small, explicit steps.

Each step you emit exactly one decision as structured output with three fields:
- reason: a short operator-facing note explaining this step. It is shown in an
  audit trace, so keep it brief and factual — it is not hidden scratch reasoning.
- action: one of the available actions listed below.
- args: the arguments for that action.

Work one step at a time. When you have enough to answer, use `reply`. If the
request is ambiguous or missing information, use `ask_clarifying_question`. Only
use actions from the list below; any other action is rejected."""


class AssistantAgent(ModelGroupAgent):
    """A bounded ReAct loop: decide a step, validate it, act, observe, repeat
    until a terminal reply or the step cap.

    A specialized `ModelGroupAgent` (not a one-shot `StructuredLLMAgent`) because
    it makes several structured calls — one per step — inside a single
    `handle()`, each reusing the shared model-group fallback via
    `_structured_completion`.

    PR 2 enables only the terminal actions; the read-only action dispatch branch
    and the `query_*`/`workspace_read_command`/`kanban_read` actions arrive in
    PR 4. The per-step trace is held in `self._steps` and committed through the
    `_record_step` seam, which PR 3 swaps for durable `assistant_run` /
    `assistant_step` rows.
    """

    # Loop + prompt budget caps (PR 1-4: simple counts/char caps, not a
    # tokenizer-aware budget — that is Phase 3).
    STEP_LIMIT: int = 6
    MAX_RECENT_MESSAGES: int = 30
    MAX_SCRATCHPAD_CHARS: int = 5000

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)
        self.step_limit = self.STEP_LIMIT
        # PR 2 enables only terminal actions; PR 4 widens this to the read set.
        self._enabled_actions: frozenset[AssistantActionName] = _TERMINAL_ACTIONS
        self._steps: list[dict[str, Any]] = []

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("assistant payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        self._steps = []
        # Only real conversation turns feed the prompt; diagnostic rows
        # (debug-*, progress, thinking) are operator-only and excluded.
        messages = [
            m for m in db.list_room_messages(room_uuid)
            if m.get("kind") == "message"
        ]
        transcript = format_history(messages, context_limit=self.MAX_RECENT_MESSAGES)
        scratchpad: list[str] = []

        for step_index in range(self.step_limit):
            decision = self._decide_next_step(
                transcript=transcript, scratchpad=scratchpad, step_index=step_index
            )
            self._record_step(step_index=step_index, phase="planned", decision=decision)

            error = self._validate_decision(decision)
            if error is not None:
                self._record_step(
                    step_index=step_index, phase="failed", decision=decision, error=error
                )
                scratchpad.append(
                    f"step {step_index}: action '{decision.action.value}' "
                    f"rejected: {error}"
                )
                continue

            # PR 2 enables only terminal actions; PR 4 replaces this assert with
            # the read-action dispatch branch for enabled non-terminal actions.
            assert decision.action in _TERMINAL_ACTIONS
            self._record_step(step_index=step_index, phase="final", decision=decision)
            text = self._terminal_text(decision)
            db.post_chat_message(room_uuid, self.agent_uuid, text, kind="message")
            logger.info(
                "assistant finished run in room %s at step %d", room_uuid, step_index
            )
            return {
                "ok": True,
                "status": "finished",
                "final_summary": text[:200],
                "step_count": len(self._steps),
            }

        # Ran out of steps without a terminal action.
        msg = (
            "I couldn't complete this within the step limit. "
            "Please rephrase or narrow the request."
        )
        db.post_chat_message(room_uuid, self.agent_uuid, msg, kind="message")
        logger.warning(
            "assistant hit step limit (%d) in room %s", self.step_limit, room_uuid
        )
        return {
            "ok": True,
            "status": "stopped",
            "final_summary": "step limit reached",
            "step_count": len(self._steps),
        }

    # --- the live-model seam --------------------------------------------------

    def _decide_next_step(
        self,
        *,
        transcript: str,
        scratchpad: list[str],
        step_index: int,
    ) -> AssistantStepDecision:
        """Ask the model for the next step. The single live-model seam: tests
        monkeypatch this with `agents.assistant_fakes.scripted_decisions(...)`."""
        user_prompt = self._build_user_prompt(
            transcript=transcript, scratchpad=scratchpad, step_index=step_index
        )
        result = self._structured_completion(
            system_prompt=self._system_prompt(),
            user_prompt=user_prompt,
            response_model=AssistantStepDecision,
        )
        return cast(AssistantStepDecision, result)

    # --- prompt assembly ------------------------------------------------------

    def _system_prompt(self) -> str:
        return f"{ASSISTANT_SYSTEM_PROMPT}\n\n{self._action_catalog()}"

    def _action_catalog(self) -> str:
        lines = ["Available actions (choose exactly one per step):"]
        for action in AssistantActionName:
            if action in self._enabled_actions:
                lines.append(f"- {action.value}: {_ACTION_HELP[action]}")
        return "\n".join(lines)

    def _build_user_prompt(
        self,
        *,
        transcript: str,
        scratchpad: list[str],
        step_index: int,
    ) -> str:
        parts = [transcript]
        rendered = self._render_scratchpad(scratchpad)
        if rendered:
            parts.append(f"Steps you have already taken this turn:\n{rendered}")
        parts.append(
            f"Decide step {step_index + 1} of at most {self.step_limit}."
        )
        return "\n\n".join(parts)

    def _render_scratchpad(self, scratchpad: list[str]) -> str:
        if not scratchpad:
            return ""
        text = "\n".join(scratchpad)
        if len(text) > self.MAX_SCRATCHPAD_CHARS:
            # Keep the most recent context when the budget is exceeded.
            text = text[-self.MAX_SCRATCHPAD_CHARS:]
        return text

    # --- validation, terminal output, trace -----------------------------------

    def _validate_decision(self, decision: AssistantStepDecision) -> str | None:
        """Return an error string if the decision can't be dispatched, else None."""
        action = decision.action
        if action not in self._enabled_actions:
            return f"action '{action.value}' is not available"
        args = decision.args or {}
        for key in _REQUIRED_ARGS.get(action, ()):
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"action '{action.value}' requires a non-empty '{key}' argument"
        return None

    def _terminal_text(self, decision: AssistantStepDecision) -> str:
        # Validation guarantees the required key is present and non-empty.
        key = "message" if decision.action is AssistantActionName.REPLY else "question"
        return str(decision.args[key]).strip()

    def _record_step(
        self,
        *,
        step_index: int,
        phase: str,
        decision: AssistantStepDecision | None = None,
        error: str | None = None,
    ) -> None:
        """Commit one step-transition to the trace. PR 2 keeps the trace in
        memory; PR 3 overrides this to persist `assistant_run`/`assistant_step`
        rows and post a thin `debug-assistant` chat pointer."""
        self._steps.append(
            {
                "step_index": step_index,
                "phase": phase,
                "action": decision.action.value if decision is not None else None,
                "reason": decision.reason if decision is not None else None,
                "error": error,
            }
        )
