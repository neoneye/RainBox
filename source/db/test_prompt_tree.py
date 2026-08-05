"""Tests for the system-prompt tree persistence + version lineage (db.prompt)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import Prompt, PromptFolder


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


def test_prompt_models_round_trip(app_ctx):
    fu, pu = uuid4(), uuid4()
    db.db.session.add(PromptFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Prompt(
        uuid=pu, name="T-prompt", content="You are helpful.",
        parent_uuid=None, folder_uuid=fu, position=0,
    ))
    db.db.session.commit()
    try:
        f = db.db.session.execute(sa.select(PromptFolder).where(PromptFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(sa.select(Prompt).where(Prompt.uuid == pu)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.content == "You are helpful." and p.folder_uuid == fu
        assert f.created_at and p.updated_at  # timestamp defaults fire
    finally:
        db.db.session.execute(sa.delete(Prompt).where(Prompt.uuid == pu))
        db.db.session.execute(sa.delete(PromptFolder).where(PromptFolder.uuid == fu))
        db.db.session.commit()


@pytest.fixture
def prompt_tree_snapshot(app_ctx):
    """Snapshot the prompt tables, yield, then restore — non-destructive."""
    def grab(model):
        rows = db.db.session.execute(sa.select(model)).scalars().all()
        return [
            {c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"}
            for r in rows
        ]
    fsnap, psnap = grab(PromptFolder), grab(Prompt)
    try:
        yield
    finally:
        db.db.session.execute(sa.delete(Prompt))
        db.db.session.execute(sa.delete(PromptFolder))
        for row in fsnap:
            db.db.session.add(PromptFolder(**row))
        for row in psnap:
            db.db.session.add(Prompt(**row))
        db.db.session.commit()


@pytest.fixture
def empty_tree(prompt_tree_snapshot):
    db.db.session.execute(sa.delete(Prompt))
    db.db.session.execute(sa.delete(PromptFolder))
    db.db.session.commit()


def _lineage_row(name: str, parent_uuid: UUID | None) -> str:
    """A prompt row with a hand-set parent_uuid. The tree save deliberately
    never writes lineage (only prompt_clone does), so the dangling/cyclic
    lineage cases have to be planted directly."""
    row = Prompt(uuid=uuid4(), name=name, content="", folder_uuid=None,
                 parent_uuid=parent_uuid, position=0)
    db.db.session.add(row)
    db.db.session.commit()
    return str(row.uuid)


def test_save_and_load_roundtrip(app_ctx, empty_tree):
    f_root = db.prompt_create_folder("Root", None)
    f_child = db.prompt_create_folder("Child", UUID(f_root["id"]))
    pr = db.prompt_create("MyPersona", UUID(f_child["id"]))
    db.prompt_save_tree(
        [{"id": f_root["id"], "name": "Root", "description": "top", "parentId": None},
         {"id": f_child["id"], "name": "Child", "parentId": f_root["id"]}],
        [{"uuid": pr["uuid"], "name": "MyPersona", "folderId": f_child["id"],
          "parentUuid": None}])
    out = db.prompt_load_tree()
    assert [f["name"] for f in out["folders"]] == ["Root", "Child"]  # order preserved
    assert out["folders"][1]["parentId"] == f_root["id"]
    assert len(out["prompts"]) == 1
    assert out["prompts"][0]["folderId"] == f_child["id"]
    assert out["prompts"][0]["parentUuid"] is None
    assert "content" not in out["prompts"][0]  # tree payload stays light
    assert out["version"]


def test_tree_save_preserves_content(app_ctx, empty_tree):
    pr = db.prompt_create("P", None)
    assert db.prompt_update_content(UUID(pr["uuid"]), "the prompt text")
    # A structural save (rename) must not touch content.
    db.prompt_save_tree([], [{"uuid": pr["uuid"], "name": "P renamed",
                              "folderId": None}])
    got = db.prompt_get(UUID(pr["uuid"]))
    assert got["name"] == "P renamed"
    assert got["content"] == "the prompt text"


def test_tree_save_never_rewrites_lineage(app_ctx, empty_tree):
    """parentUuid is written once, by the clone that made the row. A payload
    claiming a different ancestor is ignored rather than obeyed."""
    src = db.prompt_create("P", None)
    clone = db.prompt_clone(UUID(src["uuid"]))
    db.prompt_save_tree([], [
        {"uuid": src["uuid"], "name": "P", "folderId": None, "parentUuid": None},
        {"uuid": clone["uuid"], "name": "P 2", "folderId": None,
         "parentUuid": None},           # a lie: the clone does have a parent
    ])
    assert db.prompt_get(UUID(clone["uuid"]))["parentUuid"] == src["uuid"]


def test_content_excluded_from_version(app_ctx, empty_tree):
    pr = db.prompt_create("P", None)
    v1 = db.prompt_tree_version()
    db.prompt_update_content(UUID(pr["uuid"]), "new text")
    assert db.prompt_tree_version() == v1  # autosave never invalidates the tree


def test_version_conflict(app_ctx, prompt_tree_snapshot):
    with pytest.raises(db.PromptTreeConflict):
        db.prompt_save_tree([], [], base_version="stale-token-xyz")


def test_save_tree_refuses_to_omit_an_existing_row(app_ctx, empty_tree):
    pr = db.prompt_create("Keep me", None)
    with pytest.raises(db.PromptTreeError, match="omitted"):
        db.prompt_save_tree([], [])
    assert [p["uuid"] for p in db.prompt_load_tree()["prompts"]] == [pr["uuid"]]


def test_save_tree_refuses_an_unknown_row(app_ctx, empty_tree):
    with pytest.raises(db.PromptTreeError, match="unknown"):
        db.prompt_save_tree([], [{"uuid": str(uuid4()), "name": "ghost",
                                  "folderId": None}])
    assert db.prompt_load_tree()["prompts"] == []


def test_create_places_at_end_of_folder(app_ctx, empty_tree):
    f = db.prompt_create_folder("F", None)
    db.prompt_create("A", UUID(f["id"]))
    db.prompt_create("B", UUID(f["id"]))
    assert [p["name"] for p in db.prompt_load_tree()["prompts"]] == ["A", "B"]


def test_delete_prompt_keeps_the_versions_derived_from_it(app_ctx, empty_tree):
    src = db.prompt_create("P", None)
    clone = db.prompt_clone(UUID(src["uuid"]))
    assert db.prompt_delete(UUID(src["uuid"])) is True
    got = db.prompt_get(UUID(clone["uuid"]))
    assert got is not None
    assert got["parentUuid"] == src["uuid"] and got["parentExists"] is False
    assert db.prompt_delete(UUID(src["uuid"])) is False


def test_delete_folder_cascades_the_subtree(app_ctx, empty_tree):
    outer = db.prompt_create_folder("Outer", None)
    inner = db.prompt_create_folder("Inner", UUID(outer["id"]))
    db.prompt_create("A", UUID(inner["id"]))
    assert db.prompt_delete_folder(UUID(outer["id"])) is True
    out = db.prompt_load_tree()
    assert out["folders"] == [] and out["prompts"] == []


def test_validate_rejects_dangling_folder(app_ctx):
    with pytest.raises(db.PromptTreeError):
        db.validate_prompt_tree([], [{"uuid": str(uuid4()), "name": "P",
                                      "folderId": str(uuid4())}])


def test_validate_allows_dangling_parent_uuid(app_ctx):
    # parentUuid may reference a deleted version — lineage links can dangle.
    db.validate_prompt_tree([], [{"uuid": str(uuid4()), "name": "P",
                                  "folderId": None, "parentUuid": str(uuid4())}])


def test_validate_rejects_prompt_folder_uuid_collision(app_ctx):
    shared = str(uuid4())
    with pytest.raises(db.PromptTreeError):
        db.validate_prompt_tree(
            [{"id": shared, "name": "F", "parentId": None}],
            [{"uuid": shared, "name": "P", "folderId": None}])


def test_validate_rejects_cycle(app_ctx):
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.PromptTreeError):
        db.validate_prompt_tree(
            [{"id": a, "name": "A", "parentId": b},
             {"id": b, "name": "B", "parentId": a}], [])


def test_clone_copies_content_and_links_parent(app_ctx, empty_tree):
    f = db.prompt_create_folder("F", None)
    src = db.prompt_create("Persona", UUID(f["id"]))
    db.prompt_update_content(UUID(src["uuid"]), "v1 text")
    clone = db.prompt_clone(UUID(src["uuid"]))
    assert clone["parentUuid"] == src["uuid"]
    assert clone["name"] == "Persona 2"
    assert clone["folderId"] == f["id"]
    got = db.prompt_get(UUID(clone["uuid"]))
    assert got["content"] == "v1 text"
    assert got["parentName"] == "Persona"
    assert got["parentExists"] is True
    # The clone sits right after its source in load order.
    order = [p["uuid"] for p in db.prompt_load_tree()["prompts"]]
    assert order.index(clone["uuid"]) == order.index(src["uuid"]) + 1


def test_clone_unknown_uuid(app_ctx, prompt_tree_snapshot):
    assert db.prompt_clone(uuid4()) is None


def test_clone_name_increments_trailing_number(app_ctx, empty_tree):
    a = db.prompt_create("Daily quiz 73", None)
    b = db.prompt_create("take 09", None)
    c = db.prompt_create("Notes", None)
    assert db.prompt_clone(UUID(a["uuid"]))["name"] == "Daily quiz 74"
    assert db.prompt_clone(UUID(b["uuid"]))["name"] == "take 10"  # zero-padding kept
    assert db.prompt_clone(UUID(c["uuid"]))["name"] == "Notes 2"  # no number -> " 2"


def test_clone_name_skips_taken_names(app_ctx, empty_tree):
    """Cloning "… 73" while "… 74" already exists counts on to "… 75"."""
    src = db.prompt_create("Daily quiz 73", None)
    db.prompt_create("Daily quiz 74", None)
    assert db.prompt_clone(UUID(src["uuid"]))["name"] == "Daily quiz 75"


def test_get_reports_deleted_parent(app_ctx, empty_tree):
    src = db.prompt_create("P", None)
    clone = db.prompt_clone(UUID(src["uuid"]))
    db.prompt_delete(UUID(src["uuid"]))
    got = db.prompt_get(UUID(clone["uuid"]))
    assert got["parentUuid"] == src["uuid"]
    assert got["parentExists"] is False
    assert got["parentName"] is None


def test_diff_against_parent_and_grandparent(app_ctx, empty_tree):
    root = db.prompt_create("gen1", None)
    db.prompt_update_content(UUID(root["uuid"]), "alpha\nbeta\n")
    gen2 = db.prompt_clone(UUID(root["uuid"]))
    db.prompt_update_content(UUID(gen2["uuid"]), "alpha\nbeta prime\n")
    gen3 = db.prompt_clone(UUID(gen2["uuid"]))
    db.prompt_update_content(UUID(gen3["uuid"]), "alpha\nbeta prime\ngamma\n")
    # Default: against the immediate parent.
    d = db.prompt_diff(UUID(gen3["uuid"]))
    assert d["ok"] is True
    assert d["against"]["uuid"] == gen2["uuid"]
    assert [a["uuid"] for a in d["ancestors"]] == [gen2["uuid"], root["uuid"]]
    assert any(line.startswith("+gamma") for line in d["lines"])
    # Explicit ancestor: the grandparent.
    d2 = db.prompt_diff(UUID(gen3["uuid"]), UUID(root["uuid"]))
    assert d2["against"]["uuid"] == root["uuid"]
    assert any(line.startswith("-beta") for line in d2["lines"])
    assert any(line.startswith("+beta prime") for line in d2["lines"])


def test_diff_rejects_non_ancestor(app_ctx, empty_tree):
    a = db.prompt_create("A", None)
    b = db.prompt_create("B", None)
    clone = db.prompt_clone(UUID(a["uuid"]))
    d = db.prompt_diff(UUID(clone["uuid"]), UUID(b["uuid"]))
    assert d["ok"] is False
    assert "ancestor" in d["error"]


def test_diff_without_ancestor(app_ctx, empty_tree):
    root = db.prompt_create("P", None)
    d = db.prompt_diff(UUID(root["uuid"]))
    assert d["ok"] is False


def test_ancestors_stop_at_dangling_and_cycles(app_ctx, empty_tree):
    # Dangling: parentUuid points at a version that no longer exists.
    orphan = _lineage_row("P", uuid4())
    assert db.prompt_ancestors(UUID(orphan)) == []
    # Cycle: two versions pointing at each other must not spin.
    a = _lineage_row("A", None)
    b = _lineage_row("B", UUID(a))
    db.db.session.execute(sa.update(Prompt).where(Prompt.uuid == UUID(a))
                          .values(parent_uuid=UUID(b)))
    db.db.session.commit()
    chain = db.prompt_ancestors(UUID(a))
    assert [c.uuid for c in chain] == [UUID(b)]
