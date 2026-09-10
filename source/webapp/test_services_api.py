"""The launcher <-> core endpoints and the core-side registry.

Hits the live local Postgres via the Flask test client; every setting a test
writes is restored in teardown (same guard as test_settings_views)."""
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
    registry.STATUS.reset()
    try:
        yield app.test_client()
    finally:
        db.session.rollback()
        for row in db.session.query(db.AppSetting).all():
            row.value = before.get(row.key)
        db.session.commit()
        registry.STATUS.reset()
        ctx.pop()


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setenv(registry.LAUNCHER_ID_ENV, "L1")
    monkeypatch.setenv(registry.CORE_INSTANCE_ID_ENV, "C1")


def _status(seq=1, **services):
    return {"schema_version": 1, "launcher_id": "L1", "core_instance_id": "C1",
            "sequence": seq,
            "launcher": {"pid": 1, "state_dir": "/tmp/x", "started_at": "t"},
            "services": {k: {"state": v} for k, v in services.items()}}


def test_unmanaged_core_refuses_desired_and_status(client, monkeypatch):
    monkeypatch.delenv(registry.LAUNCHER_ID_ENV, raising=False)
    monkeypatch.delenv(registry.CORE_INSTANCE_ID_ENV, raising=False)
    assert client.get("/services/api/desired").status_code == 409
    assert client.post("/services/api/status", json=_status()).status_code == 409
    view = client.get("/services/api/status").get_json()
    assert view["managed"] is False
    assert view["services"]["core"] == {"state": "unknown"}


def test_desired_snapshot_shape(client, managed):
    resp = client.get("/services/api/desired")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["schema_version"] == 1
    assert (d["launcher_id"], d["core_instance_id"]) == ("L1", "C1")
    assert isinstance(d["core_pid"], int)
    keys = [s["key"] for s in d["services"]]
    assert sorted(keys) == sorted(STATIC_SERVICES)
    for s in d["services"]:
        assert s["kind"] == s["key"]
        assert s["enabled"] is False
        assert s["env"] == {}
        assert set(s) == {"key", "kind", "enabled", "restart_nonce", "env"}


def test_enable_and_env_edit_bump_the_nonce_atomically(client, managed):
    key = "voice_stt_whisper"
    r = client.post("/settings/api/set", json={"key": enabled_setting_key(key), "value": True})
    assert r.status_code == 200, r.get_json()
    nonce1 = db.get_setting(nonce_setting_key(key))
    assert nonce1
    # Same value again: no change, nonce untouched.
    client.post("/settings/api/set", json={"key": enabled_setting_key(key), "value": True})
    assert db.get_setting(nonce_setting_key(key)) == nonce1
    # An env override changes the launch environment: new nonce, and the
    # desired snapshot carries the value.
    r = client.post("/settings/api/set", json={"key": env_setting_key(key, "WHISPER_MODEL"), "value": "medium.en"})
    assert r.status_code == 200
    nonce2 = db.get_setting(nonce_setting_key(key))
    assert nonce2 and nonce2 != nonce1
    entry = next(s for s in client.get("/services/api/desired").get_json()["services"] if s["key"] == key)
    assert entry == {"key": key, "kind": key, "enabled": True, "restart_nonce": nonce2,
                     "env": {"WHISPER_MODEL": "medium.en"}}


def test_restart_rewrites_nonce_and_is_internal_to_settings_api(client, managed):
    r = client.post("/services/api/restart/reranker")
    assert r.status_code == 200
    assert db.get_setting(nonce_setting_key("reranker")) == r.get_json()["restart_nonce"]
    r2 = client.post("/services/api/restart/reranker")
    assert r2.get_json()["restart_nonce"] != r.get_json()["restart_nonce"]
    assert client.post("/services/api/restart/core").status_code == 200
    assert client.get("/services/api/desired").get_json()["core_restart_nonce"] == db.get_setting(nonce_setting_key("core"))
    assert client.post("/services/api/restart/nope").status_code == 404
    # The nonce is machine-owned: the public settings endpoint refuses it.
    r = client.post("/settings/api/set", json={"key": nonce_setting_key("reranker"), "value": "x"})
    assert r.status_code == 400


