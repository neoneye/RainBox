"""Tests for the persona tree persistence + revision history (db.persona)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import Persona, PersonaFolder, PersonaRevision


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


def test_persona_models_round_trip(app_ctx):
    fu, pu, ru = uuid4(), uuid4(), uuid4()
    db.db.session.add(PersonaFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Persona(
        uuid=pu, name="T-persona", content="Curious and blunt.",
        folder_uuid=fu, position=0,
    ))
    db.db.session.add(PersonaRevision(
        uuid=ru, persona_uuid=pu, content="Curious and blunt."))
    db.db.session.commit()
    try:
        f = db.db.session.execute(
            sa.select(PersonaFolder).where(PersonaFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(
            sa.select(Persona).where(Persona.uuid == pu)).scalar_one()
        r = db.db.session.execute(
            sa.select(PersonaRevision).where(PersonaRevision.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.content == "Curious and blunt." and p.folder_uuid == fu
        assert r.persona_uuid == pu and r.content == p.content
        assert f.created_at and p.updated_at and r.created_at  # timestamp defaults fire
    finally:
        db.db.session.execute(
            sa.delete(PersonaRevision).where(PersonaRevision.uuid == ru))
        db.db.session.execute(sa.delete(Persona).where(Persona.uuid == pu))
        db.db.session.execute(
            sa.delete(PersonaFolder).where(PersonaFolder.uuid == fu))
        db.db.session.commit()


@pytest.fixture
def clean_tree(app_ctx):
    """Empty the persona tables around a test (the DB is shared)."""
    def wipe():
        db.db.session.execute(sa.delete(PersonaRevision))
        db.db.session.execute(sa.delete(Persona))
        db.db.session.execute(sa.delete(PersonaFolder))
        db.db.session.commit()
    wipe()
    try:
        yield
    finally:
        wipe()


def test_load_tree_shape_and_version(clean_tree):
    out = db.persona_load_tree()
    assert out["folders"] == [] and out["personas"] == []
    assert isinstance(out["version"], str) and out["version"]


def test_validate_rejects_dangling_folder_parent(clean_tree):
    with pytest.raises(db.PersonaTreeError, match="missing parent"):
        db.validate_persona_tree(
            [{"id": str(uuid4()), "name": "a", "parentId": str(uuid4())}], [])


def test_validate_rejects_folder_cycle(clean_tree):
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.PersonaTreeError, match="cycle"):
        db.validate_persona_tree(
            [{"id": a, "name": "a", "parentId": b},
             {"id": b, "name": "b", "parentId": a}], [])


def test_validate_rejects_uuid_collision(clean_tree):
    shared = str(uuid4())
    with pytest.raises(db.PersonaTreeError, match="collides"):
        db.validate_persona_tree(
            [{"id": shared, "name": "f", "parentId": None}],
            [{"uuid": shared, "name": "p", "folderId": None}])


def test_save_tree_updates_placement_and_name(clean_tree):
    f = db.persona_create_folder("Folder", None)
    p = db.persona_create("Original", None)
    db.persona_save_tree(
        [{"id": f["id"], "name": "Renamed folder", "description": "d", "parentId": None}],
        [{"uuid": p["uuid"], "name": "Renamed", "folderId": f["id"]}])
    tree = db.persona_load_tree()
    assert tree["folders"][0]["name"] == "Renamed folder"
    assert tree["personas"][0]["name"] == "Renamed"
    assert tree["personas"][0]["folderId"] == f["id"]


def test_save_tree_refuses_to_omit_an_existing_row(clean_tree):
    p = db.persona_create("Keep me", None)
    with pytest.raises(db.PersonaTreeError, match="omitted"):
        db.persona_save_tree([], [])
    # nothing was touched
    assert [x["uuid"] for x in db.persona_load_tree()["personas"]] == [p["uuid"]]


def test_save_tree_refuses_an_unknown_row(clean_tree):
    with pytest.raises(db.PersonaTreeError, match="unknown"):
        db.persona_save_tree(
            [], [{"uuid": str(uuid4()), "name": "ghost", "folderId": None}])


def test_save_tree_stale_version_conflicts(clean_tree):
    p = db.persona_create("A", None)
    stale = db.persona_tree_version()
    db.persona_create("B", None)          # someone else changed the tree
    with pytest.raises(db.PersonaTreeConflict):
        db.persona_save_tree(
            [], [{"uuid": p["uuid"], "name": "A", "folderId": None}],
            base_version=stale)


def test_version_ignores_content(clean_tree):
    p = db.persona_create("A", None)
    before = db.persona_tree_version()
    db.persona_update_content(p["uuid"], "some text")
    assert db.persona_tree_version() == before


def test_create_places_at_end_of_folder(clean_tree):
    f = db.persona_create_folder("F", None)
    a = db.persona_create("A", f["id"])
    b = db.persona_create("B", f["id"])
    names = [p["name"] for p in db.persona_load_tree()["personas"]]
    assert names == ["A", "B"]
    assert a["folderId"] == f["id"] and b["folderId"] == f["id"]
    assert a["revisionCount"] == 0


def test_first_save_creates_revision_one(clean_tree):
    p = db.persona_create("A", None)
    out = db.persona_update_content(p["uuid"], "Dry wit, no filler.")
    assert out["changed"] is True
    revs = db.persona_revisions(p["uuid"])
    assert len(revs) == 1
    assert revs[0]["current"] is True
    assert revs[0]["preview"] == "Dry wit, no filler."
    assert db.persona_get(p["uuid"])["content"] == "Dry wit, no filler."


def test_unchanged_save_appends_nothing(clean_tree):
    p = db.persona_create("A", None)
    db.persona_update_content(p["uuid"], "same")
    out = db.persona_update_content(p["uuid"], "same")
    assert out["changed"] is False and out["revision"] is None
    assert len(db.persona_revisions(p["uuid"])) == 1


def test_newest_revision_always_mirrors_content(clean_tree):
    p = db.persona_create("A", None)
    for text in ("one", "two", "three"):
        db.persona_update_content(p["uuid"], text)
    revs = db.persona_revisions(p["uuid"])
    assert len(revs) == 3 and revs[0]["current"] is True
    assert db.persona_get(p["uuid"])["content"] == "three"
    assert db.persona_revision_diff(p["uuid"], revs[0]["uuid"])["lines"] == []


def test_restore_appends_rather_than_rewinds(clean_tree):
    p = db.persona_create("A", None)
    db.persona_update_content(p["uuid"], "one")
    db.persona_update_content(p["uuid"], "two")
    oldest = db.persona_revisions(p["uuid"])[-1]
    out = db.persona_restore_revision(p["uuid"], UUID(oldest["uuid"]))
    assert out["ok"] is True and out["changed"] is True and out["content"] == "one"
    revs = db.persona_revisions(p["uuid"])
    assert len(revs) == 3               # nothing was removed
    assert revs[0]["current"] is True
    assert db.persona_get(p["uuid"])["content"] == "one"


def test_restore_of_a_foreign_revision_fails(clean_tree):
    a = db.persona_create("A", None)
    b = db.persona_create("B", None)
    db.persona_update_content(a["uuid"], "a text")
    rev = db.persona_revisions(a["uuid"])[0]
    out = db.persona_restore_revision(b["uuid"], UUID(rev["uuid"]))
    assert out["ok"] is False and out["error"] == "revision not found"


def test_diff_reports_the_change(clean_tree):
    p = db.persona_create("A", None)
    db.persona_update_content(p["uuid"], "old line")
    db.persona_update_content(p["uuid"], "new line")
    oldest = db.persona_revisions(p["uuid"])[-1]
    out = db.persona_revision_diff(p["uuid"], UUID(oldest["uuid"]))
    assert out["ok"] is True
    assert any(line.startswith("-old line") for line in out["lines"])
    assert any(line.startswith("+new line") for line in out["lines"])


def test_delete_persona_cascades_revisions(clean_tree):
    p = db.persona_create("A", None)
    db.persona_update_content(p["uuid"], "text")
    assert db.persona_delete(UUID(p["uuid"])) is True
    assert db.persona_get(UUID(p["uuid"])) is None
    assert db.db.session.execute(
        sa.select(sa.func.count(PersonaRevision.id))
        .where(PersonaRevision.persona_uuid == UUID(p["uuid"]))
    ).scalar_one() == 0


def test_delete_folder_cascades_the_subtree(clean_tree):
    outer = db.persona_create_folder("Outer", None)
    inner = db.persona_create_folder("Inner", outer["id"])
    p = db.persona_create("A", inner["id"])
    db.persona_update_content(p["uuid"], "text")
    assert db.persona_delete_folder(UUID(outer["id"])) is True
    tree = db.persona_load_tree()
    assert tree["folders"] == [] and tree["personas"] == []
    assert db.db.session.execute(
        sa.select(sa.func.count(PersonaRevision.id))).scalar_one() == 0


def test_delete_folder_walks_a_subtree_larger_than_the_old_cap(clean_tree):
    # A 150-deep chain used to trip the removed 100-folder walk cap, which
    # stopped collecting mid-subtree and left the remainder (and any
    # persona inside it) orphaned after delete. The walk must now cover
    # the whole subtree regardless of size.
    root = db.persona_create_folder("Root", None)
    parent_uuid = UUID(root["id"])
    chain = []
    for i in range(150):
        row = PersonaFolder(uuid=uuid4(), name=f"F{i}", description="",
                                parent_uuid=parent_uuid, position=0)
        chain.append(row)
        parent_uuid = row.uuid
    db.db.session.add_all(chain)
    leaf = db.persona_create("Leaf", parent_uuid)  # commits the whole chain too

    assert db.persona_delete_folder(UUID(root["id"])) is True
    tree = db.persona_load_tree()
    assert tree["folders"] == [] and tree["personas"] == []
    assert db.persona_get(UUID(leaf["uuid"])) is None
