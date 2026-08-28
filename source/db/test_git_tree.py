"""Tests for the git tree persistence backend (db.models + db.git)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import GitFolder, GitRepo


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


def test_git_models_round_trip(app_ctx):
    fu, ru = uuid4(), uuid4()
    db.session.add(GitFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.session.add(GitRepo(
        uuid=ru, name="T-repo", folder_uuid=fu,
        path="/tmp/t-repo", description="d", position=0,
    ))
    db.session.commit()
    try:
        f = db.session.execute(sa.select(GitFolder).where(GitFolder.uuid == fu)).scalar_one()
        r = db.session.execute(sa.select(GitRepo).where(GitRepo.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert r.path == "/tmp/t-repo" and r.folder_uuid == fu
        assert f.created_at and r.updated_at  # timestamp defaults fire
    finally:
        db.session.execute(sa.delete(GitRepo).where(GitRepo.uuid == ru))
        db.session.execute(sa.delete(GitFolder).where(GitFolder.uuid == fu))
        db.session.commit()


@pytest.fixture
def git_tree_snapshot(app_ctx):
    """Snapshot the git tables, yield, then restore — non-destructive."""
    def grab(model):
        rows = db.session.execute(sa.select(model)).scalars().all()
        return [
            {c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"}
            for r in rows
        ]
    fsnap, rsnap = grab(GitFolder), grab(GitRepo)
    try:
        yield
    finally:
        db.session.execute(sa.delete(GitRepo))
        db.session.execute(sa.delete(GitFolder))
        for row in fsnap:
            db.session.add(GitFolder(**row))
        for row in rsnap:
            db.session.add(GitRepo(**row))
        db.session.commit()


@pytest.fixture
def clean_tree(git_tree_snapshot):
    """An empty git tree for the duration of the test; git_tree_snapshot puts
    the operator's real rows back afterwards."""
    db.session.execute(sa.delete(GitRepo))
    db.session.execute(sa.delete(GitFolder))
    db.session.commit()
    yield


def test_save_and_load_roundtrip(clean_tree):
    f_root = db.git_create_folder("Root", None)
    f_child = db.git_create_folder("Child", UUID(f_root["id"]))
    repo = db.git_create_repo("MyRepo", "/tmp/myrepo", UUID(f_child["id"]))
    db.git_save_tree(
        [{"id": f_root["id"], "name": "Root", "description": "top", "parentId": None},
         {"id": f_child["id"], "name": "Child", "parentId": f_root["id"]}],
        [{"uuid": repo["uuid"], "name": "MyRepo", "folderId": f_child["id"],
          "description": "note"}])
    out = db.git_load_tree()
    assert [f["name"] for f in out["folders"]] == ["Root", "Child"]  # order preserved
    assert out["folders"][1]["parentId"] == f_root["id"]
    assert out["folders"][0]["created_at"]
    assert len(out["repos"]) == 1
    assert out["repos"][0]["path"] == "/tmp/myrepo"
    assert out["repos"][0]["description"] == "note"
    assert out["repos"][0]["folderId"] == f_child["id"]
    assert out["version"]


def test_version_conflict(clean_tree):
    with pytest.raises(db.GitTreeConflict):
        db.git_save_tree([], [], base_version="stale-token-xyz")


def test_save_tree_refuses_to_omit_an_existing_row(clean_tree):
    repo = db.git_create_repo("Keep me", "/tmp/keep", None)
    with pytest.raises(db.GitTreeError, match="omitted"):
        db.git_save_tree([], [])
    assert [r["uuid"] for r in db.git_load_tree()["repos"]] == [repo["uuid"]]


def test_save_tree_refuses_an_unknown_row(clean_tree):
    with pytest.raises(db.GitTreeError, match="unknown"):
        db.git_save_tree([], [{"uuid": str(uuid4()), "name": "ghost",
                               "folderId": None}])
    assert db.git_load_tree()["repos"] == []


def test_save_tree_never_touches_the_path(clean_tree):
    repo = db.git_create_repo("R", "/tmp/real", None)
    db.git_save_tree([], [{"uuid": repo["uuid"], "name": "R", "folderId": None,
                           "path": "/tmp/somewhere-else"}])
    assert db.git_load_tree()["repos"][0]["path"] == "/tmp/real"


def test_create_places_at_end_of_folder(clean_tree):
    f = db.git_create_folder("F", None)
    db.git_create_repo("A", "/tmp/a", UUID(f["id"]))
    db.git_create_repo("B", "/tmp/b", UUID(f["id"]))
    assert [r["name"] for r in db.git_load_tree()["repos"]] == ["A", "B"]


def test_delete_repo(clean_tree):
    repo = db.git_create_repo("R", "/tmp/r", None)
    assert db.git_delete_repo(UUID(repo["uuid"])) is True
    assert db.git_load_tree()["repos"] == []
    assert db.git_delete_repo(UUID(repo["uuid"])) is False


def test_delete_folder_cascades_the_subtree(clean_tree):
    outer = db.git_create_folder("Outer", None)
    inner = db.git_create_folder("Inner", UUID(outer["id"]))
    db.git_create_repo("R", "/tmp/r", UUID(inner["id"]))
    assert db.git_delete_folder(UUID(outer["id"])) is True
    out = db.git_load_tree()
    assert out["folders"] == [] and out["repos"] == []


def test_create_and_delete_hand_back_a_usable_version(clean_tree):
    f = db.git_create_folder("F", None)
    repo = db.git_create_repo("R", "/tmp/r", None)
    db.git_save_tree(
        [{"id": f["id"], "name": "F", "parentId": None}],
        [{"uuid": repo["uuid"], "name": "R", "folderId": None}],
        base_version=db.git_tree_version())
    db.git_delete_repo(UUID(repo["uuid"]))
    db.git_save_tree([{"id": f["id"], "name": "F", "parentId": None}], [],
                     base_version=db.git_tree_version())


def test_validate_rejects_dangling_folder(app_ctx):
    with pytest.raises(db.GitTreeError):
        db.validate_git_tree([], [{"uuid": str(uuid4()), "name": "R",
                                   "path": "/x", "folderId": str(uuid4())}])


def test_validate_rejects_repo_folder_uuid_collision(app_ctx):
    shared = str(uuid4())
    with pytest.raises(db.GitTreeError):
        db.validate_git_tree(
            [{"id": shared, "name": "F", "parentId": None}],
            [{"uuid": shared, "name": "R", "path": "/x", "folderId": None}])


def test_validate_rejects_cycle(app_ctx):
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.GitTreeError):
        db.validate_git_tree(
            [{"id": a, "name": "A", "parentId": b},
             {"id": b, "name": "B", "parentId": a}], [])
