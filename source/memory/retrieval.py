"""Memory retrieval: deterministic, token-overlap based, no embeddings.

`retrieve_memories` returns a small, explainable bundle. `format_memory_context`
turns the bundle into a compact block injected before the chat transcript.
`record_memory_use` posts a `debug-memory` chat row so the operator (and
the explanation command) can audit which memories an agent used.

This is the first cut — favouring correctness and auditability over
aggressive recall, as the spec requires.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import db
from db import ChatMessage, MemoryClaim, MemoryEvidence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedMemory:
    """One memory row delivered to the agent prompt, with the reason it
    was retrieved and a short evidence summary."""

    uuid: UUID
    text: str
    kind: str
    scope: str
    confidence: float
    sensitivity: str
    reason: str
    evidence_summary: list[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Common English function words that carry no topical signal. Excluding them
# prevents spurious overlap between unrelated queries and memories (e.g. the
# word "the" matching an unrelated fact).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "shall", "should", "may", "might", "can", "could",
        "it", "its", "this", "that", "these", "those", "i", "you", "he",
        "she", "we", "they", "me", "him", "her", "us", "them", "my", "your",
        "his", "our", "their", "what", "which", "who", "how", "when",
        "where", "s", "t",
    }
)


def _tokenize(text: str) -> set[str]:
    """Lower-case word tokens with English stopwords removed.

    Stopword filtering prevents common function words (``the``, ``in``,
    ``is``, …) from creating spurious overlap between unrelated queries
    and memories.
    """
    if not text:
        return set()
    return {
        tok.lower()
        for tok in _TOKEN_RE.findall(text)
        if tok.lower() not in _STOPWORDS
    }


def _evidence_summary(memory_uuid: UUID) -> list[str]:
    """Distinct provenance labels for a claim, ordered by first appearance."""
    rows = (
        db.db.session.query(MemoryEvidence.provenance)
        .filter(MemoryEvidence.memory_uuid == memory_uuid)
        .order_by(MemoryEvidence.id.asc())
        .all()
    )
    seen: list[str] = []
    for (prov,) in rows:
        if prov not in seen:
            seen.append(prov)
    return seen


def _scope_tier(
    claim: MemoryClaim,
    room_uuid: UUID | None,
    agent_uuid: UUID | None,
) -> int:
    """0=room match, 1=agent match, 2=global/other. Lower = better."""
    if room_uuid is not None and claim.room_uuid == room_uuid:
        return 0
    if agent_uuid is not None and claim.agent_uuid == agent_uuid:
        return 1
    return 2


def retrieve_memories(
    query: str,
    *,
    agent_uuid: UUID | None,
    room_uuid: UUID | None,
    limit: int = 6,
    include_secret: bool = False,
) -> list[RetrievedMemory]:
    """Return up to `limit` active memories whose text/subject/object share
    at least one token with `query`. Secrets are excluded unless
    `include_secret=True`. Sort: scope tier (room > agent > global) →
    confidence desc → updated_at desc."""
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    q = db.db.session.query(MemoryClaim).filter(MemoryClaim.status == "active")
    if not include_secret:
        q = q.filter(MemoryClaim.sensitivity != "secret")
    # Active rows with a past expires_at are stale and must not be retrieved
    # even though their status hasn't been flipped to "expired" yet.
    now = datetime.now(UTC)
    q = q.filter(
        (MemoryClaim.expires_at.is_(None)) | (MemoryClaim.expires_at > now)
    )
    candidates = q.all()

    scored: list[tuple[int, float, Any, MemoryClaim, int]] = []
    for c in candidates:
        haystack = " ".join(
            x for x in (c.text, c.subject or "", c.object or "") if x
        )
        cand_tokens = _tokenize(haystack)
        overlap = len(query_tokens & cand_tokens)
        if overlap == 0:
            continue
        scored.append(
            (
                _scope_tier(c, room_uuid, agent_uuid),
                -float(c.confidence),
                # Negative sort key so descending updated_at sorts via ascending tuples.
                # Use the timestamp directly; SQLAlchemy returns timezone-aware datetimes.
                c.updated_at,
                c,
                overlap,
            )
        )

    # Sort: scope tier ascending (room=0 wins), then confidence descending
    # (the key already carries -confidence), then updated_at descending
    # (negate the unix timestamp so larger times sort first).
    scored.sort(
        key=lambda s: (
            s[0],
            s[1],
            -s[2].timestamp() if hasattr(s[2], "timestamp") else 0,
        )
    )

    out: list[RetrievedMemory] = []
    for _tier, _negconf, _updated, claim, _overlap in scored[:limit]:
        out.append(
            RetrievedMemory(
                uuid=claim.uuid,
                text=claim.text,
                kind=claim.kind,
                scope=claim.scope,
                confidence=float(claim.confidence),
                sensitivity=claim.sensitivity,
                reason="token_overlap",
                evidence_summary=_evidence_summary(claim.uuid),
            )
        )
    return out


_DEBUG_MEMORY_KIND: str = "debug-memory"


def format_memory_context(memories: list[RetrievedMemory]) -> str:
    """Render the memory block for `ChatAgent.user_prompt`. Returns the
    empty string when `memories` is empty (so callers can unconditionally
    concatenate without producing a stray header)."""
    if not memories:
        return ""
    lines = ["Relevant remembered facts:"]
    for m in memories:
        # Tags: [<kind>, <sensitivity>, <provenance...>]. Keeps the model
        # informed about both the kind and the audit trail at a glance.
        evidence_tag = ", ".join(m.evidence_summary) or "no evidence"
        lines.append(f"- [{m.kind}, {m.sensitivity}, {evidence_tag}] {m.text}")
    return "\n".join(lines)


def _record_memory_telemetry(
    *,
    query: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    journal_id: int | None,
    retrieval_limit: int,
    include_secret: bool,
    memories: list[RetrievedMemory],
) -> None:
    """Write one RetrievalEvent per memory: 'retrieved' for the result set,
    then 'used' for any memory injected into the prompt context.

    Phase 1: every retrieved memory is also injected into the prompt, so the
    retrieved and used target sets are identical. A future WP that adds a
    memory-filter stage between retrieval and injection can split these.

    Batches inserts with `commit=False` per row and a single
    `db.db.session.commit()` at the end to avoid N fsyncs per chat turn."""
    if not memories:
        return
    metadata = {
        "retrieval_limit": retrieval_limit,
        "include_secret": include_secret,
    }
    for i, mem in enumerate(memories):
        db.record_retrieval_event(
            target_type="memory_claim",
            target_id=str(mem.uuid),
            stage="retrieved",
            query=query,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            source="chat_memory_retrieval",
            retrieval_rank=i,
            retrieval_score=getattr(mem, "score", None),
            filter_label=None,
            metadata=metadata,
            commit=False,
        )
    for i, mem in enumerate(memories):
        db.record_retrieval_event(
            target_type="memory_claim",
            target_id=str(mem.uuid),
            stage="used",
            query=query,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            source="chat_memory_retrieval",
            retrieval_rank=i,
            retrieval_score=getattr(mem, "score", None),
            filter_label="relevant",
            metadata=metadata,
            commit=False,
        )
    db.db.session.commit()


def build_chat_memory_block(
    messages: list[dict[str, Any]],
    *,
    agent_uuid: UUID,
    room_uuid: UUID,
    journal_id: int | None = None,
    retrieval_limit: int = 6,
    include_secret: bool = False,
) -> tuple[str, str, list[RetrievedMemory]]:
    """Run chat memory retrieval for a room transcript and return
    `(memory_block, query, memories)`.

    The retrieval query is the latest human message, falling back to the latest
    message overall. Retrieved memories are recorded as RetrievalEvents
    (retrieved + used) for the relevance dashboard; a telemetry-side failure is
    swallowed (and rolled back) so it can never block the chat turn.
    `memory_block` is "" when nothing is retrieved, so callers can concatenate
    it unconditionally. Shared by the structured and unstructured chat agents."""
    query = ""
    for m in reversed(messages):
        if m.get("sender_type") == "human":
            query = m.get("text") or ""
            break
    if not query and messages:
        query = messages[-1].get("text") or ""

    memories = retrieve_memories(
        query,
        agent_uuid=agent_uuid,
        room_uuid=room_uuid,
        limit=retrieval_limit,
        include_secret=include_secret,
    )
    try:
        _record_memory_telemetry(
            query=query,
            room_uuid=room_uuid,
            agent_uuid=agent_uuid,
            journal_id=journal_id,
            retrieval_limit=retrieval_limit,
            include_secret=include_secret,
            memories=memories,
        )
    except Exception:
        logger.exception(
            "telemetry: failed to record memory retrieval; "
            "swallowing so the chat turn is not blocked"
        )
        db.db.session.rollback()
    return format_memory_context(memories), query, memories


def record_memory_use(
    journal_id: int | None,
    room_uuid: UUID | None,
    agent_uuid: UUID,
    query: str,
    memories: list[RetrievedMemory],
) -> ChatMessage | None:
    """Post a folded `debug-memory` chat row so the operator (and the
    explanation command) can see which memories the agent injected.
    Returns the new row, or None if there's nothing to record or the
    room is unknown.

    The payload mirrors the `debug-query` shape used by `QueryAgent`."""
    if not memories or room_uuid is None:
        return None
    payload: dict[str, Any] = {
        "query": query,
        "journal_id": journal_id,
        "memories": [
            {
                "memory_uuid": str(m.uuid),
                "reason": m.reason,
                "confidence": m.confidence,
                "provenance": list(m.evidence_summary),
            }
            for m in memories
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return db.post_chat_message(
        room_uuid, agent_uuid, text, "json", kind=_DEBUG_MEMORY_KIND,
    )
