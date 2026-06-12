"""Integration: a memory command routed through QueryAgent.handle
creates a memory row; a non-memory query bypasses the memory dispatcher
and reaches the existing Q&A path.

No LM Studio dependency: the Q&A path is exercised via monkeypatched
internals so the test stays deterministic.
"""

from uuid import uuid4

import pytest

import db
import query_agent
from agent_config import QUERY_UUID
from db import MemoryClaim


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def room_with_human(app_ctx):
    human = db.get_human_user()
    assert human is not None
    name = f"memops-test-{uuid4().hex[:8]}"
    room = db.create_chatroom(name, human.uuid, [QUERY_UUID])
    try:
        yield room.uuid, human.uuid
    finally:
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid
        ).delete()
        db.db.session.commit()


def _stub_query_internals(monkeypatch):
    """Make the Q&A path a no-op so handle() doesn't hit LM Studio."""
    monkeypatch.setattr(query_agent, "_load_kb", lambda: None)
    monkeypatch.setattr(query_agent, "_vector_store", lambda: None)
    monkeypatch.setattr(query_agent, "_ensure_populated", lambda vs: None)
    monkeypatch.setattr(query_agent, "_exact_match", lambda q: None)
    monkeypatch.setattr(query_agent, "_semantic_match", lambda q, vs: None)


def test_remember_command_via_query_agent_handle_creates_memory(
    room_with_human, monkeypatch
):
    room_uuid, human_uuid = room_with_human
    _stub_query_internals(monkeypatch)

    text = f"the special fact is {uuid4().hex[:8]}"
    msg = db.post_chat_message(
        room_uuid, human_uuid, f"remember that {text}",
    )
    agent = query_agent.QueryAgent(
        agent_uuid=QUERY_UUID, name="query", send=lambda _: None,
    )
    try:
        agent.handle(journal_id=0, payload={
            "room_uuid": str(room_uuid),
            "message_uuid": str(msg.uuid),
        })
        rows = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == text)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "active"
    finally:
        # Cleanup: delete the memory + cascade evidence.
        db.db.session.query(MemoryClaim).filter(
            MemoryClaim.text == text
        ).delete()
        db.db.session.commit()


def test_non_memory_query_falls_through_to_qa_path(
    room_with_human, monkeypatch
):
    room_uuid, human_uuid = room_with_human
    calls: list[str] = []

    def fake_exact(q: str):
        calls.append(q)
        return None

    monkeypatch.setattr(query_agent, "_load_kb", lambda: None)
    monkeypatch.setattr(query_agent, "_vector_store", lambda: None)
    monkeypatch.setattr(query_agent, "_ensure_populated", lambda vs: None)
    monkeypatch.setattr(query_agent, "_exact_match", fake_exact)
    monkeypatch.setattr(query_agent, "_semantic_match", lambda q, vs: None)

    msg = db.post_chat_message(room_uuid, human_uuid, "what is 2 + 2?")
    agent = query_agent.QueryAgent(
        agent_uuid=QUERY_UUID, name="query", send=lambda _: None,
    )
    agent.handle(journal_id=0, payload={
        "room_uuid": str(room_uuid),
        "message_uuid": str(msg.uuid),
    })

    # The non-memory query reached the Q&A path: _exact_match was called.
    assert calls == ["what is 2 + 2?"], (
        "non-memory query should have fallen through to _exact_match"
    )


def test_remember_command_does_not_initialize_qa_path(
    room_with_human, monkeypatch
):
    """Memory commands must not depend on `_load_kb` / `_vector_store` /
    `_ensure_populated`. If the Q&A KB is broken, a `remember that …`
    command must still succeed."""
    room_uuid, human_uuid = room_with_human

    def boom_load_kb():
        raise AssertionError("_load_kb was called for a memory command")

    def boom_vector_store():
        raise AssertionError("_vector_store was called for a memory command")

    def boom_ensure_populated(vs):
        raise AssertionError("_ensure_populated was called for a memory command")

    monkeypatch.setattr(query_agent, "_load_kb", boom_load_kb)
    monkeypatch.setattr(query_agent, "_vector_store", boom_vector_store)
    monkeypatch.setattr(query_agent, "_ensure_populated", boom_ensure_populated)

    text = f"qa-bypass fact {uuid4().hex[:8]}"
    msg = db.post_chat_message(
        room_uuid, human_uuid, f"remember that {text}",
    )
    agent = query_agent.QueryAgent(
        agent_uuid=QUERY_UUID, name="query", send=lambda _: None,
    )
    try:
        agent.handle(journal_id=0, payload={
            "room_uuid": str(room_uuid),
            "message_uuid": str(msg.uuid),
        })
        rows = (
            db.db.session.query(MemoryClaim)
            .filter(MemoryClaim.text == text)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "active"
    finally:
        db.db.session.query(MemoryClaim).filter(
            MemoryClaim.text == text
        ).delete()
        db.db.session.commit()
