"""Tests for the personality tree persistence + revision history (db.personality)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import Personality, PersonalityFolder, PersonalityRevision


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


def test_personality_models_round_trip(app_ctx):
    fu, pu, ru = uuid4(), uuid4(), uuid4()
    db.db.session.add(PersonalityFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Personality(
        uuid=pu, name="T-personality", content="Curious and blunt.",
        folder_uuid=fu, position=0,
    ))
    db.db.session.add(PersonalityRevision(
        uuid=ru, personality_uuid=pu, content="Curious and blunt."))
    db.db.session.commit()
    try:
        f = db.db.session.execute(
            sa.select(PersonalityFolder).where(PersonalityFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(
            sa.select(Personality).where(Personality.uuid == pu)).scalar_one()
        r = db.db.session.execute(
            sa.select(PersonalityRevision).where(PersonalityRevision.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.content == "Curious and blunt." and p.folder_uuid == fu
        assert r.personality_uuid == pu and r.content == p.content
        assert f.created_at and p.updated_at and r.created_at  # timestamp defaults fire
    finally:
        db.db.session.execute(
            sa.delete(PersonalityRevision).where(PersonalityRevision.uuid == ru))
        db.db.session.execute(sa.delete(Personality).where(Personality.uuid == pu))
        db.db.session.execute(
            sa.delete(PersonalityFolder).where(PersonalityFolder.uuid == fu))
        db.db.session.commit()


@pytest.fixture
def clean_tree(app_ctx):
    """Empty the personality tables around a test (the DB is shared)."""
    def wipe():
        db.db.session.execute(sa.delete(PersonalityRevision))
        db.db.session.execute(sa.delete(Personality))
        db.db.session.execute(sa.delete(PersonalityFolder))
        db.db.session.commit()
    wipe()
    try:
        yield
    finally:
        wipe()


def test_load_tree_shape_and_version(clean_tree):
    out = db.personality_load_tree()
    assert out["folders"] == [] and out["personalities"] == []
    assert isinstance(out["version"], str) and out["version"]


def test_validate_rejects_dangling_folder_parent(clean_tree):
    with pytest.raises(db.PersonalityTreeError, match="missing parent"):
        db.validate_personality_tree(
            [{"id": str(uuid4()), "name": "a", "parentId": str(uuid4())}], [])


def test_validate_rejects_folder_cycle(clean_tree):
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.PersonalityTreeError, match="cycle"):
        db.validate_personality_tree(
            [{"id": a, "name": "a", "parentId": b},
             {"id": b, "name": "b", "parentId": a}], [])


def test_validate_rejects_uuid_collision(clean_tree):
    shared = str(uuid4())
    with pytest.raises(db.PersonalityTreeError, match="collides"):
        db.validate_personality_tree(
            [{"id": shared, "name": "f", "parentId": None}],
            [{"uuid": shared, "name": "p", "folderId": None}])


def test_save_tree_updates_placement_and_name(clean_tree):
    f = db.personality_create_folder("Folder", None)
    p = db.personality_create("Original", None)
    db.personality_save_tree(
        [{"id": f["id"], "name": "Renamed folder", "description": "d", "parentId": None}],
        [{"uuid": p["uuid"], "name": "Renamed", "folderId": f["id"]}])
    tree = db.personality_load_tree()
    assert tree["folders"][0]["name"] == "Renamed folder"
    assert tree["personalities"][0]["name"] == "Renamed"
    assert tree["personalities"][0]["folderId"] == f["id"]


def test_save_tree_refuses_to_omit_an_existing_row(clean_tree):
    p = db.personality_create("Keep me", None)
    with pytest.raises(db.PersonalityTreeError, match="omitted"):
        db.personality_save_tree([], [])
    # nothing was touched
    assert [x["uuid"] for x in db.personality_load_tree()["personalities"]] == [p["uuid"]]


def test_save_tree_refuses_an_unknown_row(clean_tree):
    with pytest.raises(db.PersonalityTreeError, match="unknown"):
        db.personality_save_tree(
            [], [{"uuid": str(uuid4()), "name": "ghost", "folderId": None}])


def test_save_tree_stale_version_conflicts(clean_tree):
    p = db.personality_create("A", None)
    stale = db.personality_tree_version()
    db.personality_create("B", None)          # someone else changed the tree
    with pytest.raises(db.PersonalityTreeConflict):
        db.personality_save_tree(
            [], [{"uuid": p["uuid"], "name": "A", "folderId": None}],
            base_version=stale)


def test_version_ignores_content(clean_tree):
    p = db.personality_create("A", None)
    before = db.personality_tree_version()
    db.personality_update_content(p["uuid"], "some text")
    assert db.personality_tree_version() == before


def test_create_places_at_end_of_folder(clean_tree):
    f = db.personality_create_folder("F", None)
    a = db.personality_create("A", f["id"])
    b = db.personality_create("B", f["id"])
    names = [p["name"] for p in db.personality_load_tree()["personalities"]]
    assert names == ["A", "B"]
    assert a["folderId"] == f["id"] and b["folderId"] == f["id"]
    assert a["revisionCount"] == 0


def test_first_save_creates_revision_one(clean_tree):
    p = db.personality_create("A", None)
    out = db.personality_update_content(p["uuid"], "Dry wit, no filler.")
    assert out["changed"] is True
    revs = db.personality_revisions(p["uuid"])
    assert len(revs) == 1
    assert revs[0]["current"] is True
    assert revs[0]["preview"] == "Dry wit, no filler."
    assert db.personality_get(p["uuid"])["content"] == "Dry wit, no filler."


def test_unchanged_save_appends_nothing(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "same")
    out = db.personality_update_content(p["uuid"], "same")
    assert out["changed"] is False and out["revision"] is None
    assert len(db.personality_revisions(p["uuid"])) == 1


def test_newest_revision_always_mirrors_content(clean_tree):
    p = db.personality_create("A", None)
    for text in ("one", "two", "three"):
        db.personality_update_content(p["uuid"], text)
    revs = db.personality_revisions(p["uuid"])
    assert len(revs) == 3 and revs[0]["current"] is True
    assert db.personality_get(p["uuid"])["content"] == "three"
    assert db.personality_revision_diff(p["uuid"], revs[0]["uuid"])["lines"] == []


def test_restore_appends_rather_than_rewinds(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "one")
    db.personality_update_content(p["uuid"], "two")
    oldest = db.personality_revisions(p["uuid"])[-1]
    out = db.personality_restore_revision(p["uuid"], UUID(oldest["uuid"]))
    assert out["ok"] is True and out["changed"] is True and out["content"] == "one"
    revs = db.personality_revisions(p["uuid"])
    assert len(revs) == 3               # nothing was removed
    assert revs[0]["current"] is True
    assert db.personality_get(p["uuid"])["content"] == "one"


def test_restore_of_a_foreign_revision_fails(clean_tree):
    a = db.personality_create("A", None)
    b = db.personality_create("B", None)
    db.personality_update_content(a["uuid"], "a text")
    rev = db.personality_revisions(a["uuid"])[0]
    out = db.personality_restore_revision(b["uuid"], UUID(rev["uuid"]))
    assert out["ok"] is False and out["error"] == "revision not found"


def test_diff_reports_the_change(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "old line")
    db.personality_update_content(p["uuid"], "new line")
    oldest = db.personality_revisions(p["uuid"])[-1]
    out = db.personality_revision_diff(p["uuid"], UUID(oldest["uuid"]))
    assert out["ok"] is True
    assert any(line.startswith("-old line") for line in out["lines"])
    assert any(line.startswith("+new line") for line in out["lines"])


def test_delete_personality_cascades_revisions(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "text")
    assert db.personality_delete(UUID(p["uuid"])) is True
    assert db.personality_get(UUID(p["uuid"])) is None
    assert db.db.session.execute(
        sa.select(sa.func.count(PersonalityRevision.id))
        .where(PersonalityRevision.personality_uuid == UUID(p["uuid"]))
    ).scalar_one() == 0


def test_delete_folder_cascades_the_subtree(clean_tree):
    outer = db.personality_create_folder("Outer", None)
    inner = db.personality_create_folder("Inner", outer["id"])
    p = db.personality_create("A", inner["id"])
    db.personality_update_content(p["uuid"], "text")
    assert db.personality_delete_folder(UUID(outer["id"])) is True
    tree = db.personality_load_tree()
    assert tree["folders"] == [] and tree["personalities"] == []
    assert db.db.session.execute(
        sa.select(sa.func.count(PersonalityRevision.id))).scalar_one() == 0
