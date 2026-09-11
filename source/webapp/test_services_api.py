"""The core's side of the launcher control channel: snapshots pushed on
settings writes, status accepted from the launcher, and the /settings view.

The channel is a socketpair the test holds the other end of. Hits the live
local Postgres via the Flask test client; every setting a test writes is
restored in teardown (same guard as test_settings_views)."""
import json
import socket

import pytest

import db
from services import registry
from services.definitions import STATIC_SERVICES, enabled_setting_key, env_setting_key, nonce_setting_key
from webapp.core import app


@pytest.fixture
def client():
    ctx = app.app_context()
    ctx.push()
    before = {r.key: r.value for r in db.session.query(db.AppSetting).all()}
    registry.CHANNEL.close()
    try:
        yield app.test_client()
    finally:
        db.session.rollback()
        for row in db.session.query(db.AppSetting).all():
            row.value = before.get(row.key)
        db.session.commit()
        registry.CHANNEL.close()
        ctx.pop()


class Launcher:
    """The test's end of the channel."""

    def __init__(self) -> None:
        self.mine, self.theirs = socket.socketpair()
        self.mine.setblocking(False)
        self._buf = b""

    def lines(self) -> list[dict]:
        out = []
        while True:
            try:
                chunk = self.mine.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            out.append(json.loads(raw))
        return out

    def send(self, message: dict) -> None:
        self.mine.sendall((json.dumps(message) + "\n").encode())

    def close(self) -> None:
        self.mine.close()


@pytest.fixture
def managed(client):
    l = Launcher()
    registry.CHANNEL.attach_socket(l.theirs)
    registry.CHANNEL.start(app)
    try:
        yield l
    finally:
        l.close()
        registry.CHANNEL.close()


def _status(seq=1, **services):
    return {"type": "status", "schema_version": 1, "sequence": seq,
            "launcher": {"pid": 1, "state_dir": "/tmp/x", "started_at": "t", "core_only": False},
            "services": {k: {"state": v} for k, v in services.items()}}


def _wait_lines(l: Launcher, n: int, timeout: float = 3.0) -> list[dict]:
    import time
    got: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(got) < n:
        got += l.lines()
        time.sleep(0.02)
    return got


def _wait_status(pred, timeout: float = 3.0) -> dict:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = registry.CHANNEL.view()
        if pred(v):
            return v
        time.sleep(0.02)
    raise AssertionError("status not observed in time")


def test_unmanaged_core_reports_unknown_everywhere(client):
    view = client.get("/services/api/status").get_json()
    assert view["managed"] is False
    assert view["services"]["core"] == {"state": "unknown"}
    assert all(v == {"state": "unknown"} for v in view["services"].values())


def test_startup_pushes_one_snapshot_with_the_full_shape(client, managed):
    lines = _wait_lines(managed, 1)
    assert len(lines) == 1
    d = lines[0]
    assert d["type"] == "desired" and d["schema_version"] == 1
    assert isinstance(d["core_pid"], int)
    assert sorted(s["key"] for s in d["services"]) == sorted(STATIC_SERVICES)
    for s in d["services"]:
        assert s["kind"] == s["key"] and s["enabled"] is False and s["env"] == {}
        assert set(s) == {"key", "kind", "enabled", "restart_nonce", "env"}


def test_enable_and_env_edit_push_a_snapshot_with_a_new_nonce(client, managed):
    _wait_lines(managed, 1)
    key = "voice_stt_whisper"
    r = client.post("/settings/api/set", json={"key": enabled_setting_key(key), "value": True})
    assert r.status_code == 200, r.get_json()
    pushed = _wait_lines(managed, 1)
    assert len(pushed) == 1
    entry = next(s for s in pushed[0]["services"] if s["key"] == key)
    nonce1 = db.get_setting(nonce_setting_key(key))
    assert entry["enabled"] is True and entry["restart_nonce"] == nonce1 and nonce1
    # Same value again: no change, no nonce, no push.
    client.post("/settings/api/set", json={"key": enabled_setting_key(key), "value": True})
    assert _wait_lines(managed, 1, timeout=0.3) == []
    assert db.get_setting(nonce_setting_key(key)) == nonce1
    # An env override changes the launch environment: new nonce, pushed.
    r = client.post("/settings/api/set", json={"key": env_setting_key(key, "WHISPER_MODEL"), "value": "medium.en"})
    assert r.status_code == 200
    pushed = _wait_lines(managed, 1)
    entry = next(s for s in pushed[-1]["services"] if s["key"] == key)
    assert entry["env"] == {"WHISPER_MODEL": "medium.en"}
    assert entry["restart_nonce"] == db.get_setting(nonce_setting_key(key)) != nonce1


