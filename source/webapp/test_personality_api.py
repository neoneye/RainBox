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


def test_create_folder_rejects_a_non_object_body():
    resp = _client().post("/personality/api/folders", json=["not", "an", "object"])
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_create_personality_rejects_a_wrong_typed_name():
    resp = _client().post("/personality/api/personalities", json={"name": ["x"]})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_create_folder_rejects_a_wrong_typed_name():
    resp = _client().post("/personality/api/folders", json={"name": 123})
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_create_folder_rejects_a_wrong_typed_parent_id():
    resp = _client().post("/personality/api/folders",
                          json={"name": "ok", "parentId": {"a": 1}})
    assert resp.status_code == 400, resp.get_data(as_text=True)


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
    try:
        resp = c.delete(f"/personality/api/folders/{folder['folder']['id']}")
        assert resp.status_code == 200 and resp.get_json()["version"]
        tree = c.get("/personality/api/tree").get_json()
        assert not any(f["id"] == folder["folder"]["id"] for f in tree["folders"])
        assert not any(p["uuid"] == inside["uuid"] for p in tree["personalities"])
    finally:
        # Best-effort: if the delete under test worked, the folder cascade
        # already removed both rows and these 404; if it failed, this is what
        # actually cleans up so the shared DB doesn't keep the leftovers.
        c.delete(f"/personality/api/personalities/{inside['uuid']}")
        c.delete(f"/personality/api/folders/{folder['folder']['id']}")


def test_delete_unknown_is_404():
    resp = _client().delete(
        "/personality/api/personalities/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_bad_uuid_is_400():
    assert _client().delete("/personality/api/personalities/not-a-uuid").status_code == 400


def test_content_put_appends_a_revision():
    c = _client()
    made = _create_personality(c, "ContentTest")
    uuid = made["personality"]["uuid"]
    try:
        resp = c.put(f"/personality/api/personalities/{uuid}",
                     json={"content": "Warm, concrete, allergic to filler."})
        assert resp.status_code == 200
        assert resp.get_json()["changed"] is True
        detail = c.get(f"/personality/api/personalities/{uuid}").get_json()
        assert detail["content"] == "Warm, concrete, allergic to filler."
        assert detail["revisionCount"] == 1
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        assert len(revs["revisions"]) == 1 and revs["revisions"][0]["current"] is True
    finally:
        _delete_personality(c, uuid)


def test_unchanged_content_put_reports_no_change():
    c = _client()
    made = _create_personality(c, "NoopTest")
    uuid = made["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "same"})
        resp = c.put(f"/personality/api/personalities/{uuid}", json={"content": "same"})
        assert resp.get_json()["changed"] is False
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        assert len(revs["revisions"]) == 1
    finally:
        _delete_personality(c, uuid)


def test_restore_appends_and_returns_the_old_text():
    c = _client()
    made = _create_personality(c, "RestoreTest")
    uuid = made["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "first"})
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "second"})
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        oldest = revs["revisions"][-1]["uuid"]
        resp = c.post(
            f"/personality/api/personalities/{uuid}/revisions/{oldest}/restore")
        assert resp.status_code == 200
        assert resp.get_json()["content"] == "first"
        after = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        assert len(after["revisions"]) == 3   # appended, not rewound
    finally:
        _delete_personality(c, uuid)


def test_diff_lists_the_change():
    c = _client()
    made = _create_personality(c, "DiffTest")
    uuid = made["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "before"})
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "after"})
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        oldest = revs["revisions"][-1]["uuid"]
        out = c.get(
            f"/personality/api/personalities/{uuid}/revisions/{oldest}/diff").get_json()
        assert out["ok"] is True
        assert any(ln.startswith("-before") for ln in out["lines"])
        assert any(ln.startswith("+after") for ln in out["lines"])
    finally:
        _delete_personality(c, uuid)


def test_foreign_revision_diff_is_404():
    c = _client()
    a = _create_personality(c, "OwnerA")
    b = _create_personality(c, "OwnerB")
    ua, ub = a["personality"]["uuid"], b["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{ua}", json={"content": "a text"})
        rev = c.get(f"/personality/api/personalities/{ua}/revisions"
                    ).get_json()["revisions"][0]["uuid"]
        resp = c.get(f"/personality/api/personalities/{ub}/revisions/{rev}/diff")
        assert resp.status_code == 404
    finally:
        _delete_personality(c, ua)
        _delete_personality(c, ub)


def test_foreign_revision_restore_is_404():
    c = _client()
    a = _create_personality(c, "OwnerA")
    b = _create_personality(c, "OwnerB")
    ua, ub = a["personality"]["uuid"], b["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{ua}", json={"content": "a text"})
        rev = c.get(f"/personality/api/personalities/{ua}/revisions"
                    ).get_json()["revisions"][0]["uuid"]
        resp = c.post(f"/personality/api/personalities/{ub}/revisions/{rev}/restore")
        assert resp.status_code == 404
    finally:
        _delete_personality(c, ua)
        _delete_personality(c, ub)


def test_content_put_requires_a_string():
    c = _client()
    made = _create_personality(c, "TypeTest")
    uuid = made["personality"]["uuid"]
    try:
        assert c.put(f"/personality/api/personalities/{uuid}",
                     json={"content": 42}).status_code == 400
    finally:
        _delete_personality(c, uuid)
