"""Tests for the one-time repair of rooms built around the direct-chat
responder while it was still offered in the /chat member picker.

Uses the live local Postgres database. Every test cleans up rows it
created so artifacts don't accumulate.
"""

from uuid import uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID, DIRECT_CHAT_UUID
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
def make_room(app_ctx):
    """Builds rooms and drops them again, whatever the test did to them."""
    created = []

    def _make(members, room_type="agents"):
        human = db.get_human_user()
        assert human is not None
        room = db.create_chatroom(
            f"retype-test-{uuid4().hex[:6]}", human.uuid, members,
            room_type=room_type,
        )
        created.append(room.uuid)
        return room.uuid

    try:
        yield _make
    finally:
        db.session.query(Chatroom).filter(
            Chatroom.uuid.in_(created)
        ).delete(synchronize_session=False)
        db.session.commit()


def test_retypes_room_whose_only_agent_is_the_direct_chat_responder(make_room):
    room_uuid = make_room([DIRECT_CHAT_UUID])
    db._retype_direct_chat_only_rooms()
    assert db.get_chatroom(room_uuid).room_type == "direct"


def test_leaves_a_room_that_also_holds_a_real_responder(make_room):
    room_uuid = make_room([DIRECT_CHAT_UUID, ASSISTANT_UUID])
    db._retype_direct_chat_only_rooms()
    assert db.get_chatroom(room_uuid).room_type == "agents"


def test_leaves_an_ordinary_agents_room(make_room):
    room_uuid = make_room([ASSISTANT_UUID])
    db._retype_direct_chat_only_rooms()
    assert db.get_chatroom(room_uuid).room_type == "agents"


def test_leaves_an_agent_less_agents_room(make_room):
    """A room the operator hasn't added anyone to yet is still an agents room —
    the repair keys on the direct-chat responder being there, not on emptiness."""
    room_uuid = make_room([])
    db._retype_direct_chat_only_rooms()
    assert db.get_chatroom(room_uuid).room_type == "agents"


def test_is_idempotent(make_room):
    room_uuid = make_room([DIRECT_CHAT_UUID])
    db._retype_direct_chat_only_rooms()
    db._retype_direct_chat_only_rooms()
    assert db.get_chatroom(room_uuid).room_type == "direct"
