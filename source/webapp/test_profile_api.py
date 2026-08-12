"""Tests for webapp/profile_api.py.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app (webapp.core.app); DB seeding uses the same endpoints, and each
test deletes the rows it created through the dedicated DELETE endpoints — the
tree PUT can neither create nor delete (notes/ui-tree-persistence.md).
"""
import json
from uuid import uuid4

import yaml

from webapp.core import app


def _cleanup(client, profile_uuids):
    for pu in profile_uuids:
        client.delete(f"/profile/api/profiles/{pu}")


def _user_rows(client):
    """The tree's user-owned rows, projected to the PUT's field names (the
    virtual built-ins never ride a save)."""
    tree = client.get("/profile/api/tree").get_json()
    folders = [{"id": f["id"], "name": f["name"],
                "description": f.get("description") or "",
                "parentId": f.get("parentId")}
               for f in tree["folders"] if not f.get("builtin")]
    profiles = [{"uuid": p["uuid"], "name": p["name"], "folderId": p.get("folderId")}
                for p in tree["profiles"] if not p.get("builtin")]
    return folders, profiles, tree["version"]


def _seed_profile(client, name="ApiTest"):
    """Create one root profile through the public API; returns its uuid str."""
    resp = client.post("/profile/api/profiles", json={"name": name})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["profile"]["uuid"]


def test_tree_get_shape_includes_builtins():
    out = app.test_client().get("/profile/api/tree").get_json()
    assert isinstance(out["folders"], list) and isinstance(out["profiles"], list)
    assert out["version"]
    builtins = [p for p in out["profiles"] if p.get("builtin")]
    assert len(builtins) == 21
    assert all("summary" in p for p in out["profiles"])
    assert all("data" not in p for p in out["profiles"])


def test_tree_put_guards():
    client = app.test_client()
    assert client.put("/profile/api/tree",
                      json={"folders": [], "profiles": []}).status_code == 400
    tree = client.get("/profile/api/tree").get_json()
    resp = client.put("/profile/api/tree", json={
        "folders": [], "profiles": [], "version": "stale-token-xyz"})
    assert resp.status_code == 409 and resp.get_json()["version"]
    # A payload carrying a built-in uuid is refused outright.
    bp = next(p for p in tree["profiles"] if p.get("builtin"))
    resp = client.put("/profile/api/tree", json={
        "folders": [], "profiles": [{"uuid": bp["uuid"], "name": "X", "folderId": None}],
        "version": tree["version"]})
    assert resp.status_code == 400


def test_tree_put_refuses_to_omit_an_existing_row():
    """The whole point of the split shape: a payload that drops a row is a
    malformed request, not a deletion (notes/ui-tree-persistence.md)."""
    client = app.test_client()
    pu = _seed_profile(client)
    try:
        folders, profiles, version = _user_rows(client)
        resp = client.put("/profile/api/tree", json={
            "folders": folders,
            "profiles": [p for p in profiles if p["uuid"] != pu],
            "version": version})
        assert resp.status_code == 400
        assert "omitted" in resp.get_json()["error"]
        # …and nothing was mutated.
        assert any(p["uuid"] == pu for p in _user_rows(client)[1])
    finally:
        _cleanup(client, [pu])


def test_tree_put_refuses_an_unknown_row():
    client = app.test_client()
    folders, profiles, version = _user_rows(client)
    resp = client.put("/profile/api/tree", json={
        "folders": folders,
        "profiles": profiles + [{"uuid": str(uuid4()), "name": "ghost",
                                 "folderId": None}],
        "version": version})
    assert resp.status_code == 400
    assert "unknown" in resp.get_json()["error"]


