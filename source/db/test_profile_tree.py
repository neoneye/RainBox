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
