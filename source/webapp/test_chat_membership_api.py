"""HTTP API tests for chatroom membership add/remove endpoints."""

import json
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
def room(client):
    """Room with human + agent_a; agent_b is a non-member spare."""
    _client, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        agent_a = db.ChatUser(
            uuid=uuid4(), name=f"mem-api-a-{uuid4().hex[:6]}", user_type="agent"
        )
        agent_b = db.ChatUser(
            uuid=uuid4(), name=f"mem-api-b-{uuid4().hex[:6]}", user_type="agent"
        )
        db.db.session.add_all([agent_a, agent_b])
        db.db.session.flush()
        room = db.create_chatroom(
            f"mem-api-{uuid4().hex[:6]}", human.uuid, [agent_a.uuid]
        )
        agent_a_uuid, agent_b_uuid = agent_a.uuid, agent_b.uuid
        try:
            yield room.uuid, human.uuid, agent_a_uuid, agent_b_uuid
        finally:
            db.db.session.query(db.Chatroom).filter(
                db.Chatroom.uuid == room.uuid
            ).delete()
            db.db.session.query(db.ChatUser).filter(
                db.ChatUser.uuid.in_([agent_a_uuid, agent_b_uuid])
            ).delete()
            db.db.session.commit()


def _add(client, room_uuid, user_uuid):
    return client.post(
        f"/chat/api/rooms/{room_uuid}/members",
        data=json.dumps({"user_uuid": str(user_uuid)}),
        content_type="application/json",
    )


def _remove(client, room_uuid, user_uuid):
    return client.delete(f"/chat/api/rooms/{room_uuid}/members/{user_uuid}")


def _member_uuids(app, room_uuid):
    with app.app_context():
        return {m["uuid"] for m in db.list_room_members(room_uuid)}


def test_add_member(client, room):
    flask_client, app = client
    room_uuid, _human, _agent_a, agent_b = room
    resp = _add(flask_client, room_uuid, agent_b)
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["added"] is True
    assert str(agent_b) in _member_uuids(app, room_uuid)


def test_add_member_idempotent(client, room):
    flask_client, app = client
    room_uuid, _human, agent_a, _agent_b = room
    resp = _add(flask_client, room_uuid, agent_a)  # already a member
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["added"] is False


def test_remove_member(client, room):
    flask_client, app = client
    room_uuid, _human, agent_a, _agent_b = room
    resp = _remove(flask_client, room_uuid, agent_a)
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["removed"] is True
    assert str(agent_a) not in _member_uuids(app, room_uuid)


def test_remove_human_rejected(client, room):
    flask_client, app = client
    room_uuid, human_uuid, _agent_a, _agent_b = room
    resp = _remove(flask_client, room_uuid, human_uuid)
    assert resp.status_code == 409, resp.data
    assert str(human_uuid) in _member_uuids(app, room_uuid)


def test_add_to_unknown_room_404(client, room):
    flask_client, _app = client
    _room_uuid, _human, _agent_a, agent_b = room
    resp = _add(flask_client, uuid4(), agent_b)
    assert resp.status_code == 404


def test_remove_from_unknown_room_404(client, room):
    flask_client, _app = client
    _room_uuid, _human, agent_a, _agent_b = room
    resp = _remove(flask_client, uuid4(), agent_a)
    assert resp.status_code == 404


def test_add_missing_user_uuid_400(client, room):
    flask_client, _app = client
    room_uuid, _human, _agent_a, _agent_b = room
    resp = flask_client.post(
        f"/chat/api/rooms/{room_uuid}/members",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_add_unknown_user_404(client, room):
    flask_client, _app = client
    room_uuid, _human, _agent_a, _agent_b = room
    # Valid room, but the user_uuid doesn't belong to any chat_user.
    resp = _add(flask_client, room_uuid, uuid4())
    assert resp.status_code == 404
