"""Tests for the chat progress-message mechanism in db.py.

Uses the live local Postgres database (db.psycopg_dsn()). Every test
cleans up rows it created so artifacts don't accumulate.
"""

import json
from uuid import UUID, uuid4

import psycopg
import pytest

import db
from db import ChatMessage, ChatUser, Chatroom


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
def room_with_two_agents(app_ctx):
    """Create a fresh room with one human + two agents. Returns
    (room_uuid, human_uuid, agent_a_uuid, agent_b_uuid). Tears the
    whole room (and its messages + members, via CASCADE) down at end."""
    human = db.get_human_user()
    assert human is not None, "seed_chat_defaults should have run"
    agent_a = ChatUser(uuid=uuid4(), name=f"prog-test-a-{uuid4().hex[:6]}", user_type="agent")
    agent_b = ChatUser(uuid=uuid4(), name=f"prog-test-b-{uuid4().hex[:6]}", user_type="agent")
    db.db.session.add_all([agent_a, agent_b])
    db.db.session.flush()
    room = db.create_chatroom(
        f"prog-test-{uuid4().hex[:6]}", human.uuid, [agent_a.uuid, agent_b.uuid]
    )
    try:
        yield room.uuid, human.uuid, agent_a.uuid, agent_b.uuid
    finally:
        # ChatroomMember + ChatMessage rows cascade via FK ondelete=CASCADE on
        # chatroom.uuid; ChatUser rows we added need explicit cleanup.
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.query(ChatUser).filter(
            ChatUser.uuid.in_([agent_a.uuid, agent_b.uuid])
        ).delete()
        db.db.session.commit()


def _progress_rows_for(room_uuid: UUID, sender_uuid: UUID) -> list[ChatMessage]:
    return (
        db.db.session.query(ChatMessage)
        .filter(
            ChatMessage.room_uuid == room_uuid,
            ChatMessage.sender_uuid == sender_uuid,
            ChatMessage.kind == "progress",
        )
        .order_by(ChatMessage.id.asc())
        .all()
    )


def test_post_progress_inserts_kind_progress_row(room_with_two_agents):
    room_uuid, _human, agent_a, _agent_b = room_with_two_agents
    msg = db.post_progress(room_uuid, agent_a, "thinking")
    assert msg.kind == "progress"
    assert msg.text == "thinking"
    assert msg.sender_uuid == agent_a
    rows = _progress_rows_for(room_uuid, agent_a)
    assert [r.id for r in rows] == [msg.id]


def test_post_message_clears_same_senders_progress(room_with_two_agents):
    room_uuid, _human, agent_a, _agent_b = room_with_two_agents
    db.post_progress(room_uuid, agent_a, "thinking")
    db.post_progress(room_uuid, agent_a, "step 1 of 3")
    db.post_progress(room_uuid, agent_a, "step 2 of 3")
    assert len(_progress_rows_for(room_uuid, agent_a)) == 3

    reply = db.post_chat_message(room_uuid, agent_a, "here is the answer")
    assert reply.kind == "message"
    assert _progress_rows_for(room_uuid, agent_a) == []


def test_other_senders_progress_is_preserved(room_with_two_agents):
    room_uuid, _human, agent_a, agent_b = room_with_two_agents
    db.post_progress(room_uuid, agent_a, "A is thinking")
    db.post_progress(room_uuid, agent_b, "B is thinking")

    db.post_chat_message(room_uuid, agent_a, "A replies")

    assert _progress_rows_for(room_uuid, agent_a) == []
    rows_b = _progress_rows_for(room_uuid, agent_b)
    assert len(rows_b) == 1 and rows_b[0].text == "B is thinking"


def test_non_message_kind_does_not_clear_progress(room_with_two_agents):
    room_uuid, _human, agent_a, _agent_b = room_with_two_agents
    db.post_progress(room_uuid, agent_a, "thinking")
    # A diagnostic emission (e.g. "thinking" or "debug-router") must not
    # accidentally clear the in-flight progress trail.
    db.post_chat_message(room_uuid, agent_a, "diagnostic", kind="thinking")
    assert len(_progress_rows_for(room_uuid, agent_a)) == 1


def _listen_for_chat_notify(channel: str) -> psycopg.Connection:
    """Open an autocommit connection listening to `channel`. Caller must
    close it. Uses the same DSN as the app under test."""
    conn = psycopg.connect(db.psycopg_dsn(), autocommit=True)
    # Channel is a fixed internal constant, safe to interpolate (LISTEN takes
    # an identifier, which can't be a bind parameter). Matches webapp/chat_api.py.
    conn.execute(f"LISTEN {channel}")
    return conn


def _collect_notifies(conn: psycopg.Connection, count: int, timeout: float = 2.0):
    """Block in the kernel on conn.notifies(timeout=...) until `count`
    payloads arrive or `timeout` elapses. Returns parsed payloads in
    arrival order."""
    out = []
    for note in conn.notifies(timeout=timeout):
        out.append(json.loads(note.payload))
        if len(out) >= count:
            break
    return out


def test_notify_payload_includes_deleted_progress_ids(room_with_two_agents):
    room_uuid, _human, agent_a, _agent_b = room_with_two_agents
    p1 = db.post_progress(room_uuid, agent_a, "thinking")
    p2 = db.post_progress(room_uuid, agent_a, "step 1 of 2")
    # Capture IDs now; post_chat_message will DELETE these rows, causing
    # SQLAlchemy lazy-loads of p1.id / p2.id to raise ObjectDeletedError.
    p1_id, p2_id = p1.id, p2.id

    conn = _listen_for_chat_notify(db.CHAT_NOTIFY_CHANNEL)
    try:
        reply = db.post_chat_message(room_uuid, agent_a, "done")
        notifies = _collect_notifies(conn, count=1)
    finally:
        conn.close()

    assert len(notifies) == 1
    payload = notifies[0]
    assert payload["room_uuid"] == str(room_uuid)
    assert payload["message_id"] == reply.id
    assert sorted(payload["deleted_progress_ids"]) == sorted([p1_id, p2_id])


def test_notify_payload_progress_post_has_empty_deleted_list(room_with_two_agents):
    room_uuid, _human, agent_a, _agent_b = room_with_two_agents

    conn = _listen_for_chat_notify(db.CHAT_NOTIFY_CHANNEL)
    try:
        prog = db.post_progress(room_uuid, agent_a, "thinking")
        notifies = _collect_notifies(conn, count=1)
    finally:
        conn.close()

    assert len(notifies) == 1
    payload = notifies[0]
    assert payload["message_id"] == prog.id
    assert payload["deleted_progress_ids"] == []
