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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa

import db
from db import ChatMessage, MemoryClaim, MemoryEmbedding, MemoryEvidence

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
    # Merged hybrid score (0 for the legacy token-overlap path).
    score: float = 0.0


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


# --- hybrid retrieval (Phase 3) ----------------------------------------------

# Merge weights for the minimal hybrid blend (docs/proposals/2026-06-19-...,
# "Draft: memory embedding storage and ranking"). Confidence/scope are
# tie-breakers after the score, not hidden multipliers.
_W_VECTOR = 0.55
_W_FULLTEXT = 0.30
_W_ENTITY = 0.15


def hard_filtered_claims(
    include_secret: bool,
    room_uuid: UUID | None,
    agent_uuid: UUID | None,
) -> list[MemoryClaim]:
    """Active, allowed-sensitivity, non-expired, in-scope claims — applied
    BEFORE any ranking so forbidden claims never enter the candidate set.

    This is the single source of truth for the 'filter before rank' contract:
    both hybrid retrieval and the user-profile digest reuse it so forbidden
    claims can't leak through a divergent copy of the filter."""
    q = db.db.session.query(MemoryClaim).filter(MemoryClaim.status == "active")
    if not include_secret:
        q = q.filter(MemoryClaim.sensitivity != "secret")
    now = datetime.now(UTC)
    q = q.filter(
        (MemoryClaim.expires_at.is_(None)) | (MemoryClaim.expires_at > now)
    )
    candidates = q.all()
    # Scope: a global claim is always allowed; an agent/room-scoped claim is only
    # allowed for its own agent/room. A project-scoped claim has no project key
    # to match against (MemoryClaim carries only agent/room keys, and the turn
    # has no project context), so it is excluded entirely until project context
    # exists — otherwise it would leak into every unrelated room/agent.
    out = []
    for c in candidates:
        if c.scope == "project":
            continue
        if c.scope == "room" and c.room_uuid != room_uuid:
            continue
        if c.scope == "agent" and agent_uuid is not None and c.agent_uuid != agent_uuid:
            continue
        if c.scope == "agent" and agent_uuid is None:
            continue
        out.append(c)
    return out


def _fulltext_scores(query: str, ids: list[UUID]) -> dict[UUID, float]:
    """Postgres ts_rank for each candidate that shares at least one query term.

    Two things matter here:
    - We OR the query terms (`term1 | term2 | …`) rather than using
      `plainto_tsquery`, which ANDs them — an AND query would only match a claim
      containing *every* query word, killing partial-overlap recall.
    - The `@@` match filter is essential: `ts_rank` returns a tiny non-zero
      epsilon (~1e-20) even for non-matching documents, so without the filter the
      caller's max-normalization would turn that uniform epsilon into a full-text
      score of 1.0 for every claim, and an unrelated query would retrieve the
      whole active set. With `@@`, only claims sharing a term appear here;
      everything else is treated as 0 by the caller's `.get(uuid, 0.0)`.
    """
    if not ids:
        return {}
    terms = _tokenize(query)
    if not terms:
        return {}
    tsq = " | ".join(sorted(terms))
    stmt = sa.text(
        "SELECT uuid, ts_rank("
        "  to_tsvector('english', coalesce(text,'') || ' ' || "
        "    coalesce(subject,'') || ' ' || coalesce(object,'')),"
        "  to_tsquery('english', :tsq)) AS rank "
        "FROM memory_claim WHERE uuid IN :ids "
        "  AND to_tsvector('english', coalesce(text,'') || ' ' || "
        "      coalesce(subject,'') || ' ' || coalesce(object,'')) "
        "      @@ to_tsquery('english', :tsq)"
    ).bindparams(sa.bindparam("ids", expanding=True))
    rows = db.db.session.execute(stmt, {"tsq": tsq, "ids": ids}).all()
    return {row[0]: float(row[1]) for row in rows}


def _vector_sims(
    query: str, ids: list[UUID], embed_fn: Callable[[str], list[float]] | None
) -> dict[UUID, float]:
    """Cosine similarity (0..1) for candidates that have an embedding. Empty when
    no query embedding is available — retrieval degrades to lexical-only."""
    if not ids or not query.strip():
        return {}
    if embed_fn is None:
        from memory.embeddings import _default_embed
        embed_fn = _default_embed
    try:
        qvec = embed_fn(query)
    except Exception:
        logger.warning("memory: query embedding failed; lexical-only", exc_info=True)
        return {}
    if not qvec:
        return {}
    from memory.embeddings import EMBED_MODEL_NAME

    rows = (
        db.db.session.query(
            MemoryEmbedding.memory_uuid,
            MemoryEmbedding.embedding.cosine_distance(qvec),
        )
        .filter(
            MemoryEmbedding.memory_uuid.in_(ids),
            MemoryEmbedding.model_name == EMBED_MODEL_NAME,
        )
        .all()
    )
    # pgvector cosine distance is in [0,2]; map to a [0,1] similarity.
    return {mu: max(0.0, 1.0 - float(dist) / 2.0) for mu, dist in rows}


def _entity_boost(claim: MemoryClaim, query_tokens: set[str]) -> float:
    subj = (claim.subject or "").lower().strip()
    obj = (claim.object or "").lower().strip()
    if (subj and subj in query_tokens) or (obj and obj in query_tokens):
        return 1.0
    field_tokens = _tokenize(" ".join([claim.subject or "", claim.object or ""]))
    if field_tokens & query_tokens:
        return 0.5
    return 0.0


