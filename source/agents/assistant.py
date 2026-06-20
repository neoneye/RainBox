"""The assistant: a rainbox-owned ReAct loop over a typed action enum.

Layered by PR so each slice stays shippable:

- PR 1 added the *contract* — `AssistantActionName` and `AssistantStepDecision` —
  so the eval harness could drive a deterministic fake model
  (`agents/assistant_fakes.py`) before any live LLM behaviour existed.
- PR 2 added `AssistantAgent`: the bounded plan -> act -> observe loop.
- PR 3 made the per-step trace durable (assistant_run / assistant_step tables).
- PR 4 (this) adds the read-only actions — `query_memory`, `query_qa`,
  `workspace_read_command`, `kanban_read` — and the dispatcher that runs them
  with a trace-before-action `running` row, an output cap, and an observation
  the loop feeds back to the model. Each action reuses an existing rainbox
  surface; none writes. Writes/MCP/generated code remain out of scope.

The loop owns validation, the step cap, terminal posting, and trace boundaries;
the only live-model seam is `_decide_next_step`. See
docs/proposals/2026-06-19-improvements-v2.md ("Loop skeleton", "Step-decision
schema") for the binding rationale. Concrete shapes here are
illustrative-until-promoted: they may be refined by a later PR as long as they
still satisfy the assistant contracts.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field

import db
import skills
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

# Optional args an action accepts beyond its required ones. Anything outside
# required ∪ optional is rejected at validation ("unknown args are risky"), so a
# stray or unsupported arg becomes a traceable failed step rather than a
# silently-wrong read. kanban_read takes board_uuid only for now; task_uuid is
# not yet implemented and is rejected rather than ignored.
_OPTIONAL_ARGS: dict[AssistantActionName, frozenset[str]] = {
    AssistantActionName.KANBAN_READ: frozenset({"board_uuid"}),
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
        'read kanban board/card state. args: optional {"board_uuid"}; '
        "empty lists all boards"
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


@dataclass(frozen=True)
class AssistantActionContext:
    """What a read action is told about the request it serves. No payload: the
    loop owns the conversation; an action performs one bounded read."""

    journal_id: int
    room_uuid: UUID
    agent_uuid: UUID
    step_index: int


@dataclass(frozen=True)
class AssistantObservation:
    """The result of one read action. `text` is fed back to the model (capped by
    the dispatcher); `data` carries structured detail for the trace, not the
    prompt."""

    ok: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)


AssistantAction = Callable[[AssistantActionContext, dict[str, Any]], AssistantObservation]


def _action_query_memory(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Hybrid memory retrieval (vector + full-text + entity, hard-filtered).
    Secrets are never returned to the model (filter-before-rank:
    include_secret stays False)."""
    from memory.retrieval import format_memory_context, retrieve_memories_hybrid

    query = str(args.get("query", "")).strip()
    memories = retrieve_memories_hybrid(
        query, agent_uuid=ctx.agent_uuid, room_uuid=ctx.room_uuid,
        include_secret=False, journal_id=ctx.journal_id,
    )
    if not memories:
        return AssistantObservation(ok=True, text="No relevant remembered facts.")
    return AssistantObservation(
        ok=True, text=format_memory_context(memories), data={"count": len(memories)}
    )


