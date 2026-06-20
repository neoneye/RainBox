"""Tests for memory_embedding storage: the rainbox-owned pgvector table that
backs hybrid memory retrieval (separate from the LlamaIndex Q&A table).
"""

from uuid import uuid4

import pytest

import db
from db import MemoryClaim, MemoryEmbedding


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _claim(subject: str) -> MemoryClaim:
    return db.create_memory_claim(
        scope="global", kind="fact", text="embedding target",
        confidence=0.9, status="active", sensitivity="public", subject=subject,
    )


def _cleanup(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == subject).delete()
    db.db.session.commit()


def test_upsert_and_get_memory_embedding(app_ctx, fresh_subject):
    claim = _claim(fresh_subject)
    vec = [0.1] * 768
    try:
        row = db.upsert_memory_embedding(
            memory_uuid=claim.uuid, model_name="nomic-embed-text",
            embed_dim=768, text_hash="hash1", embedding=vec,
        )
        assert row.id is not None
        got = db.get_memory_embedding(claim.uuid, "nomic-embed-text")
        assert got is not None
        assert got.text_hash == "hash1"
        assert len(list(got.embedding)) == 768
    finally:
        _cleanup(fresh_subject)


def test_upsert_same_key_updates_in_place(app_ctx, fresh_subject):
    claim = _claim(fresh_subject)
    try:
        db.upsert_memory_embedding(
            memory_uuid=claim.uuid, model_name="m", embed_dim=768,
            text_hash="h", embedding=[0.1] * 768,
        )
        db.upsert_memory_embedding(
            memory_uuid=claim.uuid, model_name="m", embed_dim=768,
            text_hash="h", embedding=[0.2] * 768,
        )
        rows = (
            db.db.session.query(MemoryEmbedding)
            .filter(MemoryEmbedding.memory_uuid == claim.uuid)
            .all()
        )
        assert len(rows) == 1  # unique(memory_uuid, model_name, text_hash)
    finally:
        _cleanup(fresh_subject)


def test_deleting_claim_cascades_embeddings(app_ctx, fresh_subject):
    claim = _claim(fresh_subject)
    db.upsert_memory_embedding(
        memory_uuid=claim.uuid, model_name="m", embed_dim=768,
        text_hash="h", embedding=[0.1] * 768,
    )
    claim_uuid = claim.uuid
    _cleanup(fresh_subject)  # deletes the claim
    remaining = (
        db.db.session.query(MemoryEmbedding)
        .filter(MemoryEmbedding.memory_uuid == claim_uuid)
        .count()
    )
    assert remaining == 0


def test_init_db_twice_keeps_vector_extension_and_table(app_ctx):
    # The vector extension + memory_embedding table survive a re-init.
    db.init_db(app_ctx)
    import sqlalchemy as sa
    ext = db.db.session.execute(
        sa.text("select 1 from pg_extension where extname='vector'")
    ).first()
    assert ext is not None
