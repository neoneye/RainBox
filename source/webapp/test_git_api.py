"""Tests for webapp/git_api.py + db.git filesystem helpers.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app (webapp.core.app); DB seeding uses a db.make_app() context — both
hit the same database, so a committed row is visible to the request.
"""
import subprocess
from uuid import uuid4

import sqlalchemy as sa

import db
from db import GitRepo
from webapp.core import app


def _init_repo(path):
    """A throwaway git repo with one commit (so HEAD/branch resolves)."""
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "--allow-empty", "-m", "init"],
                   check=True, capture_output=True)


def test_tree_get_returns_shape():
    out = app.test_client().get("/git/api/tree").get_json()
    assert isinstance(out["folders"], list)
    assert isinstance(out["repos"], list)
    assert out["version"]


def test_tree_put_requires_version():
    resp = app.test_client().put("/git/api/tree", json={"folders": [], "repos": []})
    assert resp.status_code == 400


def test_check_path_on_real_repo(tmp_path):
    _init_repo(tmp_path)
    res = app.test_client().post("/git/api/check-path",
                                 json={"path": str(tmp_path)}).get_json()
    assert res["ok"] is True
    assert res["branch"]           # some branch name (main/master)
    assert res["path"]             # absolute resolved path


def test_check_path_on_nonrepo(tmp_path):
    res = app.test_client().post("/git/api/check-path",
                                 json={"path": str(tmp_path)}).get_json()
    assert res["ok"] is False
    assert "not a git repository" in res["error"]


def test_repo_detail_lists_root_including_dotgit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi")
    ru = uuid4()
    a = db.make_app()
    db.init_db(a)
    with a.app_context():
        db.db.session.add(GitRepo(uuid=ru, name="R", path=str(tmp_path), position=0))
        db.db.session.commit()
    try:
        d = app.test_client().get(f"/git/api/repos/{ru}/detail").get_json()
        assert d["ok"] is True and d["isRepo"] is True
        names = [e["name"] for e in d["entries"]]
        assert ".git" in names and "README.md" in names          # dotfiles shown
        assert d["entries"][0]["isDir"] is True                   # directories first
    finally:
        with a.app_context():
            db.db.session.execute(sa.delete(GitRepo).where(GitRepo.uuid == ru))
            db.db.session.commit()


def test_repo_detail_unknown_uuid_404():
    resp = app.test_client().get(f"/git/api/repos/{uuid4()}/detail")
    assert resp.status_code == 404
