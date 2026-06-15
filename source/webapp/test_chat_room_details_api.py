"""HTTP API tests for the /chat/api/rooms/details folder-contents endpoint.

Uses the live local Postgres (rainbox_claude via conftest).
"""

from uuid import uuid4

import pytest

import db


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


@pytest.fixture
def rooms(client):
    """Two rooms: `chatty` (human + one agent, with messages) and `empty`
    (human + one agent, no messages). Yields the uuids/names needed to assert."""
    _client, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        agent = db.ChatUser(
            uuid=uuid4(), name=f"det-agent-{uuid4().hex[:6]}", user_type="agent"
        )
        db.db.session.add(agent)
        db.db.session.flush()
        chatty = db.create_chatroom(
            f"det-chatty-{uuid4().hex[:6]}", human.uuid, [agent.uuid]
        )
        empty = db.create_chatroom(
            f"det-empty-{uuid4().hex[:6]}", human.uuid, [agent.uuid]
        )
        db.post_chat_message(chatty.uuid, human.uuid, "first")
        last_msg = db.post_chat_message(chatty.uuid, human.uuid, "second")
        info = {
            "agent_uuid": agent.uuid,
            "agent_name": agent.name,
            "human_name": human.name,
            "chatty_uuid": str(chatty.uuid),
            "empty_uuid": str(empty.uuid),
            "last_at": last_msg.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        try:
            yield info
        finally:
            db.db.session.query(db.Chatroom).filter(
                db.Chatroom.uuid.in_([chatty.uuid, empty.uuid])
            ).delete()
            db.db.session.query(db.ChatUser).filter(
                db.ChatUser.uuid == agent.uuid
            ).delete()
            db.db.session.commit()


def _by_uuid(details, uuid):
    return next(d for d in details if d["uuid"] == uuid)


def test_agents_include_agent_exclude_human(client, rooms):
    flask_client, _app = client
    details = flask_client.get("/chat/api/rooms/details").get_json()
    chatty = _by_uuid(details, rooms["chatty_uuid"])
    assert rooms["agent_name"] in chatty["agents"]
    assert rooms["human_name"] not in chatty["agents"]


def test_message_count_and_last_message_at(client, rooms):
    flask_client, _app = client
    details = flask_client.get("/chat/api/rooms/details").get_json()
    chatty = _by_uuid(details, rooms["chatty_uuid"])
    assert chatty["message_count"] == 2
    assert chatty["last_message_at"] == rooms["last_at"]


def test_empty_room_has_zero_count_and_null_last(client, rooms):
    flask_client, _app = client
    details = flask_client.get("/chat/api/rooms/details").get_json()
    empty = _by_uuid(details, rooms["empty_uuid"])
    assert empty["message_count"] == 0
    assert empty["last_message_at"] is None


def test_entry_per_room(client, rooms):
    flask_client, app = client
    details = flask_client.get("/chat/api/rooms/details").get_json()
    with app.app_context():
        room_count = len(db.list_chatrooms())
    assert len(details) == room_count
    assert {d["uuid"] for d in details} >= {rooms["chatty_uuid"], rooms["empty_uuid"]}
