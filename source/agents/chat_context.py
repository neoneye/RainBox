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
    messages: list[dict[str, Any]],
    *,
    agent_uuid: UUID,
    room_uuid: UUID,
    journal_id: UUID | None = None,
) -> tuple[str, str, list[RetrievedMemory]]:
    """Return (context_block, query, memories): the operator profile block (if
    any) then the hybrid memory block, joined by blank lines. Best-effort on the
    profile — a profile failure must never break a chat turn."""
    memory_block, query, memories = memory_retrieval.build_chat_memory_block(
        messages, agent_uuid=agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
    )
    profile_block = ""
    try:
        profile_block, _ = user_profile.build_profile_block(
            agent_uuid=agent_uuid, room_uuid=room_uuid, journal_id=journal_id,
        )
    except Exception:
        logger.warning("chat: profile block failed", exc_info=True)
    parts = [b for b in (profile_block, memory_block) if b]
    return "\n\n".join(parts), query, memories
