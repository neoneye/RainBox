"""Tests for the per-member persona endpoints."""
from uuid import UUID, uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID
from webapp.core import app


@pytest.fixture
def ctx():
    a = db.make_app()
    db.init_db(a)
    c = a.app_context()
    c.push()
    try:
        yield
    finally:
        c.pop()


@pytest.fixture
def room(ctx):
    human = db.get_human_user()
    r = db.create_chatroom(f"api-persona-{uuid4().hex[:8]}", human.uuid,
                           [ASSISTANT_UUID])
    try:
        yield r
    finally:
        db.delete_chatroom(r.uuid)


@pytest.fixture
def persona(ctx):
    p = db.persona_create(f"ApiP-{uuid4().hex[:8]}", None)
    db.persona_update_content(UUID(p["uuid"]), "voice one")
    try:
        yield p
    finally:
        db.persona_delete(UUID(p["uuid"]))


def _put(client, room, user, body):
    return client.put(f"/chat/api/rooms/{room}/members/{user}/persona", json=body)


def test_list_reports_the_assistant_with_no_persona(room):
    body = app.test_client().get(f"/chat/api/rooms/{room.uuid}/personas").get_json()
    rows = body["members"]
    assert len(rows) == 1
    assert rows[0]["user_uuid"] == str(ASSISTANT_UUID)
    assert rows[0]["persona_uuid"] is None
    assert rows[0]["persona_following"] is True


def test_put_links_a_persona_and_reports_following(room, persona):
    c = app.test_client()
    resp = _put(c, room.uuid, ASSISTANT_UUID, {"persona_uuid": persona["uuid"]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    row = resp.get_json()["member"]
    assert row["persona_uuid"] == persona["uuid"]
    assert row["persona_name"] == persona["name"]
    assert row["persona_exists"] is True
    assert row["persona_following"] is True
    assert row["persona_revision_uuid"] is None


def test_put_pins_a_revision(room, persona):
    c = app.test_client()
    rev = db.persona_revisions(UUID(persona["uuid"]))[0]["uuid"]
    _put(c, room.uuid, ASSISTANT_UUID, {"persona_uuid": persona["uuid"]})
    resp = _put(c, room.uuid, ASSISTANT_UUID, {"persona_revision_uuid": rev})
    assert resp.status_code == 200
    row = resp.get_json()["member"]
    assert row["persona_revision_uuid"] == rev
    assert row["persona_following"] is False
    assert row["persona_revision_saved_at"]


def test_unknown_persona_is_400(room):
    resp = _put(app.test_client(), room.uuid, ASSISTANT_UUID,
                {"persona_uuid": str(uuid4())})
    assert resp.status_code == 400


def test_foreign_revision_is_400(room, persona):
    c = app.test_client()
    other = db.persona_create("Other", None)
    db.persona_update_content(UUID(other["uuid"]), "other text")
    foreign = db.persona_revisions(UUID(other["uuid"]))[0]["uuid"]
    try:
        _put(c, room.uuid, ASSISTANT_UUID, {"persona_uuid": persona["uuid"]})
        resp = _put(c, room.uuid, ASSISTANT_UUID, {"persona_revision_uuid": foreign})
        assert resp.status_code == 400
    finally:
        db.persona_delete(UUID(other["uuid"]))


def test_pin_without_a_linked_persona_is_400(room, persona):
    rev = db.persona_revisions(UUID(persona["uuid"]))[0]["uuid"]
    resp = _put(app.test_client(), room.uuid, ASSISTANT_UUID,
                {"persona_revision_uuid": rev})
    assert resp.status_code == 400


def test_a_member_that_cannot_carry_a_persona_is_404(room, persona):
    """The human is a member, but personas are for persona-capable agents."""
    human = db.get_human_user()
    resp = _put(app.test_client(), room.uuid, human.uuid,
                {"persona_uuid": persona["uuid"]})
    assert resp.status_code == 404
    assert "cannot carry a persona" in resp.get_data(as_text=True)


def test_a_non_member_is_404(ctx, persona):
    """ASSISTANT_UUID is persona-capable, so it clears the capability check;
    this room just never added it as a member, so it must hit the
    not-in-this-room branch (a different 404 message than the capability
    rejection above)."""
    human = db.get_human_user()
    r = db.create_chatroom(f"api-persona-nomember-{uuid4().hex[:8]}",
                           human.uuid, [])
    try:
        resp = _put(app.test_client(), r.uuid, ASSISTANT_UUID,
                    {"persona_uuid": persona["uuid"]})
        assert resp.status_code == 404
        assert "not in this room" in resp.get_data(as_text=True)
    finally:
        db.delete_chatroom(r.uuid)


def test_cross_persona_pin_in_the_same_request_is_400(room, persona):
    """persona_revision_uuid must belong to the persona this member will have
    *after this call* — persona_uuid=A with a revision of persona B, both in
    one request, must be rejected even though the member has no persona
    linked yet."""
    c = app.test_client()
    other = db.persona_create(f"ApiOther-{uuid4().hex[:8]}", None)
    db.persona_update_content(UUID(other["uuid"]), "other voice")
    other_rev = db.persona_revisions(UUID(other["uuid"]))[0]["uuid"]
    try:
        resp = _put(c, room.uuid, ASSISTANT_UUID,
                    {"persona_uuid": persona["uuid"],
                     "persona_revision_uuid": other_rev})
        assert resp.status_code == 400
    finally:
        db.persona_delete(UUID(other["uuid"]))


def test_non_string_persona_uuid_int_is_400(room):
    resp = _put(app.test_client(), room.uuid, ASSISTANT_UUID,
                {"persona_uuid": 123})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_non_string_persona_uuid_list_is_400(room):
    resp = _put(app.test_client(), room.uuid, ASSISTANT_UUID,
                {"persona_uuid": ["a"]})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_non_string_persona_uuid_bool_is_400(room):
    resp = _put(app.test_client(), room.uuid, ASSISTANT_UUID,
                {"persona_uuid": True})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_non_string_persona_revision_uuid_dict_is_400(room, persona):
    c = app.test_client()
    _put(c, room.uuid, ASSISTANT_UUID, {"persona_uuid": persona["uuid"]})
    resp = _put(c, room.uuid, ASSISTANT_UUID,
                {"persona_revision_uuid": {"a": 1}})
    assert resp.status_code == 400, resp.get_data(as_text=True)
