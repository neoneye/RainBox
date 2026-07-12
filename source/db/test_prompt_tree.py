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


def test_save_and_load_roundtrip(app_ctx, empty_tree):
    f_root, f_child, pr = str(uuid4()), str(uuid4()), str(uuid4())
    folders = [
        {"id": f_root, "name": "Root", "description": "top", "parentId": None},
        {"id": f_child, "name": "Child", "parentId": f_root},
    ]
    prompts = [
        {"uuid": pr, "name": "MyPersona", "folderId": f_child, "parentUuid": None},
    ]
    db.prompt_save_tree(folders, prompts)
    out = db.prompt_load_tree()
    assert [f["name"] for f in out["folders"]] == ["Root", "Child"]  # order preserved
    assert out["folders"][1]["parentId"] == f_root
    assert len(out["prompts"]) == 1
    assert out["prompts"][0]["folderId"] == f_child
    assert out["prompts"][0]["parentUuid"] is None
    assert "content" not in out["prompts"][0]  # tree payload stays light
    assert out["version"]


def test_tree_save_preserves_content(app_ctx, empty_tree):
    pr = str(uuid4())
    db.prompt_save_tree([], [{"uuid": pr, "name": "P", "folderId": None}])
    assert db.prompt_update_content(UUID(pr), "the prompt text")
    # A structural save (rename) must not touch content.
    db.prompt_save_tree([], [{"uuid": pr, "name": "P renamed", "folderId": None}])
    got = db.prompt_get(UUID(pr))
    assert got["name"] == "P renamed"
    assert got["content"] == "the prompt text"


def test_content_excluded_from_version(app_ctx, empty_tree):
    pr = str(uuid4())
    db.prompt_save_tree([], [{"uuid": pr, "name": "P", "folderId": None}])
    v1 = db.prompt_tree_version()
    db.prompt_update_content(UUID(pr), "new text")
    assert db.prompt_tree_version() == v1  # autosave never invalidates the tree


def test_version_conflict(app_ctx, prompt_tree_snapshot):
    with pytest.raises(db.PromptTreeConflict):
        db.prompt_save_tree([], [], base_version="stale-token-xyz")


def test_delete_tripwire(app_ctx, empty_tree):
    f = str(uuid4())
    db.prompt_save_tree([{"id": f, "name": "F", "parentId": None}], [])
    # Saving an empty tree would delete the folder; undeclared deletion → refused.
    with pytest.raises(db.PromptTreeError):
        db.prompt_save_tree([], [], expected_deletes=0)


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
    f, src = str(uuid4()), str(uuid4())
    db.prompt_save_tree(
        [{"id": f, "name": "F", "parentId": None}],
        [{"uuid": src, "name": "Persona", "folderId": f}])
    db.prompt_update_content(UUID(src), "v1 text")
    clone = db.prompt_clone(UUID(src))
    assert clone["parentUuid"] == src
    assert clone["name"] == "Persona 2"
    assert clone["folderId"] == f
    got = db.prompt_get(UUID(clone["uuid"]))
    assert got["content"] == "v1 text"
    assert got["parentName"] == "Persona"
    assert got["parentExists"] is True
    # The clone sits right after its source in load order.
    order = [p["uuid"] for p in db.prompt_load_tree()["prompts"]]
    assert order.index(clone["uuid"]) == order.index(src) + 1


def test_clone_unknown_uuid(app_ctx, prompt_tree_snapshot):
    assert db.prompt_clone(uuid4()) is None


def test_clone_name_increments_trailing_number(app_ctx, empty_tree):
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    db.prompt_save_tree([], [
        {"uuid": a, "name": "Daily quiz 73", "folderId": None},
        {"uuid": b, "name": "take 09", "folderId": None},
        {"uuid": c, "name": "Notes", "folderId": None},
    ])
    assert db.prompt_clone(UUID(a))["name"] == "Daily quiz 74"
    assert db.prompt_clone(UUID(b))["name"] == "take 10"  # zero-padding kept
    assert db.prompt_clone(UUID(c))["name"] == "Notes 2"  # no number -> " 2"


