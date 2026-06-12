"""Tests for the feedback_event table and helpers.

Live local Postgres. Each test creates rows and cleans them up via the
`room_uuid` tag or the `subject` tag where appropriate.
"""

import json
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db import ChatMessage, FeedbackEvent


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
def room_with_agent(app_ctx):
    """Create a fresh chatroom + a synthetic agent user. Caller tears the
    chatroom down (cascades to chat_message + chatroom_member); the agent
    ChatUser is deleted explicitly."""
    human = db.get_human_user()
    assert human is not None
    agent_uuid = uuid4()
    agent_user = db.ChatUser(
        uuid=agent_uuid, name=f"fb-test-{uuid4().hex[:6]}", user_type="agent",
    )
    db.db.session.add(agent_user)
    db.db.session.flush()
    room = db.create_chatroom(
        f"fb-{uuid4().hex[:6]}", human.uuid, [agent_uuid],
    )
    try:
        yield room.uuid, human.uuid, agent_uuid
    finally:
        # Feedback rows for this room must be torn down first since they
        # reference message_uuid but have no FK cascade.
        db.db.session.query(FeedbackEvent).filter(
            FeedbackEvent.room_uuid == room.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == room.uuid
        ).delete()
        db.db.session.query(db.ChatUser).filter(
            db.ChatUser.uuid == agent_uuid
        ).delete()
        db.db.session.commit()


def test_upvote_persists_feedback_event(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "ping")
    reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="upvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    assert reloaded is not None
    assert reloaded.rating == "upvote"
    assert reloaded.comment is None
    assert reloaded.message_uuid == reply.uuid
    assert reloaded.agent_uuid == agent_uuid
    assert reloaded.created_by_uuid == human_uuid
    assert reloaded.created_at is not None


def test_downvote_with_comment_persists_the_comment(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "ping")
    reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="downvote",
        comment="answer felt unrelated",
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    assert reloaded is not None
    assert reloaded.rating == "downvote"
    assert reloaded.comment == "answer felt unrelated"


def test_invalid_rating_is_rejected_by_db_constraint(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "ping")
    reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
    with pytest.raises(sa.exc.IntegrityError):
        db.create_feedback_event(
            room_uuid=room_uuid,
            message_uuid=reply.uuid,
            agent_uuid=agent_uuid,
            rating="meh",
            comment=None,
            created_by_uuid=human_uuid,
        )
    db.db.session.rollback()


def test_metadata_includes_rated_message_text(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "what is the answer?")
    reply = db.post_chat_message(
        room_uuid, agent_uuid, "the answer is forty two",
    )
    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="upvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    assert reloaded is not None
    meta = reloaded.metadata_
    assert meta["rated_message_text"] == "the answer is forty two"
    assert meta["rated_message_content_type"] == "markdown"


def test_metadata_includes_previous_human_message_text(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    prev = db.post_chat_message(room_uuid, human_uuid, "what is the answer?")
    reply = db.post_chat_message(room_uuid, agent_uuid, "forty two")
    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="downvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    meta = reloaded.metadata_
    assert meta["prev_human_message_uuid"] == str(prev.uuid)
    assert meta["prev_human_message_text"] == "what is the answer?"


def test_metadata_includes_latest_prior_debug_memory_payload(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "ping")
    debug_payload = {
        "query": "ping",
        "memories": [
            {"memory_uuid": "abc-123", "reason": "token_overlap",
             "confidence": 1.0, "provenance": ["confirmed_by_user"]},
        ],
    }
    db.post_chat_message(
        room_uuid, agent_uuid,
        json.dumps(debug_payload, ensure_ascii=False, separators=(",", ":")),
        "json", kind="debug-memory",
    )
    reply = db.post_chat_message(room_uuid, agent_uuid, "pong")

    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="upvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    meta = reloaded.metadata_
    assert meta["debug_memory"] is not None
    assert meta["debug_memory"]["query"] == "ping"
    assert meta["debug_memory"]["memories"][0]["memory_uuid"] == "abc-123"


def test_metadata_includes_latest_prior_debug_query_payload(app_ctx, room_with_agent):
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "what is x?")
    debug_payload = {
        "query": "what is x?",
        "match": {"qa_id": "qa-xyz", "method": "exact", "score": 1.0},
    }
    db.post_chat_message(
        room_uuid, agent_uuid,
        json.dumps(debug_payload, ensure_ascii=False, separators=(",", ":")),
        "json", kind="debug-query",
    )
    reply = db.post_chat_message(room_uuid, agent_uuid, "x is foo")

    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="upvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    meta = reloaded.metadata_
    assert meta["debug_query"] is not None
    assert meta["debug_query"]["match"]["qa_id"] == "qa-xyz"


def test_metadata_omits_optional_snapshots_when_absent(app_ctx, room_with_agent):
    """No prior human msg, no debug rows — metadata still serializes
    cleanly with None for those keys."""
    room_uuid, human_uuid, agent_uuid = room_with_agent
    reply = db.post_chat_message(room_uuid, agent_uuid, "hello first")
    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="upvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    meta = reloaded.metadata_
    assert meta["rated_message_text"] == "hello first"
    assert meta["prev_human_message_uuid"] is None
    assert meta["prev_human_message_text"] is None
    assert meta["debug_memory"] is None
    assert meta["debug_query"] is None


# --------------------------------------------------------------------------
# WP07 Finding 3: diagnostic snapshot must be scoped to the rated turn.
# --------------------------------------------------------------------------


