"""Tests for the assistant's facts-invalidation marker: a one-time notice posted
into a room after a shield/Q&A change so the model re-checks facts instead of
reusing an earlier answer from the transcript."""
from uuid import uuid4

import pytest

import db
from agents.assistant import (
    AssistantAgent,
    FACTS_INVALIDATION_NOTICE,
    _demote_trailing_facts_marker,
)
from agents.config import ASSISTANT_UUID


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


def _markers(room_uuid):
    return [m for m in db.list_room_messages(room_uuid)
            if (m.get("meta") or {}).get("facts_invalidation")]


def _cleanup(room_uuid):
    db.db.session.query(db.ChatMessage).filter(db.ChatMessage.room_uuid == room_uuid).delete()
    db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
    db.set_setting("qa.facts_invalidated_at", None)
    db.db.session.commit()


def test_maybe_post_facts_marker_posts_once_then_dedups(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"fm-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    try:
        db.post_chat_message(room.uuid, human.uuid, "hi")
        db.set_setting("qa.facts_invalidated_at", None)
        db.db.session.commit()

        # Unset -> no marker.
        agent._maybe_post_facts_marker(room.uuid)
        assert _markers(room.uuid) == []

        # Stamped -> exactly one marker, kind=message, meta carries the stamp.
        stamp = db.mark_facts_invalidated()
        db.db.session.commit()
        agent._maybe_post_facts_marker(room.uuid)
        marks = _markers(room.uuid)
        assert len(marks) == 1
        assert marks[0]["kind"] == "message"
        assert marks[0]["meta"]["facts_invalidation"] == stamp
        assert marks[0]["text"] == FACTS_INVALIDATION_NOTICE

        # Same stamp again -> dedup, still one.
        agent._maybe_post_facts_marker(room.uuid)
        assert len(_markers(room.uuid)) == 1

        # A new invalidation -> a new marker.
        stamp2 = db.mark_facts_invalidated()
        db.db.session.commit()
        assert stamp2 != stamp
        agent._maybe_post_facts_marker(room.uuid)
        assert len(_markers(room.uuid)) == 2
    finally:
        _cleanup(room.uuid)


def test_demote_trailing_facts_marker_keeps_operator_message_current():
    """A marker posted after the operator's message is the newest row; it must be
    moved into history so the operator's message stays the Current message."""
    user = {"sender_type": "human", "text": "what is X?", "kind": "message", "meta": {}}
    marker = {"sender_type": "agent", "text": "notice", "kind": "message",
              "meta": {"facts_invalidation": "2026-07-06T00:00:00+00:00"}}
    out = _demote_trailing_facts_marker([user, marker])
    assert out[-1] is user           # operator message is Current
    assert out[-2] is marker         # marker demoted into history


def test_demote_trailing_facts_marker_noop_without_trailing_marker():
    a = {"sender_type": "human", "text": "a", "kind": "message", "meta": {}}
    b = {"sender_type": "agent", "text": "b", "kind": "message", "meta": {}}
    assert _demote_trailing_facts_marker([a, b]) == [a, b]
