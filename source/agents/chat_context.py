"""Assemble a chat agent's memory context: the operator profile block (active
self-model) followed by hybrid memory retrieval.

Lives in the agent layer because `user_profile` imports `memory.retrieval`, so
`memory.retrieval` cannot import `user_profile` (a cycle); the agent layer imports
both freely.
"""

import logging
from typing import Any
from uuid import UUID

import memory.retrieval as memory_retrieval
import user_profile
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
    parts = [b for b in (profile_block, seed_block, memory_block) if b]
    return "\n\n".join(parts), retrieved_query, memories
