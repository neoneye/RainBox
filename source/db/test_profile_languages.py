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
        db.session.rollback()
        ctx.pop()


@pytest.fixture
def profile(app_ctx):
    pu = uuid4()
    db.session.add(Profile(
        uuid=pu, name="LanguageTest", folder_uuid=None, position=999))
    db.session.commit()
    try:
        yield pu
    finally:
        db.session.rollback()
        db.session.query(Profile).filter(Profile.uuid == pu).delete()
        db.session.commit()


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


def test_empty_snapshot_is_authoritative(profile, fixed_stamp):
    db.languages_put(profile, [_row("da", "native", "neutral")])
    assert db.languages_put(profile, []) == []
    data = db.profile_get(profile)["data"]
    assert data["languages"] == {"rows": []}
    assert db.languages_get(profile)["rows"] == []


def test_flat_save_preserves_languages(profile, fixed_stamp):
    db.languages_put(profile, [
        _row("da", "native", "neutral"),
        _row("en-US", "fluent", "prefer"),
    ])
    before = db.profile_get(profile)["data"]["languages"]

    db.profile_update_data(profile, {"full_name": "Keeper"})
    after = db.profile_get(profile)["data"]
    assert after["languages"] == before
    assert after["full_name"] == "Keeper"


def test_languages_save_preserves_other_subtrees(profile, fixed_stamp):
    dynamic = {
        "screen": {"value": "3440x1440", "seen_at": "2026-07-01T00:00:00Z"}}
    row = db.session.execute(
        db.db.select(Profile).where(Profile.uuid == profile)).scalar_one()
    row.data = {
        "full_name": "Keeper",
        "dynamic": dynamic,
        "calibration": {"topics": [{"topic": "Python"}]},
    }
    db.session.commit()
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
        db.session.query(Profile).filter(
            Profile.uuid == UUID(duplicate["uuid"])).delete()
        db.session.commit()


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

    response = client.put(
        f"/profile/api/profiles/{profile}",
        json={"data": {"full_name": "Old schema", "language": "fr"}},
    )
    assert response.status_code == 400
    assert "unknown field: 'language'" in response.get_json()["error"]

    detail = client.get(f"/profile/api/profiles/{profile}").get_json()
    assert "languages" not in detail["data"]


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


def test_all_templates_have_only_language_rows(app_ctx):
    for entry in db.profile_templates_entries():
        data = entry["data"]
        assert data["languages"]["rows"]
        assert "language" not in data and "language_2" not in data
        assert data["languages"]["rows"][0]["stance"] == "neutral"
        assert all("note" not in row for row in data["languages"]["rows"])


def test_request_size_cap(profile):
    """The per-request cap is ten times the stored-row cap: generous enough
    that no legitimate edit (which only ever sends a handful of rows) can
    hit it, while still rejecting a clearly abusive request early."""
    request_cap = 10 * profile_languages.MAX_LANGUAGE_ROWS
    with pytest.raises(db.ProfileLanguagesError, match=f"at most {request_cap}"):
        db.languages_put(profile, [{}] * (request_cap + 1))


def test_a_language_list_longer_than_the_cap_is_rejected():
    """Four languages reach the detector and the rest only inform the model on
    the rare turn it runs, so an unbounded list has no consumer."""
    rows = [{"tag": tag, "level": "fluent", "stance": "neutral", "note": ""}
            for tag in ("en", "da", "de", "fr", "es", "it", "nl")]
    with pytest.raises(ValueError):
        profile_languages.validate_language_rows(rows, [])


def test_a_language_list_at_the_cap_is_accepted():
    rows = [{"tag": tag, "level": "fluent", "stance": "neutral", "note": ""}
            for tag in ("en", "da", "de", "fr", "es", "it")]
    assert len(profile_languages.validate_language_rows(rows, [])) == (
        profile_languages.MAX_LANGUAGE_ROWS)


def test_a_serialized_snapshot_at_the_cap_stays_under_the_byte_limit():
    """The byte-size cap exists in case a future MAX_LANGUAGE_ROWS or
    MAX_LANGUAGE_NOTE_CHARS grows again; at today's six-row cap, even the
    largest possible snapshot (max-length tag and note on every row) fits
    comfortably inside it, so the byte cap can no longer be exercised through
    the row-count cap alone."""
    rows = [
        _row(f"en-x{i:02d}", "intermediate", "neutral", note="𝕏" * 400)
        for i in range(profile_languages.MAX_LANGUAGE_ROWS)
    ]
    canonical = profile_languages.validate_language_rows(rows, [])
    blob = json.dumps({"rows": canonical}, ensure_ascii=False)
    assert len(blob.encode("utf-8")) < profile_languages.MAX_LANGUAGES_BYTES