def test_create_and_delete_return_a_token_the_next_put_accepts():
    """Every mutating endpoint hands back the fresh version, or the client's
    next drag 409s for a reason the operator can't see."""
    client = app.test_client()
    folder = client.post("/profile/api/folders", json={"name": "T-folder"}).get_json()
    made = client.post("/profile/api/profiles",
                       json={"name": "Inside", "folderId": folder["folder"]["id"]}
                       ).get_json()
    assert made["version"] and made["version"] != folder["version"]
    dup = client.post(f"/profile/api/profiles/{made['profile']['uuid']}/duplicate").get_json()
    assert dup["version"] and dup["version"] != made["version"]
    try:
        folders, profiles, version = _user_rows(client)
        assert version == dup["version"]
        assert client.put("/profile/api/tree", json={
            "folders": folders, "profiles": profiles,
            "version": dup["version"]}).status_code == 200
    finally:
        out = client.delete(f"/profile/api/folders/{folder['folder']['id']}").get_json()
        assert out["ok"] and out["version"]
    # The folder delete cascaded both profiles inside it.
    left = {p["uuid"] for p in _user_rows(client)[1]}
    assert made["profile"]["uuid"] not in left
    assert dup["profile"]["uuid"] not in left


def test_create_and_delete_refuse_builtins():
    """The Templates folder is virtual: it can neither hold a user row nor be
    deleted, and neither can the templates in it."""
    client = app.test_client()
    tree = client.get("/profile/api/tree").get_json()
    tf = next(f["id"] for f in tree["folders"] if f.get("builtin"))
    bp = next(p["uuid"] for p in tree["profiles"] if p.get("builtin"))
    assert client.post("/profile/api/profiles",
                       json={"name": "X", "folderId": tf}).status_code == 400
    assert client.post("/profile/api/folders",
                       json={"name": "X", "parentId": tf}).status_code == 400
    assert client.delete(f"/profile/api/folders/{tf}").status_code == 400
    assert client.delete(f"/profile/api/profiles/{bp}").status_code == 400


def test_create_requires_a_name():
    client = app.test_client()
    assert client.post("/profile/api/profiles", json={"name": " "}).status_code == 400
    assert client.post("/profile/api/folders", json={}).status_code == 400


def test_delete_unknown_uuid_404():
    client = app.test_client()
    assert client.delete(f"/profile/api/profiles/{uuid4()}").status_code == 404
    assert client.delete(f"/profile/api/folders/{uuid4()}").status_code == 404


def test_data_roundtrip_canonicalize_and_summary():
    client = app.test_client()
    pu = _seed_profile(client)
    try:
        got = client.get(f"/profile/api/profiles/{pu}").get_json()
        assert got["ok"] is True and got["data"] == {} and got["builtin"] is False
        resp = client.put(f"/profile/api/profiles/{pu}",
                          json={"data": {"full_name": "Ada T", "city": "", "units": "metric"}})
        assert resp.status_code == 200
        assert resp.get_json()["summary"]["full_name"] == "Ada T"
        got = client.get(f"/profile/api/profiles/{pu}").get_json()
        assert got["data"] == {"full_name": "Ada T", "units": "metric"}  # "" canonicalized away
    finally:
        _cleanup(client, [pu])


def test_data_put_rejections():
    client = app.test_client()
    pu = _seed_profile(client)
    try:
        r = client.put(f"/profile/api/profiles/{pu}", json={"data": {"units": "furlongs"}})
        assert r.status_code == 400 and "units" in r.get_json()["error"]
        r = client.put(f"/profile/api/profiles/{pu}", json={"data": {"dynamic": {}}})
        assert r.status_code == 400
        assert client.put(f"/profile/api/profiles/{pu}",
                          json={"data": "nope"}).status_code == 400
    finally:
        _cleanup(client, [pu])


def test_builtin_read_only_and_duplicate():
    client = app.test_client()
    tree = client.get("/profile/api/tree").get_json()
    bp = next(p for p in tree["profiles"] if p.get("builtin") and p["name"] == "Denmark")
    got = client.get(f"/profile/api/profiles/{bp['uuid']}").get_json()
    assert got["ok"] is True and got["builtin"] is True
    assert got["data"]["full_name"] == "Øjvind Winge"
    r = client.put(f"/profile/api/profiles/{bp['uuid']}", json={"data": {}})
    assert r.status_code == 400 and "built-in" in r.get_json()["error"]
    res = client.post(f"/profile/api/profiles/{bp['uuid']}/duplicate").get_json()
    try:
        assert res["ok"] is True and res["profile"]["name"] == "Denmark"
        assert res["profile"]["folderId"] is None
    finally:
        _cleanup(client, [res["profile"]["uuid"]])


