"""Assemble a chat agent's memory context: the operator profile block (active
self-model) followed by hybrid memory retrieval.

Lives in the agent layer because `user_profile` imports `memory.retrieval`, so
`memory.retrieval` cannot import `user_profile` (a cycle); the agent layer imports
both freely.
"""

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import memory.retrieval as memory_retrieval
import user_profile
from chat.prompt_layout import (
    EXCHANGE_TURNS, REQUEST_MESSAGE_LAYOUTS, latest_exchange, request_turns,
)
from chat.transcript import current_message_line, format_history, split_history
from memory.retrieval import RetrievedMemory

logger = logging.getLogger(__name__)


def build_chat_context_block(
    messages: list[dict[str, Any]] | None = None,
    *,
    agent_uuid: UUID,
    room_uuid: UUID,
    journal_id: UUID | None = None,
    query: str | None = None,
    _seed_retriever=None,
) -> tuple[str, str, list[RetrievedMemory]]:
    """Return (context_block, query, memories): the operator profile block (if
    any) then a "Curated facts" seed section (if any), then the hybrid memory
    block, joined by blank lines. Best-effort on the profile and seeds — a
    failure in either must never break a chat turn.

    `query` may be supplied directly (e.g. in tests with no message history);
    if omitted, the query is extracted from `messages` by
    `build_chat_memory_block`.  `_seed_retriever` is injected by tests."""
    profile_block, seed_block, memory_block, retrieved_query, memories = (
        _context_parts(messages, agent_uuid=agent_uuid, room_uuid=room_uuid,
                       journal_id=journal_id, query=query,
                       _seed_retriever=_seed_retriever))
    parts = [b for b in (profile_block, seed_block, memory_block) if b]
    joined = "\n\n".join(parts)
    if joined:
        joined, _ = memory_retrieval.fence_recalled_memory(joined)
    return joined, retrieved_query, memories


def _context_parts(
    messages: list[dict[str, Any]] | None,
    *,
    agent_uuid: UUID,
    room_uuid: UUID,
    journal_id: UUID | None,
    query: str | None,
    _seed_retriever,
) -> tuple[str, str, str, str, list[RetrievedMemory]]:
    """The three context blocks, unfenced — (profile, curated facts, recalled
    memories) — plus the retrieval query and memories."""
    from memory.seed_memory import retrieve_seed_memories

    memory_block, retrieved_query, memories = memory_retrieval.build_chat_memory_block(
        messages or [], agent_uuid=agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
    )
    seed_query = query if query is not None else retrieved_query

    seed_fn = _seed_retriever or retrieve_seed_memories
    seeds = []
    try:
        seeds = seed_fn(seed_query) if seed_query else []
    except Exception:
        logger.warning("chat: seed memory retrieval failed", exc_info=True)

    # Tier seeds: user-overlay first, then upstream; preserve score order within tier.
    overlay = [s for s in seeds if s.source == "user-overlay"]
    upstream = [s for s in seeds if s.source != "user-overlay"]
    seed_lines = [f"- {s.uuid}, seed/{s.source}: {s.answer}" for s in overlay + upstream]
    seed_block = ("Curated facts\n" + "\n".join(seed_lines)) if seed_lines else ""

    profile_block = ""
    try:
        profile_block, _ = user_profile.build_profile_block(
            agent_uuid=agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
        )
    except Exception:
        logger.warning("chat: profile block failed", exc_info=True)
    return profile_block, seed_block, memory_block, retrieved_query, memories


# How the chat agents' system prompts describe the user message, per layout.
# The single_turn paragraph is the one both prompts carry verbatim; the
# request_turn system prompt swaps it for the second (chat_system_prompt).
CHAT_SINGLE_TURN_SHAPE: str = """\
You will receive an IRC-style chat transcript:
1. An optional "Relevant remembered facts:" block, each tagged with its
   kind, sensitivity, and provenance.
2. Optional chat history, oldest first.
3. A clearly marked Current message at the bottom."""

