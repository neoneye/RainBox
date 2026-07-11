"""Tests for DirectChatAgent (agents/direct_chat.py): message-list building,
the no-model notice path, and the handle() flow with a stubbed stream."""

from uuid import uuid4

import pytest
from llama_index.core.llms import MessageRole

import db
from agents.config import DIRECT_CHAT_UUID
from agents.direct_chat import NO_MODEL_NOTICE, DirectChatAgent
from db import Chatroom


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
def direct_room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(
        f"direct-agent-{uuid4().hex[:6]}", human.uuid, [DIRECT_CHAT_UUID],
        room_type="direct",
    )
    try:
        yield room.uuid, human.uuid
    finally:
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def _agent() -> DirectChatAgent:
    return DirectChatAgent(
        agent_uuid=DIRECT_CHAT_UUID, name="direct_chat", send=lambda msg: None
    )


def test_build_messages_roles_and_system_prompt():
    history = [
        {"kind": "message", "sender_type": "human", "text": "hello"},
        {"kind": "thinking", "sender_type": "agent", "text": "hmm"},
        {"kind": "message", "sender_type": "agent", "text": "hi there"},
        {"kind": "notice", "sender_type": "agent", "text": "no model"},
        {"kind": "message", "sender_type": "human", "text": "how are you?"},
    ]
    messages = DirectChatAgent.build_messages("Be helpful.", history)
    assert [m.role for m in messages] == [
        MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert messages[0].content == "Be helpful."
    assert messages[-1].content == "how are you?"


def test_build_messages_blank_prompt_sends_no_system_message():
    history = [{"kind": "message", "sender_type": "human", "text": "hey"}]
    messages = DirectChatAgent.build_messages("   ", history)
    assert [m.role for m in messages] == [MessageRole.USER]


def test_handle_without_model_posts_notice(direct_room, monkeypatch):
    """No room model AND no global default -> friendly notice, no reply."""
    room_uuid, human_uuid = direct_room
    monkeypatch.setattr(db, "get_setting", lambda key: None)  # no chat.default_model
    db.post_chat_message(room_uuid, human_uuid, "anyone there?")
    result = _agent().handle(uuid4(), {"room_uuid": str(room_uuid)})
    assert result == {"ok": True, "notice": "no_model"}
    rows = db.list_room_messages(room_uuid)
    notice = [r for r in rows if r["kind"] == "notice"]
    assert len(notice) == 1
    assert notice[0]["text"] == NO_MODEL_NOTICE


def test_handle_without_room_model_uses_global_default(direct_room, monkeypatch):
    """A model-less room replies with the chat.default_model setting's model;
    the room row itself stays untouched (the default is not copied in)."""
    room_uuid, human_uuid = direct_room
    db.post_chat_message(room_uuid, human_uuid, "hello")
    default_uuid = uuid4()
    monkeypatch.setattr(
        db, "get_setting",
        lambda key: str(default_uuid) if key == "chat.default_model" else None,
    )
    monkeypatch.setattr(
        db, "resolved_model_kwargs", lambda target: ("lm_studio", "m", {})
    )
    agent = _agent()
    seen = {}

    def fake_stream(room, model, messages):
        seen["model"] = model
        return "stubbed reply"

    monkeypatch.setattr(agent, "_stream_reply", fake_stream)
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid)})
    assert result == {"ok": True, "reply_content": "stubbed reply"}
    assert seen["model"] == default_uuid
    assert db.get_chatroom(room_uuid).model_uuid is None


def test_handle_with_unresolvable_global_default_posts_notice(direct_room, monkeypatch):
    """A stale chat.default_model (model deleted since) degrades to the
    no-model notice instead of a failed journal."""
    room_uuid, human_uuid = direct_room
    monkeypatch.setattr(
        db, "get_setting",
        lambda key: str(uuid4()) if key == "chat.default_model" else None,
    )
    db.post_chat_message(room_uuid, human_uuid, "anyone there?")
    result = _agent().handle(uuid4(), {"room_uuid": str(room_uuid)})
    assert result == {"ok": True, "notice": "no_model"}


def test_handle_streams_full_history(direct_room, monkeypatch):
    room_uuid, human_uuid = direct_room
    db.post_chat_message(room_uuid, human_uuid, "first")
    db.post_chat_message(room_uuid, DIRECT_CHAT_UUID, "reply one")
    db.post_chat_message(room_uuid, human_uuid, "second")
    model_uuid = uuid4()
    db.set_chatroom_settings(
        room_uuid, system_prompt="Stay short.", model_uuid=model_uuid
    )
    agent = _agent()
    seen = {}

    def fake_stream(room, model, messages):
        seen["room"] = room
        seen["model"] = model
        seen["messages"] = messages
        return "stubbed reply"

    monkeypatch.setattr(agent, "_stream_reply", fake_stream)
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid)})
    assert result == {"ok": True, "reply_content": "stubbed reply"}
    assert seen["room"] == room_uuid
    assert seen["model"] == model_uuid
    roles = [m.role for m in seen["messages"]]
    assert roles == [
        MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert seen["messages"][0].content == "Stay short."


def test_handle_rejects_agents_room(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"not-direct-{uuid4().hex[:6]}", human.uuid, [])
    try:
        with pytest.raises(ValueError):
            _agent().handle(uuid4(), {"room_uuid": str(room.uuid)})
    finally:
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()
