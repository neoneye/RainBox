"""Tests for ``data.languages.rows`` validation, persistence, migration,
summary projection, duplication, and API behavior."""

import json
from uuid import UUID, uuid4

import pytest

import db
import webapp.core as webapp_core
from db import profile_languages
from db.models import Profile

FIXED_STAMP = "2026-07-24T12:00:00Z"
LATER_STAMP = "2026-07-25T09:30:00Z"


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
    pu = uuid4()
    db.db.session.add(Profile(
        uuid=pu, name="LanguageTest", folder_uuid=None, position=999))
    db.db.session.commit()
    try:
        yield pu
    finally:
        db.db.session.rollback()
        db.db.session.query(Profile).filter(Profile.uuid == pu).delete()
        db.db.session.commit()


@pytest.fixture
def fixed_stamp(monkeypatch):
    def _set(value):
        monkeypatch.setattr(profile_languages, "_now_stamp", lambda: value)
    _set(FIXED_STAMP)
    return _set


def _row(tag="en-US", level="intermediate", stance="prefer", **extra):
    return {"tag": tag, "level": level, "stance": stance, **extra}


def test_new_rows_are_canonical_and_get_server_identity(profile, fixed_stamp):
    rows = db.languages_put(profile, [
        _row("EN-us", note="Primary response language."),
        _row("da", "native", "neutral"),
    ])
    assert [(row["tag"], row["level"], row["stance"]) for row in rows] == [
        ("en-US", "intermediate", "prefer"),
        ("da", "native", "neutral"),
    ]
    for row in rows:
        UUID(row["id"])
        assert row["updated_at"] == FIXED_STAMP


def test_noop_reorder_and_semantic_edit_timestamp_rules(
    profile, fixed_stamp,
):
    first = db.languages_put(profile, [
        _row("en-US"), _row("da", "native", "neutral")])
    fixed_stamp(LATER_STAMP)
    resend = [
        {key: value for key, value in row.items() if key != "updated_at"}
        for row in reversed(first)
    ]
    reordered = db.languages_put(profile, resend)
    assert [row["tag"] for row in reordered] == ["da", "en-US"]
    assert all(row["updated_at"] == FIXED_STAMP for row in reordered)

    edited = [
        {key: value for key, value in row.items() if key != "updated_at"}
        for row in reordered
    ]
    edited[0]["stance"] = "avoid"
    out = db.languages_put(profile, edited)
    assert out[0]["updated_at"] == LATER_STAMP
    assert out[1]["updated_at"] == FIXED_STAMP


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row("english!!")], "BCP-47"),
        ([_row("en", level="expert")], "'level' must be one of"),
        ([_row("en", stance="sometimes")], "'stance' must be one of"),
        ([_row("en"), _row("EN", stance="neutral")], "duplicate language tag"),
        ([_row("en"), _row("da")], "only one language"),
        ([_row("en", note="x" * 401)], "'note' exceeds 400"),
        ([{"tag": "en", "level": "native"}], "missing 'stance'"),
        ([{"tag": "en", "stance": "neutral"}], "missing 'level'"),
        ([{"level": "native", "stance": "neutral"}], "missing 'tag'"),
        ({"tag": "en"}, "must be a list"),
    ],
)
def test_validation_rejects_invalid_snapshots(profile, rows, message):
    with pytest.raises(db.ProfileLanguagesError, match=message):
        db.languages_put(profile, rows)


def test_server_fields_unknown_fields_and_unknown_ids_rejected(profile):
    with pytest.raises(db.ProfileLanguagesError, match="updated_at"):
        db.languages_put(
            profile, [{**_row(), "updated_at": FIXED_STAMP}])
    with pytest.raises(db.ProfileLanguagesError, match="unknown key"):
        db.languages_put(profile, [{**_row(), "dialect": "American"}])
    with pytest.raises(db.ProfileLanguagesError, match="unknown row id"):
        db.languages_put(profile, [{**_row(), "id": str(uuid4())}])


def test_empty_snapshot_is_authoritative_and_keeps_legacy_rollback(
    profile, fixed_stamp,
):
    stored = db.db.session.execute(
        db.db.select(Profile).where(Profile.uuid == profile)).scalar_one()
    stored.data = {"language": "da", "language_2": "en-US"}
    db.db.session.commit()

    assert db.languages_put(profile, []) == []
    data = db.profile_get(profile)["data"]
    assert data["languages"] == {"rows": []}
    assert data["language"] == "da" and data["language_2"] == "en-US"
    assert db.languages_get(profile)["rows"] == []


def test_startup_migration_is_reversible_and_idempotent(app_ctx, fixed_stamp):
    pu = uuid4()
    row = Profile(
        uuid=pu, name="Legacy", folder_uuid=None, position=999,
        data={"language": "DA", "language_2": "en-us", "city": "Copenhagen"})
    db.db.session.add(row)
    db.db.session.commit()
    try:
        assert db.migrate_profile_languages() == 1
        assert db.migrate_profile_languages() == 0
        data = db.profile_get(pu)["data"]
        assert data["language"] == "DA"
        assert data["language_2"] == "en-us"
        assert data["city"] == "Copenhagen"
        rows = data["languages"]["rows"]
        assert [(r["tag"], r["level"], r["stance"]) for r in rows] == [
            ("da", "native", "neutral"),
            ("en-US", "intermediate", "neutral"),
        ]
        assert all("review level and stance" in r["note"] for r in rows)
    finally:
        db.db.session.query(Profile).filter(Profile.uuid == pu).delete()
        db.db.session.commit()


