"""Test that QueryFilterRouterAgent honors memory commands before
touching the KB / LLM. Mirrors test_query_agent_memory_ops.py."""

from uuid import uuid4

import pytest

import db
from db import ChatMessage, ChatUser, Chatroom, MemoryClaim


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
def fresh_tag() -> str:
    return f"test-{uuid4().hex[:8]}"


def _make_room_with_human(prefix: str):
    user = ChatUser(uuid=uuid4(), name=f"{prefix}-human", user_type="human")
    db.db.session.add(user)
    db.db.session.flush()
    room = db.create_chatroom(
        name=f"{prefix}-room", created_by=user.uuid,
        member_uuids=[user.uuid],
    )
    return user, room


def _cleanup(prefix: str) -> None:
    room_uuids = [
        r.uuid for r in db.db.session.query(Chatroom)
        .filter(Chatroom.name.like(f"{prefix}%")).all()
    ]
    if room_uuids:
        db.db.session.query(ChatMessage).filter(
            ChatMessage.room_uuid.in_(room_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(Chatroom).filter(
            Chatroom.uuid.in_(room_uuids)
        ).delete(synchronize_session=False)
    db.db.session.query(ChatUser).filter(
        ChatUser.name.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.query(MemoryClaim).filter(
        MemoryClaim.text.like(f"%{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.commit()


def test_remember_command_via_filter_router_creates_memory(
    app_ctx, fresh_tag, monkeypatch,
):
    """The filter-router must short-circuit on memory commands and
    create a memory claim WITHOUT touching the KB / LLM stages."""
    from agents.query_filter_router import QueryFilterRouterAgent
    import agents.query_filter_router as qfr

    # Refuse any KB / LLM access — if the filter-router incorrectly
    # falls through, these will raise.
    def _bomb(*args, **kwargs):
        raise AssertionError(
            f"{fresh_tag}: KB/LLM path was reached for a memory command"
        )
    monkeypatch.setattr(qfr, "_load_kb", _bomb)
    monkeypatch.setattr(qfr, "_vector_store", _bomb)
    monkeypatch.setattr(qfr, "_ensure_populated", _bomb)

    user, room = _make_room_with_human(fresh_tag)
    db.post_chat_message(
        room_uuid=room.uuid, sender_uuid=user.uuid,
        text=f"remember that {fresh_tag}-subject likes pizza",
        kind="message",
    )

    agent = QueryFilterRouterAgent(
        agent_uuid=uuid4(),
        name="qfr-test",
        send=lambda *a, **kw: None,
    )
    try:
        result = agent.handle(journal_id=uuid4(), payload={
            "room_uuid": str(room.uuid),
        })
        assert result.get("ok") is True, result
        assert result.get("method") == "memory", result
        claims = db.db.session.query(MemoryClaim).filter(
            MemoryClaim.text.like(f"%{fresh_tag}%")
        ).all()
        assert len(claims) >= 1, claims
    finally:
        _cleanup(fresh_tag)


def test_non_memory_query_via_filter_router_falls_through(
    app_ctx, fresh_tag, monkeypatch,
):
    """A non-memory query must NOT short-circuit — it must reach the
    KB load path."""
    from agents.query_filter_router import QueryFilterRouterAgent
    import agents.query_filter_router as qfr

    called = {"load_kb": False}

    def fake_load_kb():
        called["load_kb"] = True
        # Once we know load_kb fired, raise to abort the rest of the
        # path (we don't have a real vector store in the test env).
        raise RuntimeError("test stop after load_kb fires")

    monkeypatch.setattr(qfr, "_load_kb", fake_load_kb)

    user, room = _make_room_with_human(fresh_tag)
    db.post_chat_message(
        room_uuid=room.uuid, sender_uuid=user.uuid,
        text=f"{fresh_tag} what is the meaning of life?",
        kind="message",
    )

    agent = QueryFilterRouterAgent(
        agent_uuid=uuid4(),
        name="qfr-test",
        send=lambda *a, **kw: None,
    )
    try:
        with pytest.raises(RuntimeError, match="test stop after load_kb"):
            agent.handle(journal_id=uuid4(), payload={
                "room_uuid": str(room.uuid),
            })
        assert called["load_kb"] is True, (
            "non-memory query should reach _load_kb()"
        )
    finally:
        _cleanup(fresh_tag)
