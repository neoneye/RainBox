"""The memory write path keeps embeddings fresh end to end: activating writes
embed the claim, and deactivating writes prune it. A fake embedder (patched over
the module default) keeps these deterministic and free of Ollama.
"""

from uuid import UUID, uuid4

import pytest

import db
from db import MemoryClaim
import memory.embeddings as embeddings
from agents.assistant import AssistantActionContext, _action_activate_memory, _action_remember
from memory.embeddings import EMBED_MODEL_NAME
from memory.ops import handle_memory_command, parse_memory_command


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


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    # Patch the module-level default so the real write-path wiring runs with a
    # deterministic, network-free embedder.
    monkeypatch.setattr(embeddings, "_default_embed", lambda _t: [0.25] * 768)


def _ctx(query: str):
    from agents.query_handlers import QueryContext
    return QueryContext(room_uuid=uuid4(), query=query, payload={}, agent_uuid=uuid4())


def _cleanup_text(*texts: str):
    db.db.session.query(MemoryClaim).filter(MemoryClaim.text.in_(texts)).delete(
        synchronize_session=False
    )
    db.db.session.commit()


def test_remember_command_embeds_the_active_claim(app_ctx):
    text = f"freshness remember {uuid4()}"
    cmd = parse_memory_command(f"remember that {text}")
    assert cmd is not None
    try:
        handle_memory_command(_ctx(f"remember that {text}"), cmd)
        claim = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).one()
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is not None
    finally:
        _cleanup_text(text)


def test_forget_command_prunes_the_embedding(app_ctx):
    text = f"freshness forget {uuid4()}"
    try:
        handle_memory_command(_ctx(f"remember that {text}"), parse_memory_command(f"remember that {text}"))
        claim = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).one()
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is not None
        handle_memory_command(_ctx(f"forget that {text}"), parse_memory_command(f"forget that {text}"))
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup_text(text)


def test_correct_command_embeds_new_and_prunes_old(app_ctx):
    old = f"freshness old {uuid4()}"
    new = f"freshness new {uuid4()}"
    try:
        handle_memory_command(_ctx(f"remember that {old}"), parse_memory_command(f"remember that {old}"))
        old_claim = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == old).one()
        handle_memory_command(
            _ctx(f"correct that {old} -> {new}"),
            parse_memory_command(f"correct that {old} -> {new}"),
        )
        new_claim = db.db.session.query(MemoryClaim).filter(MemoryClaim.text == new).one()
        assert db.get_memory_embedding(new_claim.uuid, EMBED_MODEL_NAME) is not None
        assert db.get_memory_embedding(old_claim.uuid, EMBED_MODEL_NAME) is None
    finally:
        _cleanup_text(old, new)


def test_assistant_activate_memory_embeds_the_claim(app_ctx):
    room_uuid = uuid4()
    agent_uuid = uuid4()
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=room_uuid, agent_uuid=agent_uuid, step_index=0
    )
    text = f"freshness activate {uuid4()}"
    try:
        obs = _action_remember(ctx, {"text": text})
        memory_uuid = obs.data["memory_uuid"]
        claim = db.get_memory_claim(UUID(memory_uuid))
        # Candidate: not embedded yet.
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is None
        _action_activate_memory(ctx, {"memory_uuid": memory_uuid})
        assert db.get_memory_embedding(claim.uuid, EMBED_MODEL_NAME) is not None
    finally:
        db.db.session.query(MemoryClaim).filter(MemoryClaim.text == text).delete(
            synchronize_session=False
        )
        db.db.session.commit()
