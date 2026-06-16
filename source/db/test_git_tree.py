"""Tests for the git tree persistence backend (db.models + db.git)."""
from uuid import uuid4

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
    db.db.session.add(GitFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(GitRepo(
        uuid=ru, name="T-repo", folder_uuid=fu,
        path="/tmp/t-repo", description="d", position=0,
    ))
    db.db.session.commit()
    try:
        f = db.db.session.execute(sa.select(GitFolder).where(GitFolder.uuid == fu)).scalar_one()
        r = db.db.session.execute(sa.select(GitRepo).where(GitRepo.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert r.path == "/tmp/t-repo" and r.folder_uuid == fu
        assert f.created_at and r.updated_at  # timestamp defaults fire
    finally:
        db.db.session.execute(sa.delete(GitRepo).where(GitRepo.uuid == ru))
        db.db.session.execute(sa.delete(GitFolder).where(GitFolder.uuid == fu))
        db.db.session.commit()


@pytest.fixture
def git_tree_snapshot(app_ctx):
    """Snapshot the git tables, yield, then restore — non-destructive."""
    def grab(model):
        rows = db.db.session.execute(sa.select(model)).scalars().all()
        return [
            {c.name: getattr(r, c.name) for c in model.__table__.columns if c.name != "id"}
            for r in rows
        ]
    fsnap, rsnap = grab(GitFolder), grab(GitRepo)
    try:
        yield
    finally:
        db.db.session.execute(sa.delete(GitRepo))
        db.db.session.execute(sa.delete(GitFolder))
        for row in fsnap:
            db.db.session.add(GitFolder(**row))
        for row in rsnap:
            db.db.session.add(GitRepo(**row))
        db.db.session.commit()


def test_save_and_load_roundtrip(app_ctx, git_tree_snapshot):
    db.db.session.execute(sa.delete(GitRepo))
    db.db.session.execute(sa.delete(GitFolder))
    db.db.session.commit()
    f_root, f_child, repo = str(uuid4()), str(uuid4()), str(uuid4())
    folders = [
        {"id": f_root, "name": "Root", "description": "top", "parentId": None},
        {"id": f_child, "name": "Child", "parentId": f_root},
    ]
    repos = [
        {"uuid": repo, "name": "MyRepo", "folderId": f_child,
         "path": "/tmp/myrepo", "description": "note"},
    ]
    db.git_save_tree(folders, repos)
    out = db.git_load_tree()
    assert [f["name"] for f in out["folders"]] == ["Root", "Child"]  # order preserved
    assert out["folders"][1]["parentId"] == f_root
    assert out["folders"][0]["created_at"]
    assert len(out["repos"]) == 1
    assert out["repos"][0]["path"] == "/tmp/myrepo"
    assert out["repos"][0]["folderId"] == f_child
    assert out["version"]


def test_version_conflict(app_ctx, git_tree_snapshot):
    with pytest.raises(db.GitTreeConflict):
        db.git_save_tree([], [], base_version="stale-token-xyz")


def test_delete_tripwire(app_ctx, git_tree_snapshot):
    db.db.session.execute(sa.delete(GitRepo))
    db.db.session.execute(sa.delete(GitFolder))
    db.db.session.commit()
    f = str(uuid4())
    db.git_save_tree([{"id": f, "name": "F", "parentId": None}], [])
    # Saving an empty tree would delete the folder; undeclared deletion → refused.
    with pytest.raises(db.GitTreeError):
        db.git_save_tree([], [], expected_deletes=0)


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
