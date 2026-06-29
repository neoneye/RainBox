"""Embed active memory claims into the rainbox-owned memory_embedding table.

Reuses the Q&A embedding path (Ollama embeddinggemma:300m, 768-d) but keeps the
dependency lazy and injectable so retrieval/backfill stay testable without a
live model. Best-effort everywhere: a failed embed leaves the claim usable via
lexical-only retrieval rather than raising.

Freshness model:
- `ensure_memory_embedding()` embeds a single claim, re-embedding in place when
  its text changes (at most one embedding row per claim).
- `refresh_claim_embedding()` is the write-path hook: embed while a claim is
  active, prune once it isn't. The memory write path (remember/confirm/correct/
  forget and the assistant's activate_memory) calls it after a status change.
- `prune_stale_embeddings()` is the lazy safety net: drop embeddings for claims
  that are no longer retrievable (non-active or expired).
- `backfill_memory_embeddings()` / `sync_memory_embeddings()` are the
  one-shot / triggered full reconcile (backfill active claims, then prune).
"""

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import sqlalchemy as sa

import db
from db.models import MemoryClaim, MemoryEmbedding

logger = logging.getLogger(__name__)

# Must match the Q&A embedder (memory/seed_memory.py).
EMBED_MODEL_NAME: str = "embeddinggemma:300m"
EMBED_DIM: int = 768

EmbedFn = Callable[[str], list[float]]


def _default_embed(text: str) -> list[float]:
    # Lazy import so the memory layer doesn't hard-depend on the agents layer
    # (and so importing memory never spins up an embedder).
    from memory.seed_memory import _embed_model

    return _embed_model().get_text_embedding(text)


def embedding_text(claim: MemoryClaim) -> str:
    """The canonical text embedded for a claim: its text plus any structured
    subject/predicate/object, so entity terms contribute to similarity."""
    parts = [claim.text or ""]
    for f in (claim.subject, claim.predicate, claim.object):
        if f:
            parts.append(f)
    return " ".join(parts).strip()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_memory_embedding(
    claim: MemoryClaim, *, embed_fn: EmbedFn | None = None
) -> bool:
    """Embed `claim` if it has no current embedding. Returns True when an
    up-to-date embedding exists afterward, False on empty text or embed failure.
    """
    text = embedding_text(claim)
    if not text:
        return False
    text_hash = _text_hash(text)
    existing = db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME)
    if existing is not None and existing.text_hash == text_hash:
        return True  # already current

    fn = embed_fn or _default_embed
    try:
        vec = fn(text)
    except Exception:
        logger.warning("memory: embedding failed for claim %s", claim.uuid, exc_info=True)
        return False

    # Replace any prior embedding so a claim keeps exactly one current row.
    db.delete_memory_embeddings(claim.uuid)
    db.upsert_memory_embedding(
        memory_uuid=claim.uuid,
        model_name=EMBED_MODEL_NAME,
        embed_dim=EMBED_DIM,
        text_hash=text_hash,
        embedding=list(vec),
    )
    return True


def refresh_claim_embedding(
    claim: MemoryClaim, *, embed_fn: EmbedFn | None = None
) -> None:
    """Keep one claim's embedding in sync with its current status — the hook for
    the memory write path. Embed (or re-embed on text change) while the claim is
    active or candidate (candidates are embedded immediately so query_memory can
    retrieve them before operator activation); prune its embedding once it is
    neither active nor candidate. Best-effort: the underlying embed/delete already
    swallow their own failures."""
    if claim.status in ("active", "candidate"):
        ensure_memory_embedding(claim, embed_fn=embed_fn)
    else:
        db.delete_memory_embeddings(claim.uuid)


def prune_stale_embeddings() -> int:
    """Lazy prune: drop embedding rows whose claim is no longer *retrievable*
    (not active, or active-but-expired). Returns the number removed.

    This is the safety net for deactivation paths — forget/supersede/expiry —
    that don't individually refresh: even if a write site forgets to prune, a
    periodic `sync` reconciles the embedding table with what retrieval can use.
    """
    now = datetime.now(UTC)
    retrievable = db.db.session.query(MemoryClaim.uuid).filter(
        MemoryClaim.status == "active",
        sa.or_(MemoryClaim.expires_at.is_(None), MemoryClaim.expires_at > now),
    )
    n = (
        db.db.session.query(MemoryEmbedding)
        .filter(MemoryEmbedding.memory_uuid.notin_(retrievable))
        .delete(synchronize_session=False)
    )
    db.db.session.commit()
    return n


def backfill_memory_embeddings(
    *, embed_fn: EmbedFn | None = None, limit: int | None = None
) -> int:
    """Ensure an embedding for every active claim. Returns the number of claims
    with an up-to-date embedding afterward (capped by `limit`)."""
    claims = db.list_memory_claims(status="active")
    ensured = 0
    for claim in claims:
        if limit is not None and ensured >= limit:
            break
        if ensure_memory_embedding(claim, embed_fn=embed_fn):
            ensured += 1
    return ensured


def sync_memory_embeddings(
    *, embed_fn: EmbedFn | None = None, limit: int | None = None
) -> tuple[int, int]:
    """Triggered/periodic full reconcile of the embedding table: backfill active
    claims, then prune stale rows. Returns `(embedded, pruned)`."""
    embedded = backfill_memory_embeddings(embed_fn=embed_fn, limit=limit)
    pruned = prune_stale_embeddings()
    return embedded, pruned
