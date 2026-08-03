"""Tests for webapp/personality_api.py.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app; each test creates its rows through the public endpoints and
deletes them the same way, so nothing leaks between runs.
"""
from webapp.core import app


def _client():
    return app.test_client()


def _create_personality(client, name="ApiTest"):
    resp = client.post("/personality/api/personalities",
                       json={"name": name, "folderId": None})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


def _delete_personality(client, uuid):
    assert client.delete(f"/personality/api/personalities/{uuid}").status_code == 200


def test_tree_get_returns_shape():
    out = _client().get("/personality/api/tree").get_json()
    assert isinstance(out["folders"], list)
    assert isinstance(out["personalities"], list)
    assert out["version"]


def test_create_returns_201_and_a_usable_version():
    c = _client()
    made = _create_personality(c)
    try:
        assert made["personality"]["name"] == "ApiTest"
        assert made["personality"]["revisionCount"] == 0
        # the version handed back by the POST must satisfy the very next PUT
        tree = c.get("/personality/api/tree").get_json()
        resp = c.put("/personality/api/tree", json={
            "folders": tree["folders"], "personalities": tree["personalities"],
            "version": made["version"]})
        assert resp.status_code == 200, resp.get_data(as_text=True)
    finally:
        _delete_personality(c, made["personality"]["uuid"])


def test_tree_put_requires_version():
    resp = _client().put("/personality/api/tree",
                         json={"folders": [], "personalities": []})
    assert resp.status_code == 400
    assert "version" in resp.get_json()["error"]


def test_tree_put_rejects_an_omitted_row():
    c = _client()
    made = _create_personality(c, "OmitMe")
    try:
        tree = c.get("/personality/api/tree").get_json()
        keep = [p for p in tree["personalities"]
                if p["uuid"] != made["personality"]["uuid"]]
        resp = c.put("/personality/api/tree", json={
            "folders": tree["folders"], "personalities": keep,
            "version": tree["version"]})
        assert resp.status_code == 400
        assert "omitted" in resp.get_json()["error"]
        # and the row is still there
        after = c.get("/personality/api/tree").get_json()["personalities"]
        assert any(p["uuid"] == made["personality"]["uuid"] for p in after)
    finally:
        _delete_personality(c, made["personality"]["uuid"])


def test_tree_put_stale_version_is_409():
    c = _client()
    tree = c.get("/personality/api/tree").get_json()
    made = _create_personality(c, "Stale")       # invalidates the token above
    try:
        resp = c.put("/personality/api/tree", json={
            "folders": tree["folders"], "personalities": tree["personalities"],
            "version": tree["version"]})
        assert resp.status_code == 409
        assert resp.get_json()["version"]        # the fresh token to retry with
    finally:
        _delete_personality(c, made["personality"]["uuid"])


def test_delete_folder_cascades_and_returns_version():
    c = _client()
    folder = c.post("/personality/api/folders",
                    json={"name": "DelFolder", "parentId": None}).get_json()
    inside = c.post("/personality/api/personalities",
                    json={"name": "Inside", "folderId": folder["folder"]["id"]}
                    ).get_json()["personality"]
    resp = c.delete(f"/personality/api/folders/{folder['folder']['id']}")
    assert resp.status_code == 200 and resp.get_json()["version"]
    tree = c.get("/personality/api/tree").get_json()
    assert not any(f["id"] == folder["folder"]["id"] for f in tree["folders"])
    assert not any(p["uuid"] == inside["uuid"] for p in tree["personalities"])


def test_delete_unknown_is_404():
    resp = _client().delete(
        "/personality/api/personalities/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_bad_uuid_is_400():
    assert _client().delete("/personality/api/personalities/not-a-uuid").status_code == 400