def retrieve_memories_hybrid(
    query: str,
    *,
    agent_uuid: UUID | None,
    room_uuid: UUID | None,
    limit: int = 6,
    include_secret: bool = False,
    journal_id: UUID | None = None,
    embed_fn: Callable[[str], list[float]] | None = None,
    record_telemetry: bool = True,
) -> list[RetrievedMemory]:
    """Multi-signal memory retrieval: hard filters first, then a weighted merge
    of vector similarity, Postgres full-text rank, and a structured
    subject/object entity boost. Confidence/scope break ties. Records retrieval
    telemetry for the injected set.

    Degrades gracefully: claims without an embedding (or when no embedder is
    available) are still retrievable via full-text/entity signals.
    """
    if not query or not query.strip():
        return []
    candidates = hard_filtered_claims(include_secret, room_uuid, agent_uuid)
    if not candidates:
        return []

    ids = [c.uuid for c in candidates]
    query_tokens = _tokenize(query)
    fts = _fulltext_scores(query, ids)
    vec = _vector_sims(query, ids, embed_fn)
    max_fts = max(fts.values(), default=0.0)

    scored: list[tuple[float, int, float, datetime, MemoryClaim, str]] = []
    for c in candidates:
        v = vec.get(c.uuid, 0.0)
        f = (fts.get(c.uuid, 0.0) / max_fts) if max_fts > 0 else 0.0
        e = _entity_boost(c, query_tokens)
        score = _W_VECTOR * v + _W_FULLTEXT * f + _W_ENTITY * e
        if score <= 0.0:
            continue
        signals = [
            name for name, val in (("vector", v), ("fulltext", f), ("entity", e))
            if val > 0
        ]
        scored.append(
            (score, _scope_tier(c, room_uuid, agent_uuid), float(c.confidence),
             c.updated_at, c, "+".join(signals) or "hybrid")
        )

    # Best score first; then room/agent scope, higher confidence, more recent.
    scored.sort(
        key=lambda s: (
            -s[0], s[1], -s[2],
            -s[3].timestamp() if hasattr(s[3], "timestamp") else 0,
        )
    )

    out: list[RetrievedMemory] = []
    for rank, (score, _tier, _conf, _updated, claim, reason) in enumerate(scored[:limit]):
        out.append(
            RetrievedMemory(
                uuid=claim.uuid, text=claim.text, kind=claim.kind, scope=claim.scope,
                confidence=float(claim.confidence), sensitivity=claim.sensitivity,
                reason=reason, evidence_summary=_evidence_summary(claim.uuid),
                score=round(score, 6),
            )
        )
        if record_telemetry:
            try:
                db.record_retrieval_event(
                    target_type="memory_claim", target_id=str(claim.uuid),
                    stage="retrieved", query=query, room_uuid=room_uuid,
                    agent_uuid=agent_uuid, journal_id=journal_id, source="memory.hybrid",
                    retrieval_rank=rank, retrieval_score=round(score, 6),
                )
            except Exception:
                logger.warning("memory: failed to record hybrid retrieval telemetry")
    return out


_DEBUG_MEMORY_KIND: str = "debug-memory"


def format_memory_context(
    memories: list[RetrievedMemory], *, include_uuid: bool = False
) -> str:
    """Render the memory block for `ChatAgent.user_prompt`. Returns the
    empty string when `memories` is empty (so callers can unconditionally
    concatenate without producing a stray header).

    `include_uuid` appends each memory's uuid — off for the always-on chat
    context (noise for a reply), on for the assistant's `query_memory` so it can
    point at a specific memory (e.g. to forget it)."""
    if not memories:
        return ""
    # Tags = <kind>, <sensitivity>, <provenance...> — the kind + audit trail at a
    # glance. With uuids, lead each line with the uuid: putting it first (not in a
    # trailing "(memory_uuid: …)") is unambiguous even if the memory text itself
    # contains a uuid or the literal "memory_uuid:", and a one-line legend is
    # shorter than repeating the label per row.
    if include_uuid:
        lines = ["Relevant remembered facts",
                 "- {memory_uuid}, {memory_tags}: {memory_text}"]
        for m in memories:
            evidence_tag = ", ".join(m.evidence_summary) or "no evidence"
            lines.append(f"- {m.uuid}, {m.kind}, {m.sensitivity}, {evidence_tag}: {m.text}")
        return "\n".join(lines)
    lines = ["Relevant remembered facts:"]
    for m in memories:
        evidence_tag = ", ".join(m.evidence_summary) or "no evidence"
        lines.append(f"- [{m.kind}, {m.sensitivity}, {evidence_tag}] {m.text}")
    return "\n".join(lines)


def _record_memory_telemetry(
    *,
    query: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    journal_id: UUID | None,
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
    journal_id: UUID | None = None,
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

    # Hybrid retrieval (vector + full-text + entity); telemetry is owned by
    # _record_memory_telemetry below (chat_memory_retrieval), so suppress
    # hybrid's own retrieved-event write to avoid double-recording.
    memories = retrieve_memories_hybrid(
        query,
        agent_uuid=agent_uuid,
        room_uuid=room_uuid,
        limit=retrieval_limit,
        include_secret=include_secret,
        journal_id=journal_id,
        record_telemetry=False,
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
    journal_id: UUID | None,
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
        "journal_id": str(journal_id) if journal_id is not None else None,
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