def test_clone_name_skips_taken_names(app_ctx, empty_tree):
    """Cloning "… 73" while "… 74" already exists counts on to "… 75"."""
    src, taken = str(uuid4()), str(uuid4())
    db.prompt_save_tree([], [
        {"uuid": src, "name": "Daily quiz 73", "folderId": None},
        {"uuid": taken, "name": "Daily quiz 74", "folderId": None},
    ])
    assert db.prompt_clone(UUID(src))["name"] == "Daily quiz 75"


def test_get_reports_deleted_parent(app_ctx, empty_tree):
    src = str(uuid4())
    db.prompt_save_tree([], [{"uuid": src, "name": "P", "folderId": None}])
    clone = db.prompt_clone(UUID(src))
    db.prompt_save_tree([], [{"uuid": clone["uuid"], "name": "P",
                              "folderId": None, "parentUuid": src}],
                        expected_deletes=1)  # drop the source
    got = db.prompt_get(UUID(clone["uuid"]))
    assert got["parentUuid"] == src
    assert got["parentExists"] is False
    assert got["parentName"] is None


def test_diff_against_parent_and_grandparent(app_ctx, empty_tree):
    root = str(uuid4())
    db.prompt_save_tree([], [{"uuid": root, "name": "gen1", "folderId": None}])
    db.prompt_update_content(UUID(root), "alpha\nbeta\n")
    gen2 = db.prompt_clone(UUID(root))
    db.prompt_update_content(UUID(gen2["uuid"]), "alpha\nbeta prime\n")
    gen3 = db.prompt_clone(UUID(gen2["uuid"]))
    db.prompt_update_content(UUID(gen3["uuid"]), "alpha\nbeta prime\ngamma\n")
    # Default: against the immediate parent.
    d = db.prompt_diff(UUID(gen3["uuid"]))
    assert d["ok"] is True
    assert d["against"]["uuid"] == gen2["uuid"]
    assert [a["uuid"] for a in d["ancestors"]] == [gen2["uuid"], root]
    assert any(line.startswith("+gamma") for line in d["lines"])
    # Explicit ancestor: the grandparent.
    d2 = db.prompt_diff(UUID(gen3["uuid"]), UUID(root))
    assert d2["against"]["uuid"] == root
    assert any(line.startswith("-beta") for line in d2["lines"])
    assert any(line.startswith("+beta prime") for line in d2["lines"])


def test_diff_rejects_non_ancestor(app_ctx, empty_tree):
    a, b = str(uuid4()), str(uuid4())
    db.prompt_save_tree([], [
        {"uuid": a, "name": "A", "folderId": None},
        {"uuid": b, "name": "B", "folderId": None},
    ])
    clone = db.prompt_clone(UUID(a))
    d = db.prompt_diff(UUID(clone["uuid"]), UUID(b))
    assert d["ok"] is False
    assert "ancestor" in d["error"]


def test_diff_without_ancestor(app_ctx, empty_tree):
    root = str(uuid4())
    db.prompt_save_tree([], [{"uuid": root, "name": "P", "folderId": None}])
    d = db.prompt_diff(UUID(root))
    assert d["ok"] is False


def test_ancestors_stop_at_dangling_and_cycles(app_ctx, empty_tree):
    # Dangling: parentUuid points at a version that no longer exists.
    orphan = str(uuid4())
    db.prompt_save_tree([], [{"uuid": orphan, "name": "P", "folderId": None,
                              "parentUuid": str(uuid4())}])
    assert db.prompt_ancestors(UUID(orphan)) == []
    # Cycle: two versions pointing at each other must not spin.
    a, b = str(uuid4()), str(uuid4())
    db.prompt_save_tree([], [
        {"uuid": orphan, "name": "P", "folderId": None, "parentUuid": str(uuid4())},
        {"uuid": a, "name": "A", "folderId": None, "parentUuid": b},
        {"uuid": b, "name": "B", "folderId": None, "parentUuid": a},
    ])
    chain = db.prompt_ancestors(UUID(a))
    assert [c.uuid for c in chain] == [UUID(b)]