def _action_query_qa(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Reuse the QueryAgent exact/semantic Q&A pipeline and its read-only dynamic
    handlers (project status, git status, ...). Module-qualified calls so tests
    can stub the embedding-dependent internals."""
    from agents import query_kb_helpers as qkb
    from agents.query_handlers import QueryContext

    query = str(args.get("query", "")).strip()
    qkb._load_kb()
    vs = qkb._vector_store()
    qkb._ensure_populated(vs)
    match = qkb._exact_match(query) or qkb._semantic_match(query, vs)
    if match is None:
        return AssistantObservation(
            ok=True, text="No confident Q&A match.", data={"matched": False}
        )
    qctx = QueryContext(
        room_uuid=ctx.room_uuid, query=query, payload={}, agent_uuid=ctx.agent_uuid
    )
    answer = qkb._resolve_match(match, qctx)
    return AssistantObservation(
        ok=True,
        text=answer,
        data={"matched": True, "qa_id": match.qa_id, "method": match.method,
              "score": match.score},
    )


def _action_workspace_read_command(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Run one allowlisted, non-shell argv in the workspace root via the shared
    command policy + runner. The policy excludes interpreters, mutation, and
    network tools, so this stays a file-inspection reader — not a git/Python/
    shell runner."""
    from tools.command_policy import validate_command
    from tools.workspace_command_runner import (
        COMMAND_TIMEOUT,
        SHELL_ENV,
        CommandTimeout,
        run_command_once,
    )
    from tools.workspace_policy import SHELL_CWD, DisallowedCommand

    command = str(args.get("command", "")).strip()
    try:
        argv = validate_command(command, SHELL_CWD)
    except DisallowedCommand as e:
        return AssistantObservation(ok=False, text=f"blocked: {e}")
    try:
        result = run_command_once(argv, SHELL_CWD, dict(SHELL_ENV))
    except CommandTimeout:
        return AssistantObservation(
            ok=False, text=f"blocked: timed out after {COMMAND_TIMEOUT:g}s"
        )
    except DisallowedCommand as e:
        return AssistantObservation(ok=False, text=f"blocked: {e}")
    return AssistantObservation(
        ok=result.exit_code == 0,
        text=f"$ {command}\n{result.output}\n[exit code: {result.exit_code}]",
        data={"exit_code": result.exit_code},
    )


def _action_kanban_read(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Read kanban state without writing events: one board's markdown when a
    board_uuid is given, otherwise a list of all boards."""
    board_raw = args.get("board_uuid")
    if board_raw:
        try:
            board_uuid = UUID(str(board_raw))
        except (ValueError, TypeError):
            return AssistantObservation(ok=False, text=f"invalid board_uuid: {board_raw!r}")
        markdown = db.kanban_board_markdown(board_uuid)
        if markdown is None:
            return AssistantObservation(ok=False, text="no such kanban board")
        return AssistantObservation(
            ok=True, text=markdown, data={"board_uuid": str(board_uuid)}
        )
    boards = db.kanban_list_boards()
    if not boards:
        return AssistantObservation(ok=True, text="No kanban boards.")
    lines = ["Kanban boards:"]
    for b in boards:
        lines.append(f"- {b.get('name')} ({b.get('uuid')})")
    return AssistantObservation(ok=True, text="\n".join(lines), data={"count": len(boards)})


# Registry of read-only action callables. Phase 4 formalizes this into a
# capability registry with metadata; for now it is the dispatch table.
_ACTIONS: dict[AssistantActionName, AssistantAction] = {
    AssistantActionName.QUERY_MEMORY: _action_query_memory,
    AssistantActionName.QUERY_QA: _action_query_qa,
    AssistantActionName.WORKSPACE_READ_COMMAND: _action_workspace_read_command,
    AssistantActionName.KANBAN_READ: _action_kanban_read,
}


class AssistantAgent(ModelGroupAgent):
    """A bounded ReAct loop: decide a step, validate it, act, observe, repeat
    until a terminal reply or the step cap.

    A specialized `ModelGroupAgent` (not a one-shot `StructuredLLMAgent`) because
    it makes several structured calls — one per step — inside a single
    `handle()`, each reusing the shared model-group fallback via
    `_structured_completion`.

    PR 4 enables the two terminal actions plus the four read-only actions, each
    dispatched through `_dispatch_action` with a trace-before-action `running`
    row and an output cap. The per-step trace is durable via the `_record_step`
    seam (assistant_run / assistant_step rows). Writes remain out of scope.
    """

    # Loop + prompt budget caps (PR 1-4: simple counts/char caps, not a
    # tokenizer-aware budget — that is Phase 3).
    STEP_LIMIT: int = 6
    MAX_RECENT_MESSAGES: int = 30
    MAX_SCRATCHPAD_CHARS: int = 5000
    # The slice of an observation the model/trace see per step. The raw action
    # output is capped harder (MAX_OBSERVATION_CHARS) before this preview.
    MAX_OBSERVATION_PREVIEW_CHARS: int = 1200
    MAX_OBSERVATION_CHARS: int = 4000

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)
        self.step_limit = self.STEP_LIMIT
        # Terminal actions plus the PR 4 read-only set.
        self._enabled_actions: frozenset[AssistantActionName] = frozenset(
            AssistantActionName
        )
        # In-memory mirror of the trace for fast assertions/diagnostics; the
        # durable source of truth is the assistant_run/assistant_step tables.
        self._steps: list[dict[str, Any]] = []
        self._run: Any = None
        # Active-skill guidance for this turn, injected into every step's prompt.
        self._skill_block: str = ""

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("assistant payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def handle(self, journal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        self._steps = []
        # Open the durable run row up front so a crash anywhere below is recorded
        # against it (and the journal, via Agent.run, when we re-raise).
        self._run = db.start_assistant_run(
            journal_id=journal_id,
            room_uuid=room_uuid,
            agent_uuid=self.agent_uuid,
            step_limit=self.step_limit,
        )
        run = self._run
        # The logical step the loop is on, so a crash records its failed row
        # against the right step (not a row count).
        current_step = 0
        try:
            # Only real conversation turns feed the prompt; diagnostic rows
            # (debug-*, progress, thinking) are operator-only and excluded.
            messages = [
                m for m in db.list_room_messages(room_uuid)
                if m.get("kind") == "message"
            ]
            transcript = format_history(messages, context_limit=self.MAX_RECENT_MESSAGES)
            # Retrieve active procedural skills for this turn (candidates are
            # inert and never injected). Best-effort: a retrieval failure must
            # not break the turn.
            self._skill_block = self._build_skill_block(messages, journal_id, room_uuid)
            scratchpad: list[str] = []

            for step_index in range(self.step_limit):
                current_step = step_index
                decision = self._decide_next_step(
                    transcript=transcript, scratchpad=scratchpad, step_index=step_index
                )
                self._record_step(step_index=step_index, phase="planned", decision=decision)

                error = self._validate_decision(decision)
                if error is not None:
                    self._record_step(
                        step_index=step_index, phase="failed", decision=decision,
                        error=error,
                    )
                    scratchpad.append(
                        f"step {step_index}: action '{decision.action.value}' "
                        f"rejected: {error}"
                    )
                    continue

                if decision.action in _TERMINAL_ACTIONS:
                    self._record_step(
                        step_index=step_index, phase="final", decision=decision
                    )
                    text = self._terminal_text(decision)
                    db.post_chat_message(room_uuid, self.agent_uuid, text, kind="message")
                    db.finish_run(run, "finished", final_summary=text[:200])
                    logger.info(
                        "assistant finished run %s in room %s at step %d",
                        run.id, room_uuid, step_index,
                    )
                    return self._run_result("finished", text[:200])

                # Non-terminal read action: commit the `running` row before the
                # action runs (so a kill mid-action leaves it), dispatch, then
                # record the observation and feed a compact form to the model.
                self._record_step(step_index=step_index, phase="running", decision=decision)
                action_ctx = AssistantActionContext(
                    journal_id=journal_id,
                    room_uuid=room_uuid,
                    agent_uuid=self.agent_uuid,
                    step_index=step_index,
                )
                observation = self._dispatch_action(action_ctx, decision)
                preview = observation.text[: self.MAX_OBSERVATION_PREVIEW_CHARS]
                self._record_step(
                    step_index=step_index,
                    phase="observed" if observation.ok else "failed",
                    decision=decision,
                    observation_preview=preview,
                    error=None if observation.ok else preview,
                )
                scratchpad.append(
                    self._compact_step(step_index, decision, observation.ok, preview)
                )

            # Ran out of steps without a terminal action.
            msg = (
                "I couldn't complete this within the step limit. "
                "Please rephrase or narrow the request."
            )
            db.post_chat_message(room_uuid, self.agent_uuid, msg, kind="message")
            db.finish_run(run, "stopped", final_summary="step limit reached")
            logger.warning(
                "assistant run %s hit step limit (%d) in room %s",
                run.id, self.step_limit, room_uuid,
            )
            return self._run_result("stopped", "step limit reached")
        except Exception as exc:
            # Record the failure against the run so it isn't left stuck in
            # 'running'; Agent.run marks the journal failed when we re-raise.
            self._fail_run(run, exc, current_step)
            raise

    def _run_result(self, status: str, final_summary: str) -> dict[str, Any]:
        """The journal result: a short summary plus pointers to the trace — never
        the trace itself (the tables are the trace)."""
        return {
            "ok": status != "failed",
            "status": status,
            "assistant_run_id": self._run.id,
            "assistant_run_uuid": str(self._run.uuid),
            "final_summary": final_summary,
            "step_count": len(self._steps),
        }

    def _fail_run(self, run: Any, exc: Exception, step_index: int) -> None:
        err = f"{type(exc).__name__}: {exc}"
        try:
            self._record_step(step_index=step_index, phase="failed", error=err)
            db.finish_run(run, "failed", final_summary=err)
        except Exception:
            logger.exception("assistant: failed to mark run %s failed", run.id)

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

    def _build_skill_block(
        self, messages: list[dict[str, Any]], journal_id: int, room_uuid: UUID
    ) -> str:
        """Retrieve active skills for the latest human message and render the
        injectable block (empty when nothing matches)."""
        query = ""
        for m in reversed(messages):
            if m.get("sender_type") == "human":
                query = (m.get("text") or "").strip()
                break
        if not query:
            return ""
        try:
            block, _ = skills.build_skill_block(
                query, room_uuid=room_uuid, agent_uuid=self.agent_uuid,
                journal_id=journal_id,
            )
            return block
        except Exception:
            logger.warning("assistant: skill retrieval failed", exc_info=True)
            return ""

    def _build_user_prompt(
        self,
        *,
        transcript: str,
        scratchpad: list[str],
        step_index: int,
    ) -> str:
        parts = []
        if self._skill_block:
            parts.append(self._skill_block)
        parts.append(transcript)
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
        required = _REQUIRED_ARGS.get(action, ())
        for key in required:
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"action '{action.value}' requires a non-empty '{key}' argument"
        # Reject unknown args so an unsupported/typo'd read can't look successful.
        allowed = set(required) | _OPTIONAL_ARGS.get(action, frozenset())
        unknown = sorted(set(args) - allowed)
        if unknown:
            return f"action '{action.value}' got unknown argument(s): {', '.join(unknown)}"
        return None

    def _terminal_text(self, decision: AssistantStepDecision) -> str:
        # Validation guarantees the required key is present and non-empty.
        key = "message" if decision.action is AssistantActionName.REPLY else "question"
        return str(decision.args[key]).strip()

    def _dispatch_action(
        self, ctx: AssistantActionContext, decision: AssistantStepDecision
    ) -> AssistantObservation:
        """Run one validated read action. Exceptions become a failed observation
        (the loop records the failed step); output is capped so a chatty action
        can't blow the prompt budget."""
        action_fn = _ACTIONS.get(decision.action)
        if action_fn is None:
            return AssistantObservation(
                ok=False, text=f"action '{decision.action.value}' has no dispatcher"
            )
        try:
            obs = action_fn(ctx, decision.args)
        except Exception as e:  # an action must never crash the loop
            logger.warning("assistant action %s failed: %s", decision.action.value, e)
            return AssistantObservation(ok=False, text=f"{type(e).__name__}: {e}")
        if len(obs.text) > self.MAX_OBSERVATION_CHARS:
            obs = AssistantObservation(
                ok=obs.ok, text=obs.text[: self.MAX_OBSERVATION_CHARS], data=obs.data
            )
        return obs

    @staticmethod
    def _compact_step(
        step_index: int, decision: AssistantStepDecision, ok: bool, preview: str
    ) -> str:
        status = "ok" if ok else "failed"
        return f"step {step_index}: {decision.action.value} -> {status}: {preview}"

    def _record_step(
        self,
        *,
        step_index: int,
        phase: str,
        decision: AssistantStepDecision | None = None,
        error: str | None = None,
        observation_preview: str | None = None,
    ) -> None:
        """Commit one step-transition to the trace.

        Persists an `assistant_step` row (the source of truth) plus, on a step's
        first transition, a thin `debug-assistant` chat pointer — and mirrors the
        transition into `self._steps` for fast in-process assertions.
        """
        action = decision.action.value if decision is not None else None
        reason = decision.reason if decision is not None else None
        args = decision.args if decision is not None else None
        self._steps.append(
            {
                "step_index": step_index,
                "phase": phase,
                "action": action,
                "reason": reason,
                "error": error,
            }
        )
        if self._run is not None:
            db.append_assistant_step(
                run_id=self._run.id,
                step_index=step_index,
                phase=phase,  # type: ignore[arg-type]
                action=action,
                reason=reason,
                args=args,
                observation_preview=observation_preview,
                error=error,
                model_group_uuid=self.model_group_uuid,
            )
