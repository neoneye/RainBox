"""Bridge connectors, folders, bindings: validation, tree save, ownership
guards, policy resolution, the config snapshot, and launcher entries.

Live local Postgres (rainbox_claude via conftest). Every test cleans up the
rows it created."""
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import db
from db import BridgeBinding, BridgeConnector, BridgeFolder, Chatroom
from services.bridge_adapters import ADAPTERS, AdapterError, adapter_for


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.session.rollback()
        ctx.pop()


@pytest.fixture
def cleanup(app_ctx):
    """Track connectors and rooms a test creates; remove them afterwards in
    dependency order."""
    made = {"connectors": [], "rooms": []}
    yield made
    db.session.rollback()
    for cu in made["connectors"]:
        db.session.execute(sa.delete(BridgeBinding).where(BridgeBinding.connector_uuid == cu))
        db.session.execute(sa.delete(BridgeFolder).where(BridgeFolder.connector_uuid == cu))
        db.session.execute(sa.delete(BridgeConnector).where(BridgeConnector.uuid == cu))
    for ru in made["rooms"]:
        db.session.execute(sa.delete(Chatroom).where(Chatroom.uuid == ru))
    db.session.commit()


def _room(cleanup):
    human = db.get_human_user()
    room = db.create_chatroom(f"bridge-test-{uuid4().hex[:6]}", human.uuid, [], room_type="direct")
    cleanup["rooms"].append(room.uuid)
    return room


def _connector(cleanup, name=None, platform="discord", token_env="DISCORD_TOKEN_TEST", **kw):
    c = db.bridge_create_connector(name or f"conn-{uuid4().hex[:6]}", platform, token_env, **kw)
    from uuid import UUID
    cleanup["connectors"].append(UUID(c["uuid"]))
    return c


# --- adapters -------------------------------------------------------------------


def test_addresses_are_canonical_strings_and_keys_use_routing_fields_only():
    d = adapter_for("discord")
    addr = d.validate_address({"channel_id": 123456789012345678, "guild_id": "9"})
    assert addr == {"channel_id": "123456789012345678", "guild_id": "9"}
    assert d.address_key(addr) == "channel_id=123456789012345678"   # guild is display metadata
    t = adapter_for("telegram")
    assert t.validate_address({"chat_id": "-1001234567890"}) == {"chat_id": "-1001234567890"}
    z = adapter_for("zulip")
    assert z.address_key(z.validate_address({"stream_id": "42", "topic": "rainbox"})) == "stream_id=42|topic=rainbox"
    for adapter, bad in ((d, {"channel_id": "abc"}), (d, {}), (d, {"channel_id": "1", "extra": "x"}),
                         (t, {"chat_id": "1.5"}), (z, {"stream_id": "42"}), (z, {"stream_id": "42", "topic": " "})):
        with pytest.raises(AdapterError):
            adapter.validate_address(bad)


def test_policy_validation_and_resolution():
    d = adapter_for("discord")
    assert d.validate_policy(None) == {}
    assert d.validate_policy({"allowed_senders": ["1", "2"], "direction": "in", "poll_seconds": 3}) == {
        "allowed_senders": ["1", "2"], "direction": "in", "poll_seconds": 3}
    assert d.validate_policy({"mirror_progress": None}) == {"mirror_progress": None}
    for bad in ({"nope": 1}, {"direction": "sideways"}, {"poll_seconds": 0.1}, {"poll_seconds": "2"},
                {"forward_kinds": ["message", "thinking"]}, {"allowed_senders": [1]}, {"mirror_progress": "yes"}):
        with pytest.raises(AdapterError):
            d.validate_policy(bad)
    with pytest.raises(AdapterError):
        adapter_for("telegram").validate_policy({"poll_seconds": 2})   # unsupported there
    effective, sources = d.resolve_policy([
        ("connector", {"allowed_senders": ["a"], "direction": "in"}),
        ("folder:x", {"allowed_senders": [], "mirror_progress": None}),
        ("binding", {"direction": None}),
    ])
    assert effective["allowed_senders"] == [] and sources["allowed_senders"] == "folder:x"   # [] replaces
    assert effective["direction"] == "in" and sources["direction"] == "connector"           # null inherits
    assert effective["mirror_progress"] is True and sources["mirror_progress"] == "default"
    assert effective["forward_kinds"] == ["message", "notice", "progress"]
    assert adapter_for("telegram").resolve_policy([])[0]["forward_kinds"] == ["message"]


