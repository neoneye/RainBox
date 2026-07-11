"""Tests for db.settings (the app_setting registry + accessors).

Hits the live local Postgres. Each test that writes a setting resets it to NULL
in teardown so the shared app_setting rows are left as init_db seeded them. A
throwaway registry key (via monkeypatch) is used for the secret-redaction and
precedence checks so the real backup settings are never mutated.
"""
import pytest
import sqlalchemy as sa

import db
from db import settings as db_settings
from db.settings import Setting


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def temp_setting(app_ctx, monkeypatch):
    """Register a throwaway setting in the registry and clean up its row."""
    def _register(**kw):
        spec = Setting(**kw)
        monkeypatch.setitem(db_settings.SETTINGS, spec.key, spec)
        return spec
    yield _register
    # Remove any rows the test created for keys no longer in the registry after
    # monkeypatch undoes the SETTINGS edits.
    db.db.session.execute(
        sa.delete(db.AppSetting).where(db.AppSetting.key.like("test.%"))
    )
    db.db.session.commit()


# ---- precedence: DB -> env -> default -------------------------------------

def test_precedence_default_then_env_then_db(temp_setting, monkeypatch):
    temp_setting(key="test.s", env="TEST_S", type="string", default="dflt")
    monkeypatch.delenv("TEST_S", raising=False)
    assert db.get_setting("test.s") == "dflt"          # default

    monkeypatch.setenv("TEST_S", "from_env")
    assert db.get_setting("test.s") == "from_env"      # env beats default

    db.set_setting("test.s", "from_db")
    assert db.get_setting("test.s") == "from_db"       # db beats env

    db.set_setting("test.s", None)                     # clear -> back to env
    assert db.get_setting("test.s") == "from_env"


def test_unknown_key_raises(app_ctx):
    with pytest.raises(KeyError):
        db.get_setting("nope.not.a.key")


# ---- unset semantics -------------------------------------------------------

def test_empty_string_is_unset_for_strings(temp_setting, monkeypatch):
    temp_setting(key="test.s", env="TEST_S", type="string", default="dflt")
    monkeypatch.setenv("TEST_S", "envval")
    db.set_setting("test.s", "")          # empty string == unset for strings
    assert db.get_setting("test.s") == "envval"   # falls through to env


def test_bool_false_is_explicit_not_unset(temp_setting, monkeypatch):
    temp_setting(key="test.b", env="TEST_B", type="bool", default=True)
    monkeypatch.setenv("TEST_B", "true")
    db.set_setting("test.b", False)       # explicit false must win over env/default
    assert db.get_setting("test.b") is False
    db.set_setting("test.b", None)        # only None is unset
    assert db.get_setting("test.b") is True   # default


def test_bool_and_int_coercion_from_env(temp_setting, monkeypatch):
    temp_setting(key="test.b", env="TEST_B", type="bool", default=False)
    temp_setting(key="test.i", env="TEST_I", type="int", default=0)
    monkeypatch.setenv("TEST_B", "yes")
    monkeypatch.setenv("TEST_I", "42")
    assert db.get_setting("test.b") is True
    assert db.get_setting("test.i") == 42


# ---- validation ------------------------------------------------------------

def test_set_setting_validates(temp_setting):
    def reject_x(v):
        if v == "x":
            raise ValueError("no x")
    temp_setting(key="test.v", env=None, type="string", default="", validate=reject_x)
    with pytest.raises(ValueError):
        db.set_setting("test.v", "x")
    db.set_setting("test.v", "ok")  # valid value is accepted
    assert db.get_setting("test.v") == "ok"


def test_age_recipient_validation():
    db_settings._validate_age_recipient("age1abc, age1def")  # ok (multiple)
    db_settings._validate_age_recipient("")                   # empty == unset, ok
    with pytest.raises(ValueError):
        db_settings._validate_age_recipient("not-a-key")
    with pytest.raises(ValueError):
        db_settings._validate_age_recipient("ssh-ed25519 AAAA")  # space; use file


# ---- dynamic defaults ------------------------------------------------------

def test_dynamic_default_used_when_unset(temp_setting, monkeypatch):
    """A dynamic_default computes the fallback; DB and env still beat it."""
    temp_setting(key="test.dyn", env="TEST_DYN", type="string", default="static",
                 dynamic_default=lambda: "computed")
    monkeypatch.delenv("TEST_DYN", raising=False)
    assert db.get_setting("test.dyn") == "computed"     # dynamic beats static default

    monkeypatch.setenv("TEST_DYN", "from_env")
    assert db.get_setting("test.dyn") == "from_env"     # env beats dynamic

    db.set_setting("test.dyn", "from_db")
    assert db.get_setting("test.dyn") == "from_db"      # db beats everything


def test_chat_default_model_dynamic_default_is_earliest_override(app_ctx, monkeypatch):
    """Unset chat.default_model resolves to the alphabetically earliest model
    config override (as a uuid string), or None when no overrides exist."""
    import db.model_config as model_config
    from uuid import uuid4

    row = db.db.session.query(db.AppSetting).filter_by(key="chat.default_model").one()
    assert row.value in (None, ""), "operator has chat.default_model set; test needs it unset"

    earliest = uuid4()
    monkeypatch.setattr(model_config, "default_chat_model_uuid", lambda: earliest)
    assert db.get_setting("chat.default_model") == str(earliest)

    monkeypatch.setattr(model_config, "default_chat_model_uuid", lambda: None)
    assert db.get_setting("chat.default_model") is None


