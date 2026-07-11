"""DB tests for the streaming chat primitives: post_chat_message(streaming=True),
update_chat_message (in-place text/flag), get_room_message, and the pure NOTIFY
payload builder. Hits the live Postgres; each test tears down its room + agent."""

from uuid import uuid4

import pytest

import db
from db import (
    CHAT_NOTIFY_MAX_TEXT,
    _chat_event_payload,
    get_room_message,
    post_chat_message,
    update_chat_message,
)


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


def _room_with_agent(human_uuid):
    agent_uuid = uuid4()
    db.db.session.add(
        db.ChatUser(uuid=agent_uuid, name=f"stream-{uuid4().hex[:6]}", user_type="agent")
    )
    db.db.session.flush()
    room = db.create_chatroom(f"stream-{uuid4().hex[:6]}", human_uuid, [agent_uuid])
    return room.uuid, agent_uuid


def _cleanup(room_uuid, agent_uuid):
    db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
    db.db.session.query(db.ChatUser).filter(db.ChatUser.uuid == agent_uuid).delete()
    db.db.session.commit()


def test_streaming_lifecycle_updates_in_place(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_agent(human.uuid)
    try:
        msg = post_chat_message(
            room_uuid, agent_uuid, "", kind="message", streaming=True
        )
        assert msg.streaming is True
        assert msg.text == ""

        update_chat_message(msg.id, "partial", streaming=True)
        row = get_room_message(room_uuid, msg.id)
        assert row is not None
        assert row["text"] == "partial"
        assert row["streaming"] is True

        update_chat_message(msg.id, "final answer", streaming=False)
        row = get_room_message(room_uuid, msg.id)
        assert row["text"] == "final answer"
        assert row["streaming"] is False
    finally:
        _cleanup(room_uuid, agent_uuid)


def test_get_room_message_wrong_room_is_none(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room_uuid, agent_uuid = _room_with_agent(human.uuid)
    try:
        msg = post_chat_message(room_uuid, agent_uuid, "hi")
        assert get_room_message(uuid4(), msg.id) is None
    finally:
        _cleanup(room_uuid, agent_uuid)


def test_update_missing_message_is_noop(app_ctx):
    # No row with this id — must not raise.
    update_chat_message(2_000_000_000, "x", streaming=False)


def test_payload_inlines_small_text_and_omits_large():
    room = uuid4()
    small = _chat_event_payload(
        room_uuid=room, message_id=1, kind="message", streaming=True, text="hello"
    )
    assert small["streaming"] is True
    assert small["kind"] == "message"
    assert small["text"] == "hello"

    big_text = "x" * (CHAT_NOTIFY_MAX_TEXT + 1)
    big = _chat_event_payload(
        room_uuid=room, message_id=1, kind="thinking", streaming=True, text=big_text
    )
    assert big["streaming"] is True
    assert "text" not in big  # omitted -> browser refetches by id


def test_payload_measures_json_encoded_size_not_raw_bytes():
    """Regression: unicode-heavy text (box-drawing art) doubles in size when
    json.dumps escapes it to \\uXXXX; a raw-bytes check let such payloads
    exceed Postgres's 8000-byte NOTIFY cap and crash the agent supervisor
    ('payload string too long'). Each '╔' is 3 UTF-8 bytes but 6 JSON bytes."""
    import json
    n = (CHAT_NOTIFY_MAX_TEXT // 3) - 100          # raw bytes UNDER the cap...
    art = "\N{BOX DRAWINGS DOUBLE DOWN AND RIGHT}" * n
    assert len(art.encode("utf-8")) <= CHAT_NOTIFY_MAX_TEXT
    assert len(json.dumps(art)) > CHAT_NOTIFY_MAX_TEXT  # ...but over it encoded
    p = _chat_event_payload(
        room_uuid=uuid4(), message_id=1, kind="thinking", streaming=True, text=art
    )
    assert "text" not in p  # omitted -> browser refetches by id


def test_payload_plain_insert_has_no_streaming_keys():
    p = _chat_event_payload(
        room_uuid=uuid4(), message_id=5, deleted_progress_ids=[1, 2]
    )
    assert p["deleted_progress_ids"] == [1, 2]
    assert "streaming" not in p
    assert "kind" not in p
    assert "text" not in p
