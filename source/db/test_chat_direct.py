"""Tests for the direct-room helpers in db/chat.py: room_type on
create_chatroom, set_chatroom_settings, and edit_chat_message.

Uses the live local Postgres database. Every test cleans up rows it
created so artifacts don't accumulate.
"""

from uuid import uuid4

import pytest

import db
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
    assert human is not None, "seed_chat_defaults should have run"
    room = db.create_chatroom(
        f"direct-test-{uuid4().hex[:6]}", human.uuid, [], room_type="direct"
    )
    try:
        yield room.uuid, human.uuid
    finally:
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


@pytest.fixture
def agents_room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(f"agents-test-{uuid4().hex[:6]}", human.uuid, [])
    try:
        yield room.uuid, human.uuid
    finally:
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def test_create_chatroom_defaults_to_agents(agents_room):
    room_uuid, _human = agents_room
    room = db.get_chatroom(room_uuid)
    assert room.room_type == "agents"
    assert room.system_prompt == ""
    assert room.model_uuid is None


def test_create_chatroom_direct(direct_room):
    room_uuid, _human = direct_room
    assert db.get_chatroom(room_uuid).room_type == "direct"


def test_create_chatroom_rejects_invalid_room_type(app_ctx):
    human = db.get_human_user()
    with pytest.raises(ValueError):
        db.create_chatroom("bad-type", human.uuid, [], room_type="bogus")


def test_list_chatrooms_carries_room_type_and_model(direct_room):
    room_uuid, _human = direct_room
    entry = next(r for r in db.list_chatrooms() if r["uuid"] == str(room_uuid))
    assert entry["room_type"] == "direct"
    assert entry["model_uuid"] is None


def test_set_chatroom_settings_roundtrip(direct_room):
    room_uuid, _human = direct_room
    model_uuid = uuid4()
    db.set_chatroom_settings(
        room_uuid, system_prompt="Be terse.", model_uuid=model_uuid
    )
    room = db.get_chatroom(room_uuid)
    assert room.system_prompt == "Be terse."
    assert room.model_uuid == model_uuid
    # Partial update: only the passed field changes.
    db.set_chatroom_settings(room_uuid, system_prompt="Be verbose.")
    room = db.get_chatroom(room_uuid)
    assert room.system_prompt == "Be verbose."
    assert room.model_uuid == model_uuid
    # model_uuid=None clears the model.
    db.set_chatroom_settings(room_uuid, model_uuid=None)
    assert db.get_chatroom(room_uuid).model_uuid is None


@pytest.fixture
def stored_prompt(app_ctx):
    """One /prompt row to link rooms to."""
    from db.models import Prompt
    row = Prompt(uuid=uuid4(), name="Pirate", content="You are a pirate.")
    db.db.session.add(row)
    db.db.session.commit()
    try:
        yield row.uuid
    finally:
        db.db.session.query(Prompt).filter(Prompt.uuid == row.uuid).delete()
        db.db.session.commit()


def test_set_chatroom_settings_prompt_link(direct_room, stored_prompt):
    room_uuid, _human = direct_room
    db.set_chatroom_settings(room_uuid, prompt_uuid=stored_prompt)
    assert db.get_chatroom(room_uuid).prompt_uuid == stored_prompt
    # Partial update elsewhere leaves the link alone.
    db.set_chatroom_settings(room_uuid, system_prompt="free text")
    assert db.get_chatroom(room_uuid).prompt_uuid == stored_prompt
    # prompt_uuid=None unlinks.
    db.set_chatroom_settings(room_uuid, prompt_uuid=None)
    assert db.get_chatroom(room_uuid).prompt_uuid is None


def test_resolve_room_system_prompt(direct_room, stored_prompt):
    room_uuid, _human = direct_room
    # Unlinked: the room's own free text.
    db.set_chatroom_settings(room_uuid, system_prompt="Be terse.")
    assert db.resolve_room_system_prompt(db.get_chatroom(room_uuid)) == "Be terse."
    # Linked: the stored version's content wins over the free text.
    db.set_chatroom_settings(room_uuid, prompt_uuid=stored_prompt)
    assert db.resolve_room_system_prompt(
        db.get_chatroom(room_uuid)) == "You are a pirate."
    # Linked version deleted: no system message (NOT the stale free text).
    from db.models import Prompt
    db.db.session.query(Prompt).filter(Prompt.uuid == stored_prompt).delete()
    db.db.session.commit()
    assert db.resolve_room_system_prompt(db.get_chatroom(room_uuid)) == ""


def test_set_chatroom_settings_rejects_agents_room(agents_room):
    room_uuid, _human = agents_room
    with pytest.raises(ValueError):
        db.set_chatroom_settings(room_uuid, system_prompt="nope")


def test_set_chatroom_settings_missing_room(app_ctx):
    with pytest.raises(LookupError):
        db.set_chatroom_settings(uuid4(), system_prompt="x")


def test_edit_chat_message_updates_text_and_content_type(direct_room):
    room_uuid, human_uuid = direct_room
    msg = db.post_chat_message(room_uuid, human_uuid, "hello", "markdown")
    db.edit_chat_message(msg.id, '{"now": "json"}')
    row = db.get_room_message(room_uuid, msg.id)
    assert row["text"] == '{"now": "json"}'
    assert row["content_type"] == "json"


def test_delete_chat_message_removes_row(direct_room):
    room_uuid, human_uuid = direct_room
    msg = db.post_chat_message(room_uuid, human_uuid, "delete me")
    keep = db.post_chat_message(room_uuid, human_uuid, "keep me")
    db.delete_chat_message(msg.id)
    ids = [r["id"] for r in db.list_room_messages(room_uuid)]
    assert msg.id not in ids
    assert keep.id in ids


def test_delete_chat_message_guards(direct_room):
    room_uuid, human_uuid = direct_room
    with pytest.raises(LookupError):
        db.delete_chat_message(-1)
    thinking = db.post_chat_message(room_uuid, human_uuid, "hmm", kind="thinking")
    with pytest.raises(ValueError):
        db.delete_chat_message(thinking.id)
    streaming = db.post_chat_message(room_uuid, human_uuid, "part", streaming=True)
    with pytest.raises(ValueError):
        db.delete_chat_message(streaming.id)


def test_edit_chat_message_guards(direct_room):
    room_uuid, human_uuid = direct_room
    with pytest.raises(LookupError):
        db.edit_chat_message(-1, "x")
    thinking = db.post_chat_message(
        room_uuid, human_uuid, "hmm", kind="thinking"
    )
    with pytest.raises(ValueError):
        db.edit_chat_message(thinking.id, "x")
    streaming = db.post_chat_message(
        room_uuid, human_uuid, "part", streaming=True
    )
    with pytest.raises(ValueError):
        db.edit_chat_message(streaming.id, "x")
