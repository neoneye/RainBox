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


from agents.config import ASSISTANT_UUID


def _run_and_step(room_uuid):
    run = db.start_assistant_run(journal_id=uuid4(), room_uuid=room_uuid,
                                 agent_uuid=ASSISTANT_UUID)
    step = db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed", action="set_reminder")
    return run, step


def test_list_room_messages_enriches_intent_state(app_ctx):
    room = _room()
    run, step = _run_and_step(room.uuid)
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="set_reminder",
        payload={"text": "t", "when": "2026-06-29T09:00"}, preview_text="p",
        room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID,
    )
    db.post_chat_message(
        room.uuid, ASSISTANT_UUID, "awaiting confirmation",
        meta={"write_intent": str(intent.uuid), "capability": "set_reminder",
              "step_link": db.assistant_step_path(run.uuid, step.uuid)},
    )
    msgs = db.list_room_messages(room.uuid)
    card = next(m for m in msgs if m["meta"].get("write_intent") == str(intent.uuid))
    assert card["meta"]["intent_state"] == "proposed"
    assert card["meta"]["step_link"] == db.assistant_step_path(run.uuid, step.uuid)

    # The state tracks a transition done elsewhere (e.g. on /assistant).
    db.set_write_intent_state(intent, "rejected")
    msgs2 = db.list_room_messages(room.uuid)
    card2 = next(m for m in msgs2 if m["meta"].get("write_intent") == str(intent.uuid))
    assert card2["meta"]["intent_state"] == "rejected"


def test_list_room_messages_meta_empty_for_plain_message(app_ctx):
    room = _room()
    db.post_chat_message(room.uuid, db.get_human_user().uuid, "plain")
    msg = db.list_room_messages(room.uuid)[-1]
    assert msg["meta"] == {} and "intent_state" not in msg["meta"]