def test_duplicate_user_owned_copies_data():
    client = app.test_client()
    pu = _seed_profile(client, name="DupSrc")
    created = [pu]
    try:
        client.put(f"/profile/api/profiles/{pu}", json={"data": {"full_name": "Src Person"}})
        res = client.post(f"/profile/api/profiles/{pu}/duplicate").get_json()
        assert res["ok"] is True
        created.append(res["profile"]["uuid"])
        assert res["profile"]["name"] == "DupSrc copy"
        got = client.get(f"/profile/api/profiles/{res['profile']['uuid']}").get_json()
        assert got["data"] == {"full_name": "Src Person"}
    finally:
        _cleanup(client, created)


def test_bad_and_unknown_uuids():
    client = app.test_client()
    assert client.get("/profile/api/profiles/not-a-uuid").status_code == 400
    assert client.get(f"/profile/api/profiles/{uuid4()}").status_code == 404
    assert client.put(f"/profile/api/profiles/{uuid4()}",
                      json={"data": {}}).status_code == 404
    assert client.post(f"/profile/api/profiles/{uuid4()}/duplicate").status_code == 404


# --- export -------------------------------------------------------------------


def _seed_export_profile(client):
    """A profile carrying all three exportable blocks."""
    pu = _seed_profile(client, name="ExportTest")
    client.put(f"/profile/api/profiles/{pu}",
               json={"data": {"full_name": "Ada T", "timezone": "Europe/Copenhagen"}})
    client.put(f"/profile/api/profiles/{pu}/languages",
               json={"rows": [{"tag": "en-US", "level": "intermediate",
                               "stance": "prefer", "note": "primary"}]})
    client.put(f"/profile/api/profiles/{pu}/calibration",
               json={"topics": [{"topic": "Mathematics", "level": "expert",
                                 "stance": "neutral", "depth": "concise"}]})
    return pu


def test_export_defaults_to_json_with_every_section():
    client = app.test_client()
    pu = _seed_export_profile(client)
    try:
        body = client.get(f"/profile/api/profiles/{pu}/export").get_json()
        assert body["ok"] is True and body["format"] == "json"
        doc = json.loads(body["text"])
        assert {"timezone", "language", "knowledge"} <= set(doc)
        assert doc["timezone"] == "Europe/Copenhagen"
    finally:
        _cleanup(client, [pu])


def test_export_honours_format_and_sections():
    client = app.test_client()
    pu = _seed_export_profile(client)
    try:
        body = client.get(
            f"/profile/api/profiles/{pu}/export"
            "?format=yaml&sections=languages").get_json()
        assert body["ok"] is True
        doc = yaml.safe_load(body["text"])
        assert set(doc) == {"language"}
        assert doc["language"][0]["code"] == "en-US"
    finally:
        _cleanup(client, [pu])


def test_export_reports_utf8_byte_sizes_for_every_format():
    """The dialog compares formats, so all three sizes must describe the same
    document — and be bytes, not characters, or non-ASCII values undercount."""
    client = app.test_client()
    pu = _seed_export_profile(client)
    try:
        client.put(f"/profile/api/profiles/{pu}",
                   json={"data": {"city": "København"}})
        body = client.get(f"/profile/api/profiles/{pu}/export"
                          "?format=yaml&sections=profile").get_json()
        assert set(body["sizes"]) == {"json", "yaml", "xml"}
        assert body["sizes"]["yaml"] == len(body["text"].encode("utf-8"))
        assert body["sizes"]["yaml"] > len(body["text"])      # ø costs 2 bytes
        for size in body["sizes"].values():
            assert size > 0
    finally:
        _cleanup(client, [pu])


def test_export_rejects_unknown_format_and_section():
    client = app.test_client()
    pu = _seed_export_profile(client)
    try:
        bad_fmt = client.get(f"/profile/api/profiles/{pu}/export?format=toml")
        assert bad_fmt.status_code == 400
        bad_sec = client.get(
            f"/profile/api/profiles/{pu}/export?sections=languages,secrets")
        assert bad_sec.status_code == 400
        assert "secrets" in bad_sec.get_json()["error"]
    finally:
        _cleanup(client, [pu])


def test_export_unknown_profile_is_404():
    resp = app.test_client().get(f"/profile/api/profiles/{uuid4()}/export")
    assert resp.status_code == 404
