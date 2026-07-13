"""The assistant: a rainbox-owned ReAct loop over a typed action enum.

The contract is `AssistantActionName` + `AssistantStepDecision`: the model emits
one structured decision per step, the loop validates it, dispatches the action,
records a durable per-step trace (assistant_run / assistant_step tables), feeds
the observation back, and repeats until a terminal `reply`/`ask_clarifying_question`
or the step cap. Actions are read-only (query_memory,
workspace_read_command, kanban_read) and write families (memory, skills, kanban,
reminders, files) — each risk-tiered (log-and-undo / confirm) and traced.

The loop owns validation, the step cap, terminal posting, and trace boundaries;
the only live-model seam is `_decide_next_step` (the eval harness swaps in a
deterministic fake via `agents/assistant_fakes.py`).
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field

import db
import skills
import user_profile
from agents.base import ModelGroupAgent, StatusSender
from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID, ASSISTANT_WORKING_NOTICE
from chat.transcript import format_history

logger = logging.getLogger(__name__)


class AssistantActionName(str, Enum):
    """The bounded set of capabilities a single assistant step may request.

    This enum is the capability registry (the `CAPABILITIES` table carries each
    action's metadata). The model can only ever name an action in this enum;
    code, not prompt text, decides what each one is allowed to do.
    """

    # Terminal actions: the loop ends the run and posts a chat message.
    REPLY = "reply"
    ASK_CLARIFYING_QUESTION = "ask_clarifying_question"

    # Read-only actions: each performs one bounded read and returns an
    # observation the loop feeds back to the model.
    QUERY_MEMORY = "query_memory"
    WORKSPACE_READ_COMMAND = "workspace_read_command"
    KANBAN_READ = "kanban_read"

    # Write actions, each risk-tiered:
    REMEMBER = "remember"              # log-and-undo: create a candidate memory
    ACTIVATE_MEMORY = "activate_memory"  # confirm-tier: activate a candidate
    FORGET_MEMORY = "forget_memory"      # log-and-undo: reject a memory (stop recalling it)
    KANBAN_MOVE_TASK = "kanban_move_task"  # log-and-undo: move a task between columns
    KANBAN_COMPLETE = "kanban_complete"  # log-and-undo: mark a task done
    KANBAN_COMMENT = "kanban_comment"    # log-and-undo: comment on a task
    KANBAN_CREATE_TASK = "kanban_create_task"  # log-and-undo: create a task on a board
    KANBAN_DELETE_TASK = "kanban_delete_task"  # internal: create_task's undo inverse (not prompt-exposed)
    KANBAN_CREATE_BOARD = "kanban_create_board"  # log-and-undo: create a new board
    KANBAN_DELETE_BOARD = "kanban_delete_board"  # internal: create_board's undo inverse (not prompt-exposed)
    SET_REMINDER = "set_reminder"      # confirm-tier (dry-run): schedule a reminder message
    EDIT_FILE = "edit_file"            # confirm-tier (dry-run diff): edit a workspace file
    PROPOSE_SKILL = "propose_skill"    # log-and-undo: write an inert candidate skill
    ACTIVATE_SKILL = "activate_skill"  # confirm-tier: activate a candidate skill
    SKILL_DELETE = "skill_delete"      # internal: propose_skill's undo inverse (not prompt-exposed)
    REJECT_MEMORY_CANDIDATE = "reject_memory_candidate"  # internal: remember's undo inverse
    REACTIVATE_MEMORY = "reactivate_memory"  # internal: forget's undo inverse (not prompt-exposed)


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
use actions from the list below; any other action is rejected.

Match the read action to the data you need: `kanban_read` for boards/tasks,
`query_memory` for remembered facts and general questions (project/git status,
capabilities). Do not use `query_memory` to inspect kanban or files.
Earlier messages are context, not a source of facts. Before you answer any
question about remembered facts, stored data, or a live value (e.g. token
usage or status), call the matching read action this turn.
Do not reuse an answer from an earlier message: stored facts may have changed
or become restricted since, and live values change between turns.
A recalled fact tagged `truncateN` (e.g. `truncate1200`) is shortened to N
characters, and an "omitted" note means lower-ranked facts were dropped to fit.
When you need the full text of such a fact, call `query_memory` again with that
fact's uuid — `{"uuid": "<the fact's uuid>"}` — instead of a query.
When a step fails, fix the specific problem it reports — never resubmit the same
args, and never invent placeholder values like `<COLUMN_UUID>`; if you lack an
id, read for it or omit the optional argument.

Never tell the operator you did something (moved, created, completed, commented,
remembered, edited…) unless an earlier step actually ran that write action and it
returned ok. Reading a task is not moving it. If you have not performed the action
yet, perform it now — do not `reply` claiming a result you have not produced."""


# Posted into a room once after a shield/Q&A change so the model re-checks facts
# instead of reusing an earlier answer from the transcript.
FACTS_INVALIDATION_NOTICE: str = (
    "Notice: a setting changed since earlier in this conversation, so stored "
    "facts and the Q&A knowledge base may now differ. Re-check any fact with "
    "query_memory before relying on it — earlier answers in this conversation "
    "may be out of date."
)


def _demote_trailing_facts_marker(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the operator's message as the transcript's Current message. A facts
    marker is posted after the operator's triggering message, so it is the newest
    row; move it back into history so `format_history` treats the operator's
    message (not the notice) as the current one."""
    if len(messages) >= 2 and (messages[-1].get("meta") or {}).get("facts_invalidation"):
        messages = list(messages)
        messages[-2], messages[-1] = messages[-1], messages[-2]
    return messages


@dataclass(frozen=True)
class AssistantActionContext:
    """What a read action is told about the request it serves. No payload: the
    loop owns the conversation; an action performs one bounded read."""

    journal_id: UUID | None
    room_uuid: UUID
    agent_uuid: UUID
    step_index: int
    # The producing step's stable uuid, bound onto any write-intent this action
    # records so the intent points at its step by identity (not (run_id,
    # step_index)). None for the dry-run preview call, which records nothing.
    step_uuid: UUID | None = None
    # True only inside _propose_write's preview call: a dry_run-capable action
    # must compute + return a preview without mutating anything.
    dry_run: bool = False
    # The chat message UUID that triggered this action (the operator's last
    # message). Used as evidence source_id so every belief write can be traced
    # back to the specific message that motivated it. None when not available.
    message_uuid: UUID | None = None


@dataclass(frozen=True)
class AssistantObservation:
    """The result of one read action. `text` is fed back to the model (capped by
    the dispatcher); `data` carries structured detail for the trace, not the
    prompt."""

    ok: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)


AssistantAction = Callable[[AssistantActionContext, dict[str, Any]], AssistantObservation]


# query_memory keeps its observation readable and bounded: each fact is capped
# to PER_FACT chars (long ones tagged `truncateN`), and the whole block to TOTAL
# chars (lower-ranked facts past the budget are dropped at a fact boundary, never
# mid-word). A shortened or omitted fact can be read in full via the uuid mode
# (`{"uuid": ...}`) — see ASSISTANT_SYSTEM_PROMPT.
QUERY_MEMORY_PER_FACT_CHARS: int = 1200
QUERY_MEMORY_TOTAL_CHARS: int = 11000


RECALLED_MEMORY_LEGEND: str = "{memory_uuid}, {memory_tags}: {memory_text}"


def _fact_line(uuid: str, tags: str, text: str) -> tuple[str, bool]:
    """Render one recalled-fact line (see RECALLED_MEMORY_LEGEND), shortening text
    over the per-fact cap and marking it with a `truncate{cap}` tag. Returns
    (line, was_truncated)."""
    if len(text) > QUERY_MEMORY_PER_FACT_CHARS:
        return (f"{uuid}, {tags}, truncate{QUERY_MEMORY_PER_FACT_CHARS}: "
                f"{text[:QUERY_MEMORY_PER_FACT_CHARS]}", True)
    return f"{uuid}, {tags}: {text}", False


def _query_memory_full(ctx: AssistantActionContext, uuid_str: str) -> AssistantObservation:
    """Return one memory in full (untruncated) by uuid — the escape hatch for a
    fact query_memory shortened. Seed entries respect shields; claims never
    return secrets. `matched: False` when the uuid resolves to nothing visible."""
    from memory import seed_memory as qkb
    from memory.retrieval import fence_recalled_memory
    from agents.query_handlers import QueryContext

    none = AssistantObservation(ok=True, text="No memory with that uuid.",
                                data={"matched": False})
    qkb._load_kb()
    entry = qkb._entries_by_id.get(uuid_str)
    if entry is not None:
        if qkb._entry_locked(entry, qkb._unlocked_shields()):
            return none
        qctx = QueryContext(room_uuid=ctx.room_uuid, query="", payload={},
                            agent_uuid=ctx.agent_uuid)
        answer = qkb._resolve_match(
            qkb.Match(qa_id=uuid_str, method="exact", score=1.0), qctx)
        # Same tag shape as the query-mode fact lines: source, dynamic?, path.
        tags = f"seed/{entry.get('_source', 'upstream')}"
        if entry.get("kind") == "dynamic":
            tags += ", dynamic"
        if entry.get("path"):
            tags += f", {entry['path']}"
        text, _ = fence_recalled_memory(f"{uuid_str}, {tags}: {answer}")
        return AssistantObservation(
            ok=True, text=text, data={"matched": True, "uuid": uuid_str, "source": "seed"})
    try:
        claim = db.get_memory_claim(UUID(uuid_str))
    except Exception:
        claim = None
    if claim is not None and claim.status == "active" and claim.sensitivity != "secret":
        text, _ = fence_recalled_memory(
            f"{uuid_str}, {claim.kind}, {claim.sensitivity}: {claim.text}")
        return AssistantObservation(
            ok=True, text=text, data={"matched": True, "uuid": uuid_str, "source": "claim"})
    return none


def _action_query_memory(
    ctx: AssistantActionContext, args: dict[str, Any], *, _seed_retriever=None
) -> AssistantObservation:
    """Hybrid retrieval over dynamic claims, curated static seed answers, AND
    live dynamic seed handlers (project status, git status, capabilities, model
    info). Results are tiered: user-overlay seed, then upstream seed, then
    dynamic claims. Secrets are never returned (include_secret stays False).

    Long facts are shortened (tagged `truncateN`) and the block is bounded; pass
    `{"uuid": ...}` instead of `{"query": ...}` to read one fact in full."""
    from memory.retrieval import fence_recalled_memory, format_memory_context, retrieve_memories_hybrid
    from memory import seed_memory as qkb
    from agents.query_handlers import QueryContext

    uuid_arg = str(args.get("uuid", "")).strip()
    if uuid_arg:
        return _query_memory_full(ctx, uuid_arg)
    query = str(args.get("query", "")).strip()
    if not query:
        return AssistantObservation(ok=False, text="query_memory needs a 'query' or a 'uuid'.")
    qctx = QueryContext(
        room_uuid=ctx.room_uuid, query=query, payload={}, agent_uuid=ctx.agent_uuid
    )
    seed_fn = _seed_retriever or qkb.retrieve_seed_answers
    seeds = []
    try:
        # The assistant loop, unlike the chat route's query_filter_router.handle(),
        # never loads the seed KB — so load the registry (_entries_by_id) and ensure
        # the pgvector table is populated before retrieving, or every seed match is
        # dropped. Skip when a retriever is injected (tests stay hermetic).
        if _seed_retriever is None:
            qkb._load_kb()
            qkb._ensure_populated(qkb._vector_store())
        seeds = seed_fn(query, qctx=qctx)
    except Exception:
        logger.warning("assistant: seed memory retrieval failed", exc_info=True)
    # Tier seeds: user-overlay first, then upstream; preserve score order within tier.
    overlay = [s for s in seeds if s.source == "user-overlay"]
    upstream = [s for s in seeds if s.source != "user-overlay"]
    memories = retrieve_memories_hybrid(
        query, agent_uuid=ctx.agent_uuid, room_uuid=ctx.room_uuid,
        include_secret=False, journal_id=ctx.journal_id,
    )
    dynamic_block = format_memory_context(memories, include_uuid=True) if memories else ""

    if not (overlay or upstream or memories):
        return AssistantObservation(ok=True, text="No relevant remembered facts.")

    # (B) Per-fact cap: build one line per fact, shortening long ones. Dynamic
    # seed entries (live handlers) carry a `dynamic` tag; static ones do not.
    # The entry's `path` (e.g. system.uptime_host) rides along as a tag so the
    # model — and the operator reading the trace — can tell apart answers whose
    # text alone is ambiguous (two uptime strings vs a load average, …).
    fact_lines: list[str] = []
    truncated_count = 0
    for s in overlay + upstream:
        tags = f"seed/{s.source}" + (", dynamic" if s.kind == "dynamic" else "")
        if s.path:
            tags += f", {s.path}"
        line, tr = _fact_line(s.uuid, tags, s.answer)
        fact_lines.append(line)
        truncated_count += tr
    if dynamic_block:
        # format_memory_context(include_uuid=True) emits TWO header lines (title +
        # legend); its fact lines are "- {uuid}, {tags}: {text}". Drop the leading
        # "- " (the fence holds bare fact lines) and cap each text.
        for raw in dynamic_block.split("\n")[2:]:
            raw = raw[2:] if raw.startswith("- ") else raw
            head, sep, body = raw.partition(": ")
            if sep and len(body) > QUERY_MEMORY_PER_FACT_CHARS:
                raw = (f"{head}, truncate{QUERY_MEMORY_PER_FACT_CHARS}: "
                       f"{body[:QUERY_MEMORY_PER_FACT_CHARS]}")
                truncated_count += 1
            fact_lines.append(raw)

    # (C) Overall budget: keep top-ranked facts up to TOTAL chars; drop the tail
    # at a fact boundary (never mid-word) and count what was omitted.
    used = len(RECALLED_MEMORY_LEGEND) + 1
    kept: list[str] = []
    omitted = 0
    for i, line in enumerate(fact_lines):
        if kept and used + len(line) + 1 > QUERY_MEMORY_TOTAL_CHARS:
            omitted = len(fact_lines) - i
            break
        used += len(line) + 1
        kept.append(line)

    # The format legend lives OUTSIDE the fence (it is our own instruction, not
    # recalled data); the fence holds only the bare fact lines.
    fenced, _ = fence_recalled_memory("\n".join(kept))
    text = f"Recalled memory format\n{RECALLED_MEMORY_LEGEND}\n\n{fenced}"
    if truncated_count or omitted:
        # A note outside the fence. The retrieval mechanism is also in
        # ASSISTANT_SYSTEM_PROMPT.
        segs = []
        if truncated_count:
            segs.append(f"Long facts shortened to {QUERY_MEMORY_PER_FACT_CHARS} chars "
                        f"(tagged truncate{QUERY_MEMORY_PER_FACT_CHARS}).")
        if omitted:
            segs.append(f"{omitted} lower-ranked fact(s) omitted.")
        segs.append('To read a fact in full, call query_memory with '
                    '{"uuid": "<the fact\'s uuid>"}.')
        text += "\n\n" + " ".join(segs)
    return AssistantObservation(
        ok=True, text=text,
        data={"qa_static": sum(1 for s in seeds if s.kind == "static"),
              "qa_dynamic": sum(1 for s in seeds if s.kind == "dynamic"),
              "memory": len(memories), "truncated": truncated_count, "omitted": omitted},
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
    """Read kanban state without writing events: one task's detail + recent
    events when a task_uuid is given, one board's markdown when a board_uuid is
    given, otherwise a list of all boards."""
    task_raw = args.get("task_uuid")
    if task_raw:
        try:
            task_uuid = UUID(str(task_raw))
        except (ValueError, TypeError):
            return AssistantObservation(ok=False, text=f"invalid task_uuid: {task_raw!r}")
        task = db.kanban_get_task(task_uuid)
        if task is None:
            return AssistantObservation(ok=False, text="no such kanban task")
        board = db.kanban_load_board(UUID(str(task["boardUuid"])))
        cur = next((c["name"] for c in (board["columns"] if board else [])
                    if str(c["uuid"]) == str(task["columnUuid"])), task["columnUuid"])
        all_cols = ", ".join(c["name"] for c in board["columns"]) if board else ""
        lines = [
            f"Task: {task['title']}",
            f"  board: {task['boardUuid']}  column: {cur}",
            f"  description: {task['description'] or '(none)'}",
            f"  board columns (move targets): {all_cols}",
        ]
        events = db.kanban_task_events(task_uuid, limit=10) or []
        if events:
            lines.append("  recent events (newest first):")
            for e in events:
                detail = f": {e['detail']}" if e.get("detail") else ""
                lines.append(f"    - {e['kind']}{detail}")
        return AssistantObservation(
            ok=True, text="\n".join(lines), data={"task_uuid": str(task_uuid)}
        )
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
    """Log-and-undo write: create a *candidate* memory claim for operator
    confirmation. The model composed the claim text, so it is
    assistant_interpreted (candidate-by-default) and must never override a
    tombstone. The candidate is embedded immediately to keep the index warm for
    later activation (candidates are not retrieved into prompts — retrieval is
    active-only); undo rejects the candidate."""
    text = str(args.get("text", "")).strip()
    # source_id: prefer the specific triggering message; fall back to the
    # journal handle UUID so evidence is never left without a provenance anchor.
    source_id = str(ctx.message_uuid or ctx.journal_id or "")
    if source_id:
        evidence = {"provenance": "confirmed_by_user", "source_type": "chat_message",
                    "source_id": source_id, "excerpt": text,
                    "created_by_uuid": ctx.agent_uuid}
    else:
        evidence = {"provenance": "confirmed_by_user", "source_type": "manual",
                    "excerpt": text, "created_by_uuid": None}
    result = db.record_belief(
        actor="assistant_interpreted", scope="room", kind="fact", text=text,
        confidence=1.0, sensitivity="private",
        agent_uuid=ctx.agent_uuid, room_uuid=ctx.room_uuid,
        evidence=evidence,
    )
    if result.outcome == "refused_tombstone":
        return AssistantObservation(
            ok=True,
            text=("That was previously rejected, so I did not re-add it. "
                  "Reply to the operator."),
            data={"noop": True, "reason": result.reason},
        )
    if result.outcome == "corroborated":
        existing = result.claim
        return AssistantObservation(
            ok=True,
            text=(f"Already remembered (no duplicate created). memory_uuid: "
                  f"{existing.uuid}. Reply to the operator."),
            data={"memory_uuid": str(existing.uuid), "status": existing.status,
                  "link": _memory_link(existing.uuid), "noop": True},
        )
    claim = result.claim
    from memory.embeddings import refresh_claim_embedding
    refresh_claim_embedding(claim)  # embed now to keep the index warm for activation
    return AssistantObservation(
        ok=True,
        text=(f"Remembered as a candidate memory (pending operator confirmation). "
              f"memory_uuid: {claim.uuid}. "
              f"Done — reply to the operator. To forget it later, use this exact "
              f"memory_uuid (never invent one)."),
        data={"memory_uuid": str(claim.uuid), "status": claim.status,
              "link": _memory_link(claim.uuid),
              "undo": {"capability": "reject_memory_candidate",
                       "payload": {"memory_uuid": str(claim.uuid)}}},
    )


def _action_reject_memory_candidate(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Internal: reject a remembered memory claim — remember's undo inverse. Not
    prompt-exposed (reached only via undo_write_intent). Rejects a claim that is
    still candidate or active (the states remember leaves it in); refuses if it
    has since been removed/changed (rejected/superseded/expired), so the undo
    can't clobber a later state."""
    raw = args.get("memory_uuid")
    try:
        memory_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid memory_uuid: {raw!r}")
    claim = db.get_memory_claim(memory_uuid)
    if claim is None or claim.status not in ("candidate", "active"):
        return AssistantObservation(
            ok=False, text="memory is no longer candidate/active; not rejecting")
    db.reject_memory(memory_uuid, {"provenance": "confirmed_by_user",
                                   "source_type": "manual"}, tombstone=False)
    return AssistantObservation(
        ok=True, text=f"Rejected candidate memory {memory_uuid}",
        data={"memory_uuid": str(memory_uuid)})


def _action_forget_memory(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: forget (reject) a memory so it stops being recalled.
    Resolve the target by `memory_uuid` (from query_memory) or by `text` — text
    searches active AND candidate claims, so a just-remembered memory can be
    forgotten. Executes immediately and reversibly: rejects the claim, prunes its
    embedding, and carries an inverse op (`reactivate_memory`) so undo restores
    it. The mirror image of `remember`."""
    raw_uuid = args.get("memory_uuid")
    text = str(args.get("text", "")).strip()
    claim = None
    if raw_uuid:
        try:
            memory_uuid = UUID(str(raw_uuid))
        except (ValueError, TypeError):
            return AssistantObservation(ok=False, text=f"invalid memory_uuid: {raw_uuid!r}")
        claim = db.get_memory_claim(memory_uuid)
        if claim is None or claim.status == "rejected":
            return AssistantObservation(
                ok=False, text="no such memory (or it is already forgotten)")
    elif text:
        from memory.ops import find_memory_matches
        matches = [c for c in find_memory_matches(text, status=None)
                   if c.status != "rejected"]
        if not matches:
            return AssistantObservation(
                ok=False, text=f"nothing in memory matches {text!r}")
        if len(matches) > 1:
            uuids = ", ".join(str(c.uuid) for c in matches)
            return AssistantObservation(
                ok=False,
                text=(f"{len(matches)} memories match {text!r} — forget by "
                      f"memory_uuid instead. Candidates: {uuids}"))
        claim = matches[0]
    else:
        return AssistantObservation(
            ok=False, text="forget_memory needs a memory_uuid or text")

    db.reject_memory(claim.uuid, {"provenance": "confirmed_by_user",
                                  "source_type": "manual"})
    from memory.embeddings import refresh_claim_embedding
    refresh_claim_embedding(claim)  # rejected → prune its embedding
    return AssistantObservation(
        ok=True,
        text=(f"Forgot: '{claim.text}'. Done — reply to the operator. (Reversible: "
              f"undo reactivates it.)"),
        data={"memory_uuid": str(claim.uuid),
              "link": _memory_link(claim.uuid),
              "undo": {"capability": "reactivate_memory",
                       "payload": {"memory_uuid": str(claim.uuid)}}},
    )


def _action_reactivate_memory(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Internal: reactivate a forgotten memory — forget_memory's undo inverse. Not
    prompt-exposed (reached only via undo_write_intent). Refuses a claim that is
    not currently `rejected`, so the undo can't clobber a state that changed since
    forget (the version-guard discipline shared by every reversible write)."""
    raw = args.get("memory_uuid")
    try:
        memory_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid memory_uuid: {raw!r}")
    claim = db.get_memory_claim(memory_uuid)
    if claim is None or claim.status != "rejected":
        return AssistantObservation(
            ok=False, text="memory is no longer rejected; not reactivating")
    activated = db.activate_memory_claim(memory_uuid, confirmed_by_uuid=ctx.agent_uuid)
    from memory.embeddings import refresh_claim_embedding
    refresh_claim_embedding(activated)  # active again → re-embed for retrieval
    return AssistantObservation(
        ok=True, text=f"Reactivated memory {memory_uuid}",
        data={"memory_uuid": str(memory_uuid), "status": "active"})


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
    # human_confirmed_write_intent actor: operator approved this exact payload.
    # If the candidate was flagged as conflicting with a rival, route through
    # resolve_conflict (supersede) so the rival's tombstone is written correctly.
    # Otherwise, plain activate is sufficient.
    if claim.conflicts_with_uuid is not None:
        activated = db.resolve_conflict(
            memory_uuid, "supersede", created_by_uuid=ctx.agent_uuid)
    else:
        activated = db.activate_memory_claim(
            memory_uuid, confirmed_by_uuid=ctx.agent_uuid)
    # Newly active → embed it so hybrid retrieval can use it immediately
    # (best-effort; falls back to lexical-only if no embedder is available).
    from memory.embeddings import refresh_claim_embedding
    refresh_claim_embedding(activated)
    return AssistantObservation(
        ok=True, text=f"Activated memory {memory_uuid}",
        data={"memory_uuid": str(memory_uuid), "status": activated.status},
    )


def _kanban_link(target_uuid: UUID | str) -> str:
    """A relative link to the kanban page (origin-independent). `?id=` accepts a
    board OR a task uuid — a task uuid selects its board and opens that task's
    overlay — so writes link to the specific task they touched. Surfaced in the
    assistant's reply so the operator can jump straight to what changed."""
    return f"/kanban?id={target_uuid}"


def _memory_link(memory_uuid: UUID | str) -> str:
    """A relative link to the /memory review page that opens a specific claim's
    detail (its `?id=` deep-link). Surfaced in the assistant's reply after a
    memory write so the operator can jump straight to the claim it touched —
    e.g. inspect a just-forgotten memory and reactivate it if needed."""
    return f"/memory?id={memory_uuid}"


def _resolve_board_column(
    board_uuid: UUID | str, raw: Any
) -> tuple[UUID | None, list[dict[str, Any]]]:
    """Resolve a column reference to a column uuid on the given board. Accepts the
    column's uuid OR its name (case-insensitive) — operators name a column ("In
    progress"), and the model can't be relied on to know its uuid. Returns
    (uuid_or_None, columns); columns lets callers list options in an error."""
    board = db.kanban_load_board(UUID(str(board_uuid)))
    cols: list[dict[str, Any]] = board["columns"] if board else []
    s = str(raw).strip()
    for c in cols:  # exact uuid first
        if str(c["uuid"]) == s:
            return UUID(str(c["uuid"])), cols
    for c in cols:  # then case-insensitive name
        if str(c["name"]).strip().lower() == s.lower():
            return UUID(str(c["uuid"])), cols
    return None, cols


def _action_move_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: move a kanban task to another column of its board.
    `column_uuid` may be the column's name or uuid. Reversible — `data["undo"]`
    is the inverse move. Code-owned authority: this does not route through the
    worker observe/work/shape dispatcher; reversibility + trace is the safety."""
    raw_task, raw_col = args.get("task_uuid"), args.get("column_uuid")
    try:
        task_uuid = UUID(str(raw_task))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw_task!r}")
    before = db.kanban_get_task(task_uuid)
    if before is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    from_column_uuid = before["columnUuid"]
    column_uuid, cols = _resolve_board_column(before["boardUuid"], raw_col)
    if column_uuid is None:
        available = ", ".join(f"'{c['name']}'" for c in cols) or "(none)"
        return AssistantObservation(
            ok=False,
            text=f"no column matching {raw_col!r} on this board. Columns: {available}",
        )
    col_name = next((str(c["name"]) for c in cols
                     if str(c["uuid"]) == str(column_uuid)), str(column_uuid))
    # Position-aware undo: an undo carries `expect_column` (where the original
    # write left the task). If the task has since moved, refuse — don't yank it
    # from where it now sits.
    expect = args.get("expect_column")
    if expect is not None and str(from_column_uuid) != str(expect):
        return AssistantObservation(
            ok=False, text="task moved since the write; not undoing")
    # No-op guard: targeting the column the task is already in changes nothing —
    # flag it rather than report a phantom "Moved", so the model can't claim a
    # move that never happened.
    if str(column_uuid) == str(from_column_uuid):
        others = ", ".join(f"'{c['name']}'" for c in cols
                           if str(c["uuid"]) != str(from_column_uuid)) or "(none)"
        return AssistantObservation(
            ok=False,
            text=(f"the destination column must be different from the source: "
                  f"'{before['title']}' is already in '{col_name}', so this move "
                  f"changes nothing. If you meant a different column, pick one of: "
                  f"{others}."),
        )
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
        text=f"Moved '{before['title']}' to '{col_name}' (undoable).",
        data={
            "task_uuid": str(task_uuid),
            "from_column_uuid": str(from_column_uuid),
            "to_column_uuid": str(column_uuid),
            "link": _kanban_link(str(task_uuid)),
            "undo": {
                "capability": "kanban_move_task",
                "payload": {"task_uuid": str(task_uuid),
                            "column_uuid": str(from_column_uuid),
                            "expect_column": str(column_uuid)},
            },
        },
    )


def _action_complete_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: mark a task done (move it to the board's Done/last
    column + a 'done' event). Reversible — the undo is a kanban_move_task back to the
    task's prior column. Operator-proxy intent → Done, not worker review-routing."""
    raw = args.get("task_uuid")
    try:
        task_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw!r}")
    before = db.kanban_get_task(task_uuid)
    if before is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    from_column_uuid = before["columnUuid"]
    after = db.kanban_complete_task(
        task_uuid, True, actor=str(ctx.agent_uuid),
        detail="assistant marked done (undoable)", review=False,
    )
    if after is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    return AssistantObservation(
        ok=True,
        text=f"Marked '{before['title']}' done (undoable).",
        data={
            "task_uuid": str(task_uuid),
            "from_column_uuid": str(from_column_uuid),
            "to_column_uuid": after["columnUuid"],
            "link": _kanban_link(str(task_uuid)),
            "undo": {
                "capability": "kanban_move_task",
                "payload": {"task_uuid": str(task_uuid),
                            "column_uuid": str(from_column_uuid),
                            "expect_column": str(after["columnUuid"])},
            },
        },
    )


def _action_comment_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: append a comment event to a task. The event log is
    append-only, so the undo posts a retraction comment rather than erasing."""
    raw = args.get("task_uuid")
    text = str(args.get("text", "")).strip()
    try:
        task_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw!r}")
    is_retraction = text.startswith("↩ retracted: ")
    event = db.kanban_append_event(
        task_uuid, "comment", actor=str(ctx.agent_uuid), detail=text,
    )
    if event is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    data: dict[str, Any] = {"task_uuid": str(task_uuid)}
    # A retraction (posted by undo) is itself a comment but needs no further undo.
    if not is_retraction:
        data["undo"] = {
            "capability": "kanban_comment",
            "payload": {"task_uuid": str(task_uuid),
                        "text": f"↩ retracted: {text}"},
        }
    return AssistantObservation(
        ok=True, text=f"Commented on task {task_uuid} (undoable).", data=data,
    )


def _action_create_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: create a task. Undo deletes it. `column_uuid` is
    optional — operators name a board, not a column ("add a task to board ax"),
    so an omitted/unresolvable column lands the task in the board's first column
    rather than forcing the model to guess a column uuid."""
    raw_board, raw_col = args.get("board_uuid"), args.get("column_uuid")
    title = str(args.get("title", "")).strip()
    description = str(args.get("description", "")).strip()
    try:
        board_uuid = UUID(str(raw_board))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid board_uuid: {raw_board!r}")
    # A valid column uuid is honored; anything unparseable (omitted, null, or a
    # placeholder like '<COLUMN_UUID>') falls back to the board's first column.
    column_uuid: UUID | None = None
    try:
        column_uuid = UUID(str(raw_col))
    except (ValueError, TypeError):
        column_uuid = None
    if column_uuid is None:
        board = db.kanban_load_board(board_uuid)
        if board is None or not board.get("columns"):
            return AssistantObservation(ok=False, text="no such board (or it has no columns)")
        column_uuid = UUID(str(board["columns"][0]["uuid"]))
    created = db.kanban_create_task(
        board_uuid, column_uuid, title=title, description=description,
        actor=str(ctx.agent_uuid),
    )
    if created is None:
        return AssistantObservation(ok=False, text="no such board or column")
    return AssistantObservation(
        ok=True,
        text=f"Created task '{title}' (undoable — undo deletes it).",
        data={
            "task_uuid": created["uuid"],
            "board_uuid": str(board_uuid),
            "column_uuid": str(column_uuid),
            "link": _kanban_link(created["uuid"]),
            "undo": {"capability": "kanban_delete_task",
                     "payload": {"task_uuid": created["uuid"]}},
        },
    )


def _action_delete_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Internal: hard-delete a task. Not prompt-exposed — reached only as the
    undo-inverse of kanban_create_task (via undo_write_intent)."""
    raw = args.get("task_uuid")
    try:
        task_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw!r}")
    if not db.kanban_delete_task(task_uuid):
        return AssistantObservation(ok=False, text="no such kanban task")
    return AssistantObservation(
        ok=True, text=f"Deleted task {task_uuid}", data={"task_uuid": str(task_uuid)},
    )


def _action_create_kanban_board(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: create a new board (with the default columns). The
    caller supplies only a name — the board's uuid is assigned by the store, so
    a caller can never pick (and collide) a board uuid. Undo deletes the board."""
    title = str(args.get("title", "")).strip()
    if not title:
        return AssistantObservation(ok=False, text="kanban_create_board needs a non-empty title")
    description = str(args.get("description", "")).strip()
    try:
        created = db.kanban_create_board(title, description=description)
    except db.KanbanError as exc:
        return AssistantObservation(ok=False, text=str(exc))
    board_uuid = str(created["uuid"])
    return AssistantObservation(
        ok=True,
        text=f"Created board '{title}' (undoable — undo deletes it).",
        data={
            "board_uuid": board_uuid,
            "link": _kanban_link(board_uuid),
            "undo": {"capability": "kanban_delete_board",
                     "payload": {"board_uuid": board_uuid}},
        },
    )


def _action_delete_kanban_board(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Internal: hard-delete a board (with its columns/tasks/events). Not
    prompt-exposed — reached only as the undo-inverse of kanban_create_board."""
    raw = args.get("board_uuid")
    try:
        board_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid board_uuid: {raw!r}")
    if not db.kanban_delete_board(board_uuid):
        return AssistantObservation(ok=False, text="no such kanban board")
    return AssistantObservation(
        ok=True, text=f"Deleted board {board_uuid}", data={"board_uuid": str(board_uuid)},
    )


def _action_set_reminder(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Confirm-tier write: schedule a one-shot reminder that posts a chat message
    at `when` (ISO-8601). In dry-run (propose) it resolves the time and previews
    without creating anything; on real execution it creates the one-shot cron job."""
    text = str(args.get("text", "")).strip()
    raw_when = str(args.get("when", "")).strip()
    try:
        fire_at = datetime.fromisoformat(raw_when)
    except ValueError:
        return AssistantObservation(
            ok=False,
            text=f"invalid 'when' (use ISO-8601, e.g. 2026-06-27T09:00): {raw_when!r}",
        )
    if fire_at.tzinfo is None:
        # A naive 'when' is the operator's local wall-clock time, not UTC.
        # astimezone() on a naive datetime presumes local time and is DST-correct
        # for the given date; the model is also told the current local time so a
        # relative offset ("in 10 minutes") lands in the same local basis.
        fire_at = fire_at.astimezone()
    when_str = fire_at.isoformat()
    if ctx.dry_run:
        return AssistantObservation(
            ok=True, text=f"Would remind you at {when_str}: {text}",
            data={"fire_at": when_str},
        )
    origin_run_uuid = None
    if ctx.step_uuid is not None:
        step = db.db.session.query(db.AssistantStep).filter_by(uuid=ctx.step_uuid).first()
        origin_run_uuid = step.run_uuid if step is not None else None
    job = db.cron_create_one_shot_message(
        message=f"⏰ Reminder: {text}", fire_at=fire_at, target=str(ctx.room_uuid),
        name=f"Reminder: {text[:40]}",
        origin_run_uuid=origin_run_uuid, origin_step_uuid=ctx.step_uuid,
    )
    return AssistantObservation(
        ok=True, text=f"Reminder set for {when_str}: {text}",
        data={"cron_job_uuid": str(job.uuid), "fire_at": when_str,
              # Link to the created cron job; the chat card surfaces it as
              # "View reminder ↗" both on confirm and on reload (via the stored
              # intent result), and the run loop's result_links harvest reuses it.
              "link": f"/cron?id={job.uuid}"},
    )


MAX_EDIT_BYTES: int = 100_000


def _action_edit_file(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Confirm-tier write: replace a workspace file's content. Dry-run (propose)
    shows the unified diff and writes nothing; real execution applies it. Confined
    to the workspace by resolve_workspace_path (rejects traversal/sensitive/escape)."""
    import difflib

    from tools.workspace_policy import (
        SHELL_CWD,
        DisallowedCommand,
        resolve_workspace_path,
    )

    path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if len(content.encode("utf-8", "ignore")) > MAX_EDIT_BYTES:
        return AssistantObservation(ok=False, text="new content too large (>100KB)")
    try:
        resolved = resolve_workspace_path(path, SHELL_CWD)
    except DisallowedCommand as e:
        return AssistantObservation(ok=False, text=f"blocked: {e}")
    if resolved.is_dir():
        return AssistantObservation(ok=False, text=f"path is a directory: {path}")
    import hashlib

    old = ""
    if resolved.exists():
        if resolved.stat().st_size > MAX_EDIT_BYTES:
            return AssistantObservation(ok=False, text="existing file too large to edit (>100KB)")
        old = resolved.read_text(encoding="utf-8", errors="replace")
    if old == content:
        return AssistantObservation(ok=False, text="no change: new content matches the file")
    base_sha = hashlib.sha256(old.encode("utf-8")).hexdigest()
    # The previewed diff was computed against `old`. On real execution, refuse if
    # the file changed since the preview — otherwise we'd silently clobber an
    # unpreviewed version. `base_sha` is folded into the stored payload by
    # _propose_write from this action's dry-run `confirm_payload`.
    expected = args.get("base_sha")
    if not ctx.dry_run and expected is not None and expected != base_sha:
        return AssistantObservation(
            ok=False,
            text=f"{path} changed since the preview; not applying (re-propose the edit)",
        )
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), content.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    ))
    verb = "create" if not resolved.exists() else "edit"
    if ctx.dry_run:
        return AssistantObservation(
            ok=True, text=f"Would {verb} {path}:\n{diff}",
            data={"path": path, "confirm_payload": {"base_sha": base_sha}},
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return AssistantObservation(
        ok=True, text=f"Applied edit to {path} ({len(old)} → {len(content)} chars).",
        data={"path": path, "old_chars": len(old), "new_chars": len(content)},
    )


def _action_propose_skill(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: write an inert candidate skill to the overlay. It is
    never injected until activated; undo deletes it."""
    skill_id = str(args.get("skill_id", "")).strip().lower()
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", "")).strip()
    tags = [t for t in str(args.get("tags", "")).split(",") if t.strip()]
    path = skills.write_candidate_skill(
        skill_id=skill_id, title=title, body=body, created_by="assistant",
        retrieval_tags=tags, source_journal_id=ctx.journal_id,
        source_step_id=ctx.step_index,
    )
    if path is None:
        return AssistantObservation(
            ok=False,
            text=("couldn't propose skill (no skills overlay configured, invalid id, "
                  "or that id already exists)"),
        )
    return AssistantObservation(
        ok=True,
        text=f"Proposed candidate skill '{skill_id}' (inert until you activate it; reject to undo).",
        data={"skill_id": skill_id,
              "undo": {"capability": "skill_delete", "payload": {"skill_id": skill_id}}},
    )


def _action_activate_skill(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Confirm-tier write: activate a candidate skill so it can steer future turns."""
    skill_id = str(args.get("skill_id", "")).strip().lower()
    if not skills.set_skill_status(skill_id, "active", if_current="candidate"):
        return AssistantObservation(ok=False, text=f"no such candidate skill: {skill_id}")
    return AssistantObservation(
        ok=True, text=f"Activated skill '{skill_id}'.", data={"skill_id": skill_id})


def _action_delete_skill(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Internal: delete a skill file — propose_skill's undo inverse. Not
    prompt-exposed (reached only via undo_write_intent)."""
    skill_id = str(args.get("skill_id", "")).strip().lower()
    # Only delete a still-pending candidate: undoing the proposal must not remove
    # a skill the operator has since activated.
    if not skills.delete_skill_file(skill_id, if_status="candidate"):
        return AssistantObservation(
            ok=False, text=f"skill '{skill_id}' is not a pending candidate; not deleting")
    return AssistantObservation(
        ok=True, text=f"Deleted skill '{skill_id}'", data={"skill_id": skill_id})


@dataclass(frozen=True)
class Capability:
    """Code-owned metadata + dispatch for one assistant action — the capability
    registry. The model can only request a capability that
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
    # `description` is the LLM-facing text (verbose: usage caveats, undo notes,
    # arg schema). `summary` is the short human-readable line for the operator UI
    # (e.g. the /assistant timeline). Falls back to `description` when empty.
    description: str
    summary: str = ""
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
        summary="send the final answer to the user",
        required_args=("message",), terminal=True,
    ),
    AssistantActionName.ASK_CLARIFYING_QUESTION: Capability(
        name=AssistantActionName.ASK_CLARIFYING_QUESTION, family="conversation", read=False,
        description=('ask the user for missing information; ends the turn. '
                     'args: {"question": "..."}'),
        summary="ask the user for missing information",
        required_args=("question",), terminal=True,
    ),
    AssistantActionName.QUERY_MEMORY: Capability(
        name=AssistantActionName.QUERY_MEMORY, family="memory",
        description=('recall stored facts AND answer general questions (project '
                     'status, git status, capabilities, model info) from the '
                     'knowledge base. NOT for kanban or files — use kanban_read / '
                     'workspace_read_command. args: {"query": "..."} to search, '
                     'or {"uuid": "..."} to read one shortened/omitted fact in full.'),
        summary="recall facts and answer general questions",
        required_args=(), optional_args=frozenset({"query", "uuid"}),
        action=_action_query_memory, output_cap_chars=12000,
    ),
    AssistantActionName.WORKSPACE_READ_COMMAND: Capability(
        name=AssistantActionName.WORKSPACE_READ_COMMAND, family="workspace",
        description='run an allowlisted read-only file-inspection command. args: {"command": "..."}',
        summary="run a read-only file-inspection command",
        required_args=("command",), action=_action_workspace_read_command,
    ),
    AssistantActionName.KANBAN_READ: Capability(
        name=AssistantActionName.KANBAN_READ, family="kanban",
        description=('read kanban state — use this to find a board or list a '
                     'board\'s columns before creating/moving a task. args: optional '
                     '{"task_uuid"} for one task\'s detail + recent events, '
                     '{"board_uuid"} for a board; empty lists all boards'),
        summary="read kanban boards and tasks",
        optional_args=frozenset({"board_uuid", "task_uuid"}), action=_action_kanban_read,
    ),
    AssistantActionName.REMEMBER: Capability(
        name=AssistantActionName.REMEMBER, family="memory",
        description=('remember a fact as an inert candidate (reject to undo). '
                     'args: {"text": "..."}'),
        summary="remember a fact as a candidate",
        required_args=("text",), action=_action_remember,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.ACTIVATE_MEMORY: Capability(
        name=AssistantActionName.ACTIVATE_MEMORY, family="memory",
        description=('propose activating a candidate memory so it steers future '
                     'answers; needs your confirmation. args: {"memory_uuid": "..."}'),
        summary="activate a candidate memory",
        required_args=("memory_uuid",), action=_action_activate_memory,
        read=False, write=True, tier="confirm",
    ),
    AssistantActionName.FORGET_MEMORY: Capability(
        name=AssistantActionName.FORGET_MEMORY, family="memory",
        description=('forget a memory so it stops being recalled; reversible '
                     '(undoable). args: {"memory_uuid": "..."} (from query_memory) '
                     'or {"text": "..."} — text matches active AND candidate '
                     'memories'),
        summary="forget a memory",
        optional_args=frozenset({"memory_uuid", "text"}),
        action=_action_forget_memory,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_MOVE_TASK: Capability(
        name=AssistantActionName.KANBAN_MOVE_TASK, family="kanban",
        description=('move a kanban task to another column; reversible (undoable). '
                     'args: {"task_uuid": "...", "column_uuid": "..."} where '
                     'column_uuid is the target column\'s NAME (e.g. "In progress") '
                     'or its uuid — prefer the name the operator used'),
        summary="move a kanban task to another column",
        required_args=("task_uuid", "column_uuid"),
        action=_action_move_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_COMPLETE: Capability(
        name=AssistantActionName.KANBAN_COMPLETE, family="kanban",
        description=('mark a kanban task done (moves it to the Done column); '
                     'reversible. args: {"task_uuid": "..."}'),
        summary="mark a kanban task done",
        required_args=("task_uuid",), action=_action_complete_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_COMMENT: Capability(
        name=AssistantActionName.KANBAN_COMMENT, family="kanban",
        description=('add a comment to a kanban task; reversible (posts a '
                     'retraction). args: {"task_uuid": "...", "text": "..."}'),
        summary="add a comment to a kanban task",
        required_args=("task_uuid", "text"), action=_action_comment_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_CREATE_TASK: Capability(
        name=AssistantActionName.KANBAN_CREATE_TASK, family="kanban",
        description=('create a kanban TASK on an EXISTING board (to make a new '
                     'board, use kanban_create_board). reversible (undo deletes '
                     'it). args: {"board_uuid": "..." (an existing board, from '
                     'kanban_read), "title": "...", optional "description", '
                     'optional "column_uuid" — omit it to use the board\'s first '
                     'column (the usual case)}'),
        summary="create a kanban task on an existing board",
        required_args=("board_uuid", "title"),
        optional_args=frozenset({"description", "column_uuid"}),
        action=_action_create_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_DELETE_TASK: Capability(
        name=AssistantActionName.KANBAN_DELETE_TASK, family="kanban",
        description="(internal) delete a kanban task — the undo-inverse of kanban_create_task.",
        summary="delete a kanban task",
        required_args=("task_uuid",), action=_action_delete_kanban_task,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
    ),
    AssistantActionName.KANBAN_CREATE_BOARD: Capability(
        name=AssistantActionName.KANBAN_CREATE_BOARD, family="kanban",
        description=('create a NEW kanban board with the given name (the default '
                     'columns are added; the board uuid is assigned automatically '
                     '— never pass one). reversible (undo deletes it). args: '
                     '{"title": "...", optional "description"}'),
        summary="create a new kanban board",
        required_args=("title",),
        optional_args=frozenset({"description"}),
        action=_action_create_kanban_board,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_DELETE_BOARD: Capability(
        name=AssistantActionName.KANBAN_DELETE_BOARD, family="kanban",
        description="(internal) delete a kanban board — the undo-inverse of kanban_create_board.",
        summary="delete a kanban board",
        required_args=("board_uuid",), action=_action_delete_kanban_board,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
    ),
    AssistantActionName.SET_REMINDER: Capability(
        name=AssistantActionName.SET_REMINDER, family="cron",
        description=('schedule a reminder that messages you at a time; needs your '
                     'confirmation. args: {"text": "...", "when": "ISO-8601 datetime"}. '
                     "Express 'when' in the operator's local time (use the current "
                     "local time given above to resolve relative offsets like 'in 10 "
                     'minutes\'); a bare datetime with no offset is read as local time, '
                     'not UTC.'),
        summary="schedule a reminder",
        required_args=("text", "when"), action=_action_set_reminder,
        read=False, write=True, tier="confirm", dry_run=True,
    ),
    AssistantActionName.EDIT_FILE: Capability(
        name=AssistantActionName.EDIT_FILE, family="workspace",
        description=('propose an edit to a workspace file (you supply the full new '
                     'content); shows a diff and needs your confirmation. args: '
                     '{"path": "...", "content": "..."}'),
        summary="propose an edit to a workspace file",
        required_args=("path", "content"), action=_action_edit_file,
        read=False, write=True, tier="confirm", dry_run=True,
        output_cap_chars=12000,
    ),
    AssistantActionName.PROPOSE_SKILL: Capability(
        name=AssistantActionName.PROPOSE_SKILL, family="skill",
        description=('propose a reusable "how to" skill as an inert candidate '
                     '(never used until you activate it; reject to undo). args: '
                     '{"skill_id": "kebab-slug", "title": "...", "body": "markdown", '
                     'optional "tags": "a,b"}'),
        summary="propose a reusable skill as a candidate",
        required_args=("skill_id", "title", "body"),
        optional_args=frozenset({"tags"}),
        action=_action_propose_skill, read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.ACTIVATE_SKILL: Capability(
        name=AssistantActionName.ACTIVATE_SKILL, family="skill",
        description=('activate a candidate skill so it steers future answers; needs '
                     'your confirmation. args: {"skill_id": "..."}'),
        summary="activate a candidate skill",
        required_args=("skill_id",), action=_action_activate_skill,
        read=False, write=True, tier="confirm",
    ),
    AssistantActionName.SKILL_DELETE: Capability(
        name=AssistantActionName.SKILL_DELETE, family="skill",
        description="(internal) delete a skill file — propose_skill's undo inverse.",
        summary="delete a skill",
        required_args=("skill_id",), action=_action_delete_skill,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
    ),
    AssistantActionName.REJECT_MEMORY_CANDIDATE: Capability(
        name=AssistantActionName.REJECT_MEMORY_CANDIDATE, family="memory",
        description="(internal) reject a candidate memory — remember's undo inverse.",
        summary="reject a candidate memory",
        required_args=("memory_uuid",), action=_action_reject_memory_candidate,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
    ),
    AssistantActionName.REACTIVATE_MEMORY: Capability(
        name=AssistantActionName.REACTIVATE_MEMORY, family="memory",
        description="(internal) reactivate a forgotten memory — forget's undo inverse.",
        summary="reactivate a forgotten memory",
        required_args=("memory_uuid",), action=_action_reactivate_memory,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
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

    Terminal, read-only, and write actions are all dispatched through
    `_dispatch_action` with a trace-before-action `running` row and an output
    cap. The per-step trace is durable via the `_record_step` seam (assistant_run
    / assistant_step rows).
    """

    # Loop + prompt budget caps: simple counts/char caps, not a tokenizer-aware
    # budget.
    STEP_LIMIT: int = 6
    MAX_RECENT_MESSAGES: int = 30
    MAX_SCRATCHPAD_CHARS: int = 5000
    # How much of an observation the trace stores per step. Set to the largest
    # per-capability output_cap_chars (12000) so the trace captures the whole
    # observation an action returned — the operator inspecting a run wants all of
    # it, not a 1200-char slice. (The raw action output is already capped by
    # output_cap_chars before this.)
    MAX_OBSERVATION_PREVIEW_CHARS: int = 12000

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

    @staticmethod
    def _message_uuid(payload: dict[str, Any]) -> UUID | None:
        """Extract the triggering chat message UUID from the payload (present
        when enqueued by a human chat post; absent for test/manual handles)."""
        raw = payload.get("message_uuid")
        if not raw:
            return None
        return raw if isinstance(raw, UUID) else UUID(str(raw))

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        room_uuid = self._room_uuid(payload)
        message_uuid = self._message_uuid(payload)
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
        # If facts were invalidated (a shield toggle or Q&A repopulate) since the
        # last marker here, drop a one-time re-check notice. The notice is a
        # kind="message" — a terminal kind whose side effect reaps the sender's
        # progress rows, INCLUDING the enqueue-time "working on it" bubble
        # (posted in webapp._maybe_trigger_chat_agents so it appears before
        # this process finished spawning). So when the marker posts, re-post
        # the bubble right after it: the model calls ahead can take tens of
        # seconds and the operator must not sit without a signal.
        if self._maybe_post_facts_marker(room_uuid):
            db.post_progress(room_uuid, self.agent_uuid, ASSISTANT_WORKING_NOTICE)
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
            # A facts marker just posted is the newest row; keep the operator's
            # message as the Current message by demoting it into history.
            messages = _demote_trailing_facts_marker(messages)
            transcript = format_history(messages, context_limit=self.MAX_RECENT_MESSAGES)
            # Retrieve active procedural skills for this turn (candidates are
            # inert and never injected). Best-effort: a retrieval failure must
            # not break the turn.
            self._skill_block = self._build_skill_block(messages, journal_id, room_uuid)
            # Operator self-model digest: query-independent, always present (best
            # -effort — a retrieval failure must not break the turn).
            self._profile_block = self._build_profile_block(journal_id, room_uuid)
            scratchpad: list[str] = []
            # Signatures of writes already completed this run. A model that doesn't
            # notice a write succeeded can re-issue the identical write; replaying
            # it would duplicate state, so an identical repeat is blocked and the
            # model is steered to `reply`.
            done_writes: set[str] = set()
            # Relative links a write surfaced (e.g. /kanban?id=...), appended to the
            # reply so the operator can jump to what just changed. Order-preserving.
            result_links: list[str] = []
            # The card payload for a confirm-tier write proposed this turn, attached
            # as `meta` on the terminal reply so chat can render confirm/reject.
            pending_proposal: dict[str, Any] | None = None

            for step_index in range(self.step_limit):
                current_step = step_index
                # Step boundary: honour any operator stop/redirect before the
                # next model call, so a stop leaves a clean trace (not a killed
                # process) and a redirect steers the next step.
                stopped = self._apply_pending_controls(run, step_index, scratchpad)
                if stopped is not None:
                    return stopped
                self._activity = f"deciding step {step_index}"
                requested_at = datetime.now(UTC)
                decision = self._decide_next_step(
                    transcript=transcript, scratchpad=scratchpad, step_index=step_index
                )
                # Token counts + the model used for THIS step's decide call (None
                # if the seam set nothing). Carried explicitly so a later control
                # step can't inherit them.
                usage = self._last_usage
                model_uuid = self._last_model_uuid
                system_prompt = self._last_system_prompt
                user_prompt = self._last_user_prompt
                # The model's native reasoning ("thinking") channel for this
                # decide call; None for a non-reasoning model. Stored on the
                # step row and surfaced in the room as a collapsible thought
                # bubble (kind="thinking" — the same kind the direct-chat agent
                # streams; excluded from transcripts, so the model never sees
                # its own reasoning fed back).
                reasoning = self._last_reasoning
                if reasoning:
                    db.post_chat_message(
                        room_uuid, self.agent_uuid, reasoning, kind="thinking"
                    )

                error = self._validate_decision(decision)
                if error is not None:
                    self._record_step(
                        step_index=step_index, phase="failed", decision=decision,
                        error=error, usage=usage, model_uuid=model_uuid,
                        system_prompt=system_prompt, user_prompt=user_prompt,
                        reasoning=reasoning, requested_at=requested_at,
                    )
                    scratchpad.append(
                        f"step {step_index}: action '{decision.action.value}' "
                        f"rejected: {error}"
                    )
                    continue

                if self._caps[decision.action].terminal:
                    self._record_step(
                        step_index=step_index, phase="final", decision=decision,
                        usage=usage, model_uuid=model_uuid,
                        system_prompt=system_prompt, user_prompt=user_prompt,
                        reasoning=reasoning, requested_at=requested_at,
                    )
                    text = self._terminal_text(decision)
                    if decision.action is AssistantActionName.REPLY:
                        text = self._append_result_links(text, result_links)
                    db.post_chat_message(room_uuid, self.agent_uuid, text,
                                         kind="message", meta=pending_proposal)
                    db.finish_run(run, "finished", final_summary=text[:200])
                    logger.info(
                        "assistant finished run %s in room %s at step %d",
                        run.uuid, room_uuid, step_index,
                    )
                    self._request_summary(run)
                    return self._run_result("finished", text[:200])

                # Non-terminal action: commit the `running` row before acting (so
                # a kill mid-action leaves it), then act and record the
                # observation. A confirm-tier write is *proposed* here, never
                # executed inline; everything else (reads, log-and-undo writes)
                # executes immediately.
                self._activity = f"running {decision.action.value}"
                step_row = self._open_step(
                    step_index=step_index, decision=decision, usage=usage,
                    model_uuid=model_uuid,
                    system_prompt=system_prompt, user_prompt=user_prompt,
                    reasoning=reasoning, requested_at=requested_at)
                action_ctx = AssistantActionContext(
                    journal_id=journal_id,
                    room_uuid=room_uuid,
                    agent_uuid=self.agent_uuid,
                    step_index=step_index,
                    step_uuid=step_row.uuid if step_row is not None else None,
                    message_uuid=message_uuid,
                )
                cap = self._caps[decision.action]
                write_sig = (
                    f"{decision.action.value}:"
                    f"{json.dumps(decision.args, sort_keys=True, default=str)}"
                    if cap.write else None
                )
                if write_sig is not None and write_sig in done_writes:
                    # Identical to a write already completed this run — don't replay
                    # it (that would duplicate state); tell the model it's done.
                    observation = AssistantObservation(
                        ok=True,
                        text=("You already completed this exact action earlier in "
                              "this run. Do not repeat it — use `reply` to confirm "
                              "to the operator."),
                    )
                elif cap.write and cap.tier == "confirm":
                    observation = self._propose_write(action_ctx, decision, cap)
                else:
                    observation = self._dispatch_action(action_ctx, decision)
                    # A `noop` write changed no state (e.g. remember found an
                    # existing duplicate) — there is nothing to undo, so don't
                    # record a ledger row; the link still surfaces in the reply.
                    if (cap.write and cap.tier == "log_and_undo" and observation.ok
                            and not observation.data.get("noop")):
                        self._record_log_and_undo(action_ctx, cap, decision, observation)
                if write_sig is not None and observation.ok:
                    done_writes.add(write_sig)
                    link = observation.data.get("link")
                    if link and link not in result_links:
                        result_links.append(link)
                    proposal = observation.data.get("proposal")
                    if proposal:
                        pending_proposal = proposal
                preview = observation.text[: self.MAX_OBSERVATION_PREVIEW_CHARS]
                self._settle_step(
                    step_row,
                    phase="observed" if observation.ok else "failed",
                    observation_preview=preview,
                    observation={"ok": observation.ok, "text": observation.text,
                                 "data": observation.data},
                    error=None if observation.ok else preview,
                )
                scratchpad.append(
                    self._compact_step(step_index, decision, observation.ok, preview)
                )
                if write_sig is not None and observation.ok:
                    # A write landed: steer the model to confirm, not to keep going
                    # (and certainly not to re-write). This is the common tail.
                    scratchpad.append(
                        "the write succeeded — the request is fulfilled; use `reply` "
                        "now to confirm and do not perform another write for it"
                    )

            # Ran out of steps without a terminal action. Link the run page so the
            # operator can inspect what it did before giving up (relative path as
            # clickable markdown, matching _append_result_links).
            run_link = f"/assistant?id={run.uuid}"
            msg = (
                "I couldn't complete this within the step limit. "
                "Please rephrase or narrow the request.\n\n"
                f"Inspect the run: [{run_link}]({run_link})"
            )
            db.post_chat_message(room_uuid, self.agent_uuid, msg, kind="message")
            db.finish_run(run, "stopped", final_summary="step limit reached")
            logger.warning(
                "assistant run %s hit step limit (%d) in room %s",
                run.uuid, self.step_limit, room_uuid,
            )
            self._request_summary(run)
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
            "assistant_run_uuid": str(self._run.uuid),
            "final_summary": final_summary,
            "step_count": len(self._steps),
        }

    def _request_summary(self, run: Any) -> None:
        """Enqueue the assistant_run_summarizer for a now-terminal run — off the critical
        path (a non-blocking inbox insert the supervisor drains in its own
        process). Best-effort: a failure to enqueue must never break the turn or
        mask the operator's reply."""
        try:
            db.enqueue(ASSISTANT_RUN_SUMMARIZER_UUID, {"run_uuid": str(run.uuid)})
        except Exception:
            logger.exception("assistant: failed to enqueue summary for run %s", run.uuid)

    def _fail_run(self, run: Any, exc: Exception, step_index: int) -> None:
        err = f"{type(exc).__name__}: {exc}"
        try:
            # A crash mid-decide (e.g. a reasoning model timing out) leaves the
            # partial reasoning on the seam; record it so the trace shows what
            # the model was thinking when the step died.
            self._record_step(step_index=step_index, phase="failed", error=err,
                              reasoning=self._last_reasoning)
            db.finish_run(run, "failed", final_summary=err)
        except Exception:
            logger.exception("assistant: failed to mark run %s failed", run.uuid)
        self._request_summary(run)

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
        system_prompt = self._system_prompt()
        # Snapshot the exact request so the step row can persist the "model
        # request" half of the interaction (the scripted-seam test path skips
        # this method, so these stay None there — read defensively downstream).
        self._last_system_prompt = system_prompt
        self._last_user_prompt = user_prompt
        result = self._structured_completion(
            system_prompt=system_prompt,
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

    def _maybe_post_facts_marker(self, room_uuid: UUID) -> bool:
        """Post a one-time re-check-facts notice when facts were invalidated
        (a shield toggle or Q&A repopulate) since the last marker in this room.
        Dedup is by the exact invalidation timestamp carried in the marker's
        meta, so at most one marker per invalidation per room. Returns True
        when a marker was posted (the caller must then restore the progress
        bubble the terminal-kind post just reaped). Best-effort: a failure
        here must never break the turn."""
        try:
            stamp = db.get_setting("qa.facts_invalidated_at")
            if not stamp:
                return False
            msgs = db.list_room_messages(room_uuid)
            if any((m.get("meta") or {}).get("facts_invalidation") == stamp for m in msgs):
                return False
            db.post_chat_message(
                room_uuid, self.agent_uuid, FACTS_INVALIDATION_NOTICE,
                kind="message", meta={"facts_invalidation": stamp},
            )
            return True
        except Exception:
            logger.warning("assistant: facts-invalidation marker failed", exc_info=True)
            return False

    def _build_user_prompt(
        self,
        *,
        transcript: str,
        scratchpad: list[str],
        step_index: int,
    ) -> str:
        parts = []
        # The current local time is the operator's clock — the model's only other
        # time anchor is the transcript's (UTC) message timestamps, which made
        # relative reminders ("in 10 minutes") resolve in UTC. Stating local time
        # explicitly lets set_reminder land in the operator's zone.
        now_local = datetime.now().astimezone()
        parts.append(f"Current local time: {now_local.strftime('%Y-%m-%d %H:%M %Z')}.")
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
        # Keep the most recent context when the budget is exceeded, dropping
        # WHOLE entries oldest-first — an entry can hold a <recalled_memory>
        # fence, and a character-level cut through it leaves a dangling end
        # tag in the prompt. The newest entry is always kept intact (the model
        # needs the observation it just made); it is bounded upstream by
        # MAX_OBSERVATION_PREVIEW_CHARS.
        kept: list[str] = []
        used = 0
        for entry in reversed(scratchpad):
            if kept and used + len(entry) + 1 > self.MAX_SCRATCHPAD_CHARS:
                omitted = len(scratchpad) - len(kept)
                kept.append(f"({omitted} earlier step(s) omitted — over the "
                            "scratchpad budget)")
                break
            used += len(entry) + 1
            kept.append(entry)
        return "\n".join(reversed(kept))

    # --- validation, terminal output, trace -----------------------------------

    def _validate_decision(self, decision: AssistantStepDecision) -> str | None:
        """Return an error string if the decision can't be dispatched, else None.
        Everything is checked against the effective capability registry, so a
        disabled or unknown capability is rejected here before any dispatch."""
        action = decision.action
        cap = self._caps.get(action)
        if cap is None:
            return f"action '{action.value}' is not available"
        if not cap.prompt_exposed:
            # The model may request only prompt-exposed capabilities; internal
            # ones (e.g. undo-inverses) are dispatched only by undo_write_intent.
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

    @staticmethod
    def _append_result_links(text: str, links: list[str]) -> str:
        """Append any links a write surfaced this run as clickable markdown whose
        visible text is the relative path itself (so it's both shown and clickable).
        Skips a link the model already wrote into its reply."""
        extra = [link for link in links if link not in text]
        if not extra:
            return text
        footer = "\n".join(f"[{link}]({link})" for link in extra)
        return f"{text}\n\n{footer}" if text else footer

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
        # NOTE: this cap also applies to dry-run preview text built via
        # _propose_write. Fine for short previews (reminders); a future dry_run
        # capability with a long preview (e.g. an S5 file-patch diff) should
        # raise its output_cap_chars accordingly.
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
        payload = dict(decision.args)
        if cap.dry_run:
            # Build a rich preview by running the action in dry-run (it must not
            # mutate). Bad input fails here → no proposal is recorded.
            dry = self._dispatch_action(replace(ctx, dry_run=True), decision)
            if not dry.ok:
                return dry
            preview = dry.text
            # The dry-run may pin execution-time guards into the stored payload
            # (e.g. edit_file's base_sha, so confirm refuses a since-changed file).
            payload.update(dry.data.get("confirm_payload") or {})
        intent = db.create_write_intent(
            run_uuid=self._run.uuid,
            step_uuid=ctx.step_uuid,
            capability_name=cap.name.value,
            payload=payload,
            preview_text=preview,
            room_uuid=ctx.room_uuid,
            agent_uuid=ctx.agent_uuid,
        )
        proposal: dict[str, Any] = {
            "write_intent": str(intent.uuid),
            "capability": cap.name.value,
        }
        if ctx.step_uuid is not None:
            proposal["step_link"] = db.assistant_step_path(self._run.uuid, ctx.step_uuid)
        return AssistantObservation(
            ok=True,
            text=(f"Proposed for the operator's approval: {preview}. "
                  f"This is the end of your job for this request — there is no "
                  f"action you can take to apply it yourself; the operator "
                  f"confirms it. Reply to the operator that it awaits their "
                  f"confirmation, and do not take any further action."),
            data={"write_intent_uuid": str(intent.uuid), "state": "proposed",
                  "proposal": proposal},
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
        if observation.data.get("undo") is None:
            # The row is still recorded (the trace must exist) and undo_write_intent
            # refuses a None undo gracefully — but a log-and-undo write with no
            # inverse is a capability bug worth surfacing.
            logger.warning(
                "assistant: log-and-undo write '%s' produced no undo record; "
                "it will not be undoable", cap.name.value,
            )
        preview = f"{cap.name.value}: {json.dumps(decision.args, sort_keys=True)}"
        db.create_write_intent(
            run_uuid=self._run.uuid,
            step_uuid=ctx.step_uuid,
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
            extra["assistant_run_uuid"] = str(self._run.uuid)
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
        controls = db.list_pending_controls(run.uuid)
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
            logger.info("assistant run %s stopped by operator at step %d", run.uuid, step_index)
            self._request_summary(run)
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
                run_uuid=self._run.uuid, step_index=step_index, phase="control",
                action=command, reason=detail, model_group_uuid=self.model_group_uuid,
            )

    @staticmethod
    def _compact_step(
        step_index: int, decision: AssistantStepDecision, ok: bool, preview: str
    ) -> str:
        status = "ok" if ok else "failed"
        return f"step {step_index}: {decision.action.value} -> {status}: {preview}"

    def _open_step(
        self, *, step_index: int, decision: AssistantStepDecision,
        usage: dict[str, int] | None = None, model_uuid: "UUID | None" = None,
        system_prompt: str | None = None, user_prompt: str | None = None,
        reasoning: str | None = None,
        requested_at: "datetime | None" = None,
    ) -> "db.AssistantStep | None":
        """Open a non-terminal action step: insert its single `running` row
        (committed before the action runs) and mirror it as one in-process entry
        that `_settle_step` later mutates in place. Returns the row so the loop
        can bind a write-intent to its uuid; None when there is no run (the
        scripted-seam unit path). `usage` is the decide call's token counts."""
        self._steps.append(
            {
                "step_index": step_index,
                "phase": "running",
                "action": decision.action.value,
                "reason": decision.reason,
                "error": None,
            }
        )
        if self._run is None:
            return None
        return db.open_assistant_step(
            run_uuid=self._run.uuid,
            step_index=step_index,
            action=decision.action.value,
            reason=decision.reason,
            args=decision.args,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            reasoning=reasoning,
            requested_at=requested_at,
            model_group_uuid=self.model_group_uuid,
            model_uuid=model_uuid,
            input_tokens=(usage or {}).get("input"),
            output_tokens=(usage or {}).get("output"),
            duration_ms=(usage or {}).get("ms"),
        )

    def _settle_step(
        self,
        step: "db.AssistantStep | None",
        *,
        phase: str,
        observation_preview: str | None = None,
        observation: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Settle the step opened by `_open_step`: mutate its row (and the mirror
        entry) in place to the terminal `phase` with the observation. The
        terminal `debug-assistant` trace row is posted here, where the
        observation exists — exactly one anchor per step."""
        if self._steps:
            self._steps[-1].update(phase=phase, error=error)
        if step is not None:
            db.settle_assistant_step(
                step, phase=phase,  # type: ignore[arg-type]
                observation_preview=observation_preview,
                observation=observation, error=error,
            )

    def _record_step(
        self,
        *,
        step_index: int,
        phase: str,
        decision: AssistantStepDecision | None = None,
        error: str | None = None,
        observation_preview: str | None = None,
        usage: dict[str, int] | None = None,
        model_uuid: "UUID | None" = None,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        reasoning: str | None = None,
        requested_at: "datetime | None" = None,
    ) -> None:
        """Record a single-insert (no open/settle lifecycle) trace step — the
        terminal-only path: a `failed` validation, the `final` reply, and a
        crash-failure row. Persists one `assistant_step` row and, when terminal,
        its self-contained `debug-assistant` chat anchor — and mirrors one entry
        into `self._steps` for fast in-process assertions. `usage` is the decide
        call's token counts (None for a crash/control row).
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
                run_uuid=self._run.uuid,
                step_index=step_index,
                phase=phase,  # type: ignore[arg-type]
                action=action,
                reason=reason,
                args=args,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                reasoning=reasoning,
                requested_at=requested_at,
                observation_preview=observation_preview,
                error=error,
                model_group_uuid=self.model_group_uuid,
                model_uuid=model_uuid,
                input_tokens=(usage or {}).get("input"),
                output_tokens=(usage or {}).get("output"),
                duration_ms=(usage or {}).get("ms"),
            )