def test_flat_save_preserves_languages_and_legacy_fields(profile, fixed_stamp):
    row = db.db.session.execute(
        db.db.select(Profile).where(Profile.uuid == profile)).scalar_one()
    row.data = {"language": "da", "language_2": "en-US"}
    db.db.session.commit()
    db.migrate_profile_languages()
    before = db.profile_get(profile)["data"]["languages"]

    db.profile_update_data(profile, {"full_name": "Keeper"})
    after = db.profile_get(profile)["data"]
    assert after["languages"] == before
    assert after["language"] == "da" and after["language_2"] == "en-US"
    assert after["full_name"] == "Keeper"


def test_languages_save_preserves_other_subtrees(profile, fixed_stamp):
    dynamic = {
        "screen": {"value": "3440x1440", "seen_at": "2026-07-01T00:00:00Z"}}
    row = db.db.session.execute(
        db.db.select(Profile).where(Profile.uuid == profile)).scalar_one()
    row.data = {
        "full_name": "Keeper",
        "dynamic": dynamic,
        "calibration": {"topics": [{"topic": "Python"}]},
    }
    db.db.session.commit()
    db.languages_put(profile, [_row()])
    data = db.profile_get(profile)["data"]
    assert data["full_name"] == "Keeper"
    assert data["dynamic"] == dynamic
    assert data["calibration"] == {"topics": [{"topic": "Python"}]}


def test_summary_prefers_preferred_row_then_declaration_order(
    profile, fixed_stamp,
):
    db.languages_put(profile, [
        _row("da", "native", "neutral"),
        _row("en-US", "intermediate", "prefer"),
    ])
    assert db.profile_get(profile)["data"]["languages"]["rows"][0]["tag"] == "da"
    summary = db.profile_update_data(profile, {"full_name": "Simon"})
    assert summary["language"] == "en-US"


def test_duplicate_keeps_semantics_and_mints_language_identity(
    profile, fixed_stamp,
):
    source = db.languages_put(profile, [
        _row("en-US"), _row("da", "native", "neutral")])
    fixed_stamp(LATER_STAMP)
    duplicate = db.profile_duplicate(profile)
    try:
        copied = db.languages_get(UUID(duplicate["uuid"]))["rows"]
        assert [(r["tag"], r["level"], r["stance"]) for r in copied] == [
            (r["tag"], r["level"], r["stance"]) for r in source]
        assert {r["id"] for r in copied}.isdisjoint(
            {r["id"] for r in source})
        assert all(r["updated_at"] == LATER_STAMP for r in copied)
    finally:
        db.db.session.query(Profile).filter(
            Profile.uuid == UUID(duplicate["uuid"])).delete()
        db.db.session.commit()


def test_languages_api_round_trip_and_profile_projection(profile, fixed_stamp):
    client = webapp_core.app.test_client()
    base = f"/profile/api/profiles/{profile}/languages"

    response = client.get(base)
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True, "builtin": False, "rows": []}

    response = client.put(base, json={"rows": [_row()]})
    assert response.status_code == 200
    body = response.get_json()
    assert body["rows"][0]["tag"] == "en-US"
    assert body["rows"][0]["id"]

    response = client.put(
        base, json={"rows": [_row("en"), _row("da")]})
    assert response.status_code == 400
    assert "only one language" in response.get_json()["error"]

    detail = client.get(f"/profile/api/profiles/{profile}").get_json()
    assert "languages" not in detail["data"]
    assert "language" not in detail["data"]
    assert "language_2" not in detail["data"]


def test_languages_api_errors_and_builtin_readonly(app_ctx):
    client = webapp_core.app.test_client()
    assert client.get(
        "/profile/api/profiles/not-a-uuid/languages").status_code == 400
    assert client.get(
        f"/profile/api/profiles/{uuid4()}/languages").status_code == 404

    builtin = db.profile_templates_entries()[0]["uuid"]
    response = client.get(
        f"/profile/api/profiles/{builtin}/languages")
    assert response.status_code == 200
    assert response.get_json()["builtin"] is True
    response = client.put(
        f"/profile/api/profiles/{builtin}/languages", json={"rows": []})
    assert response.status_code == 400
    assert "read-only built-in" in response.get_json()["error"]


def test_all_templates_have_rows_and_keep_legacy_rollback(app_ctx):
    for entry in db.profile_templates_entries():
        data = entry["data"]
        assert data["languages"]["rows"]
        assert data["languages"]["rows"][0]["tag"] == data["language"]
        assert data["languages"]["rows"][0]["stance"] == "neutral"
        if data.get("language_2"):
            assert data["languages"]["rows"][1]["tag"] == data["language_2"]


def test_request_and_serialized_size_caps(profile):
    with pytest.raises(db.ProfileLanguagesError, match="at most 1000"):
        db.languages_put(profile, [{}] * 1001)
    rows = [
        _row(f"en-x{i:02d}", "beginner", "neutral", note="𝕏" * 400)
        for i in range(100)
    ]
    blob = json.dumps({"rows": rows}, ensure_ascii=False)
    assert len(blob.encode("utf-8")) > 64 * 1024
    with pytest.raises(db.ProfileLanguagesError, match="exceeds"):
        db.languages_put(profile, rows)
