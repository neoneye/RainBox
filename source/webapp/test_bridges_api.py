"""HTTP API for chat-bridge configuration, the bridge-facing config snapshot,
the autostart gate, launcher entries in the desired snapshot, and the chat
delete previews/handlers with bridge blockers.

Live local Postgres via conftest; every row a test creates is removed."""
import json
import socket
import time
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db import BridgeBinding, BridgeConnector, BridgeFolder, Chatroom
from services import registry
from webapp.core import app


@pytest.fixture
def client():
    ctx = app.app_context()
    ctx.push()
    before = {r.key: r.value for r in db.session.query(db.AppSetting).all()}
    registry.CHANNEL.close()
    made = {"connectors": [], "rooms": []}
    try:
        yield app.test_client(), made
    finally:
        db.session.rollback()
        for cu in made["connectors"]:
            db.session.execute(sa.delete(BridgeBinding).where(BridgeBinding.connector_uuid == cu))
            db.session.execute(sa.delete(BridgeFolder).where(BridgeFolder.connector_uuid == cu))
            db.session.execute(sa.delete(BridgeConnector).where(BridgeConnector.uuid == cu))
        for ru in made["rooms"]:
            db.session.execute(sa.delete(Chatroom).where(Chatroom.uuid == ru))
        for row in db.session.query(db.AppSetting).all():
            row.value = before.get(row.key)
        db.session.commit()
        registry.CHANNEL.close()
        ctx.pop()


def _room(made):
    human = db.get_human_user()
    room = db.create_chatroom(f"bridge-api-{uuid4().hex[:6]}", human.uuid, [], room_type="direct")
    made["rooms"].append(room.uuid)
    return room


def _connector(c, made, **kw):
    body = {"name": f"conn-{uuid4().hex[:6]}", "platform": "discord", "token_env": "DISCORD_TOKEN_T", **kw}
    r = c.post("/bridges/api/connectors", json=body)
    assert r.status_code == 201, r.get_json()
    row = r.get_json()["connector"]
    made["connectors"].append(UUID(row["uuid"]))
    return row


def test_connector_crud_and_validation(client):
    c, made = client
    row = _connector(c, made)
    cu = row["uuid"]
    assert c.post("/bridges/api/connectors", json={"name": row["name"], "platform": "discord", "token_env": "X"}).status_code == 409
    assert c.post("/bridges/api/connectors", json={"name": "z", "platform": "zulip", "token_env": "X"}).status_code == 400
    # Telegram's bridge has no connector mode yet: a connector for it could never start.
    assert c.post("/bridges/api/connectors", json={"name": "t", "platform": "telegram", "token_env": "X"}).status_code == 400
    g = c.get(f"/bridges/api/connectors/{cu}").get_json()
    assert g["connector"]["enabled"] is False and g["launcher"] == {"state": "unknown"} and g["autostart"] is True
    r = c.put(f"/bridges/api/connectors/{cu}", json={"enabled": True, "policy": {"direction": "in"}})
    assert r.status_code == 200 and r.get_json()["connector"]["enabled"] is True
    assert c.put(f"/bridges/api/connectors/{cu}", json={"token_env": "Y"}).status_code == 400
    assert c.get(f"/bridges/api/connectors/{uuid4()}").status_code == 404
    assert c.delete(f"/bridges/api/connectors/{cu}").status_code == 200
    made["connectors"].remove(UUID(cu))


