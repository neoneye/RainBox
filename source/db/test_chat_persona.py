"""Tests for the member→persona binding: which persona a room participant
speaks with, and which revision of it produced the text."""
from uuid import UUID, uuid4

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
        ctx.pop()


@pytest.fixture
def assistant_uuid():
    from agents.config import ASSISTANT_UUID
    return ASSISTANT_UUID


@pytest.fixture
def room(app_ctx, assistant_uuid):
    # create_chatroom(name, created_by, member_uuids, room_type="agents")
    human = db.get_human_user()
    r = db.create_chatroom(f"persona-test-{uuid4().hex[:8]}", human.uuid,
                           [assistant_uuid])
    try:
        yield r
    finally:
        db.delete_chatroom(r.uuid)


@pytest.fixture
def persona(app_ctx):
    p = db.persona_create(f"P-{uuid4().hex[:8]}", None)
    try:
        yield p
    finally:
        db.persona_delete(UUID(p["uuid"]))


def test_no_persona_linked_resolves_to_nothing(room, assistant_uuid):
    out = db.resolve_member_persona(room.uuid, assistant_uuid)
    assert out.text == "" and out.revision_uuid is None and out.persona_uuid is None


def test_a_non_member_resolves_to_nothing(room):
    out = db.resolve_member_persona(room.uuid, uuid4())
    assert out.text == ""


def test_following_resolves_to_newest_and_stamps_it(room, persona, assistant_uuid):
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "first")
    db.persona_update_content(pu, "second")
    db.set_member_persona(room.uuid, assistant_uuid, persona_uuid=pu)
    out = db.resolve_member_persona(room.uuid, assistant_uuid)
    newest = db.persona_revisions(pu)[0]
    assert out.text == "second"
    assert str(out.revision_uuid) == newest["uuid"]
    assert out.name == persona["name"]


def test_pinned_resolves_to_that_revision_not_the_newest(room, persona, assistant_uuid):
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "old text")
    oldest = UUID(db.persona_revisions(pu)[0]["uuid"])
    db.persona_update_content(pu, "new text")
    db.set_member_persona(room.uuid, assistant_uuid,
                          persona_uuid=pu, persona_revision_uuid=oldest)
    out = db.resolve_member_persona(room.uuid, assistant_uuid)
    assert out.text == "old text"
    assert out.revision_uuid == oldest


def test_persona_never_saved_resolves_to_nothing(room, persona, assistant_uuid):
    db.set_member_persona(room.uuid, assistant_uuid,
                          persona_uuid=UUID(persona["uuid"]))
    out = db.resolve_member_persona(room.uuid, assistant_uuid)
    assert out.text == "" and out.revision_uuid is None


def test_deleted_persona_resolves_to_nothing_not_stale_text(room, assistant_uuid):
    p = db.persona_create("Doomed", None)
    pu = UUID(p["uuid"])
    db.persona_update_content(pu, "text that must not survive deletion")
    db.set_member_persona(room.uuid, assistant_uuid, persona_uuid=pu)
    db.persona_delete(pu)
    out = db.resolve_member_persona(room.uuid, assistant_uuid)
    assert out.text == "" and out.revision_uuid is None


def test_two_members_resolve_independently(room, assistant_uuid):
    """The whole reason the binding is per-member: a second persona-capable
    participant carries its own voice, with no room-level collision."""
    other = db.get_human_user().uuid   # any second member row will do here
    a = db.persona_create("Voice A", None)
    b = db.persona_create("Voice B", None)
    try:
        db.persona_update_content(UUID(a["uuid"]), "I am A")
        db.persona_update_content(UUID(b["uuid"]), "I am B")
        db.set_member_persona(room.uuid, assistant_uuid, persona_uuid=UUID(a["uuid"]))
        db.set_member_persona(room.uuid, other, persona_uuid=UUID(b["uuid"]))
        assert db.resolve_member_persona(room.uuid, assistant_uuid).text == "I am A"
        assert db.resolve_member_persona(room.uuid, other).text == "I am B"
    finally:
        db.persona_delete(UUID(a["uuid"]))
        db.persona_delete(UUID(b["uuid"]))


def test_setting_a_persona_for_a_non_member_raises(room, persona):
    with pytest.raises(LookupError):
        db.set_member_persona(room.uuid, uuid4(),
                              persona_uuid=UUID(persona["uuid"]))


def test_picking_a_persona_clears_an_existing_pin(room, persona, assistant_uuid):
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "one")
    pinned = UUID(db.persona_revisions(pu)[0]["uuid"])
    db.set_member_persona(room.uuid, assistant_uuid,
                          persona_uuid=pu, persona_revision_uuid=pinned)
    assert db.get_member_persona_row(room.uuid, assistant_uuid).persona_revision_uuid == pinned
    db.set_member_persona(room.uuid, assistant_uuid, persona_uuid=pu)
    assert db.get_member_persona_row(room.uuid, assistant_uuid).persona_revision_uuid is None


def test_unlinking_clears_both_columns(room, persona, assistant_uuid):
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "one")
    db.set_member_persona(room.uuid, assistant_uuid, persona_uuid=pu)
    db.set_member_persona(room.uuid, assistant_uuid, persona_uuid=None)
    row = db.get_member_persona_row(room.uuid, assistant_uuid)
    assert row.persona_uuid is None and row.persona_revision_uuid is None


def test_revision_get_rejects_a_foreign_revision(app_ctx):
    a = db.persona_create("Owner A", None)
    b = db.persona_create("Owner B", None)
    au, bu = UUID(a["uuid"]), UUID(b["uuid"])
    try:
        db.persona_update_content(au, "a text")
        rev = UUID(db.persona_revisions(au)[0]["uuid"])
        assert db.persona_revision_get(au, rev) is not None
        assert db.persona_revision_get(bu, rev) is None
    finally:
        db.persona_delete(au)
        db.persona_delete(bu)
