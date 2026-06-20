"""Tests for hybrid memory retrieval: hard filters before ranking, full-text +
entity + vector signals merged, and improved recall over pure token overlap.

Deterministic: vector tests use a fake embedder; the full-text recall test
relies on Postgres English stemming (no model needed).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db import MemoryClaim
from memory.embeddings import ensure_memory_embedding
from memory.retrieval import retrieve_memories, retrieve_memories_hybrid


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


def _claim(subject, text, *, status="active", sensitivity="public",
           subj=None, obj=None, expires_at=None, room_uuid=None, scope="global"):
    return db.create_memory_claim(
        scope=scope, kind="fact", text=text, confidence=0.9,
        status=status, sensitivity=sensitivity, subject=subj or subject,
        object=obj, expires_at=expires_at, room_uuid=room_uuid,
    )


def _cleanup(subject):
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == subject).delete()
    db.db.session.commit()


def _boom_embed(_text):
    raise RuntimeError("no embedder in this test")


def _uuids(results):
    return {r.uuid for r in results}


def test_fulltext_recall_beats_token_overlap(app_ctx, fresh_subject):
    """Stemmed full-text matches where exact token overlap does not — the core
    'improved recall' claim. Vector is disabled so the win is full-text alone."""
    claim = _claim(fresh_subject, "the user enjoys running marathons")
    try:
        # Exact token overlap finds nothing (running != run, marathons != marathon).
        assert claim.uuid not in _uuids(
            retrieve_memories("marathon run", agent_uuid=None, room_uuid=None)
        )
        # Hybrid full-text (English stemming) finds it.
        out = retrieve_memories_hybrid(
            "marathon run", agent_uuid=None, room_uuid=None, embed_fn=_boom_embed,
        )
        assert claim.uuid in _uuids(out)
    finally:
        _cleanup(fresh_subject)


def test_hard_filters_exclude_secret_expired_out_of_scope(app_ctx, fresh_subject):
    secret = _claim(fresh_subject, "the password is hunter2 widget", sensitivity="secret")
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = _claim(fresh_subject, "the widget expired fact", expires_at=past)
    other_room = _claim(fresh_subject, "room widget secret note",
                        scope="room", room_uuid=uuid4())
    public = _claim(fresh_subject, "the widget is blue")
    try:
        out = retrieve_memories_hybrid(
            "widget", agent_uuid=None, room_uuid=uuid4(), embed_fn=_boom_embed,
        )
        ids = _uuids(out)
        assert public.uuid in ids
        assert secret.uuid not in ids       # sensitivity filter
        assert expired.uuid not in ids      # expiry filter
        assert other_room.uuid not in ids   # out-of-scope room
    finally:
        _cleanup(fresh_subject)


def test_entity_boost_prefers_subject_object_match(app_ctx, fresh_subject):
    # Both mention "capital"; only one has the matching object entity "france".
    # (Use the object column so `subject` stays the cleanup tag.)
    match = _claim(fresh_subject, "the capital city", obj="france")
    other = _claim(fresh_subject, "the capital city", obj="spain")
    try:
        out = retrieve_memories_hybrid(
            "france capital", agent_uuid=None, room_uuid=None, embed_fn=_boom_embed,
        )
        ids = [r.uuid for r in out]
        assert match.uuid in ids
        assert ids.index(match.uuid) < ids.index(other.uuid) if other.uuid in ids else True
    finally:
        _cleanup(fresh_subject)


def test_vector_recall_with_no_lexical_overlap(app_ctx, fresh_subject):
    """A semantically-close claim is retrieved even with zero token/full-text
    overlap — proven with a fake embedder."""
    rel = _claim(fresh_subject, "favorite programming language is rust")
    unrel = _claim(fresh_subject, "the weather today is nice")

    def fake_embed(text):
        if "rust" in text:
            return [1.0, 0.0] + [0.0] * 766
        if "weather" in text:
            return [0.0, 1.0] + [0.0] * 766
        if "xyzzy" in text:  # the query, embedded close to rust
            return [1.0, 0.0] + [0.0] * 766
        return [0.0, 0.0, 1.0] + [0.0] * 765

    try:
        ensure_memory_embedding(rel, embed_fn=fake_embed)
        ensure_memory_embedding(unrel, embed_fn=fake_embed)
        # No token/full-text overlap for "xyzzy".
        assert retrieve_memories("xyzzy", agent_uuid=None, room_uuid=None) == []
        out = retrieve_memories_hybrid(
            "xyzzy", agent_uuid=None, room_uuid=None, embed_fn=fake_embed,
        )
        ids = [r.uuid for r in out]
        assert rel.uuid in ids
        # The semantically-close claim outranks the unrelated one.
        if unrel.uuid in ids:
            assert ids.index(rel.uuid) < ids.index(unrel.uuid)
    finally:
        _cleanup(fresh_subject)


def test_records_retrieval_telemetry(app_ctx, fresh_subject):
    from db import RetrievalEvent
    claim = _claim(fresh_subject, "telemetry widget fact")
    try:
        retrieve_memories_hybrid(
            "widget", agent_uuid=None, room_uuid=None, journal_id=1,
            embed_fn=_boom_embed,
        )
        events = (
            db.db.session.query(RetrievalEvent)
            .filter(RetrievalEvent.target_type == "memory_claim",
                    RetrievalEvent.target_id == str(claim.uuid),
                    RetrievalEvent.source == "memory.hybrid")
            .all()
        )
        assert events, "hybrid retrieval should record telemetry"
        db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id == str(claim.uuid)
        ).delete()
        db.db.session.commit()
    finally:
        _cleanup(fresh_subject)
