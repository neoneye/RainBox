"""Tests for webapp/profile_api.py.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app (webapp.core.app); DB seeding uses the same endpoints, so each
test cleans up the rows it created.
"""
import json
from uuid import uuid4

import yaml

import sqlalchemy as sa

import db
from db.models import Profile
from webapp.core import app


def _cleanup(profile_uuids):
    a = db.make_app()
    db.init_db(a)
    with a.app_context():
        db.db.session.execute(
            sa.delete(Profile).where(Profile.uuid.in_(profile_uuids)))
        db.db.session.commit()


def _seed_profile(client, name="ApiTest"):
    """Create one root profile through the public API; returns its uuid str."""
    tree = client.get("/profile/api/tree").get_json()
    pu = str(uuid4())
    folders = [{"id": f["id"], "name": f["name"],
                "description": f.get("description") or "",
                "parentId": f.get("parentId")}
               for f in tree["folders"] if not f.get("builtin")]
    profiles = [{"uuid": p["uuid"], "name": p["name"], "folderId": p.get("folderId")}
                for p in tree["profiles"] if not p.get("builtin")]
    profiles.append({"uuid": pu, "name": name, "folderId": None})
    resp = client.put("/profile/api/tree", json={
        "folders": folders, "profiles": profiles,
        "version": tree["version"], "deletes": 0})
    assert resp.status_code == 200, resp.get_json()
    return pu


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
        "folders": [], "profiles": [], "version": "stale-token-xyz", "deletes": 0})
    assert resp.status_code == 409 and resp.get_json()["version"]
    # A payload carrying a built-in uuid is refused outright.
    bp = next(p for p in tree["profiles"] if p.get("builtin"))
    resp = client.put("/profile/api/tree", json={
        "folders": [], "profiles": [{"uuid": bp["uuid"], "name": "X", "folderId": None}],
        "version": tree["version"], "deletes": 0})
    assert resp.status_code == 400


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
        _cleanup([pu])


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
        _cleanup([pu])


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
        _cleanup([res["profile"]["uuid"]])


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
        _cleanup(created)


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
        _cleanup([pu])


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
        _cleanup([pu])


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
        _cleanup([pu])


def test_export_unknown_profile_is_404():
    resp = app.test_client().get(f"/profile/api/profiles/{uuid4()}/export")
    assert resp.status_code == 404
