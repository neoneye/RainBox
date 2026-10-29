"""HTTP API tests for assistant rooms: the operator and the assistant, members
fixed, born with chat.default_persona."""

from uuid import UUID, uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID, CHAT_STRUCTURED_UUID


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    with webapp_core.app.app_context():
        yield webapp_core.app.test_client()


@pytest.fixture
def default_persona(client):
    """Set chat.default_persona to a fresh persona; restore the prior value."""
    prior = db.get_setting("chat.default_persona")
    p = db.persona_create(f"room-default-{uuid4().hex[:6]}", None)
    db.persona_update_content(UUID(p["uuid"]), "voice")
    db.set_setting("chat.default_persona", p["uuid"])
    try:
        yield p
    finally:
        db.set_setting("chat.default_persona", prior)
        db.persona_delete(UUID(p["uuid"]))


@pytest.fixture
def no_default_persona(client):
    prior = db.get_setting("chat.default_persona")
    db.set_setting("chat.default_persona", None)
    try:
        yield
    finally:
        db.set_setting("chat.default_persona", prior)


@pytest.fixture
def created():
    """Room uuids a test created, deleted afterwards."""
    uuids: list[UUID] = []
    yield uuids
    for u in uuids:
        db.delete_chatroom(u)


def _create(test_client, created, **extra):
    resp = test_client.post("/chat/api/rooms", json={
        "name": f"assistant-room-{uuid4().hex[:6]}", "room_type": "assistant",
        **extra})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    room_uuid = UUID(resp.get_json()["uuid"])
    created.append(room_uuid)
    return room_uuid


def test_members_are_the_operator_and_the_assistant(client, created,
                                                    no_default_persona):
    room_uuid = _create(client, created,
                        member_uuids=[str(CHAT_STRUCTURED_UUID)])
    members = {m["uuid"] for m in db.list_room_members(room_uuid)}
    assert members == {str(db.get_human_user().uuid), str(ASSISTANT_UUID)}
    assert db.get_chatroom(room_uuid).room_type == "assistant"


def test_born_with_the_default_persona_following_newest(client, created,
                                                        default_persona):
    room_uuid = _create(client, created)
    row = db.get_member_persona_row(room_uuid, ASSISTANT_UUID)
    assert str(row.persona_uuid) == default_persona["uuid"]
    assert row.persona_revision_uuid is None


def test_no_default_persona_means_none(client, created, no_default_persona):
    room_uuid = _create(client, created)
    assert db.get_member_persona_row(room_uuid, ASSISTANT_UUID).persona_uuid is None


def test_a_deleted_default_persona_means_none(client, created, no_default_persona):
    p = db.persona_create(f"gone-{uuid4().hex[:6]}", None)
    db.set_setting("chat.default_persona", p["uuid"])
    db.persona_delete(UUID(p["uuid"]))
    room_uuid = _create(client, created)
    assert db.get_member_persona_row(room_uuid, ASSISTANT_UUID).persona_uuid is None
    assert client.get("/chat/api/new-room-defaults").get_json() == {"persona": None}


def test_new_room_defaults_names_the_persona(client, default_persona):
    assert client.get("/chat/api/new-room-defaults").get_json() == {
        "persona": {"uuid": default_persona["uuid"],
                    "name": default_persona["name"]}}


def test_members_cannot_be_added_or_removed(client, created, no_default_persona):
    room_uuid = _create(client, created)
    resp = client.post(f"/chat/api/rooms/{room_uuid}/members",
                       json={"user_uuid": str(CHAT_STRUCTURED_UUID)})
    assert resp.status_code == 400
    resp = client.delete(f"/chat/api/rooms/{room_uuid}/members/{ASSISTANT_UUID}")
    assert resp.status_code == 400
    members = {m["uuid"] for m in db.list_room_members(room_uuid)}
    assert str(ASSISTANT_UUID) in members and str(CHAT_STRUCTURED_UUID) not in members


def test_a_post_enqueues_the_assistant(client, created, no_default_persona):
    room_uuid = _create(client, created)
    resp = client.post(f"/chat/api/rooms/{room_uuid}/messages",
                       json={"text": "hello"})
    assert resp.status_code == 201
    rows = [r for r in db.session.query(db.Inbox)
            .filter(db.Inbox.agent_uuid == ASSISTANT_UUID).all()
            if str(room_uuid) in r.payload]
    try:
        assert len(rows) == 1
    finally:
        for r in rows:
            db.session.delete(r)
        db.session.commit()


def test_the_persona_picker_accepts_an_assistant_room(client, created,
                                                      default_persona):
    room_uuid = _create(client, created)
    resp = client.put(f"/chat/api/rooms/{room_uuid}/members/{ASSISTANT_UUID}/persona",
                      json={"persona_uuid": None})
    assert resp.status_code == 200
    assert db.get_member_persona_row(room_uuid, ASSISTANT_UUID).persona_uuid is None
