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
    a = db.make_app()
    db.init_db(a)
    with a.app_context():
        res = db.git_check_path(str(tmp_path))
    assert res["ok"] is True
    assert res["branch"]           # some branch name (main/master)
    assert res["path"]             # absolute resolved path


def test_check_path_on_nonrepo(tmp_path):
    a = db.make_app()
    db.init_db(a)
    with a.app_context():
        res = db.git_check_path(str(tmp_path))
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


def test_tree_put_refuses_to_omit_an_existing_row(tmp_path):
    """The whole point of the split shape: a payload that drops a row is a
    malformed request, not a deletion (docs/ui-tree-persistence.md)."""
    _init_repo(tmp_path)
    c = app.test_client()
    made = c.post("/git/api/repos",
                  json={"name": "R", "path": str(tmp_path)}).get_json()
    uuid = made["repo"]["uuid"]
    try:
        tree = c.get("/git/api/tree").get_json()
        resp = c.put("/git/api/tree", json={
            "folders": tree["folders"],
            "repos": [r for r in tree["repos"] if r["uuid"] != uuid],
            "version": tree["version"]})
        assert resp.status_code == 400
        assert "omitted" in resp.get_json()["error"]
        # …and nothing was mutated.
        assert any(r["uuid"] == uuid for r in c.get("/git/api/tree").get_json()["repos"])
    finally:
        c.delete(f"/git/api/repos/{uuid}")


def test_tree_put_refuses_an_unknown_row():
    c = app.test_client()
    tree = c.get("/git/api/tree").get_json()
    resp = c.put("/git/api/tree", json={
        "folders": tree["folders"],
        "repos": tree["repos"] + [{"uuid": str(uuid4()), "name": "ghost",
                                   "folderId": None}],
        "version": tree["version"]})
    assert resp.status_code == 400
    assert "unknown" in resp.get_json()["error"]


def test_tree_put_stale_version_409():
    c = app.test_client()
    tree = c.get("/git/api/tree").get_json()
    resp = c.put("/git/api/tree", json={"folders": tree["folders"],
                                        "repos": tree["repos"],
                                        "version": "stale-token-xyz"})
    assert resp.status_code == 409
    assert resp.get_json()["version"] == tree["version"]


def test_create_repo_rejects_a_path_that_is_not_a_repo(tmp_path):
    resp = app.test_client().post("/git/api/repos",
                                  json={"name": "R", "path": str(tmp_path)})
    assert resp.status_code == 400
    assert "not a git repository" in resp.get_json()["error"]


def test_create_and_delete_return_a_token_the_next_put_accepts(tmp_path):
    """Every mutating endpoint hands back the fresh version, or the client's
    next drag 409s for a reason the operator can't see."""
    _init_repo(tmp_path)
    c = app.test_client()
    folder = c.post("/git/api/folders", json={"name": "T-folder"}).get_json()
    repo = c.post("/git/api/repos",
                  json={"name": "R", "path": str(tmp_path),
                        "folderId": folder["folder"]["id"]}).get_json()
    assert repo["version"] and repo["version"] != folder["version"]
    try:
        tree = c.get("/git/api/tree").get_json()
        assert tree["version"] == repo["version"]
        # The create's own token is enough to save with — no re-hydrate needed.
        assert c.put("/git/api/tree", json={
            "folders": tree["folders"], "repos": tree["repos"],
            "version": repo["version"]}).status_code == 200
    finally:
        out = c.delete(f"/git/api/folders/{folder['folder']['id']}").get_json()
        assert out["ok"] and out["version"]
    # The folder delete cascaded the repo inside it.
    assert not any(r["uuid"] == repo["repo"]["uuid"]
                   for r in c.get("/git/api/tree").get_json()["repos"])


def test_delete_unknown_uuid_404():
    c = app.test_client()
    assert c.delete(f"/git/api/repos/{uuid4()}").status_code == 404
    assert c.delete(f"/git/api/folders/{uuid4()}").status_code == 404
