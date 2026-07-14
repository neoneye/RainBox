"""Tests for the person-profile tree persistence + data validation (db.profile,
profile_fields registry, the shipped built-in templates)."""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
import profile_fields
from db.models import Profile, ProfileFolder


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


def test_registry_shape():
    keys = [f.key for f in profile_fields.PROFILE_FIELDS]
    assert len(keys) == len(set(keys)) == 19
    assert keys[0] == "full_name"
    assert profile_fields.FIELD_GROUPS == [
        "Identity", "Locale & formats", "Contact & location"]
    for f in profile_fields.PROFILE_FIELDS:
        assert f.kind in {"text", "enum", "date", "email"}
        if f.kind == "enum":
            assert f.choices
        else:
            assert f.choices == ()
    for k in profile_fields.SUMMARY_KEYS:
        assert k in profile_fields.FIELDS_BY_KEY


def test_profile_models_round_trip(app_ctx):
    fu, pu = uuid4(), uuid4()
    db.db.session.add(ProfileFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Profile(uuid=pu, name="T-profile", folder_uuid=fu, position=0,
                              data={"full_name": "Ada Test", "units": "metric"}))
    db.db.session.commit()
    try:
        f = db.db.session.execute(sa.select(ProfileFolder).where(ProfileFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(sa.select(Profile).where(Profile.uuid == pu)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.data == {"full_name": "Ada Test", "units": "metric"}
        assert p.folder_uuid == fu
        assert f.created_at and p.updated_at  # timestamp defaults fire
    finally:
        db.db.session.execute(sa.delete(Profile).where(Profile.uuid == pu))
        db.db.session.execute(sa.delete(ProfileFolder).where(ProfileFolder.uuid == fu))
        db.db.session.commit()


def test_validate_data_canonical_and_errors():
    ok = db.validate_profile_data({
        "full_name": "Jacobus van 't Hoff", "units": "metric",
        "birthday": "1987-08-30", "address": "Line one\nLine two",
        "timezone": "", "email": "x@example.com"})
    assert ok == {"full_name": "Jacobus van 't Hoff", "units": "metric",
                  "birthday": "1987-08-30", "address": "Line one\nLine two",
                  "email": "x@example.com"}          # "" canonicalized away
    assert db.validate_profile_data({}) == {}        # sparse blob valid
    with pytest.raises(db.ProfileDataError, match="no_such"):
        db.validate_profile_data({"no_such": "x"})
    with pytest.raises(db.ProfileDataError, match="no_such"):
        db.validate_profile_data({"no_such": ""})    # unknown stays rejected when empty
    with pytest.raises(db.ProfileDataError, match="units"):
        db.validate_profile_data({"units": "furlongs"})
    with pytest.raises(db.ProfileDataError, match="birthday"):
        db.validate_profile_data({"birthday": "2026-02-30"})
    with pytest.raises(db.ProfileDataError, match="birthday"):
        db.validate_profile_data({"birthday": "07/14/2026"})
    with pytest.raises(db.ProfileDataError, match="dynamic"):
        db.validate_profile_data({"dynamic": {}})    # connector-owned, read-only
    with pytest.raises(db.ProfileDataError, match="full_name"):
        db.validate_profile_data({"full_name": 5})
    with pytest.raises(db.ProfileDataError):
        db.validate_profile_data(["not", "a", "dict"])
