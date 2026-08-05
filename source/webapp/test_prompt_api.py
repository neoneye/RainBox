"""Tests for webapp/prompt_api.py.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app (webapp.core.app); DB seeding uses the same endpoints, and each
test deletes the rows it created through the dedicated DELETE endpoints — the
tree PUT can neither create nor delete (docs/ui-tree-persistence.md).
"""
from uuid import uuid4

from webapp.core import app


def _cleanup(client, prompt_uuids):
    for pu in prompt_uuids:
        client.delete(f"/prompt/api/prompts/{pu}")


def _seed_prompt(client, name="ApiTest"):
    """Create one root prompt through the public API; returns its uuid str."""
    resp = client.post("/prompt/api/prompts", json={"name": name})
    assert resp.status_code == 201
    return resp.get_json()["prompt"]["uuid"]


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
        "version": "stale-token-xyz"})
    assert resp.status_code == 409
    assert resp.get_json()["version"]  # fresh token for the re-hydrate


def test_tree_put_refuses_to_omit_an_existing_row():
    """The whole point of the split shape: a payload that drops a row is a
    malformed request, not a deletion (docs/ui-tree-persistence.md)."""
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        tree = client.get("/prompt/api/tree").get_json()
        resp = client.put("/prompt/api/tree", json={
            "folders": tree["folders"],
            "prompts": [p for p in tree["prompts"] if p["uuid"] != pu],
            "version": tree["version"]})
        assert resp.status_code == 400
        assert "omitted" in resp.get_json()["error"]
        # …and nothing was mutated.
        assert any(p["uuid"] == pu
                   for p in client.get("/prompt/api/tree").get_json()["prompts"])
    finally:
        _cleanup(client, [pu])


def test_tree_put_refuses_an_unknown_row():
    client = app.test_client()
    tree = client.get("/prompt/api/tree").get_json()
    resp = client.put("/prompt/api/tree", json={
        "folders": tree["folders"],
        "prompts": tree["prompts"] + [{"uuid": str(uuid4()), "name": "ghost",
                                       "folderId": None}],
        "version": tree["version"]})
    assert resp.status_code == 400
    assert "unknown" in resp.get_json()["error"]


def test_create_and_delete_return_a_token_the_next_put_accepts():
    """Every mutating endpoint hands back the fresh version, or the client's
    next drag 409s for a reason the operator can't see."""
    client = app.test_client()
    folder = client.post("/prompt/api/folders", json={"name": "T-folder"}).get_json()
    made = client.post("/prompt/api/prompts",
                       json={"name": "Inside", "folderId": folder["folder"]["id"]}
                       ).get_json()
    assert made["version"] and made["version"] != folder["version"]
    clone = client.post(f"/prompt/api/prompts/{made['prompt']['uuid']}/clone").get_json()
    assert clone["version"] and clone["version"] != made["version"]
    try:
        tree = client.get("/prompt/api/tree").get_json()
        assert tree["version"] == clone["version"]
        assert client.put("/prompt/api/tree", json={
            "folders": tree["folders"], "prompts": tree["prompts"],
            "version": clone["version"]}).status_code == 200
    finally:
        out = client.delete(f"/prompt/api/folders/{folder['folder']['id']}").get_json()
        assert out["ok"] and out["version"]
    # The folder delete cascaded both prompts inside it.
    left = {p["uuid"] for p in client.get("/prompt/api/tree").get_json()["prompts"]}
    assert made["prompt"]["uuid"] not in left
    assert clone["prompt"]["uuid"] not in left


def test_create_requires_a_name():
    client = app.test_client()
    assert client.post("/prompt/api/prompts", json={"name": "  "}).status_code == 400
    assert client.post("/prompt/api/folders", json={}).status_code == 400


def test_delete_unknown_uuid_404():
    client = app.test_client()
    assert client.delete(f"/prompt/api/prompts/{uuid4()}").status_code == 404
    assert client.delete(f"/prompt/api/folders/{uuid4()}").status_code == 404


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
        _cleanup(client, [pu])


def test_prompt_put_rejects_bad_body():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        resp = client.put(f"/prompt/api/prompts/{pu}", json={"content": 5})
        assert resp.status_code == 400
    finally:
        _cleanup(client, [pu])


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
        assert clone["name"] == "CloneSrc 2"
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
        _cleanup(client, created)


def test_diff_root_prompt_400():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        assert client.get(f"/prompt/api/prompts/{pu}/diff").status_code == 400
    finally:
        _cleanup(client, [pu])


def test_diff_bad_against_400():
    client = app.test_client()
    pu = _seed_prompt(client)
    try:
        resp = client.get(f"/prompt/api/prompts/{pu}/diff?against=nope")
        assert resp.status_code == 400
    finally:
        _cleanup(client, [pu])
