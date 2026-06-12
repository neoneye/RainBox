"""Tests for the dedicated cron events chatroom (db.py seeding + post_cron_event).

Uses the live local Postgres. The cron room/sender are part of normal seeding
(left in place); the only row a test creates — an event message — is torn down,
so the suite is non-destructive.
"""

import pytest
import sqlalchemy as sa

import db
from db import (
    CRON_ROOM_UUID,
    CRON_SYSTEM_UUID,
    ChatMessage,
    Chatroom,
    ChatroomMember,
    ChatUser,
)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)  # seeds the cron room + sender (idempotent)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_cron_room_and_sender_seeded(app_ctx):
    room = db.db.session.query(Chatroom).filter_by(uuid=CRON_ROOM_UUID).first()
    assert room is not None and room.name == "cron"

    sender = db.db.session.query(ChatUser).filter_by(uuid=CRON_SYSTEM_UUID).first()
    assert sender is not None and sender.user_type == "agent"

    # The cron sender is a member of the cron room.
    assert db.db.session.query(ChatroomMember).filter_by(
        room_uuid=CRON_ROOM_UUID, user_uuid=CRON_SYSTEM_UUID).count() == 1


def test_seed_chat_defaults_idempotent(app_ctx):
    """Re-seeding does not duplicate the cron room or its init message."""
    rooms_before = db.db.session.query(Chatroom).filter_by(uuid=CRON_ROOM_UUID).count()
    msgs_before = db.db.session.query(ChatMessage).filter_by(room_uuid=CRON_ROOM_UUID).count()
    db.seed_chat_defaults()
    assert db.db.session.query(Chatroom).filter_by(uuid=CRON_ROOM_UUID).count() == rooms_before == 1
    assert db.db.session.query(ChatMessage).filter_by(room_uuid=CRON_ROOM_UUID).count() == msgs_before


def test_post_cron_event(app_ctx):
    msg = db.post_cron_event("▶ test cron event line")
    try:
        assert msg is not None
        assert msg.room_uuid == CRON_ROOM_UUID       # lands in the cron room
        assert msg.sender_uuid == CRON_SYSTEM_UUID   # authored by the cron sender
        assert msg.text == "▶ test cron event line"
        # It's retrievable as a real row.
        assert db.db.session.query(ChatMessage).filter_by(uuid=msg.uuid).first() is not None
    finally:
        if msg is not None:
            db.db.session.execute(sa.delete(ChatMessage).where(ChatMessage.uuid == msg.uuid))
            db.db.session.commit()