def test_chat_default_model_validation(app_ctx):
    """Stored values must be uuids naming an existing config or override."""
    with pytest.raises(ValueError):
        db.set_setting("chat.default_model", "not-a-uuid")
    with pytest.raises(ValueError):
        db.set_setting("chat.default_model", "00000000-0000-0000-0000-000000000001")

    cfg = db.create_model_config("pp3-test-chat-default-model", {})
    try:
        db.set_setting("chat.default_model", str(cfg.uuid))
        assert db.get_setting("chat.default_model") == str(cfg.uuid)
    finally:
        db.set_setting("chat.default_model", None)
        db.db.session.delete(cfg)
        db.db.session.commit()


# ---- secrets are env-only --------------------------------------------------

def test_secret_cannot_be_persisted_to_db(temp_setting):
    """The threat-model invariant: a secret=True setting must never store a value
    in app_setting (it would leak into Postgres + every backup)."""
    temp_setting(key="test.secret", env="TEST_SECRET", type="string", default=None, secret=True)
    with pytest.raises(ValueError, match="env-only"):
        db.set_setting("test.secret", "supersecret")
    # Clearing (None) is still allowed.
    db.set_setting("test.secret", None)


def test_secret_value_redacted_in_all_settings(temp_setting, monkeypatch):
    """A secret is sourced from env; all_settings() redacts its value but still
    reports it's set."""
    temp_setting(key="test.secret", env="TEST_SECRET", type="string", default=None, secret=True)
    monkeypatch.setenv("TEST_SECRET", "supersecret")
    row = next(s for s in db.all_settings() if s["key"] == "test.secret")
    assert row["secret"] is True
    assert row["value"] == db_settings.REDACTED
    assert "supersecret" not in str(row)
    # get_setting still returns the real value (callers need it).
    assert db.get_setting("test.secret") == "supersecret"


# ---- metadata reconciliation ----------------------------------------------

def test_reconcile_seeds_rows_and_heals_metadata(app_ctx):
    # init_db already reconciled; every registry key has a row. (Don't assume the
    # operator's value is unset — this runs against the shared live DB.)
    rows = {r.key: r for r in db.db.session.query(db.AppSetting).all()}
    assert "backup.repo" in rows
    assert rows["backup.git_push"].value_type == "bool"

    # Corrupt the cached metadata, then reconcile heals it without touching value.
    value_before = rows["backup.git_push"].value
    db.db.session.query(db.AppSetting).filter_by(key="backup.git_push").update(
        {"value_type": "string", "description": "stale"}
    )
    db.db.session.commit()
    db.reconcile_app_settings()
    healed = db.db.session.query(db.AppSetting).filter_by(key="backup.git_push").one()
    assert healed.value_type == "bool"
    assert healed.description == db_settings.SETTINGS["backup.git_push"].description
    assert healed.value == value_before  # value untouched by reconcile


def test_all_settings_covers_registry(app_ctx):
    keys = {s["key"] for s in db.all_settings()}
    assert {"backup.repo", "backup.age_recipient", "backup.git_push"} <= keys


def test_customize_dir_in_registry():
    """customize.dir: string, env fallback RAINBOX_CUSTOMIZE_DIR, default None,
    not secret — the knob that points rainbox at the operator's private
    customizations directory (Q&A overlay etc.)."""
    from db.settings import SETTINGS

    spec = SETTINGS["customize.dir"]
    assert spec.env == "RAINBOX_CUSTOMIZE_DIR"
    assert spec.type == "string"
    assert spec.default is None
    assert spec.secret is False
    assert "question_answer.jsonl" in spec.description


def test_mark_facts_invalidated_sets_iso_timestamp(app_ctx):
    """mark_facts_invalidated() stamps qa.facts_invalidated_at with a non-empty
    ISO string and returns it (the assistant posts a re-check-facts notice when
    this changes)."""
    try:
        assert db.get_setting("qa.facts_invalidated_at") in (None, "")
        ts = db.mark_facts_invalidated()
        assert isinstance(ts, str) and ts
        assert db.get_setting("qa.facts_invalidated_at") == ts
    finally:
        db.set_setting("qa.facts_invalidated_at", None)
        db.db.session.commit()


def test_set_setting_commits_so_value_survives_rollback(app_ctx):
    """set_setting must commit immediately: a later rollback (Flask-SQLAlchemy's
    per-request teardown rolls back uncommitted work) must not discard the saved
    value. Regression guard — a displaced commit once left /settings saves
    uncommitted, so they were silently forgotten on reload."""
    try:
        db.set_setting("cron.paused", True)
        db.db.session.rollback()  # would undo an uncommitted write
        assert db.get_setting("cron.paused") is True
    finally:
        db.set_setting("cron.paused", False)
        db.db.session.commit()
