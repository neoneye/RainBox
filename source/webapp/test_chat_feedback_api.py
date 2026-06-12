"""HTTP API tests for POST /chat/api/messages/<message_uuid>/feedback."""

import json
from uuid import UUID, uuid4

import pytest

import db
from db import feedback as db_feedback
from db import FeedbackEvent


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    # Import webapp so the chat API routes register against this app.
    # webapp.core uses make_app() on import, so we attach a test client
    # to the same Flask app it built.
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


@pytest.fixture
def room_with_agent(client):
    """Set up a chatroom with a human + a synthetic agent. Tears down
    feedback rows and the chatroom (cascades to chat_message)."""
    _client, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        agent_uuid = uuid4()
        agent_user = db.ChatUser(
            uuid=agent_uuid, name=f"fb-api-{uuid4().hex[:6]}",
            user_type="agent",
        )
        db.db.session.add(agent_user)
        db.db.session.flush()
        room = db.create_chatroom(
            f"fb-api-{uuid4().hex[:6]}", human.uuid, [agent_uuid],
        )
        try:
            yield room.uuid, human.uuid, agent_uuid
        finally:
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


def _post_feedback(client, message_uuid, body):
    return client.post(
        f"/chat/api/messages/{message_uuid}/feedback",
        data=json.dumps(body),
        content_type="application/json",
    )


def test_feedback_can_be_posted_for_agent_message(client, room_with_agent):
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    resp = _post_feedback(
        flask_client, str(reply_uuid),
        {"rating": "upvote", "comment": ""},
    )
    assert resp.status_code == 201, resp.data
    body = resp.get_json()
    assert body["rating"] == "upvote"
    fb_uuid = UUID(body["uuid"])

    with app.app_context():
        fb = db.get_feedback_event(fb_uuid)
        assert fb is not None
        assert fb.rating == "upvote"
        assert fb.comment is None or fb.comment == ""
        assert fb.message_uuid == reply_uuid


def test_feedback_rejected_for_human_message(client, room_with_agent):
    flask_client, app = client
    room_uuid, human_uuid, _agent = room_with_agent
    with app.app_context():
        msg = db.post_chat_message(room_uuid, human_uuid, "ping")
        msg_uuid = msg.uuid

    resp = _post_feedback(
        flask_client, str(msg_uuid),
        {"rating": "upvote"},
    )
    assert resp.status_code == 400 or resp.status_code == 403


def test_feedback_rejected_for_diagnostic_row(client, room_with_agent):
    flask_client, app = client
    room_uuid, _human, agent_uuid = room_with_agent
    with app.app_context():
        msg = db.post_chat_message(
            room_uuid, agent_uuid,
            '{"memories":[]}', "json", kind="debug-memory",
        )
        msg_uuid = msg.uuid

    resp = _post_feedback(
        flask_client, str(msg_uuid),
        {"rating": "upvote"},
    )
    assert resp.status_code == 400 or resp.status_code == 403


def test_feedback_rejected_for_invalid_rating(client, room_with_agent):
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    resp = _post_feedback(
        flask_client, str(reply_uuid),
        {"rating": "meh"},
    )
    assert resp.status_code == 400


def test_feedback_rejected_for_unknown_message(client):
    flask_client, app = client
    resp = _post_feedback(
        flask_client, str(uuid4()),
        {"rating": "upvote"},
    )
    assert resp.status_code == 404


def _cleanup_retrieval_events(app, room_uuid):
    with app.app_context():
        db.db.session.query(db.RetrievalEvent).filter(
            db.RetrievalEvent.room_uuid == room_uuid
        ).delete(synchronize_session=False)
        db.db.session.commit()


def test_downvote_writes_downvoted_events_for_debug_memory(
    client, room_with_agent,
):
    """A downvote on a message whose nearby debug-memory snapshot has
    memory uuids should produce one `downvoted` RetrievalEvent per
    memory uuid, target_type='memory_claim', source='chat_feedback'."""
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    mem_uuid_a = uuid4()
    mem_uuid_b = uuid4()
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        db.post_chat_message(
            room_uuid, agent_uuid,
            json.dumps({
                "query": "q",
                "memories": [
                    {"memory_uuid": str(mem_uuid_a), "reason": "r"},
                    {"memory_uuid": str(mem_uuid_b), "reason": "r"},
                ],
            }),
            "json", kind="debug-memory",
        )
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    try:
        resp = _post_feedback(
            flask_client, str(reply_uuid),
            {"rating": "downvote"},
        )
        assert resp.status_code == 201, resp.data

        with app.app_context():
            rows = db.list_retrieval_events(
                stage="downvoted", target_type="memory_claim",
                limit=None,
            )
            ours = [
                r for r in rows
                if r.target_id in {str(mem_uuid_a), str(mem_uuid_b)}
            ]
            assert len(ours) == 2, ours
            for r in ours:
                assert r.source == "chat_feedback"
                assert "feedback_event_uuid" in (r.metadata_ or {}), \
                    r.metadata_
    finally:
        _cleanup_retrieval_events(app, room_uuid)


def test_downvote_without_diagnostic_metadata_does_not_fail(
    client, room_with_agent,
):
    """No debug-memory, no debug-query — feedback still succeeds, no
    downvote retrieval events created."""
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    try:
        resp = _post_feedback(
            flask_client, str(reply_uuid),
            {"rating": "downvote"},
        )
        assert resp.status_code == 201, resp.data
    finally:
        _cleanup_retrieval_events(app, room_uuid)


