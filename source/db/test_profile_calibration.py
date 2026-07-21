"""Tests for the knowledge-calibration subtree (db.profile_calibration): the
validator's limits and duplicate detection, stable server-owned ids and
semantic-only restamping, merge safety against the flat-field PUT through the
shared row lock, duplication identity refresh, and the JSON API."""
import json
import threading
from uuid import UUID, uuid4

import pytest

import db
import webapp.core as webapp_core
from db import profile_calibration
from db.models import Profile

FIXED_STAMP = "2026-07-21T12:00:00Z"
LATER_STAMP = "2026-07-22T09:30:00Z"


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def profile(app_ctx):
    """One throwaway user profile row, removed afterwards."""
    pu = uuid4()
    db.db.session.add(Profile(uuid=pu, name="CalTest", folder_uuid=None,
                              position=999))
    db.db.session.commit()
    try:
        yield pu
    finally:
        db.db.session.rollback()
        db.db.session.query(Profile).filter(Profile.uuid == pu).delete()
        db.db.session.commit()


@pytest.fixture
def fixed_stamp(monkeypatch):
    """Pin the server stamp so restamp assertions are deterministic."""
    def _set(value):
        monkeypatch.setattr(profile_calibration, "_now_stamp", lambda: value)
    _set(FIXED_STAMP)
    return _set


def _row(topic="Python", level="beginner", **extra):
    return {"topic": topic, "level": level, **extra}


# ---- validator --------------------------------------------------------------

def test_new_rows_get_server_ids_and_stamp(profile, fixed_stamp):
    rows = db.calibration_put(profile, [
        _row("Python", "beginner", stance="prefer", depth="teach",
             note="Knows concepts from other languages."),
        _row("JavaScript", "intermediate", stance="avoid"),
    ])
    assert [r["topic"] for r in rows] == ["Python", "JavaScript"]
    for r in rows:
        UUID(r["id"])                          # server-assigned uuid
        assert r["updated_at"] == FIXED_STAMP  # RFC 3339 Z, whole seconds
    assert rows[0]["depth"] == "teach"
    assert rows[1]["stance"] == "avoid"
    assert "depth" not in rows[1] and "note" not in rows[1]   # optionals stay off


def test_round_trip_is_a_noop_and_keeps_ids(profile, fixed_stamp):
    first = db.calibration_put(profile, [_row("Python", "beginner")])
    fixed_stamp(LATER_STAMP)
    # PUT the canonical snapshot back minus the server-owned updated_at.
    resend = [{k: v for k, v in r.items() if k != "updated_at"} for r in first]
    second = db.calibration_put(profile, resend)
    assert second == first                     # same id, same stamp, no restamp


def test_semantic_edit_restamps_only_that_row(profile, fixed_stamp):
    rows = db.calibration_put(profile, [_row("Python", "beginner"),
                                        _row("Rust", "none")])
    fixed_stamp(LATER_STAMP)
    edited = [
        {"id": rows[0]["id"], "topic": "Python", "level": "expert"},
        {"id": rows[1]["id"], "topic": "Rust", "level": "none"},
    ]
    out = db.calibration_put(profile, edited)
    assert out[0]["updated_at"] == LATER_STAMP     # level changed → restamped
    assert out[1]["updated_at"] == FIXED_STAMP     # untouched → original stamp


def test_reorder_restamps_nothing(profile, fixed_stamp):
    rows = db.calibration_put(profile, [_row("A", "expert"), _row("B", "none")])
    fixed_stamp(LATER_STAMP)
    reordered = [{"id": r["id"], **{k: r[k] for k in ("topic", "level")}}
                 for r in reversed(rows)]
    out = db.calibration_put(profile, reordered)
    assert [r["topic"] for r in out] == ["B", "A"]     # order is priority order
    assert all(r["updated_at"] == FIXED_STAMP for r in out)


def test_client_supplied_updated_at_unknown_id_and_unknown_key_rejected(profile):
    with pytest.raises(db.ProfileCalibrationError, match="updated_at"):
        db.calibration_put(profile, [
            {**_row(), "updated_at": "2026-01-01T00:00:00Z"}])
    with pytest.raises(db.ProfileCalibrationError, match="unknown row id"):
        db.calibration_put(profile, [{**_row(), "id": str(uuid4())}])
    with pytest.raises(db.ProfileCalibrationError, match="unknown key"):
        db.calibration_put(profile, [{**_row(), "aliases": "pg"}])
    with pytest.raises(db.ProfileCalibrationError, match="not a uuid"):
        db.calibration_put(profile, [{**_row(), "id": "nope"}])