def test_restart_rewrites_nonce_pushes_and_is_internal_to_settings_api(client, managed):
    _wait_lines(managed, 1)
    r = client.post("/services/api/restart/reranker")
    assert r.status_code == 200
    assert db.get_setting(nonce_setting_key("reranker")) == r.get_json()["restart_nonce"]
    pushed = _wait_lines(managed, 1)
    assert next(s for s in pushed[-1]["services"] if s["key"] == "reranker")["restart_nonce"] == r.get_json()["restart_nonce"]
    assert client.post("/services/api/restart/core").status_code == 200
    pushed = _wait_lines(managed, 1)
    assert pushed[-1]["core_restart_nonce"] == db.get_setting(nonce_setting_key("core"))
    assert client.post("/services/api/restart/nope").status_code == 404
    # The nonce is machine-owned: the public settings endpoint refuses it.
    r = client.post("/settings/api/set", json={"key": nonce_setting_key("reranker"), "value": "x"})
    assert r.status_code == 400


def test_status_lines_are_read_and_bad_ones_rejected(client, managed):
    managed.send(_status(1, core="running", reranker="stopped"))
    view = _wait_status(lambda v: v["services"]["core"]["state"] == "running")
    assert view["managed"] and view["services"]["reranker"]["state"] == "stopped"
    assert view["services"]["voice_tts_kokoro"] == {"state": "unknown"}
    managed.send(_status(1, core="stopped"))                  # stale sequence: ignored
    managed.send(_status(2, core="dancing"))                  # bad state: ignored
    managed.send({**_status(3, core="stopped"), "schema_version": 2})  # wrong schema
    managed.send(_status(4, core="backoff"))
    view = _wait_status(lambda v: v["services"]["core"]["state"] == "backoff")
    assert client.get("/services/api/status").get_json()["services"]["core"]["state"] == "backoff"


def test_launcher_eof_makes_the_core_unmanaged_at_once(client, managed):
    managed.send(_status(1, core="running"))
    _wait_status(lambda v: v["services"]["core"]["state"] == "running")
    managed.mine.close()
    view = _wait_status(lambda v: not v["managed"])
    assert view["services"]["core"] == {"state": "unknown"}


def test_service_setting_and_nonce_commit_together(client, monkeypatch):
    key = env_setting_key("reranker", "RERANKER_DEVICE")
    commits = []
    real_commit = db.session.commit
    monkeypatch.setattr(db.session, "commit", lambda: (commits.append(1), real_commit())[1])
    assert registry.set_service_setting(key, "cpu") is True
    assert commits == [1]
    nonce = db.get_setting(nonce_setting_key("reranker"))
    assert nonce and db.get_setting(key) == "cpu"
    commits.clear()
    assert registry.set_service_setting(key, "cpu") is False
    assert commits == [1] and db.get_setting(nonce_setting_key("reranker")) == nonce


def test_service_setting_locks_the_row_before_reading(client, monkeypatch):
    order = []
    real_lock, real_get = db.lock_setting_row, db.get_setting
    monkeypatch.setattr(registry.db, "lock_setting_row", lambda k: (order.append(("lock", k)), real_lock(k))[1])
    monkeypatch.setattr(registry.db, "get_setting", lambda k: (order.append(("read", k)), real_get(k))[1])
    key = env_setting_key("reranker", "RERANKER_BATCH_SIZE")
    registry.set_service_setting(key, "8")
    assert order[0] == ("lock", key) and order[1] == ("read", key)


