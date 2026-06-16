"""Tests for the git tree persistence backend (db.models + db.git)."""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import GitFolder, GitRepo


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


def test_git_models_round_trip(app_ctx):
    fu, ru = uuid4(), uuid4()
    db.db.session.add(GitFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(GitRepo(
        uuid=ru, name="T-repo", folder_uuid=fu,
        path="/tmp/t-repo", description="d", position=0,
    ))
    db.db.session.commit()
    try:
        f = db.db.session.execute(sa.select(GitFolder).where(GitFolder.uuid == fu)).scalar_one()
        r = db.db.session.execute(sa.select(GitRepo).where(GitRepo.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert r.path == "/tmp/t-repo" and r.folder_uuid == fu
        assert f.created_at and r.updated_at  # timestamp defaults fire
    finally:
        db.db.session.execute(sa.delete(GitRepo).where(GitRepo.uuid == ru))
        db.db.session.execute(sa.delete(GitFolder).where(GitFolder.uuid == fu))
        db.db.session.commit()
