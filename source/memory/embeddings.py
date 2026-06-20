"""Embed active memory claims into the rainbox-owned memory_embedding table.

Reuses the Q&A embedding path (Ollama nomic-embed-text, 768-d) but keeps the
dependency lazy and injectable so retrieval/backfill stay testable without a
live model. Best-effort everywhere: a failed embed leaves the claim usable via
lexical-only retrieval rather than raising.

Population: a one-shot `backfill_memory_embeddings()` over active claims, plus
`ensure_memory_embedding()` on each transition that makes a claim active (called
by the memory write path). A claim whose text changes is re-embedded in place
(at most one embedding row per claim).
"""

import hashlib
import logging
from collections.abc import Callable

import db
from db.models import MemoryClaim

logger = logging.getLogger(__name__)

# Must match the Q&A embedder (agents/query_kb_helpers.py).
EMBED_MODEL_NAME: str = "nomic-embed-text"
EMBED_DIM: int = 768

EmbedFn = Callable[[str], list[float]]


def _default_embed(text: str) -> list[float]:
    # Lazy import so the memory layer doesn't hard-depend on the agents layer
    # (and so importing memory never spins up an embedder).
    from agents.query_kb_helpers import _embed_model

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