CHAT_REQUEST_TURN_SHAPE: str = """\
You will receive the chat context as one message, then the Current message as
a message of its own:
1. Optional facts about the user, in a fenced block.
2. Optional chat history as an IRC-style transcript, oldest first.
3. Optional facts recalled for this message — "Curated facts" and a
   "Relevant remembered facts:" block, each tagged with its kind,
   sensitivity, and provenance — in a fenced block.
4. A line naming who sent the Current message, and when.
The Current message follows as the final user message, exactly as it was
written; your turn before it only acknowledged the context."""

# The exchange_turns layout's paragraph: request_turn's, plus the previous
# exchange that may sit between the acknowledgement and the Current message.
CHAT_EXCHANGE_TURNS_SHAPE: str = CHAT_REQUEST_TURN_SHAPE + """
When the transcript's last two lines are the user and then you, they are
repeated after your acknowledgement as turns of their own — the user's
previous message, then your reply — before the Current message. They are
context, already answered; the Current message is the one to respond to."""


def chat_system_prompt(base: str, layout: str) -> str:
    """`base` (a chat agent's single_turn system prompt) for `layout`."""
    if layout not in REQUEST_MESSAGE_LAYOUTS:
        return base
    if base.count(CHAT_SINGLE_TURN_SHAPE) != 1:
        raise ValueError("chat system prompt lost its message-shape paragraph")
    shape = (CHAT_EXCHANGE_TURNS_SHAPE if layout == EXCHANGE_TURNS
             else CHAT_REQUEST_TURN_SHAPE)
    return base.replace(CHAT_SINGLE_TURN_SHAPE, shape)


@dataclass
class ChatPrompt:
    """A chat agent's user prompt in one prompt layout: the user message, the
    turns sent after it (empty in single_turn), and the retrieval it used."""
    user_prompt: str
    followup_turns: list[dict[str, str]] = field(default_factory=list)
    query: str = ""
    memories: list[RetrievedMemory] = field(default_factory=list)


def build_chat_prompt(
    messages: list[dict[str, Any]],
    *,
    agent_uuid: UUID,
    room_uuid: UUID,
    journal_id: UUID | None = None,
    layout: str,
) -> ChatPrompt:
    """The chat agents' user prompt in `layout` (chat/prompt_layout.py).

    single_turn: the fenced context block, then the transcript ending in its
    Current message — one message.

    request_turn: the context, ordered most stable first so the next turn
    reuses this one's cached prefix — the profile, the transcript without the
    Current message, the curated facts and recalled memories (retrieved per
    message), a line naming who sent the Current message — then the
    acknowledgement and the Current message verbatim, line breaks intact. The
    profile and the per-message facts are fenced separately because the
    transcript now sits between them.

    exchange_turns: request_turn, with the user's previous message and this
    agent's reply to it repeated as turns before the Current message, when
    those are the transcript's last two lines."""
    profile_block, seed_block, memory_block, query, memories = _context_parts(
        messages, agent_uuid=agent_uuid, room_uuid=room_uuid,
        journal_id=journal_id, query=None, _seed_retriever=None)
    history, current = split_history(messages)
    if layout not in REQUEST_MESSAGE_LAYOUTS or current is None:
        joined = "\n\n".join(
            b for b in (profile_block, seed_block, memory_block) if b)
        if joined:
            joined, _ = memory_retrieval.fence_recalled_memory(joined)
        transcript = format_history(messages)
        return ChatPrompt(
            user_prompt=f"{joined}\n\n{transcript}" if joined else transcript,
            query=query, memories=memories)
    recalled = "\n\n".join(b for b in (seed_block, memory_block) if b)
    parts = [
        memory_retrieval.fence_recalled_memory(profile_block)[0],
        history,
        memory_retrieval.fence_recalled_memory(recalled)[0],
        current_message_line(current),
    ]
    exchange = (latest_exchange(messages, agent_uuid)
                if layout == EXCHANGE_TURNS else None)
    return ChatPrompt(
        user_prompt="\n\n".join(p for p in parts if p),
        followup_turns=request_turns(
            str(current.get("text") or ""),
            None if exchange is None
            else tuple(str(m.get("text") or "") for m in exchange)),
        query=query, memories=memories)
