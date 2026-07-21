"""Tests for db.set_current_profile — the runtime write path for
`profile.current` that writes the pointer and `profile.current_changed_at`
atomically on an actual change while leaving `qa.facts_invalidated_at`
independent — plus the `internal` registry flag that keeps the stamp off the
/settings page.

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


def _tree_without(*doomed: str) -> tuple[list, list]:
    """The CURRENT user-owned tree minus the given profile uuids — so a test
    can exercise the deletion path without wiping any pre-existing rows in
    the shared test database (profile_save_tree deletes everything absent
    from the payload)."""
    tree = db.profile_load_tree()
    folders = [{"id": f["id"], "name": f["name"],
                "description": f.get("description", ""),
                "parentId": f.get("parentId")}
               for f in tree["folders"] if not f.get("builtin")]
    profiles = [{"uuid": p["uuid"], "name": p["name"],
                 "folderId": p.get("folderId")}
                for p in tree["profiles"]
                if not p.get("builtin") and p["uuid"] not in doomed]
    return folders, profiles


def _raw(key: str) -> str | None:
    row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
    return row.value if row is not None else None


def test_change_writes_pointer_and_stamp_but_not_facts(app_ctx):
    db.set_setting("profile.current", None)
    db.set_setting("qa.facts_invalidated_at", "2026-01-01T00:00:00+00:00")
    db.set_setting("profile.current_changed_at", None)

    target = _template_uuid(0)
    stamp = db.set_current_profile(target)
    assert stamp
    assert db.get_setting("profile.current") == target
    assert db.get_setting("profile.current_changed_at") == stamp
    # A switch changes the declared-profile blocks, not the Q&A base: the
    # facts stamp stays independent so a still-unacknowledged Q&A event is
    # never silently absorbed into the switch.
    assert db.get_setting("qa.facts_invalidated_at") == "2026-01-01T00:00:00+00:00"


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
    """If either of the two row updates fails, neither may stick."""
    db.set_current_profile(_template_uuid(0))
    before = {k: _raw(k) for k in KEYS}

    real = db_settings._upsert_setting_row
    calls = {"n": 0}

    def failing(spec, value):
        calls["n"] += 1
        if calls["n"] == 2:            # the last of the two staged writes
            raise RuntimeError("boom")
        real(spec, value)

    monkeypatch.setattr(db_settings, "_upsert_setting_row", failing)
    with pytest.raises(RuntimeError):
        db.set_current_profile(_template_uuid(1))
    assert {k: _raw(k) for k in KEYS} == before


def test_plain_set_setting_stamps_nothing(app_ctx):
    """The low-level seam still works but never advances the event stamp."""
    db.set_current_profile(None)
    changed_before = _raw("profile.current_changed_at")
    db.set_setting("profile.current", _template_uuid(0))
    assert _raw("profile.current_changed_at") == changed_before


def test_deleting_current_profile_clears_pointer_atomically(app_ctx):
    """A tree save that deletes the active profile must clear profile.current
    and advance its change stamp in the SAME transaction — never leave a
    dangling uuid that silently disables every declared-profile block."""
    from uuid import uuid4 as u4

    pu, other = u4(), u4()
    db.db.session.add(db.Profile(uuid=pu, name="Doomed", folder_uuid=None,
                                 position=998))
    db.db.session.add(db.Profile(uuid=other, name="Kept", folder_uuid=None,
                                 position=999))
    db.db.session.commit()
    try:
        db.set_current_profile(str(pu))
        stamp_before = _raw("profile.current_changed_at")

        # Deleting an UNRELATED profile leaves the pointer alone.
        db.profile_save_tree(*_tree_without(str(other)))
        assert db.get_setting("profile.current") == str(pu)
        assert _raw("profile.current_changed_at") == stamp_before

        # Deleting the ACTIVE profile clears the pointer and stamps.
        db.profile_save_tree(*_tree_without(str(pu)))
        assert db.get_setting("profile.current") is None
        stamp_after = _raw("profile.current_changed_at")
        assert stamp_after and stamp_after != stamp_before
        # The next turn's snapshot sees a clean unset state, so the room
        # marker announces the change instead of the blocks vanishing mutely.
        import user_profile
        context = user_profile.current_profile_context()
        assert context.profile is None and context.profile_uuid is None
        assert context.profile_changed_at == stamp_after
    finally:
        db.db.session.rollback()
        db.db.session.query(db.Profile).filter(
            db.Profile.uuid.in_([pu, other])).delete()
        db.db.session.commit()


def test_concurrent_delete_and_switch_never_dangle(app_ctx):
    """Deletion and switching coordinate through the same setting-row lock:
    whichever commits second observes the other's outcome, so the pointer
    can never end up referencing a deleted profile."""
    import threading
    from uuid import uuid4 as u4

    for _ in range(3):
        pu = u4()
        db.db.session.add(db.Profile(uuid=pu, name="Racer", folder_uuid=None,
                                     position=997))
        db.db.session.commit()
        db.set_current_profile(None)
        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        app = app_ctx

        def deleter():
            with app.app_context():
                try:
                    barrier.wait(timeout=10)
                    db.profile_save_tree(*_tree_without(str(pu)))  # deletes P only
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def switcher():
            with app.app_context():
                try:
                    barrier.wait(timeout=10)
                    db.set_current_profile(str(pu))
                except ValueError:
                    pass  # legitimate: the profile was already deleted
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=deleter),
                   threading.Thread(target=switcher)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        # A deadlocked worker times out of join without adding an error;
        # a still-alive thread IS the failure.
        assert not any(t.is_alive() for t in threads), "worker deadlocked"
        assert not errors
        db.db.session.expire_all()
        pointer = db.get_setting("profile.current")
        if pointer is not None:
            # Only acceptable when it resolves — never a dangling uuid.
            assert db.profile_get(UUID(str(pointer))) is not None
            db.set_current_profile(None)
        db.db.session.query(db.Profile).filter(
            db.Profile.uuid == pu).delete()
        db.db.session.commit()


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
    assert db.get_setting("qa.facts_invalidated_at") is None   # untouched

    # An unrelated setting write must not advance the profile stamp.
    resp = client.post("/settings/api/set",
                       json={"key": "cron.paused", "value": None})
    assert resp.status_code == 200
    assert db.get_setting("profile.current_changed_at") == stamp

    # Internal keys are machine-owned: the public endpoint rejects writes,
    # not merely hides them from the listing.
    resp = client.post("/settings/api/set",
                       json={"key": "profile.current_changed_at",
                             "value": "2026-01-01T00:00:00+00:00"})
    assert resp.status_code == 400
    assert "internal" in resp.get_json()["error"]
    assert db.get_setting("profile.current_changed_at") == stamp
