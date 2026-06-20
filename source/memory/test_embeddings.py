"""Tests for memory embedding sync/backfill. A fake embedder keeps these
deterministic and free of Ollama."""

from uuid import uuid4

import pytest

import db
from db import MemoryClaim, MemoryEmbedding
from memory.embeddings import (
    EMBED_MODEL_NAME,
    backfill_memory_embeddings,
    ensure_memory_embedding,
    prune_stale_embeddings,
    refresh_claim_embedding,
    sync_memory_embeddings,
)


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


def _claim(subject, text="the deploy host is prod-web-01", status="active"):
    return db.create_memory_claim(
        scope="global", kind="fact", text=text,
        confidence=0.9, status=status, sensitivity="public", subject=subject,
    )


def _cleanup(subject):
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == subject).delete()
    db.db.session.commit()


def _fake_embed(_text):
    return [0.5] * 768


def test_ensure_embeds_and_stores(app_ctx, fresh_subject):
    claim = _claim(fresh_subject)
    try:
        assert ensure_memory_embedding(claim, embed_fn=_fake_embed) is True
        row = db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME)
        assert row is not None
        assert row.embed_dim == 768
    finally:
        _cleanup(fresh_subject)


def test_ensure_is_idempotent_when_text_unchanged(app_ctx, fresh_subject):
    claim = _claim(fresh_subject)
    calls = {"n": 0}

    def counting(_t):
        calls["n"] += 1
        return [0.1] * 768

    try:
        ensure_memory_embedding(claim, embed_fn=counting)
        ensure_memory_embedding(claim, embed_fn=counting)
        assert calls["n"] == 1  # second call is a no-op (same text hash)
    finally:
        _cleanup(fresh_subject)


def test_ensure_reembeds_when_text_changes_keeping_one_row(app_ctx, fresh_subject):
    claim = _claim(fresh_subject, text="first text")
    try:
        ensure_memory_embedding(claim, embed_fn=_fake_embed)
        claim.text = "completely different text now"
        db.db.session.commit()
        ensure_memory_embedding(claim, embed_fn=_fake_embed)
        rows = (
            db.db.session.query(MemoryEmbedding)
            .filter(MemoryEmbedding.memory_uuid == claim.uuid)
            .all()
        )
        assert len(rows) == 1  # stale embedding replaced, not accumulated
    finally:
        _cleanup(fresh_subject)


def test_ensure_returns_false_on_embed_failure(app_ctx, fresh_subject):
    claim = _claim(fresh_subject)

    def boom(_t):
        raise RuntimeError("ollama down")

    try:
        assert ensure_memory_embedding(claim, embed_fn=boom) is False
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup(fresh_subject)


def test_backfill_embeds_only_active_claims(app_ctx, fresh_subject):
    _claim(fresh_subject, text="active one")
    _claim(fresh_subject, text="active two")
    _claim(fresh_subject, text="a candidate", status="candidate")
    try:
        n = backfill_memory_embeddings(embed_fn=_fake_embed)
        # Only the active claims tagged with our subject are embedded; the
        # candidate is not. (Other active claims may exist in the shared DB, so
        # assert our two are embedded rather than an exact global count.)
        ours = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.subject == fresh_subject, MemoryClaim.status == "active")
            .all()
        )
        assert n >= 2
        for c in ours:
            assert db.get_memory_embedding(c.uuid, EMBED_MODEL_NAME) is not None
        cand = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.subject == fresh_subject, MemoryClaim.status == "candidate")
            .one()
        )
        assert db.get_memory_embedding(cand.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup(fresh_subject)


# --- freshness: refresh on write, lazy prune, full sync ----------------------


def test_refresh_embeds_an_active_claim(app_ctx, fresh_subject):
    claim = _claim(fresh_subject, status="active")
    try:
        refresh_claim_embedding(claim, embed_fn=_fake_embed)
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is not None
    finally:
        _cleanup(fresh_subject)


def test_refresh_prunes_when_claim_no_longer_active(app_ctx, fresh_subject):
    claim = _claim(fresh_subject, status="active")
    try:
        ensure_memory_embedding(claim, embed_fn=_fake_embed)
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is not None
        claim.status = "rejected"
        db.db.session.commit()
        refresh_claim_embedding(claim, embed_fn=_fake_embed)
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup(fresh_subject)


def test_prune_stale_drops_nonactive_keeps_active(app_ctx, fresh_subject):
    active = _claim(fresh_subject, text="prune active", status="active")
    stale = _claim(fresh_subject, text="prune stale", status="active")
    try:
        ensure_memory_embedding(active, embed_fn=_fake_embed)
        ensure_memory_embedding(stale, embed_fn=_fake_embed)
        stale.status = "superseded"
        db.db.session.commit()
        pruned = prune_stale_embeddings()
        assert pruned >= 1
        assert db.get_memory_embedding(active.uuid, EMBED_MODEL_NAME) is not None
        assert db.get_memory_embedding(stale.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup(fresh_subject)


def test_prune_stale_drops_expired_active_claim(app_ctx, fresh_subject):
    from datetime import UTC, datetime, timedelta

    claim = _claim(fresh_subject, status="active")
    try:
        ensure_memory_embedding(claim, embed_fn=_fake_embed)
        # Still status=active, but past its expiry — retrieval won't use it, so
        # its embedding is dead weight and should be pruned.
        claim.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db.db.session.commit()
        prune_stale_embeddings()
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup(fresh_subject)


def test_sync_backfills_active_and_prunes_stale(app_ctx, fresh_subject):
    active = _claim(fresh_subject, text="sync active", status="active")
    stale = _claim(fresh_subject, text="sync stale", status="active")
    try:
        ensure_memory_embedding(stale, embed_fn=_fake_embed)
        stale.status = "rejected"
        db.db.session.commit()
        embedded, pruned = sync_memory_embeddings(embed_fn=_fake_embed)
        assert embedded >= 1
        assert pruned >= 1
        assert db.get_memory_embedding(active.uuid, EMBED_MODEL_NAME) is not None
        assert db.get_memory_embedding(stale.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup(fresh_subject)
