"""Tests for add_room_member / remove_room_member in db/chat.py.

Uses the live local Postgres database. Every test cleans up rows it
created so artifacts don't accumulate.
"""

from uuid import uuid4

import pytest

import db
from db import ChatUser, Chatroom


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
def room_with_one_agent(app_ctx):
    """Fresh room: human + agent_a as members, agent_b a non-member spare.
    Returns (room_uuid, human_uuid, agent_a_uuid, agent_b_uuid)."""
    human = db.get_human_user()
    assert human is not None, "seed_chat_defaults should have run"
    agent_a = ChatUser(uuid=uuid4(), name=f"mem-a-{uuid4().hex[:6]}", user_type="agent")
    agent_b = ChatUser(uuid=uuid4(), name=f"mem-b-{uuid4().hex[:6]}", user_type="agent")
    db.db.session.add_all([agent_a, agent_b])
    db.db.session.flush()
    room = db.create_chatroom(
        f"mem-test-{uuid4().hex[:6]}", human.uuid, [agent_a.uuid]
    )
    try:
        yield room.uuid, human.uuid, agent_a.uuid, agent_b.uuid
    finally:
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.query(ChatUser).filter(
            ChatUser.uuid.in_([agent_a.uuid, agent_b.uuid])
        ).delete()
        db.db.session.commit()


def _member_uuids(room_uuid):
    return set(db.get_room_member_uuids(room_uuid))


def test_add_room_member_adds_new_member(room_with_one_agent):
    room_uuid, _human, _agent_a, agent_b = room_with_one_agent
    assert agent_b not in _member_uuids(room_uuid)
    added = db.add_room_member(room_uuid, agent_b)
    assert added is True
    assert agent_b in _member_uuids(room_uuid)


def test_add_room_member_is_idempotent(room_with_one_agent):
    room_uuid, _human, agent_a, _agent_b = room_with_one_agent
    # agent_a is already a member.
    added = db.add_room_member(room_uuid, agent_a)
    assert added is False
    # Still exactly one membership row for agent_a (no duplicate).
    members = db.get_room_member_uuids(room_uuid)
    assert members.count(agent_a) == 1


def test_remove_room_member_removes_existing(room_with_one_agent):
    room_uuid, _human, agent_a, _agent_b = room_with_one_agent
    removed = db.remove_room_member(room_uuid, agent_a)
    assert removed is True
    assert agent_a not in _member_uuids(room_uuid)


def test_remove_room_member_absent_returns_false(room_with_one_agent):
    room_uuid, _human, _agent_a, agent_b = room_with_one_agent
    # agent_b was never added.
    removed = db.remove_room_member(room_uuid, agent_b)
    assert removed is False
