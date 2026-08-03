"""Tests for the personality tree persistence + revision history (db.personality)."""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import Personality, PersonalityFolder, PersonalityRevision


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


def test_personality_models_round_trip(app_ctx):
    fu, pu, ru = uuid4(), uuid4(), uuid4()
    db.db.session.add(PersonalityFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Personality(
        uuid=pu, name="T-personality", content="Curious and blunt.",
        folder_uuid=fu, position=0,
    ))
    db.db.session.add(PersonalityRevision(
        uuid=ru, personality_uuid=pu, content="Curious and blunt."))
    db.db.session.commit()
    try:
        f = db.db.session.execute(
            sa.select(PersonalityFolder).where(PersonalityFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(
            sa.select(Personality).where(Personality.uuid == pu)).scalar_one()
        r = db.db.session.execute(
            sa.select(PersonalityRevision).where(PersonalityRevision.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.content == "Curious and blunt." and p.folder_uuid == fu
        assert r.personality_uuid == pu and r.content == p.content
        assert f.created_at and p.updated_at and r.created_at  # timestamp defaults fire
    finally:
        db.db.session.execute(
            sa.delete(PersonalityRevision).where(PersonalityRevision.uuid == ru))
        db.db.session.execute(sa.delete(Personality).where(Personality.uuid == pu))
        db.db.session.execute(
            sa.delete(PersonalityFolder).where(PersonalityFolder.uuid == fu))
        db.db.session.commit()
