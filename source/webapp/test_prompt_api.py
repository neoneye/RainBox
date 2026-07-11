"""Tests for webapp/prompt_api.py.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app (webapp.core.app); DB seeding uses the same endpoints, so each
test cleans up the rows it created via the tree PUT's declared-deletes path.
"""
from uuid import uuid4

import sqlalchemy as sa

import db
from db.models import Prompt
from webapp.core import app


def _cleanup(prompt_uuids):
    a = db.make_app()
    db.init_db(a)
    with a.app_context():
        db.db.session.execute(
            sa.delete(Prompt).where(Prompt.uuid.in_(prompt_uuids)))
        db.db.session.commit()


def _seed_prompt(client, name="ApiTest"):
    """Create one root prompt through the public API; returns its uuid str."""
    tree = client.get("/prompt/api/tree").get_json()
    pu = str(uuid4())
    tree["prompts"].append({"uuid": pu, "name": name, "folderId": None})
    resp = client.put("/prompt/api/tree", json={
        "folders": tree["folders"], "prompts": tree["prompts"],
        "version": tree["version"], "deletes": 0})
    assert resp.status_code == 200
    return pu


def test_tree_get_returns_shape():
    out = app.test_client().get("/prompt/api/tree").get_json()
    assert isinstance(out["folders"], list)
    assert isinstance(out["prompts"], list)
    assert out["version"]


def test_tree_put_requires_version():
    resp = app.test_client().put("/prompt/api/tree",
                                 json={"folders": [], "prompts": []})
    assert resp.status_code == 400


def test_tree_put_stale_version_409():
    client = app.test_client()
    tree = client.get("/prompt/api/tree").get_json()
    resp = client.put("/prompt/api/tree", json={
        "folders": tree["folders"], "prompts": tree["prompts"],
        "version": "stale-token-xyz", "deletes": 0})
    assert resp.status_code == 409
    assert resp.get_json()["version"]  # fresh token for the re-hydrate


def test_prompt_content_get_put_roundtrip():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        got = client.get(f"/prompt/api/prompts/{pu}").get_json()
        assert got["ok"] is True and got["content"] == ""
        assert got["parentUuid"] is None
        resp = client.put(f"/prompt/api/prompts/{pu}",
                          json={"content": "You are terse."})
        assert resp.status_code == 200
        got = client.get(f"/prompt/api/prompts/{pu}").get_json()
        assert got["content"] == "You are terse."
    finally:
        _cleanup([pu])


def test_prompt_put_rejects_bad_body():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        resp = client.put(f"/prompt/api/prompts/{pu}", json={"content": 5})
        assert resp.status_code == 400
    finally:
        _cleanup([pu])


def test_prompt_unknown_uuid_404():
    client = app.test_client()
    assert client.get(f"/prompt/api/prompts/{uuid4()}").status_code == 404
    assert client.put(f"/prompt/api/prompts/{uuid4()}",
                      json={"content": "x"}).status_code == 404
    assert client.post(f"/prompt/api/prompts/{uuid4()}/clone").status_code == 404
    assert client.get(f"/prompt/api/prompts/{uuid4()}/diff").status_code == 404


def test_prompt_bad_uuid_400():
    client = app.test_client()
    assert client.get("/prompt/api/prompts/not-a-uuid").status_code == 400


def test_clone_and_diff_flow():
    client = app.test_client()
    src = _seed_prompt(client, name="CloneSrc")
    created = [src]
    try:
        client.put(f"/prompt/api/prompts/{src}", json={"content": "line one\n"})
        res = client.post(f"/prompt/api/prompts/{src}/clone").get_json()
        assert res["ok"] is True
        clone = res["prompt"]
        created.append(clone["uuid"])
        assert clone["parentUuid"] == src
        assert clone["name"] == "CloneSrc"
        # The clone starts as a copy…
        got = client.get(f"/prompt/api/prompts/{clone['uuid']}").get_json()
        assert got["content"] == "line one\n"
        assert got["parentName"] == "CloneSrc"
        # …then diverges, and the diff shows the change against the parent.
        client.put(f"/prompt/api/prompts/{clone['uuid']}",
                   json={"content": "line one\nline two\n"})
        d = client.get(f"/prompt/api/prompts/{clone['uuid']}/diff").get_json()
        assert d["ok"] is True
        assert d["against"]["uuid"] == src
        assert any(line.startswith("+line two") for line in d["lines"])
    finally:
        _cleanup(created)


def test_diff_root_prompt_400():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        assert client.get(f"/prompt/api/prompts/{pu}/diff").status_code == 400
    finally:
        _cleanup([pu])


def test_diff_bad_against_400():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        resp = client.get(f"/prompt/api/prompts/{pu}/diff?against=nope")
        assert resp.status_code == 400
    finally:
        _cleanup([pu])