def test_duplicate_topics_name_both_positions(profile):
    with pytest.raises(db.ProfileCalibrationError,
                       match=r"row 1 and row 3"):
        db.calibration_put(profile, [
            _row(" PostgreSQL ", "expert"),
            _row("Redis", "none"),
            _row("postgresql", "beginner"),
        ])


def test_unicode_equivalent_topics_are_duplicates(profile):
    # NFKC: the ﬁ ligature normalizes to "fi".
    with pytest.raises(db.ProfileCalibrationError, match="duplicate topic"):
        db.calibration_put(profile, [_row("ﬁnance", "none"),
                                     _row("Finance", "expert")])


def test_display_topic_keeps_case_but_collapses_whitespace(profile, fixed_stamp):
    rows = db.calibration_put(profile, [_row("  Common  Lisp \n", "expert")])
    assert rows[0]["topic"] == "Common Lisp"


def test_blank_rows_dropped_and_partial_rows_rejected(profile, fixed_stamp):
    rows = db.calibration_put(profile, [
        {"topic": "", "level": "", "note": ""},          # all blank → dropped
        _row("Python", "beginner", stance="", note=""),  # blanks removed
    ])
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "topic", "level", "updated_at"}
    with pytest.raises(db.ProfileCalibrationError, match="missing 'topic'"):
        db.calibration_put(profile, [{"topic": "", "level": "expert"}])
    with pytest.raises(db.ProfileCalibrationError, match="missing 'level'"):
        db.calibration_put(profile, [{"topic": "Python", "level": ""}])


def test_enum_and_size_limits(profile):
    with pytest.raises(db.ProfileCalibrationError, match="'level' must be one of"):
        db.calibration_put(profile, [_row("X", "guru")])
    with pytest.raises(db.ProfileCalibrationError, match="'stance' must be one of"):
        db.calibration_put(profile, [_row(stance="loves")])
    with pytest.raises(db.ProfileCalibrationError, match="'depth' must be one of"):
        db.calibration_put(profile, [_row(depth="deep")])
    with pytest.raises(db.ProfileCalibrationError, match="'topic' exceeds 80"):
        db.calibration_put(profile, [_row("x" * 81, "none")])
    with pytest.raises(db.ProfileCalibrationError, match="'note' exceeds 400"):
        db.calibration_put(profile, [_row(note="x" * 401)])
    with pytest.raises(db.ProfileCalibrationError, match="at most 100"):
        db.calibration_put(profile, [_row(f"T{i}", "none") for i in range(101)])
    with pytest.raises(db.ProfileCalibrationError, match="must be a string"):
        db.calibration_put(profile, [{"topic": "X", "level": 3}])
    with pytest.raises(db.ProfileCalibrationError, match="must be a list"):
        db.calibration_put(profile, {"topic": "X"})


def test_empty_snapshot_removes_the_subtree(profile, fixed_stamp):
    db.calibration_put(profile, [_row()])
    assert db.calibration_put(profile, []) == []
    stored = db.db.session.execute(
        db.db.select(Profile).where(Profile.uuid == profile)).scalar_one()
    assert "calibration" not in (stored.data or {})    # absent reads as no topics
    assert db.calibration_get(profile) == {"builtin": False, "topics": []}


# ---- merge safety -----------------------------------------------------------

def test_flat_save_preserves_calibration_by_deep_equality(profile, fixed_stamp):
    rows = db.calibration_put(profile, [
        _row("Python", "beginner", note="wants idiomatic examples")])
    db.profile_update_data(profile, {"full_name": "New Name"})
    stored = db.profile_get(profile)["data"]
    assert stored["full_name"] == "New Name"
    assert stored["calibration"]["topics"] == rows     # deep equality


def test_calibration_save_preserves_flat_fields_and_dynamic(profile, fixed_stamp):
    row = db.db.session.execute(
        db.db.select(Profile).where(Profile.uuid == profile)).scalar_one()
    dynamic = {"screen": {"value": "3440x1440", "seen_at": "2026-07-01T00:00:00+00:00"}}
    row.data = {"full_name": "Keeper", "dynamic": dynamic}
    db.db.session.commit()
    db.calibration_put(profile, [_row()])
    stored = db.profile_get(profile)["data"]
    assert stored["full_name"] == "Keeper"
    assert stored["dynamic"] == dynamic


def test_flat_put_rejects_calibration_key(profile):
    with pytest.raises(db.ProfileDataError, match="calibration"):
        db.profile_update_data(profile, {"calibration": {"topics": []}})