def test_tree_and_binding_lifecycle_with_blockers(client):
    c, made = client
    conn = _connector(c, made)
    cu = conn["uuid"]
    room = _room(made)
    f = c.post("/bridges/api/folders", json={"connectorId": cu, "name": "server A"}).get_json()["folder"]
    b = c.post("/bridges/api/bindings", json={"connectorId": cu, "roomUuid": str(room.uuid),
                                              "address": {"channel_id": "42"}, "folderId": f["id"]})
    assert b.status_code == 201, b.get_json()
    binding = b.get_json()["binding"]
    assert binding["roomName"] == room.name and binding["addressKey"] == "channel_id=42"
    dup = c.post("/bridges/api/bindings", json={"connectorId": cu, "roomUuid": str(room.uuid), "address": {"channel_id": "42"}})
    assert dup.status_code == 409
    assert c.post("/bridges/api/bindings", json={"connectorId": cu, "roomUuid": str(room.uuid), "address": {"channel_id": "x"}}).status_code == 400
    # The room now cannot be deleted: preview says so, and the handler refuses.
    preview = c.get(f"/chat/api/rooms/{room.uuid}/delete-preview").get_json()
    assert preview["can_delete"] is False and preview["binding_count"] == 1
    assert preview["bindings"][0]["connector_name"] == conn["name"]
    r = c.delete(f"/chat/api/rooms/{room.uuid}")
    assert r.status_code == 409 and r.get_json()["blockers"][0]["binding_uuid"] == binding["uuid"]
    # Nonempty folder and connector refuse deletion with counts.
    assert c.delete(f"/bridges/api/folders/{f['id']}").status_code == 409
    r = c.delete(f"/bridges/api/connectors/{cu}")
    assert r.status_code == 409 and r.get_json()["blockers"] == {"folder_count": 1, "binding_count": 1}
    # Tree: move the binding out of the folder with the current token.
    tree = c.get("/bridges/api/tree").get_json()
    assert tree["core_url"].startswith("http://127.0.0.1:")
    for x in tree["bindings"]:
        if x["uuid"] == binding["uuid"]:
            x["folderId"] = None
    r = c.put("/bridges/api/tree", json={"connectors": tree["connectors"], "folders": tree["folders"],
                                         "bindings": tree["bindings"], "version": tree["version"]})
    assert r.status_code == 200
    stale = c.put("/bridges/api/tree", json={"connectors": tree["connectors"], "folders": tree["folders"],
                                             "bindings": tree["bindings"], "version": tree["version"]})
    assert stale.status_code == 409 and stale.get_json()["version"]
    assert c.put("/bridges/api/tree", json={"connectors": [], "folders": [], "bindings": []}).status_code == 400
    # Content edits do not move the token.
    v = c.get("/bridges/api/tree").get_json()["version"]
    assert c.put(f"/bridges/api/bindings/{binding['uuid']}", json={"enabled": True}).status_code == 200
    assert c.get("/bridges/api/tree").get_json()["version"] == v
    # Cleanup order proves the guards lift.
    assert c.delete(f"/bridges/api/bindings/{binding['uuid']}").status_code == 200
    assert c.delete(f"/bridges/api/folders/{f['id']}").status_code == 200
    assert c.get(f"/chat/api/rooms/{room.uuid}/delete-preview").get_json()["can_delete"] is True


def test_bridge_config_snapshot_endpoint(client):
    c, made = client
    conn = _connector(c, made, policy={"allowed_senders": ["7"]})
    cu = conn["uuid"]
    room = _room(made)
    b = c.post("/bridges/api/bindings", json={"connectorId": cu, "roomUuid": str(room.uuid),
                                              "address": {"channel_id": "1"}}).get_json()["binding"]
    c.put(f"/bridges/api/bindings/{b['uuid']}", json={"enabled": True})
    r = c.get(f"/bridge/api/connectors/{cu}/config")
    assert r.status_code == 200
    d = r.get_json()
    assert d["schema_version"] == 1 and d["connector"]["token_env"] == "DISCORD_TOKEN_T"
    assert "DISCORD_TOKEN_T" in json.dumps(d) and "value" not in d["connector"]
    [entry] = d["bindings"]
    assert entry["effective_enabled"] is False and entry["policy"]["allowed_senders"] == ["7"]
    rev = d["revision"]
    c.put(f"/bridges/api/connectors/{cu}", json={"enabled": True})
    d2 = c.get(f"/bridge/api/connectors/{cu}/config").get_json()
    assert d2["bindings"][0]["effective_enabled"] is True and d2["revision"] != rev
    assert c.get(f"/bridge/api/connectors/{uuid4()}/config").status_code == 404


def test_desired_snapshot_carries_bridge_entries_and_autostart_gates_them(client, monkeypatch):
    c, made = client
    conn = _connector(c, made, name=f"Main Bot {uuid4().hex[:4]}")
    cu = conn["uuid"]
    c.put(f"/bridges/api/connectors/{cu}", json={"enabled": True})
    entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")
    assert entry["kind"] == "discord_bridge" and entry["enabled"] is True
    assert entry["env"]["BRIDGE_CONNECTOR"] == cu and entry["token_env"] == "DISCORD_TOKEN_T"
    assert entry["state_file"] == {"env": "DISCORD_STATE_FILE", "name": f"bridge-{cu}.json"}
    nonce = entry["restart_nonce"]
    # Autostart off: the gate closes, nonce untouched; on again: nonce bumped.
    r = c.post("/settings/api/set", json={"key": "services.bridges.autostart", "value": False})
    assert r.status_code == 200, r.get_json()
    entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")
    assert entry["enabled"] is False and entry["restart_nonce"] == nonce
    c.post("/settings/api/set", json={"key": "services.bridges.autostart", "value": True})
    entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")
    assert entry["enabled"] is True and entry["restart_nonce"] != nonce
    # Restart through the services endpoint rewrites the connector nonce.
    r = c.post(f"/services/api/restart/bridge:{cu}")
    assert r.status_code == 200 and r.get_json()["restart_nonce"] != entry["restart_nonce"]
    assert c.post(f"/services/api/restart/bridge:{uuid4()}").status_code == 404