def _cleanup_feedback_room(room_uuid, extra_user_uuids=()):
    db.db.session.query(FeedbackEvent).filter(
        FeedbackEvent.room_uuid == room_uuid
    ).delete()
    db.db.session.query(db.Chatroom).filter(
        db.Chatroom.uuid == room_uuid
    ).delete()
    for u in extra_user_uuids:
        db.db.session.query(db.ChatUser).filter(
            db.ChatUser.uuid == u
        ).delete()
    db.db.session.commit()


def test_feedback_metadata_captures_same_turn_debug_memory(
    app_ctx, room_with_agent,
):
    """WP07 scenario 1: a debug-memory posted in the same turn as the
    rated reply IS captured in feedback metadata (sanity check)."""
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "question")
    mem_uuid = uuid4()
    debug_memory_msg = db.post_chat_message(
        room_uuid, agent_uuid,
        json.dumps({
            "memories": [{"memory_uuid": str(mem_uuid), "reason": "r"}],
        }),
        "json", kind="debug-memory",
    )
    reply = db.post_chat_message(room_uuid, agent_uuid, "reply")
    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="downvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    meta = reloaded.metadata_
    assert meta["debug_memory"] is not None
    assert meta["debug_memory_message_uuid"] == str(debug_memory_msg.uuid)
    assert (
        meta["debug_memory"]["memories"][0]["memory_uuid"] == str(mem_uuid)
    )


def test_feedback_metadata_ignores_stale_debug_memory(
    app_ctx, room_with_agent,
):
    """WP07 scenario 2 (THE bug): a debug-memory from the previous
    turn must NOT attach to feedback on a no-memory turn.

    Turn structure:
      1. human: question A
      2. agent: debug-memory (memory M)
      3. agent: reply to A
      4. human: question B
      5. agent: reply to B (no debug-memory)
      6. downvote reply-to-B
    """
    room_uuid, human_uuid, agent_uuid = room_with_agent
    db.post_chat_message(room_uuid, human_uuid, "A")
    stale_mem = uuid4()
    db.post_chat_message(
        room_uuid, agent_uuid,
        json.dumps({
            "memories": [{"memory_uuid": str(stale_mem), "reason": "r"}],
        }),
        "json", kind="debug-memory",
    )
    db.post_chat_message(room_uuid, agent_uuid, "reply-A")
    db.post_chat_message(room_uuid, human_uuid, "B")
    reply = db.post_chat_message(room_uuid, agent_uuid, "reply-B")

    fb = db.create_feedback_event(
        room_uuid=room_uuid,
        message_uuid=reply.uuid,
        agent_uuid=agent_uuid,
        rating="downvote",
        comment=None,
        created_by_uuid=human_uuid,
    )
    db.db.session.expire_all()
    reloaded = db.get_feedback_event(fb.uuid)
    meta = reloaded.metadata_
    dbg = meta.get("debug_memory")
    leaked = False
    if dbg:
        if str(stale_mem) in (dbg.get("_raw", "") or ""):
            leaked = True
        for m in (dbg.get("memories") or []):
            if m.get("memory_uuid") == str(stale_mem):
                leaked = True
    assert not leaked, f"stale memory leaked into metadata: {dbg!r}"
    assert meta.get("debug_memory_message_uuid") is None


def test_feedback_metadata_ignores_other_agent_debug_memory(app_ctx):
    """WP07 scenario 3: a debug-memory written by agent A must NOT
    attach to feedback on agent B's reply, even within the same turn."""
    human = db.get_human_user()
    assert human is not None
    agent_a_uuid = uuid4()
    agent_b_uuid = uuid4()
    tag = uuid4().hex[:6]
    db.db.session.add(db.ChatUser(
        uuid=agent_a_uuid, name=f"fb-a-{tag}", user_type="agent",
    ))
    db.db.session.add(db.ChatUser(
        uuid=agent_b_uuid, name=f"fb-b-{tag}", user_type="agent",
    ))
    db.db.session.flush()
    room = db.create_chatroom(
        f"fb-multi-{tag}", human.uuid, [agent_a_uuid, agent_b_uuid],
    )
    try:
        db.post_chat_message(room.uuid, human.uuid, "q")
        other_agent_mem = uuid4()
        db.post_chat_message(
            room.uuid, agent_a_uuid,
            json.dumps({
                "memories": [
                    {"memory_uuid": str(other_agent_mem), "reason": "r"},
                ],
            }),
            "json", kind="debug-memory",
        )
        rated = db.post_chat_message(
            room.uuid, agent_b_uuid, "reply-from-b",
        )
        fb = db.create_feedback_event(
            room_uuid=room.uuid,
            message_uuid=rated.uuid,
            agent_uuid=agent_b_uuid,
            rating="downvote",
            comment=None,
            created_by_uuid=human.uuid,
        )
        db.db.session.expire_all()
        reloaded = db.get_feedback_event(fb.uuid)
        meta = reloaded.metadata_
        dbg = meta.get("debug_memory")
        leaked = False
        if dbg:
            if str(other_agent_mem) in (dbg.get("_raw", "") or ""):
                leaked = True
            for m in (dbg.get("memories") or []):
                if m.get("memory_uuid") == str(other_agent_mem):
                    leaked = True
        assert not leaked, (
            f"other-agent's diagnostic leaked: {dbg!r}"
        )
        assert meta.get("debug_memory_message_uuid") is None
    finally:
        _cleanup_feedback_room(
            room.uuid, extra_user_uuids=[agent_a_uuid, agent_b_uuid],
        )
