"""S8: chat agents retrieve via hybrid + carry the profile block. Model-free."""

from uuid import uuid4

import pytest

import db
import memory.retrieval as memory_retrieval
from agents.chat_context import build_chat_context_block
from agents.config import ASSISTANT_UUID
from db import MemoryClaim, RetrievalEvent


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
def tag() -> str:
    return f"test-{uuid4()}"


def _cleanup(tag):
    rows = db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == tag).all()
    for r in rows:
        db.db.session.query(RetrievalEvent).filter(
            RetrievalEvent.target_id == str(r.uuid)).delete()
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == tag).delete()
    db.db.session.commit()


def _msgs(text):
    return [{"sender_type": "human", "text": text}]


def test_chat_path_uses_hybrid_recall(app_ctx, tag):
    """build_chat_memory_block now uses hybrid: a stemmed full-text match that
    exact token-overlap misses is retrieved on the chat path."""
    claim = _ = db.create_memory_claim(
        scope="global", kind="fact", text="the user enjoys running marathons",
        confidence=0.9, status="active", sensitivity="public", subject=tag)
    try:
        # token-overlap misses (running != run, marathons != marathon).
        assert claim.uuid not in {m.uuid for m in memory_retrieval.retrieve_memories(
            "marathon run", agent_uuid=None, room_uuid=None)}
        block, query, memories = memory_retrieval.build_chat_memory_block(
            _msgs("marathon run"), agent_uuid=uuid4(), room_uuid=uuid4())
        assert claim.uuid in {m.uuid for m in memories}   # hybrid full-text hit
    finally:
        _cleanup(tag)


def test_context_block_has_profile_then_memory(app_ctx, tag):
    db.create_memory_claim(
        scope="global", kind="preference", text="prefers terse replies zorp",
        confidence=0.9, status="active", sensitivity="public", subject=tag)
    try:
        block, query, memories = build_chat_context_block(
            _msgs("zorp terse"), agent_uuid=uuid4(), room_uuid=uuid4())
        assert "About the operator" in block
        assert "Relevant remembered facts" in block
        assert block.index("About the operator") < block.index("Relevant remembered facts")
    finally:
        _cleanup(tag)


def test_chat_context_includes_seed_memories(app_ctx):
    from agents.chat_context import build_chat_context_block
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="s-1", path="p", source="user-overlay", answer="curated answer", score=0.7)]
    block, *_ = build_chat_context_block(
        query="zzz unrelated", room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID,
        journal_id=uuid4(), _seed_retriever=fake_seed)
    assert "curated answer" in block and "s-1" in block


def test_chat_path_filters_secret(app_ctx, tag):
    secret = db.create_memory_claim(
        scope="global", kind="fact", text="the password is hunter2 zorp",
        confidence=0.9, status="active", sensitivity="secret", subject=tag)
    try:
        block, query, memories = memory_retrieval.build_chat_memory_block(
            _msgs("zorp"), agent_uuid=uuid4(), room_uuid=uuid4())
        assert secret.uuid not in {m.uuid for m in memories}
    finally:
        _cleanup(tag)