def test_downvote_after_no_memory_turn_does_not_downvote_earlier_memory(
    client, room_with_agent,
):
    """Full-stack regression for WP07 Finding 3: downvoting a no-memory
    turn must NOT write a `downvoted` event for a memory from a prior
    turn. The debug-memory snapshot must be scoped to the rated turn —
    i.e. younger than the most recent prior human message AND from the
    same agent — so the stale memory from turn N-1 never appears in
    feedback metadata for turn N."""
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    stale_mem = uuid4()
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "A")
        db.post_chat_message(
            room_uuid, agent_uuid,
            json.dumps({"memories": [
                {"memory_uuid": str(stale_mem), "reason": "r"},
            ]}),
            "json", kind="debug-memory",
        )
        db.post_chat_message(room_uuid, agent_uuid, "reply-A")
        db.post_chat_message(room_uuid, human_uuid, "B")
        reply = db.post_chat_message(room_uuid, agent_uuid, "reply-B")
        reply_uuid = reply.uuid

    try:
        resp = _post_feedback(
            flask_client, str(reply_uuid),
            {"rating": "downvote"},
        )
        assert resp.status_code in (200, 201), resp.data
        with app.app_context():
            rows = db.list_retrieval_events(
                stage="downvoted", target_type="memory_claim",
                limit=None,
            )
            assert not any(
                r.target_id == str(stale_mem) for r in rows
            ), (
                "stale memory from a prior turn was downvoted: "
                f"{[r.target_id for r in rows]!r}"
            )
    finally:
        _cleanup_retrieval_events(app, room_uuid)


def test_upvote_does_not_create_downvoted_events(
    client, room_with_agent,
):
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    mem_uuid = uuid4()
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        db.post_chat_message(
            room_uuid, agent_uuid,
            json.dumps({
                "query": "q",
                "memories": [
                    {"memory_uuid": str(mem_uuid), "reason": "r"},
                ],
            }),
            "json", kind="debug-memory",
        )
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    try:
        resp = _post_feedback(
            flask_client, str(reply_uuid),
            {"rating": "upvote"},
        )
        assert resp.status_code == 201, resp.data
        with app.app_context():
            rows = db.list_retrieval_events(
                stage="downvoted", target_type="memory_claim",
                limit=None,
            )
            assert not any(r.target_id == str(mem_uuid) for r in rows)
    finally:
        _cleanup_retrieval_events(app, room_uuid)


def test_downvote_qa_section_failure_does_not_lose_memory_rows(
    client, room_with_agent, monkeypatch,
):
    """Regression for WP06 Task 3 review fix: if the qa-entry section
    of link_downvote_to_retrieval_targets blows up, the already-written
    memory_claim rows must survive (previously they were lost because
    a session.rollback() discarded the still-uncommitted memory rows).
    """
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    mem_uuid = uuid4()
    qa_target_id = f"qa.boom.{uuid4().hex[:6]}"
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        # debug-memory snapshot will yield a memory_claim downvote row.
        db.post_chat_message(
            room_uuid, agent_uuid,
            json.dumps({
                "query": "q",
                "memories": [{"memory_uuid": str(mem_uuid), "reason": "r"}],
            }),
            "json", kind="debug-memory",
        )
        # debug-query snapshot will trigger the qa-section path.
        db.post_chat_message(
            room_uuid, agent_uuid,
            json.dumps({"query": "q", "filter_kept": [qa_target_id]}),
            "json", kind="debug-query",
        )
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    real = db.record_retrieval_event

    def selective_failure(**kw):
        if kw.get("target_type") == "qa_entry":
            raise RuntimeError("forced qa-section failure")
        return real(**kw)
    # link_downvote_to_retrieval_targets calls record_retrieval_event by its
    # module-local name, so patch it where it is looked up (db_feedback), not on
    # the db facade — otherwise the qa-failure path is never exercised.
    monkeypatch.setattr(db_feedback, "record_retrieval_event", selective_failure)

    try:
        resp = _post_feedback(
            flask_client, str(reply_uuid),
            {"rating": "downvote"},
        )
        assert resp.status_code in (200, 201), resp.data

        # The memory_claim row MUST be present even though the qa
        # section raised.
        with app.app_context():
            rows = db.list_retrieval_events(
                stage="downvoted", target_type="memory_claim",
                limit=None,
            )
            ours = [r for r in rows if r.target_id == str(mem_uuid)]
            assert len(ours) == 1, (
                f"memory row was lost when qa section failed: rows={ours!r}"
            )
    finally:
        _cleanup_retrieval_events(app, room_uuid)


def test_downvote_with_malformed_debug_memory_does_not_500(
    client, room_with_agent,
):
    """A debug-memory row whose JSON body cannot be parsed must NOT
    crash the feedback endpoint."""
    flask_client, app = client
    room_uuid, human_uuid, agent_uuid = room_with_agent
    with app.app_context():
        db.post_chat_message(room_uuid, human_uuid, "ping")
        db.post_chat_message(
            room_uuid, agent_uuid,
            "this is not json {{{",
            "json", kind="debug-memory",
        )
        reply = db.post_chat_message(room_uuid, agent_uuid, "pong")
        reply_uuid = reply.uuid

    try:
        resp = _post_feedback(
            flask_client, str(reply_uuid),
            {"rating": "downvote"},
        )
        assert resp.status_code == 201, resp.data
    finally:
        _cleanup_retrieval_events(app, room_uuid)