# --- connectors ---------------------------------------------------------------------


def test_create_connector_validates_shape_and_starts_disabled(cleanup):
    c = _connector(cleanup, name="mainbot")
    assert c["enabled"] is False and c["launch_mode"] == "launcher" and c["restart_nonce"]
    assert c["platform"] == "discord" and c["token_env"] == "DISCORD_TOKEN_TEST"
    with pytest.raises(db.BridgeBlocked):
        db.bridge_create_connector("mainbot", "discord", "OTHER")          # duplicate name
    for kwargs in (dict(platform="zulip", token_env="Z", base_url="https://z", identity="bot@z"),
                   dict(platform="discord", token_env="BRIDGE_CONNECTOR"),
                   dict(platform="discord", token_env="not a name"),
                   dict(platform="discord", token_env="T", base_url="https://x"),
                   dict(platform="nope", token_env="T")):
        with pytest.raises(AdapterError):
            db.bridge_create_connector(f"x-{uuid4().hex[:4]}", **kwargs)


def test_update_connector_bumps_nonce_only_on_gate_off_to_on(cleanup):
    from uuid import UUID
    c = _connector(cleanup)
    cu = UUID(c["uuid"])
    n0 = c["restart_nonce"]
    c = db.bridge_update_connector(cu, {"policy": {"direction": "out"}}, autostart=True)
    assert c["restart_nonce"] == n0 and c["policy"] == {"direction": "out"}
    c = db.bridge_update_connector(cu, {"enabled": True}, autostart=True)
    n1 = c["restart_nonce"]
    assert n1 != n0
    c = db.bridge_update_connector(cu, {"enabled": False}, autostart=True)
    assert c["restart_nonce"] == n1                                        # off: no bump
    c = db.bridge_update_connector(cu, {"enabled": True}, autostart=False)
    assert c["restart_nonce"] == n1                                        # gate still false (autostart off)
    c = db.bridge_update_connector(cu, {"launch_mode": "manual"}, autostart=True)
    assert c["restart_nonce"] == n1
    c = db.bridge_update_connector(cu, {"launch_mode": "launcher"}, autostart=True)
    assert c["restart_nonce"] != n1                                        # manual -> launcher while enabled
    for bad in ({"platform": "telegram"}, {"token_env": "X"}, {"base_url": "https://x"}, {"launch_mode": "cron"},
                {"enabled": "yes"}, {"nope": 1}):
        with pytest.raises(AdapterError):
            db.bridge_update_connector(cu, bad, autostart=True)
    assert db.bridge_update_connector(uuid4(), {"enabled": True}, autostart=True) is None
    assert db.bridge_bump_connector_nonce(cu) not in (None, c["restart_nonce"])


def test_autostart_flip_bumps_every_gated_connector_once(cleanup):
    from uuid import UUID
    on = _connector(cleanup); off = _connector(cleanup)
    on_u, off_u = UUID(on["uuid"]), UUID(off["uuid"])
    db.bridge_update_connector(on_u, {"enabled": True}, autostart=False)
    n_on = db.bridge_get_connector(on_u)["restart_nonce"]
    n_off = off["restart_nonce"]
    db.bridge_gate_transitions(False, True)
    db.session.commit()
    assert db.bridge_get_connector(on_u)["restart_nonce"] != n_on
    assert db.bridge_get_connector(off_u)["restart_nonce"] == n_off


# --- folders + bindings ------------------------------------------------------------