def test_concurrent_flat_and_calibration_writes_preserve_both(app_ctx, profile):
    """A two-transaction race between the flat-field writer and the
    calibration writer must preserve both winners: the shared FOR UPDATE row
    lock serializes them, so neither can write back a stale copy of the other
    subtree."""
    app = app_ctx
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def flat_writer():
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                db.profile_update_data(profile, {"full_name": "Racer"})
            except Exception as exc:  # noqa: BLE001 — surfaced via the assert below
                errors.append(exc)

    def calibration_writer():
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                db.calibration_put(profile, [_row("Python", "expert")])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=flat_writer),
               threading.Thread(target=calibration_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    db.db.session.expire_all()
    stored = db.profile_get(profile)["data"]
    assert stored["full_name"] == "Racer"
    assert stored["calibration"]["topics"][0]["topic"] == "Python"


# ---- duplication ------------------------------------------------------------

def test_duplicate_mints_fresh_calibration_identity(profile, fixed_stamp):
    src_rows = db.calibration_put(profile, [
        _row("Python", "beginner", stance="prefer"),
        _row("Rust", "none")])
    fixed_stamp(LATER_STAMP)
    dup = db.profile_duplicate(profile)
    try:
        copied = db.calibration_get(UUID(dup["uuid"]))["topics"]
        assert [(r["topic"], r["level"]) for r in copied] == \
               [(r["topic"], r["level"]) for r in src_rows]      # semantics + order
        assert {r["id"] for r in copied}.isdisjoint({r["id"] for r in src_rows})
        assert all(r["updated_at"] == LATER_STAMP for r in copied)
    finally:
        db.db.session.query(Profile).filter(
            Profile.uuid == UUID(dup["uuid"])).delete()
        db.db.session.commit()


# ---- API --------------------------------------------------------------------

def test_calibration_api_round_trip(profile, fixed_stamp):
    client = webapp_core.app.test_client()
    base = f"/profile/api/profiles/{profile}/calibration"

    resp = client.get(base)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "builtin": False, "topics": []}

    resp = client.put(base, json={"topics": [_row("Python", "beginner")]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["builtin"] is False
    assert body["topics"][0]["id"]                 # server ids in the response
    assert body["topics"][0]["updated_at"] == FIXED_STAMP

    resp = client.put(base, json={"topics": [_row("X", "guru")]})
    assert resp.status_code == 400
    assert "'level' must be one of" in resp.get_json()["error"]

    assert client.get("/profile/api/profiles/not-a-uuid/calibration").status_code == 400
    assert client.get(f"/profile/api/profiles/{uuid4()}/calibration").status_code == 404
    assert client.put(f"/profile/api/profiles/{uuid4()}/calibration",
                      json={"topics": []}).status_code == 404
    assert client.put(base, json={"nope": 1}).status_code == 400


def test_builtin_calibration_readonly_via_api(app_ctx):
    client = webapp_core.app.test_client()
    builtin = db.profile_templates_entries()[0]["uuid"]
    resp = client.get(f"/profile/api/profiles/{builtin}/calibration")
    assert resp.status_code == 200
    assert resp.get_json()["builtin"] is True
    resp = client.put(f"/profile/api/profiles/{builtin}/calibration",
                      json={"topics": []})
    assert resp.status_code == 400
    assert "read-only built-in" in resp.get_json()["error"]


def test_profile_detail_get_excludes_calibration(profile, fixed_stamp):
    db.calibration_put(profile, [_row()])
    db.profile_update_data(profile, {"full_name": "Keeper"})
    client = webapp_core.app.test_client()
    body = client.get(f"/profile/api/profiles/{profile}").get_json()
    assert body["ok"] is True
    assert body["data"]["full_name"] == "Keeper"
    assert "calibration" not in body["data"]       # projected out, not just hidden


def test_raw_request_limits_reject_before_traversal(profile):
    """A huge list of blank rows must be refused up front — the canonical
    100-row cap only counts survivors after full traversal."""
    with pytest.raises(db.ProfileCalibrationError, match="at most 1000"):
        db.calibration_put(profile, [{"topic": "", "level": ""}] * 1001)
    client = webapp_core.app.test_client()
    base = f"/profile/api/profiles/{profile}/calibration"
    resp = client.put(base, json={"topics": [{"topic": "", "level": ""}] * 1001})
    assert resp.status_code == 400
    # The raw body-size cap fires before JSON parsing.
    resp = client.put(base, data=b"x" * 1_000_001,
                      headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


def test_canonical_json_size_cap(profile):
    # The cap counts serialized UTF-8 bytes: 100 ASCII rows stay under it, so
    # multi-byte notes (4 bytes per char) are what can overflow 64 KiB.
    rows = [_row(f"{'t' * 78}{i:02d}", "none", note="𝕏" * 400)
            for i in range(100)]
    blob = json.dumps({"topics": rows}, ensure_ascii=False)
    assert len(blob.encode("utf-8")) > 64 * 1024   # sanity: genuinely over
    with pytest.raises(db.ProfileCalibrationError, match="exceeds"):
        db.calibration_put(profile, rows)
