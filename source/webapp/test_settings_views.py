"""Tests for the /settings page and its JSON API.

Hits the live local Postgres via the Flask test client. Any setting a test
writes is reset to NULL in teardown so the shared app_setting rows are left as
init_db seeded them.
"""
import pytest

import db
from db import settings as db_settings
from webapp.core import app


@pytest.fixture
def client():
    # init_db already ran at import; just push a context for db.session access.
    ctx = app.app_context()
    ctx.push()
    # Snapshot the raw app_setting values so teardown RESTORES operator config
    # exactly — never clobber the live, shared DB's real settings to NULL.
    before = {r.key: r.value for r in db.db.session.query(db.AppSetting).all()}
    try:
        yield app.test_client()
    finally:
        db.db.session.rollback()
        for row in db.db.session.query(db.AppSetting).all():
            row.value = before.get(row.key)  # absent-before -> None (unset)
        db.db.session.commit()
        ctx.pop()


def test_page_renders_registry_rows(client):
    body = client.get("/settings").get_data(as_text=True)
    assert "Settings" in body
    for key in ("backup.repo", "backup.age_recipient", "backup.git_push"):
        assert key in body
    # The registry data is embedded for the inline JS to render.
    assert "const SETTINGS =" in body


def test_page_uses_edit_overlay_not_inline_save(client):
    body = client.get("/settings").get_data(as_text=True)
    # Edit-button + overlay UX (Save gated on change, Cancel to back out).
    assert "data-edit=" in body
    assert 'id="s-modal"' in body and 'id="s-save"' in body and 'id="s-cancel"' in body
    assert "function openEdit" in body and "updateSaveState" in body
    # The old ambiguous inline Save/Clear buttons are gone.
    assert "saveSetting(this" not in body
    assert "clearSetting(this" not in body


def test_page_explains_source_badges(client):
    body = client.get("/settings").get_data(as_text=True)
    # A legend explains from db / env / default so the operator needn't remember.
    assert 's-legend' in body
    assert "stored in the database" in body         # 'from db' meaning
    assert "environment variable" in body           # 'from env' meaning
    assert "built-in default" in body               # 'from default' meaning
    assert "SOURCE_HELP" in body                     # per-badge hover tooltips


def test_set_string_setting_roundtrips_and_reports_db_source(client):
    resp = client.post("/settings/api/set", json={"key": "backup.repo", "value": "/tmp/repo"})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["ok"] is True
    assert d["setting"]["value"] == "/tmp/repo"
    assert d["setting"]["source"] == "db"
    assert db.get_setting("backup.repo") == "/tmp/repo"


def test_clear_setting_falls_back(client, monkeypatch):
    monkeypatch.setenv("RAINBOX_BACKUP_REPO", "/env/repo")
    client.post("/settings/api/set", json={"key": "backup.repo", "value": "/tmp/db"})
    # Clearing (value=None) drops the DB value -> env fallback.
    resp = client.post("/settings/api/set", json={"key": "backup.repo", "value": None})
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["setting"]["value"] == "/env/repo"
    assert d["setting"]["source"] == "env"


def test_bool_setting_explicit_false(client):
    resp = client.post("/settings/api/set", json={"key": "backup.git_push", "value": False})
    assert resp.status_code == 200
    assert resp.get_json()["setting"]["value"] is False
    assert db.get_setting("backup.git_push") is False


def test_invalid_recipient_rejected_400(client):
    resp = client.post("/settings/api/set",
                       json={"key": "backup.age_recipient", "value": "not-a-key"})
    assert resp.status_code == 400
    assert "age recipient" in resp.get_json()["error"]


def test_unknown_key_rejected_400(client):
    resp = client.post("/settings/api/set", json={"key": "nope.key", "value": "x"})
    assert resp.status_code == 400
    assert "unknown setting" in resp.get_json()["error"]


def test_missing_key_rejected_400(client):
    resp = client.post("/settings/api/set", json={"value": "x"})
    assert resp.status_code == 400


def test_secret_setting_cannot_be_set_via_api(client, monkeypatch):
    # Register a throwaway secret key so the API guard is exercised.
    spec = db_settings.Setting(key="test.secret", env="TEST_SECRET", type="string",
                               default=None, secret=True)
    monkeypatch.setitem(db_settings.SETTINGS, "test.secret", spec)
    resp = client.post("/settings/api/set", json={"key": "test.secret", "value": "x"})
    assert resp.status_code == 400
    assert "env-only" in resp.get_json()["error"]


def test_secret_rendered_readonly_on_page(client, monkeypatch):
    spec = db_settings.Setting(key="test.secret", env="TEST_SECRET", type="string",
                               default=None, secret=True)
    monkeypatch.setitem(db_settings.SETTINGS, "test.secret", spec)
    monkeypatch.setenv("TEST_SECRET", "supersecret")
    body = client.get("/settings").get_data(as_text=True)
    assert "supersecret" not in body            # redacted, never sent to the browser
    assert db_settings.REDACTED in body


def test_repopulate_memory_endpoint_success(client, monkeypatch):
    """POST /settings/api/repopulate_memory → rebuild_kb() counts. The
    monkeypatch targets seed_memory (the endpoint resolves the function
    at call time)."""
    import memory.seed_memory as seed_memory

    monkeypatch.setattr(seed_memory, "rebuild_kb",
                        lambda: {"entries": 7, "documents": 21})
    resp = client.post("/settings/api/repopulate_memory")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"ok": True, "entries": 7, "documents": 21}


def test_repopulate_memory_endpoint_failure_is_502(client, monkeypatch):
    import memory.seed_memory as seed_memory

    def boom():
        raise RuntimeError("Connection refused (Ollama down?)")

    monkeypatch.setattr(seed_memory, "rebuild_kb", boom)
    resp = client.post("/settings/api/repopulate_memory")
    assert resp.status_code == 502
    data = resp.get_json()
    assert data["ok"] is False and "Ollama" in data["error"]


def test_repopulate_button_rendered_for_customize_dir(client):
    """The page's JS renders the button only on the customize.dir card."""
    body = client.get("/settings").get_data(as_text=True)
    assert "repopulate_memory" in body
    assert "Repopulate Q&A memory" in body
    assert "customize.dir" in body
