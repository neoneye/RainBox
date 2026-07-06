"""The assistant's 'working on it' progress bubble is posted at enqueue time (the
moment a human message triggers it) so it appears immediately, before the agent
process has spawned and imported its stack."""
from uuid import uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE
from webapp.chat_api import _maybe_trigger_chat_agents


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield
    finally:
        db.db.session.rollback()
        ctx.pop()


def _assistant_progress_rows(room_uuid):
    return [
        m for m in db.list_room_messages(room_uuid)
        if m["kind"] == "progress" and m["sender_uuid"] == str(ASSISTANT_UUID)
    ]


def test_enqueue_posts_working_notice_for_assistant(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"wn-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    try:
        msg = db.post_chat_message(room.uuid, human.uuid, "hi")
        _maybe_trigger_chat_agents(room.uuid, human.uuid, msg.uuid)
        rows = _assistant_progress_rows(room.uuid)
        assert len(rows) == 1
        assert rows[0]["text"] == ASSISTANT_WORKING_NOTICE
    finally:
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == room.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()