def test_connector_writes_push_the_launcher_snapshot(client):
    """A launch-gate change reaches the launcher over the control socket."""
    c, made = client
    mine, theirs = socket.socketpair()
    mine.setblocking(False)
    registry.CHANNEL.attach_socket(theirs)
    registry.CHANNEL.start(app)
    try:
        conn = _connector(c, made)
        cu = conn["uuid"]
        c.put(f"/bridges/api/connectors/{cu}", json={"enabled": True})
        deadline = time.monotonic() + 3
        lines: list[dict] = []
        buf = b""
        while time.monotonic() < deadline and not any(
                any(s.get("key") == f"bridge:{cu}" and s.get("enabled") for s in ln.get("services", [])) for ln in lines):
            try:
                buf += mine.recv(65536)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                lines.append(json.loads(raw))
        assert any(any(s.get("key") == f"bridge:{cu}" and s.get("enabled") for s in ln.get("services", [])) for ln in lines)
    finally:
        mine.close()
        registry.CHANNEL.close()


def test_bridge_config_event_rides_the_chat_notify_channel(client):
    """Every bridge write NOTIFYs the chat channel with a bridge_config
    payload (no room_uuid), which /chat/stream forwards."""
    import psycopg
    c, made = client
    conn = psycopg.connect(db.psycopg_dsn(), autocommit=True)
    try:
        conn.execute(f"LISTEN {db.CHAT_NOTIFY_CHANNEL}")
        row = _connector(c, made)
        got = []
        for note in conn.notifies(timeout=3.0):
            payload = json.loads(note.payload)
            got.append(payload)
            if payload.get("event") == "bridge_config" and payload.get("connector_uuid") == row["uuid"]:
                break
        assert any(p.get("event") == "bridge_config" and p.get("connector_uuid") == row["uuid"] and "room_uuid" not in p
                   for p in got)
    finally:
        conn.close()


def test_credential_endpoints_are_write_only_and_feed_the_launcher_snapshot(client, monkeypatch):
    from services import credential_box
    monkeypatch.setenv(credential_box.KEY_ENV, "k" * 40)
    c, made = client
    conn = _connector(c, made)
    cu = conn["uuid"]
    g = c.get(f"/bridges/api/connectors/{cu}").get_json()
    assert g["credential"] == {"set": False, "updated_at": None, "key_configured": True}
    assert c.put(f"/bridges/api/connectors/{cu}/credential", json={"value": ""}).status_code == 400
    assert c.put(f"/bridges/api/connectors/{uuid4()}/credential", json={"value": "x"}).status_code == 404
    r = c.put(f"/bridges/api/connectors/{cu}/credential", json={"value": "tok-secret-1"})
    assert r.status_code == 200 and r.get_json()["credential"]["set"] is True
    assert "tok-secret-1" not in r.get_data(as_text=True)
    # Nothing a browser can fetch carries it.
    for path in (f"/bridges/api/connectors/{cu}", "/bridges/api/tree", f"/bridge/api/connectors/{cu}/config",
                 "/admin/bridgecredential/", "/admin/bridgeconnector/"):
        resp = c.get(path)
        assert "tok-secret-1" not in resp.get_data(as_text=True), path
    # The launcher snapshot is the one place it appears, and saving while the
    # gate is on rewrites the nonce so the process restarts with it.
    entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")
    assert entry["credential"] == "tok-secret-1"
    c.put(f"/bridges/api/connectors/{cu}", json={"enabled": True})
    nonce = next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")["restart_nonce"]
    c.put(f"/bridges/api/connectors/{cu}/credential", json={"value": "tok-secret-2"})
    entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")
    assert entry["credential"] == "tok-secret-2" and entry["restart_nonce"] != nonce
    assert c.delete(f"/bridges/api/connectors/{cu}/credential").status_code == 200
    assert c.delete(f"/bridges/api/connectors/{cu}/credential").status_code == 404
    assert next(s for s in registry.desired_snapshot()["services"] if s["key"] == f"bridge:{cu}")["credential"] is None
    # Without the key: saving is refused with a 409 that says how to fix it.
    monkeypatch.delenv(credential_box.KEY_ENV)
    r = c.put(f"/bridges/api/connectors/{cu}/credential", json={"value": "tok"})
    assert r.status_code == 409 and r.get_json()["key_configured"] is False and "RAINBOX_CREDENTIAL_KEY" in r.get_json()["error"]
    assert c.get(f"/bridges/api/connectors/{cu}").get_json()["credential"]["key_configured"] is False
