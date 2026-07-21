"""Tests for db.set_current_profile — the runtime write path for
`profile.current` that stamps `qa.facts_invalidated_at` and
`profile.current_changed_at` atomically on an actual change — plus the
`internal` registry flag that keeps the stamp off the /settings page.

Hits the live local Postgres (rainbox_claude via conftest); the three touched
settings are saved and restored around every test.
"""
from uuid import uuid4

import pytest

import db
from db import settings as db_settings

KEYS = ("profile.current", "qa.facts_invalidated_at", "profile.current_changed_at")


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    saved = {}
    for key in KEYS:
        row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
        saved[key] = row.value if row is not None else None
    try:
        yield app
    finally:
        db.db.session.rollback()
        for key, value in saved.items():
            row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
            if row is not None:
                row.value = value
        db.db.session.commit()
        ctx.pop()


def _template_uuid(index: int = 0) -> str:
    return db.profile_templates_entries()[index]["uuid"]


def _raw(key: str) -> str | None:
    row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
    return row.value if row is not None else None


def test_change_writes_all_three_in_one_stamp(app_ctx):
    db.set_setting("profile.current", None)
    db.set_setting("qa.facts_invalidated_at", None)
    db.set_setting("profile.current_changed_at", None)

    target = _template_uuid(0)
    stamp = db.set_current_profile(target)
    assert stamp
    assert db.get_setting("profile.current") == target
    assert db.get_setting("qa.facts_invalidated_at") == stamp
    assert db.get_setting("profile.current_changed_at") == stamp


def test_same_value_is_a_noop(app_ctx):
    target = _template_uuid(0)
    stamp = db.set_current_profile(target)
    assert stamp is not None or db.get_setting("profile.current") == target
    before = {k: _raw(k) for k in KEYS}
    assert db.set_current_profile(target) is None      # same uuid → no-op
    assert {k: _raw(k) for k in KEYS} == before        # nothing restamped


def test_switch_between_profiles_and_unset(app_ctx):
    a, b = _template_uuid(0), _template_uuid(1)
    db.set_current_profile(a)
    stamp_ab = db.set_current_profile(b)
    assert stamp_ab is not None
    assert db.get_setting("profile.current") == b
    assert db.get_setting("profile.current_changed_at") == stamp_ab

    stamp_clear = db.set_current_profile(None)
    assert stamp_clear is not None and stamp_clear != stamp_ab
    assert db.get_setting("profile.current") is None
    assert db.get_setting("profile.current_changed_at") == stamp_clear

    # Clearing an already-unset value is a no-op.
    assert db.set_current_profile(None) is None
    assert db.set_current_profile("") is None


def test_invalid_target_raises_and_changes_nothing(app_ctx):
    db.set_current_profile(_template_uuid(0))
    before = {k: _raw(k) for k in KEYS}
    with pytest.raises(ValueError):
        db.set_current_profile("not-a-uuid")
    with pytest.raises(ValueError):
        db.set_current_profile(str(uuid4()))  # unknown profile
    assert {k: _raw(k) for k in KEYS} == before


def test_failure_rolls_back_the_whole_transaction(app_ctx, monkeypatch):
    """If any of the three row updates fails, none of them may stick."""
    db.set_current_profile(_template_uuid(0))
    before = {k: _raw(k) for k in KEYS}

    real = db_settings._upsert_setting_row
    calls = {"n": 0}

    def failing(spec, value):
        calls["n"] += 1
        if calls["n"] == 3:            # the last of the three staged writes
            raise RuntimeError("boom")
        real(spec, value)

    monkeypatch.setattr(db_settings, "_upsert_setting_row", failing)
    with pytest.raises(RuntimeError):
        db.set_current_profile(_template_uuid(1))
    assert {k: _raw(k) for k in KEYS} == before


def test_plain_set_setting_stamps_nothing(app_ctx):
    """The low-level seam still works but never advances the event stamps."""
    db.set_current_profile(None)
    facts_before = _raw("qa.facts_invalidated_at")
    changed_before = _raw("profile.current_changed_at")
    db.set_setting("profile.current", _template_uuid(0))
    assert _raw("qa.facts_invalidated_at") == facts_before
    assert _raw("profile.current_changed_at") == changed_before


def test_internal_setting_hidden_from_listing_but_readable(app_ctx):
    keys_default = {s["key"] for s in db.all_settings()}
    assert "profile.current_changed_at" not in keys_default
    assert "profile.current" in keys_default           # the pointer stays listed
    keys_all = {s["key"] for s in db.all_settings(include_internal=True)}
    assert "profile.current_changed_at" in keys_all
    # get/set treat internal keys like any other setting.
    db.set_setting("profile.current_changed_at", "2026-07-21T00:00:00+00:00")
    assert db.get_setting("profile.current_changed_at") == "2026-07-21T00:00:00+00:00"


def test_settings_endpoint_routes_profile_current_through_helper(app_ctx):
    """A /settings page write of profile.current must fire the stamps — the
    endpoint routing, not just the helper."""
    import webapp.core as webapp_core

    db.set_current_profile(None)
    db.set_setting("qa.facts_invalidated_at", None)
    db.set_setting("profile.current_changed_at", None)

    client = webapp_core.app.test_client()
    target = _template_uuid(0)
    resp = client.post("/settings/api/set",
                       json={"key": "profile.current", "value": target})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert db.get_setting("profile.current") == target
    stamp = db.get_setting("profile.current_changed_at")
    assert stamp
    assert db.get_setting("qa.facts_invalidated_at") == stamp

    # An unrelated setting write must not advance the profile stamp.
    resp = client.post("/settings/api/set",
                       json={"key": "cron.paused", "value": None})
    assert resp.status_code == 200
    assert db.get_setting("profile.current_changed_at") == stamp