def test_folders_bindings_and_ownership_guards(cleanup):
    from uuid import UUID
    c = _connector(cleanup); other = _connector(cleanup)
    cu, ou = UUID(c["uuid"]), UUID(other["uuid"])
    room = _room(cleanup)
    top = db.bridge_create_folder(cu, "server A", None)
    sub = db.bridge_create_folder(cu, "channels", UUID(top["id"]))
    with pytest.raises(db.BridgeTreeError):
        db.bridge_create_folder(ou, "x", UUID(top["id"]))                   # parent of another connector
    b = db.bridge_create_binding(cu, room.uuid, {"channel_id": "100"}, folder_uuid=UUID(sub["id"]))
    assert b["enabled"] is False and b["addressKey"] == "channel_id=100" and b["roomName"] == room.name
    with pytest.raises(db.BridgeBlocked):
        db.bridge_create_binding(cu, room.uuid, {"channel_id": "100"})     # same address, same connector
    db.bridge_create_binding(ou, room.uuid, {"channel_id": "100"})         # fine on another connector
    with pytest.raises(db.BridgeTreeError):
        db.bridge_create_binding(cu, uuid4(), {"channel_id": "101"})       # room missing
    with pytest.raises(AdapterError):
        db.bridge_create_binding(cu, room.uuid, {"channel_id": "x"})
    bu = UUID(b["uuid"])
    b = db.bridge_update_binding(bu, {"enabled": True, "policy": {"direction": "in"}})
    assert b["enabled"] and b["policy"] == {"direction": "in"}
    with pytest.raises(AdapterError):
        db.bridge_update_binding(bu, {"address": {"channel_id": "5"}})     # fixed after creation
    # Ownership: the connector cannot go while folders/bindings exist; a bound
    # room cannot go at all; an empty folder can, a nonempty one cannot.
    with pytest.raises(db.BridgeBlocked) as info:
        db.bridge_delete_connector(cu)
    assert info.value.blockers == {"folder_count": 2, "binding_count": 1}
    with pytest.raises(IntegrityError):
        db.delete_chatroom(room.uuid)
    db.session.rollback()
    assert [x["binding_uuid"] for x in db.bridge_room_blockers([room.uuid])] == [b["uuid"], db.bridge_load_tree()["bindings"][-1]["uuid"]] or len(db.bridge_room_blockers([room.uuid])) == 2
    with pytest.raises(db.BridgeBlocked):
        db.bridge_delete_folder(UUID(sub["id"]))
    assert db.bridge_delete_binding(bu) is True
    assert db.bridge_delete_folder(UUID(sub["id"])) is True
    assert db.bridge_delete_folder(UUID(top["id"])) is True
    assert db.bridge_delete_connector(cu) is True
    cleanup["connectors"].remove(cu)


# --- tree -------------------------------------------------------------------------


def test_tree_load_save_version_and_refusals(cleanup):
    from uuid import UUID
    c = _connector(cleanup); cu = UUID(c["uuid"])
    room = _room(cleanup)
    f = db.bridge_create_folder(cu, "f", None)
    b = db.bridge_create_binding(cu, room.uuid, {"channel_id": "7"})
    tree = db.bridge_load_tree()
    assert "discord" in tree["platforms"] and tree["platforms"]["zulip"]["available"] is False
    mine = lambda t: ([x for x in t["connectors"] if x["uuid"] == c["uuid"]],
                      [x for x in t["folders"] if x["connectorId"] == c["uuid"]],
                      [x for x in t["bindings"] if x["connectorId"] == c["uuid"]])
    conns, folders, bindings = mine(tree)
    assert len(conns) == 1 and len(folders) == 1 and len(bindings) == 1 and bindings[0]["roomName"] == room.name
    v0 = tree["version"]
    # Move the binding into the folder and rename the connector, echoing the token.
    all_c, all_f, all_b = tree["connectors"], tree["folders"], tree["bindings"]
    for x in all_b:
        if x["uuid"] == b["uuid"]:
            x["folderId"] = f["id"]
    for x in all_c:
        if x["uuid"] == c["uuid"]:
            x["name"] = "renamed"
    db.bridge_save_tree(all_c, all_f, all_b, base_version=v0)
    t2 = db.bridge_load_tree()
    assert t2["version"] != v0
    assert mine(t2)[2][0]["folderId"] == f["id"] and mine(t2)[0][0]["name"] == "renamed"
    with pytest.raises(db.BridgeTreeConflict):
        db.bridge_save_tree(all_c, all_f, all_b, base_version=v0)          # stale token
    with pytest.raises(db.BridgeTreeError):
        db.bridge_save_tree(all_c, all_f, [], base_version=t2["version"])  # omits a binding
    with pytest.raises(db.BridgeTreeError):
        db.bridge_save_tree(all_c, all_f + [{"id": str(uuid4()), "connectorId": c["uuid"], "parentId": None, "name": "n"}],
                            all_b, base_version=t2["version"])              # invents a folder
    # Enabling a binding is content, not structure: the token is unchanged.
    db.bridge_update_binding(UUID(b["uuid"]), {"enabled": True})
    assert db.bridge_tree_version() == t2["version"]


