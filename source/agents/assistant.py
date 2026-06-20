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

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field

import db
import skills
import user_profile
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

    # Write actions (PR 9): the first controlled-write family.
    REMEMBER = "remember"              # log-and-undo: create an inert candidate
    ACTIVATE_MEMORY = "activate_memory"  # confirm-tier: activate a candidate
    KANBAN_MOVE = "kanban_move"        # log-and-undo: move a task between columns


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

    journal_id: UUID | None
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


def _action_remember(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: create an inert *candidate* memory claim. Low blast
    radius (candidates never affect behavior until activated), executes
    immediately, and is reversed by rejecting the candidate."""
    text = str(args.get("text", "")).strip()
    claim = db.create_memory_claim(
        scope="room", kind="fact", text=text, confidence=0.5,
        status="candidate", sensitivity="private",
        agent_uuid=ctx.agent_uuid, room_uuid=ctx.room_uuid,
    )
    db.add_memory_evidence(
        memory_uuid=claim.uuid, provenance="inferred_by_model",
        source_type="chat_message", created_by_uuid=ctx.agent_uuid,
    )
    return AssistantObservation(
        ok=True,
        text=f"Remembered as a candidate (reject to undo): {text}",
        data={"memory_uuid": str(claim.uuid), "status": "candidate"},
    )


def _action_activate_memory(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Confirm-tier write *executor*: activate a candidate memory (steers future
    behavior). The dispatcher never calls this inline — it runs only via an
    approved write intent."""
    raw = args.get("memory_uuid")
    try:
        memory_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid memory_uuid: {raw!r}")
    claim = db.get_memory_claim(memory_uuid)
    if claim is None:
        return AssistantObservation(ok=False, text="no such memory claim")
    activated = db.activate_memory_claim(memory_uuid, confirmed_by_uuid=ctx.agent_uuid)
    # Newly active → embed it so hybrid retrieval can use it immediately
    # (best-effort; falls back to lexical-only if no embedder is available).
    from memory.embeddings import refresh_claim_embedding
    refresh_claim_embedding(activated)
    return AssistantObservation(
        ok=True, text=f"Activated memory {memory_uuid}",
        data={"memory_uuid": str(memory_uuid), "status": "active"},
    )


def _action_move_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: move a kanban task to another column of its board.
    Reversible — `data["undo"]` is the inverse move (back to the task's current
    column). Code-owned authority: this does not route through the worker
    observe/work/shape dispatcher; reversibility + trace is the safety."""
    raw_task, raw_col = args.get("task_uuid"), args.get("column_uuid")
    try:
        task_uuid = UUID(str(raw_task))
        column_uuid = UUID(str(raw_col))
    except (ValueError, TypeError):
        return AssistantObservation(
            ok=False, text=f"invalid task_uuid/column_uuid: {raw_task!r}, {raw_col!r}"
        )
    before = db.kanban_get_task(task_uuid)
    if before is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    from_column_uuid = before["columnUuid"]
    try:
        moved = db.kanban_move_task(
            task_uuid, column_uuid,
            actor=str(ctx.agent_uuid), note="assistant move (undoable)",
        )
    except db.KanbanError as e:
        return AssistantObservation(ok=False, text=f"cannot move: {e}")
    if moved is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    return AssistantObservation(
        ok=True,
        text=f"Moved '{before['title']}' to column {column_uuid} (undoable).",
        data={
            "task_uuid": str(task_uuid),
            "from_column_uuid": str(from_column_uuid),
            "to_column_uuid": str(column_uuid),
            "undo": {
                "capability": "kanban_move",
                "payload": {"task_uuid": str(task_uuid),
                            "column_uuid": str(from_column_uuid)},
            },
        },
    )


@dataclass(frozen=True)
class Capability:
    """Code-owned metadata + dispatch for one assistant action — the primitive
    capability registry (Phase 4). The model can only request a capability that
    is in this registry, enabled, and (for the catalog) prompt_exposed. Both the
    prompt catalog and dispatch are generated from this single object, so
    disabling a capability removes it from prompt *and* dispatch at once.

    `family` is the grouping (query/memory/kanban/workspace/conversation/…), kept
    separate from the read/write/network/secrets permission flags. `adapter` is
    None for rainbox-native capabilities and names the owning adapter for
    external ones (e.g. "mcp:github") — unused until the adapter boundary lands.
    """

    name: AssistantActionName
    family: str
    description: str
    required_args: tuple[str, ...] = ()
    optional_args: frozenset[str] = frozenset()
    terminal: bool = False
    action: AssistantAction | None = None
    read: bool = True
    write: bool = False
    # Write approval tier: "log_and_undo" executes immediately with a reversible
    # trace; "confirm" only proposes and needs operator approval. None for reads.
    tier: str | None = None
    network: bool = False
    secrets: bool = False
    confirm_required: bool = False
    dry_run: bool = False
    timeout_seconds: int = 10
    output_cap_chars: int = 4000
    enabled: bool = True
    prompt_exposed: bool = True
    adapter: str | None = None


# The capability registry: one record per action. Boring and explicit on
# purpose. Write-capable capabilities cannot be added without metadata here.
CAPABILITIES: dict[AssistantActionName, Capability] = {
    AssistantActionName.REPLY: Capability(
        name=AssistantActionName.REPLY, family="conversation", read=False,
        description='give your final answer to the user; ends the turn. args: {"message": "..."}',
        required_args=("message",), terminal=True,
    ),
    AssistantActionName.ASK_CLARIFYING_QUESTION: Capability(
        name=AssistantActionName.ASK_CLARIFYING_QUESTION, family="conversation", read=False,
        description=('ask the user for missing information; ends the turn. '
                     'args: {"question": "..."}'),
        required_args=("question",), terminal=True,
    ),
    AssistantActionName.QUERY_MEMORY: Capability(
        name=AssistantActionName.QUERY_MEMORY, family="memory",
        description='search remembered facts. args: {"query": "..."}',
        required_args=("query",), action=_action_query_memory,
    ),
    AssistantActionName.QUERY_QA: Capability(
        name=AssistantActionName.QUERY_QA, family="query",
        description=('answer from the Q&A knowledge base and read-only handlers. '
                     'args: {"query": "..."}'),
        required_args=("query",), action=_action_query_qa, output_cap_chars=6000,
    ),
    AssistantActionName.WORKSPACE_READ_COMMAND: Capability(
        name=AssistantActionName.WORKSPACE_READ_COMMAND, family="workspace",
        description='run an allowlisted read-only file-inspection command. args: {"command": "..."}',
        required_args=("command",), action=_action_workspace_read_command,
    ),
    AssistantActionName.KANBAN_READ: Capability(
        name=AssistantActionName.KANBAN_READ, family="kanban",
        description=('read kanban board/card state. args: optional {"board_uuid"}; '
                     "empty lists all boards"),
        optional_args=frozenset({"board_uuid"}), action=_action_kanban_read,
    ),
    AssistantActionName.REMEMBER: Capability(
        name=AssistantActionName.REMEMBER, family="memory",
        description=('remember a fact as an inert candidate (reject to undo). '
                     'args: {"text": "..."}'),
        required_args=("text",), action=_action_remember,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.ACTIVATE_MEMORY: Capability(
        name=AssistantActionName.ACTIVATE_MEMORY, family="memory",
        description=('propose activating a candidate memory so it steers future '
                     'answers; needs your confirmation. args: {"memory_uuid": "..."}'),
        required_args=("memory_uuid",), action=_action_activate_memory,
        read=False, write=True, tier="confirm",
    ),
    AssistantActionName.KANBAN_MOVE: Capability(
        name=AssistantActionName.KANBAN_MOVE, family="kanban",
        description=('move a kanban task to another column; reversible (undoable). '
                     'args: {"task_uuid": "...", "column_uuid": "..."}'),
        required_args=("task_uuid", "column_uuid"),
        action=_action_move_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
}


def _disabled_capability_names() -> set[str]:
    """Capability names the operator has turned off via the
    assistant.disabled_capabilities setting. Best-effort (no app context → none)."""
    try:
        val = db.get_setting("assistant.disabled_capabilities")
    except Exception:
        return set()
    if isinstance(val, (list, tuple)):
        return {str(x) for x in val}
    if isinstance(val, str):
        return {t.strip() for t in val.split(",") if t.strip()}
    return set()


def _base_enabled_capabilities() -> dict[AssistantActionName, Capability]:
    """Capabilities enabled in code, ignoring the operator override. Safe without
    an app context (e.g. at agent construction)."""
    return {n: c for n, c in CAPABILITIES.items() if c.enabled}


def enabled_capabilities() -> dict[AssistantActionName, Capability]:
    """Capabilities enabled in code AND not disabled by the operator setting.
    Requires an app context (reads settings)."""
    disabled = _disabled_capability_names()
    return {
        n: c for n, c in _base_enabled_capabilities().items()
        if n.value not in disabled
    }


def capability_report() -> list[dict[str, Any]]:
    """A flat, inspectable view of every capability and whether it is currently
    enabled — so the operator can see exactly which powers the assistant has."""
    disabled = _disabled_capability_names()
    return [
        {
            "name": n.value, "family": c.family, "read": c.read, "write": c.write,
            "network": c.network, "secrets": c.secrets, "terminal": c.terminal,
            "prompt_exposed": c.prompt_exposed, "adapter": c.adapter,
            "enabled": c.enabled and n.value not in disabled,
        }
        for n, c in CAPABILITIES.items()
    ]


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
    # output is capped harder (per-capability output_cap_chars) before this preview.
    MAX_OBSERVATION_PREVIEW_CHARS: int = 1200

    def __init__(self, agent_uuid: UUID, name: str, send: StatusSender) -> None:
        super().__init__(agent_uuid, name, send)
        self.step_limit = self.STEP_LIMIT
        # The capabilities this turn may use. Defaults to the code-enabled set;
        # handle() refreshes it with the operator's disable setting (which needs
        # an app context). Catalog, validation, and dispatch all read from it, so
        # a disabled capability disappears from prompt and dispatch together.
        self._caps: dict[AssistantActionName, Capability] = _base_enabled_capabilities()
        # In-memory mirror of the trace for fast assertions/diagnostics; the
        # durable source of truth is the assistant_run/assistant_step tables.
        self._steps: list[dict[str, Any]] = []
        self._run: Any = None
        # Active-skill guidance for this turn, injected into every step's prompt.
        self._skill_block: str = ""
        # Operator self-model digest (active memory) for this turn, injected
        # before the skill block.
        self._profile_block: str = ""
        # Coarse current activity, surfaced in heartbeats so a slow run looks
        # different from a hung one.
        self._activity: str = "idle"

    @staticmethod
    def _room_uuid(payload: dict[str, Any]) -> UUID:
        raw = payload.get("room_uuid")
        if not raw:
            raise ValueError("assistant payload missing 'room_uuid'")
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        # Resolve the operator-effective capability set for this turn.
        self._caps = enabled_capabilities()
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
            # Operator self-model digest: query-independent, always present (best
            # -effort — a retrieval failure must not break the turn).
            self._profile_block = self._build_profile_block(journal_id, room_uuid)
            scratchpad: list[str] = []

            for step_index in range(self.step_limit):
                current_step = step_index
                # Step boundary: honour any operator stop/redirect before the
                # next model call, so a stop leaves a clean trace (not a killed
                # process) and a redirect steers the next step.
                stopped = self._apply_pending_controls(run, step_index, scratchpad)
                if stopped is not None:
                    return stopped
                self._activity = f"deciding step {step_index}"
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

                if self._caps[decision.action].terminal:
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

                # Non-terminal action: commit the `running` row before acting (so
                # a kill mid-action leaves it), then act and record the
                # observation. A confirm-tier write is *proposed* here, never
                # executed inline; everything else (reads, log-and-undo writes)
                # executes immediately.
                self._activity = f"running {decision.action.value}"
                self._record_step(step_index=step_index, phase="running", decision=decision)
                action_ctx = AssistantActionContext(
                    journal_id=journal_id,
                    room_uuid=room_uuid,
                    agent_uuid=self.agent_uuid,
                    step_index=step_index,
                )
                cap = self._caps[decision.action]
                if cap.write and cap.tier == "confirm":
                    observation = self._propose_write(action_ctx, decision, cap)
                else:
                    observation = self._dispatch_action(action_ctx, decision)
                    if cap.write and cap.tier == "log_and_undo" and observation.ok:
                        self._record_log_and_undo(action_ctx, cap, decision, observation)
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
        for action in AssistantActionName:  # stable enum order
            cap = self._caps.get(action)
            if cap is not None and cap.prompt_exposed:
                lines.append(f"- {action.value}: {cap.description}")
        return "\n".join(lines)

    def _build_skill_block(
        self, messages: list[dict[str, Any]], journal_id: UUID, room_uuid: UUID
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

    def _build_profile_block(self, journal_id: UUID, room_uuid: UUID) -> str:
        """Render the operator self-model digest (active memory) for this turn.
        Query-independent (unlike `query_memory`); empty when there is no active
        profile. Best-effort: a retrieval failure must not break the turn."""
        try:
            block, _ = user_profile.build_profile_block(
                agent_uuid=self.agent_uuid, room_uuid=room_uuid,
                journal_id=journal_id,
            )
            return block
        except Exception:
            logger.warning("assistant: profile retrieval failed", exc_info=True)
            return ""

    def _build_user_prompt(
        self,
        *,
        transcript: str,
        scratchpad: list[str],
        step_index: int,
    ) -> str:
        parts = []
        # Profile (who the operator is) before skills (how to do the task).
        if self._profile_block:
            parts.append(self._profile_block)
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
        """Return an error string if the decision can't be dispatched, else None.
        Everything is checked against the effective capability registry, so a
        disabled or unknown capability is rejected here before any dispatch."""
        action = decision.action
        cap = self._caps.get(action)
        if cap is None:
            return f"action '{action.value}' is not available"
        args = decision.args or {}
        for key in cap.required_args:
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"action '{action.value}' requires a non-empty '{key}' argument"
        # Reject unknown args so an unsupported/typo'd read can't look successful.
        allowed = set(cap.required_args) | cap.optional_args
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
        """Run one validated read action via its registry callable. Exceptions
        become a failed observation (the loop records the failed step); output is
        capped per the capability so a chatty action can't blow the prompt
        budget."""
        cap = self._caps.get(decision.action)
        if cap is None or cap.action is None:
            return AssistantObservation(
                ok=False, text=f"action '{decision.action.value}' has no dispatcher"
            )
        try:
            obs = cap.action(ctx, decision.args)
        except Exception as e:  # an action must never crash the loop
            logger.warning("assistant action %s failed: %s", decision.action.value, e)
            return AssistantObservation(ok=False, text=f"{type(e).__name__}: {e}")
        if len(obs.text) > cap.output_cap_chars:
            obs = AssistantObservation(
                ok=obs.ok, text=obs.text[: cap.output_cap_chars], data=obs.data
            )
        return obs

    def _propose_write(
        self,
        ctx: AssistantActionContext,
        decision: AssistantStepDecision,
        cap: "Capability",
    ) -> AssistantObservation:
        """Record a confirm-tier write as a proposed intent instead of executing
        it. The actual write runs only via agents.assistant_writes.execute_write_intent
        after the operator approves — so a confirm-tier write can never execute
        inline, by code, not prompt discipline."""
        preview = f"{cap.name.value}: {json.dumps(decision.args, sort_keys=True)}"
        intent = db.create_write_intent(
            run_id=self._run.id,
            step_index=ctx.step_index,
            capability_name=cap.name.value,
            payload=decision.args,
            preview_text=preview,
            room_uuid=ctx.room_uuid,
            agent_uuid=ctx.agent_uuid,
        )
        return AssistantObservation(
            ok=True,
            text=(f"Proposed (awaiting your confirmation): {preview}. "
                  f"Confirm intent {intent.uuid} to apply."),
            data={"write_intent_uuid": str(intent.uuid), "state": "proposed"},
        )

    def _record_log_and_undo(
        self,
        ctx: AssistantActionContext,
        cap: "Capability",
        decision: AssistantStepDecision,
        observation: AssistantObservation,
    ) -> None:
        """Record an executed log-and-undo write as a `completed`, reversible
        ledger row. Created atomically in `completed` (never `proposed`) so it
        can't be confirm-executed into a duplicate write; `result["undo"]`
        carries the inverse op consumed by undo_write_intent."""
        preview = f"{cap.name.value}: {json.dumps(decision.args, sort_keys=True)}"
        db.create_write_intent(
            run_id=self._run.id,
            step_index=ctx.step_index,
            capability_name=cap.name.value,
            payload=decision.args,
            preview_text=preview,
            room_uuid=ctx.room_uuid,
            agent_uuid=ctx.agent_uuid,
            state="completed",
            result={"undo": observation.data.get("undo"), "text": observation.text},
        )

    def _heartbeat_extra(self) -> dict[str, Any]:
        extra: dict[str, Any] = {"activity": self._activity}
        if self._run is not None:
            extra["assistant_run_id"] = self._run.id
        return extra

    def _apply_pending_controls(
        self, run: Any, step_index: int, scratchpad: list[str]
    ) -> dict[str, Any] | None:
        """Apply operator controls at a step boundary. Returns a run-result dict
        when the run was stopped (the loop should return it), else None.

        A pending stop wins: it records a `control` trace step, posts a clean
        message, finishes the run `stopped`, and ignores any other pending
        controls. Otherwise pending redirects are folded into the scratchpad so
        the next step sees them — prior steps are never touched."""
        controls = db.list_pending_controls(run.id)
        if not controls:
            return None

        stop = next((c for c in controls if c.command == "stop"), None)
        if stop is not None:
            self._record_control(step_index, "stop", "stop requested by operator")
            db.mark_control_state(stop, "applied", note=f"stopped at step {step_index}")
            for other in controls:
                if other.id != stop.id:
                    db.mark_control_state(other, "ignored", note="run stopped")
            self._activity = "stopped"
            db.post_chat_message(
                run.room_uuid, self.agent_uuid, "Stopped at your request.", kind="message"
            )
            db.finish_run(run, "stopped",
                          final_summary=f"stopped by operator at step {step_index}")
            logger.info("assistant run %s stopped by operator at step %d", run.id, step_index)
            return self._run_result("stopped", "stopped by operator")

        for c in controls:  # redirects only at this point
            instruction = str((c.payload or {}).get("instruction", "")).strip()
            self._record_control(step_index, "redirect", instruction or "(no instruction)")
            if instruction:
                scratchpad.append(f"operator redirect: {instruction}")
            db.mark_control_state(c, "applied", note="redirect applied")
        return None

    def _record_control(self, step_index: int, command: str, detail: str) -> None:
        """Persist a `control` trace step (and mirror it) so an applied stop/
        redirect is visible in the trace."""
        self._steps.append(
            {"step_index": step_index, "phase": "control", "action": command,
             "reason": detail, "error": None}
        )
        if self._run is not None:
            db.append_assistant_step(
                run_id=self._run.id, step_index=step_index, phase="control",
                action=command, reason=detail, model_group_uuid=self.model_group_uuid,
            )

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
