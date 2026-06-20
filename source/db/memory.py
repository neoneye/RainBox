"""Memory persistence: memory claims and their evidence.

Split out of db.py. Holds the memory claim/evidence operations
(create_memory_claim, add_memory_evidence, list_memory_claims, supersede_memory,
reject_memory, ...). Re-exported from db for import compatibility.
"""
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db.models import MemoryClaim, MemoryEmbedding, MemoryEvidence, db


def create_memory_claim(
    *,
    scope: str,
    kind: str,
    text: str,
    confidence: float,
    status: str = "candidate",
    sensitivity: str = "private",
    agent_uuid: UUID | None = None,
    room_uuid: UUID | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    supersedes_uuid: UUID | None = None,
    expires_at: datetime | None = None,
) -> MemoryClaim:
    """Insert a memory_claim row. Defaults: status=candidate, sensitivity=private.
    Returns the persisted claim with id/uuid/created_at populated."""
    claim = MemoryClaim(
        scope=scope, kind=kind, text=text, confidence=confidence,
        status=status, sensitivity=sensitivity,
        agent_uuid=agent_uuid, room_uuid=room_uuid,
        subject=subject, predicate=predicate, object=object,
        supersedes_uuid=supersedes_uuid, expires_at=expires_at,
    )
    db.session.add(claim)
    db.session.commit()
    return claim


def get_memory_claim(memory_uuid: UUID) -> "MemoryClaim | None":
    """Fetch a memory claim by uuid, or None if not present."""
    return db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()


def add_memory_evidence(
    *,
    memory_uuid: UUID,
    provenance: str,
    source_type: str,
    source_id: str | None = None,
    excerpt: str | None = None,
    created_by_uuid: UUID | None = None,
) -> MemoryEvidence:
    """Attach a provenance row to a memory claim. Returns the persisted
    evidence with id/uuid/created_at populated."""
    ev = MemoryEvidence(
        memory_uuid=memory_uuid,
        provenance=provenance,
        source_type=source_type,
        source_id=source_id,
        excerpt=excerpt,
        created_by_uuid=created_by_uuid,
    )
    db.session.add(ev)
    db.session.commit()
    return ev


def list_memory_claims(
    *,
    scope: str | None = None,
    agent_uuid: UUID | None = None,
    room_uuid: UUID | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[MemoryClaim]:
    """Return claims matching every supplied filter (AND-ed). All filters
    are optional; passing none returns every claim."""
    q = db.session.query(MemoryClaim)
    if scope is not None:
        q = q.filter(MemoryClaim.scope == scope)
    if agent_uuid is not None:
        q = q.filter(MemoryClaim.agent_uuid == agent_uuid)
    if room_uuid is not None:
        q = q.filter(MemoryClaim.room_uuid == room_uuid)
    if status is not None:
        q = q.filter(MemoryClaim.status == status)
    if kind is not None:
        q = q.filter(MemoryClaim.kind == kind)
    return q.order_by(MemoryClaim.id.asc()).all()


def supersede_memory(
    old_uuid: UUID,
    new_claim_args: dict[str, Any],
    evidence_args: dict[str, Any],
) -> MemoryClaim:
    """In one transaction: mark `old_uuid` as superseded, create a new
    `active` claim with `new_claim_args` (its supersedes_uuid wired to
    `old_uuid`), and attach `evidence_args` to the new claim."""
    old = db.session.query(MemoryClaim).filter_by(uuid=old_uuid).first()
    if old is None:
        raise ValueError(f"memory claim not found: {old_uuid}")
    old.status = "superseded"
    new_args = dict(new_claim_args)
    new_args["supersedes_uuid"] = old_uuid
    new_args.setdefault("status", "active")
    new_claim = MemoryClaim(**new_args)
    db.session.add(new_claim)
    db.session.flush()  # assigns new_claim.uuid
    ev = MemoryEvidence(memory_uuid=new_claim.uuid, **evidence_args)
    db.session.add(ev)
    db.session.commit()
    return new_claim


def reject_memory(memory_uuid: UUID, evidence_args: dict[str, Any]) -> None:
    """Mark a claim `rejected` and attach an evidence row recording the
    rejection. Existing evidence is left untouched (the audit trail
    survives)."""
    claim = db.session.query(MemoryClaim).filter_by(uuid=memory_uuid).first()
    if claim is None:
        raise ValueError(f"memory claim not found: {memory_uuid}")
    claim.status = "rejected"
    ev = MemoryEvidence(memory_uuid=memory_uuid, **evidence_args)
    db.session.add(ev)
    db.session.commit()


# --- embeddings (hybrid retrieval) -------------------------------------------


def upsert_memory_embedding(
    *,
    memory_uuid: UUID,
    model_name: str,
    embed_dim: int,
    text_hash: str,
    embedding: list[float],
) -> MemoryEmbedding:
    """Insert or update the embedding row for (memory_uuid, model_name,
    text_hash). Idempotent on the unique key."""
    row = (
        db.session.query(MemoryEmbedding)
        .filter(
            MemoryEmbedding.memory_uuid == memory_uuid,
            MemoryEmbedding.model_name == model_name,
            MemoryEmbedding.text_hash == text_hash,
        )
        .one_or_none()
    )
    if row is None:
        row = MemoryEmbedding(
            memory_uuid=memory_uuid, model_name=model_name,
            embed_dim=embed_dim, text_hash=text_hash, embedding=embedding,
        )
        db.session.add(row)
    else:
        row.embedding = embedding
        row.embed_dim = embed_dim
        row.updated_at = datetime.now(UTC)
    db.session.commit()
    return row


def get_memory_embedding(
    memory_uuid: UUID, model_name: str
) -> MemoryEmbedding | None:
    """The most recent embedding row for a claim under one model, or None."""
    return (
        db.session.query(MemoryEmbedding)
        .filter(
            MemoryEmbedding.memory_uuid == memory_uuid,
            MemoryEmbedding.model_name == model_name,
        )
        .order_by(MemoryEmbedding.id.desc())
        .first()
    )


def delete_memory_embeddings(memory_uuid: UUID) -> int:
    """Drop all embedding rows for a claim. Returns the number removed."""
    n = (
        db.session.query(MemoryEmbedding)
        .filter(MemoryEmbedding.memory_uuid == memory_uuid)
        .delete()
    )
    db.session.commit()
    return n