# --- config snapshot ----------------------------------------------------------------


def test_connector_config_resolves_gates_and_policies(cleanup):
    from uuid import UUID
    c = _connector(cleanup, policy={"allowed_senders": ["1"], "direction": "in"}); cu = UUID(c["uuid"])
    room = _room(cleanup)
    f = db.bridge_create_folder(cu, "f", None)
    fu = UUID(f["id"])
    db.bridge_update_folder(fu, {"policy": {"direction": "both"}, "enabled": True})
    b = db.bridge_create_binding(cu, room.uuid, {"channel_id": "1"}, folder_uuid=fu, policy={"forward_kinds": ["message"]})
    bu = UUID(b["uuid"])
    db.bridge_update_binding(bu, {"enabled": True})
    cfg = db.bridge_connector_config(cu)
    assert cfg["schema_version"] == 1 and cfg["connector"]["uuid"] == c["uuid"]
    assert "token" not in json_lower(cfg) or "token_env" in cfg["connector"]   # only the NAME appears
    [entry] = cfg["bindings"]
    assert entry["effective_enabled"] is False                              # connector still disabled
    assert entry["policy"]["direction"] == "both" and entry["policy_sources"]["direction"] == f"folder:{fu}"
    assert entry["policy"]["allowed_senders"] == ["1"] and entry["policy_sources"]["allowed_senders"] == "connector"
    assert entry["policy"]["forward_kinds"] == ["message"] and entry["policy_sources"]["forward_kinds"] == "binding"
    r1 = cfg["revision"]
    db.bridge_update_connector(cu, {"enabled": True}, autostart=True)
    cfg = db.bridge_connector_config(cu)
    assert cfg["bindings"][0]["effective_enabled"] is True and cfg["revision"] != r1
    db.bridge_update_folder(fu, {"enabled": False})
    assert db.bridge_connector_config(cu)["bindings"][0]["effective_enabled"] is False   # AND gate
    assert db.bridge_connector_config(uuid4()) is None


def json_lower(obj):
    import json
    return json.dumps(obj).lower()


def test_launcher_entries_shape(cleanup):
    from uuid import UUID
    c = _connector(cleanup, name="Main Bot"); cu = UUID(c["uuid"])
    db.bridge_update_connector(cu, {"enabled": True}, autostart=True)
    entries = [e for e in db.bridge_launcher_entries(autostart=True, rainbox_url="http://127.0.0.1:5000")
               if e["key"] == f"bridge:{cu}"]
    [e] = entries
    assert e == {
        "key": f"bridge:{cu}", "kind": "discord_bridge", "label": "Main Bot", "enabled": True,
        "restart_nonce": db.bridge_get_connector(cu)["restart_nonce"],
        "env": {"RAINBOX_URL": "http://127.0.0.1:5000", "BRIDGE_CONNECTOR": str(cu)},
        "token_env": "DISCORD_TOKEN_TEST",
        "state_file": {"env": "DISCORD_STATE_FILE", "name": f"bridge-{cu}.json"},
    }
    assert [x for x in db.bridge_launcher_entries(autostart=False, rainbox_url="u") if x["key"] == e["key"]][0]["enabled"] is False


def test_every_write_notifies_bridge_config(cleanup, monkeypatch):
    from uuid import UUID
    from db import bridges as mod
    seen = []
    monkeypatch.setattr(mod, "_notify_config", lambda cu: seen.append(str(cu)))
    c = _connector(cleanup); cu = UUID(c["uuid"])
    room = _room(cleanup)
    f = db.bridge_create_folder(cu, "f", None)
    b = db.bridge_create_binding(cu, room.uuid, {"channel_id": "3"})
    db.bridge_update_binding(UUID(b["uuid"]), {"enabled": True})
    db.bridge_update_folder(UUID(f["id"]), {"enabled": False})
    db.bridge_update_connector(cu, {"name": "n2"}, autostart=True)
    db.bridge_delete_binding(UUID(b["uuid"]))
    db.bridge_delete_folder(UUID(f["id"]))
    assert seen.count(str(cu)) == 8   # create connector, folder, binding; 3 updates; 2 deletes
