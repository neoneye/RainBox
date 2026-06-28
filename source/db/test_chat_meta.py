"""chat_message.meta carries structured attachments (e.g. a write proposal)."""

from uuid import uuid4

import pytest

import db


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


def _room():
    human = db.get_human_user()
    return db.create_chatroom(f"meta-{uuid4().hex[:8]}", human.uuid, [])


def test_post_chat_message_persists_meta(app_ctx):
    room = _room()
    sender = db.get_human_user()
    meta = {"write_intent": str(uuid4()), "capability": "set_reminder"}
    msg = db.post_chat_message(room.uuid, sender.uuid, "hi", meta=meta)
    fetched = db.db.session.get(db.ChatMessage, msg.id)
    assert fetched.meta == meta


def test_post_chat_message_meta_defaults_empty(app_ctx):
    room = _room()
    sender = db.get_human_user()
    msg = db.post_chat_message(room.uuid, sender.uuid, "hi")
    fetched = db.db.session.get(db.ChatMessage, msg.id)
    assert fetched.meta == {}