def test_status_accepts_only_matching_markers_and_increasing_sequence(client, managed):
    assert client.post("/services/api/status", json=_status(1, core="running", reranker="stopped")).status_code == 200
    view = client.get("/services/api/status").get_json()
    assert view["managed"] and not view["stale"]
    assert view["services"]["core"]["state"] == "running"
    assert view["services"]["reranker"]["state"] == "stopped"
    assert view["services"]["voice_tts_kokoro"] == {"state": "unknown"}
    # stale sequence, wrong marker, bad state, wrong schema
    assert client.post("/services/api/status", json=_status(1, core="running")).status_code == 400
    bad = _status(2, core="running"); bad["launcher_id"] = "other"
    assert client.post("/services/api/status", json=bad).status_code == 400
    assert client.post("/services/api/status", json=_status(3, core="dancing")).status_code == 400
    bad = _status(4, core="running"); bad["schema_version"] = 2
    assert client.post("/services/api/status", json=bad).status_code == 400
    assert client.post("/services/api/status", json=_status(5, core="backoff")).status_code == 200


def test_status_goes_unknown_after_90_seconds(client, managed):
    client.post("/services/api/status", json=_status(1, core="running"))
    import time
    fresh = registry.STATUS.view(now=time.monotonic() + 10)
    assert fresh["services"]["core"]["state"] == "running" and not fresh["stale"]
    old = registry.STATUS.view(now=time.monotonic() + registry.STATUS_STALE_AFTER + 1)
    assert old["stale"] and old["services"]["core"] == {"state": "unknown"}


def test_service_setting_and_nonce_commit_together(client, managed, monkeypatch):
    """A crash after the value write but before the nonce write must be
    impossible: both are staged and committed once."""
    key = env_setting_key("reranker", "RERANKER_DEVICE")
    commits = []
    real_commit = db.session.commit
    monkeypatch.setattr(db.session, "commit", lambda: (commits.append(1), real_commit())[1])
    assert registry.set_service_setting(key, "cpu") is True
    assert commits == [1]
    nonce = db.get_setting(nonce_setting_key("reranker"))
    assert nonce and db.get_setting(key) == "cpu"
    commits.clear()
    assert registry.set_service_setting(key, "cpu") is False  # unchanged: no nonce
    assert commits == [1] and db.get_setting(nonce_setting_key("reranker")) == nonce


def test_desired_snapshot_reads_one_statement(client, managed, monkeypatch):
    """The snapshot must come from one query, not one per key."""
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


def test_service_setting_locks_the_row_before_reading(client, managed, monkeypatch):
    """The compare-then-write must serialize through the row lock, taken
    before the previous value is read."""
    order = []
    real_lock, real_get = db.lock_setting_row, db.get_setting
    monkeypatch.setattr(registry.db, "lock_setting_row", lambda k: (order.append(("lock", k)), real_lock(k))[1])
    monkeypatch.setattr(registry.db, "get_setting", lambda k: (order.append(("read", k)), real_get(k))[1])
    key = env_setting_key("reranker", "RERANKER_BATCH_SIZE")
    registry.set_service_setting(key, "8")
    assert order[0] == ("lock", key) and order[1] == ("read", key)


def test_invalid_env_override_is_refused_before_any_restart(client, managed):
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
    entry = next(s for s in client.get("/services/api/desired").get_json()["services"] if s["key"] == "reranker")
    assert entry["env"] == {"RERANKER_BATCH_SIZE": "8"}


def test_unreadable_stored_override_reads_as_unset_and_can_be_repaired(client, managed):
    """A blank or unparseable value left in an int-typed row (a key retyped
    after it was saved) must not break /settings or the desired snapshot,
    and a write must repair it without reading it first."""
    key = env_setting_key("reranker", "RERANKER_BATCH_SIZE")
    for legacy in ("", "oops"):
        row = db.session.query(db.AppSetting).filter_by(key=key).one()
        row.value = legacy
        db.session.commit()
        assert db.get_setting(key) is None
        assert client.get("/settings").status_code == 200
        entry = next(s for s in client.get("/services/api/desired").get_json()["services"] if s["key"] == "reranker")
        assert entry["env"] == {}
        nonce_before = db.get_setting(nonce_setting_key("reranker"))
        assert registry.set_service_setting(key, 8) is True
        assert db.get_setting(key) == 8
        assert db.get_setting(nonce_setting_key("reranker")) != nonce_before