def test_desired_snapshot_reads_one_statement(client):
    statements = []
    from sqlalchemy import event
    engine = db.session.get_bind()

    def before_cursor_execute(conn, cursor, statement, params, context, executemany):
        if "app_setting" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        registry.desired_snapshot()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    assert len(statements) == 1, statements


def test_invalid_env_override_is_refused_before_any_restart(client):
    nonce_before = db.get_setting(nonce_setting_key("reranker"))
    r = client.post("/settings/api/set", json={"key": env_setting_key("reranker", "RERANKER_BATCH_SIZE"), "value": "oops"})
    assert r.status_code == 400
    r = client.post("/settings/api/set", json={"key": env_setting_key("reranker", "RERANKER_BATCH_SIZE"), "value": 0})
    assert r.status_code == 400
    r = client.post("/settings/api/set", json={"key": env_setting_key("reranker", "RERANKER_DEVICE"), "value": "gpu9"})
    assert r.status_code == 400
    assert db.get_setting(nonce_setting_key("reranker")) == nonce_before
    r = client.post("/settings/api/set", json={"key": env_setting_key("reranker", "RERANKER_BATCH_SIZE"), "value": 8})
    assert r.status_code == 200
    assert db.get_setting(nonce_setting_key("reranker")) != nonce_before
    entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == "reranker")
    assert entry["env"] == {"RERANKER_BATCH_SIZE": "8"}


def test_unreadable_stored_override_reads_as_unset_and_can_be_repaired(client):
    key = env_setting_key("reranker", "RERANKER_BATCH_SIZE")
    for legacy in ("", "oops"):
        row = db.session.query(db.AppSetting).filter_by(key=key).one()
        row.value = legacy
        db.session.commit()
        assert db.get_setting(key) is None
        assert client.get("/settings").status_code == 200
        entry = next(s for s in registry.desired_snapshot()["services"] if s["key"] == "reranker")
        assert entry["env"] == {}
        nonce_before = db.get_setting(nonce_setting_key("reranker"))
        assert registry.set_service_setting(key, 8) is True
        assert db.get_setting(key) == 8
        assert db.get_setting(nonce_setting_key("reranker")) != nonce_before


def test_status_view_carries_reported_bridge_keys_only_while_reported(client, managed):
    """Dynamic keys (bridge:<uuid>) are not in the static catalogue; the view
    lists them exactly as long as the launcher reports them."""
    cu = "0f7a1b2c-3d4e-4f50-8a9b-0c1d2e3f4a5b"
    key = f"bridge:{cu}"
    first = _status(1, core="running")
    first["services"][key] = {"state": "credential missing", "label": "Main Bot",
                              "message": "no credential stored for this connector and BOT_TOKEN is not in the environment"}
    first["services"]["rogue"] = {"state": "running"}  # an unknown static-looking key never surfaces
    managed.send(first)
    view = _wait_status(lambda v: key in v["services"])
    assert view["services"][key]["state"] == "credential missing"
    assert view["services"][key]["label"] == "Main Bot" and "rogue" not in view["services"]
    api = client.get("/services/api/status").get_json()["services"]
    assert api[key]["label"] == "Main Bot"
    managed.send(_status(2, core="running"))  # the connector row was removed
    view = _wait_status(lambda v: key not in v["services"])
    assert set(view["services"]) == {"core", *STATIC_SERVICES}


def test_desired_snapshot_is_read_in_one_repeatable_read_transaction(client, monkeypatch):
    """Settings, connector rows, and credentials must come from one
    REPEATABLE READ read-only transaction (a racing autostart flip cannot
    pair with newer rows), and the session is left clean afterwards."""
    import sqlalchemy as sa
    seen = {}
    real = db.bridge_launcher_entries

    def spy(**kw):
        seen["isolation"] = db.session.execute(sa.text("SHOW transaction_isolation")).scalar_one()
        seen["read_only"] = db.session.execute(sa.text("SHOW transaction_read_only")).scalar_one()
        return real(**kw)
    monkeypatch.setattr(db, "bridge_launcher_entries", spy)
    registry.desired_snapshot()
    assert seen == {"isolation": "repeatable read", "read_only": "on"}
    assert db.session.execute(sa.text("SHOW transaction_isolation")).scalar_one() != "repeatable read"  # ended
