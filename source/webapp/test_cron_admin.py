"""Tests for the cron Flask-Admin formatters (webapp/core.py).

Runs against the test DB (rainbox_claude, pinned by conftest). Creates a small
folder tree + job + run and tears them down.
"""
import uuid

import pytest

import db
from webapp.core import _cron_job_path, _cron_run_job_path, _cron_target_label
from webapp.core import app as flask_app


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
def nested(app_ctx):
    """My Life / Computer / <job>, with one run. Yields (job, run); cleans up."""
    s = db.db.session
    f1, f2, ju, ru = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    s.add(db.CronFolder(uuid=f1, name="My Life", parent_uuid=None, position=0))
    s.add(db.CronFolder(uuid=f2, name="Computer", parent_uuid=f1, position=0))
    s.add(db.CronJob(uuid=ju, name="Database backup", folder_uuid=f2, action_type="backup"))
    s.add(db.CronRun(uuid=ru, cron_uuid=ju, trigger="manual"))
    s.commit()
    job = s.query(db.CronJob).filter_by(uuid=ju).one()
    run = s.query(db.CronRun).filter_by(uuid=ru).one()
    try:
        yield job, run
    finally:
        s.query(db.CronRun).filter_by(uuid=ru).delete()
        s.query(db.CronJob).filter_by(uuid=ju).delete()
        s.query(db.CronFolder).filter(db.CronFolder.uuid.in_([f1, f2])).delete(
            synchronize_session=False)
        s.commit()


def test_cron_job_path_is_root_first(nested):
    job, _ = nested
    assert _cron_job_path(job) == "My Life / Computer / Database backup"


def test_cron_run_cell_shows_short_uuid_and_path(nested):
    job, run = nested
    cell = str(_cron_run_job_path(None, None, run, "cron_uuid"))
    assert str(job.uuid)[:6] in cell                 # truncated uuid
    assert str(job.uuid) in cell                     # full uuid in hover title
    assert "My Life / Computer / Database backup" in cell
    assert "<br>" in cell                            # uuid NEWLINE path


def test_cron_run_cell_unfiled_job(app_ctx):
    s = db.db.session
    ju, ru = uuid.uuid4(), uuid.uuid4()
    s.add(db.CronJob(uuid=ju, name="Loose", folder_uuid=None, action_type="message"))
    s.add(db.CronRun(uuid=ru, cron_uuid=ju, trigger="manual"))
    s.commit()
    try:
        run = s.query(db.CronRun).filter_by(uuid=ru).one()
        assert "Loose" in str(_cron_run_job_path(None, None, run, "cron_uuid"))
    finally:
        s.query(db.CronRun).filter_by(uuid=ru).delete()
        s.query(db.CronJob).filter_by(uuid=ju).delete()
        s.commit()


def test_cron_run_cell_deleted_job(app_ctx):
    s = db.db.session
    run = db.CronRun(uuid=uuid.uuid4(), cron_uuid=uuid.uuid4(), trigger="scheduled")
    cell = str(_cron_run_job_path(None, None, run, "cron_uuid"))
    assert "deleted job" in cell


# ---- target column (message jobs store a chatroom uuid) --------------------

def test_cron_target_label_variants(app_ctx):
    s = db.db.session
    room_uuid = uuid.uuid4()
    s.add(db.Chatroom(uuid=room_uuid, name="Ops", created_by=uuid.uuid4()))
    s.commit()
    try:
        known = type("M", (), {"target": str(room_uuid)})()
        assert "#Ops" in str(_cron_target_label(None, None, known, "target"))
        unknown = type("M", (), {"target": str(uuid.uuid4())})()
        assert "unknown room" in str(_cron_target_label(None, None, unknown, "target"))
        notuuid = type("M", (), {"target": "#dev"})()
        assert "not a uuid" in str(_cron_target_label(None, None, notuuid, "target"))
        assert _cron_target_label(None, None, type("M", (), {"target": ""})(), "target") == ""
    finally:
        s.query(db.Chatroom).filter_by(uuid=room_uuid).delete()
        s.commit()


def test_admin_cronjob_page_renders(app_ctx):
    """Render /admin/cronjob/ end-to-end (catches formatter import/runtime errors
    the unit tests miss)."""
    s = db.db.session
    room_uuid, ju = uuid.uuid4(), uuid.uuid4()
    s.add(db.Chatroom(uuid=room_uuid, name="AdminRoom", created_by=uuid.uuid4()))
    s.add(db.CronJob(uuid=ju, name="MsgJob", action_type="message", target=str(room_uuid)))
    s.commit()
    try:
        resp = flask_app.test_client().get("/admin/cronjob/")
        assert resp.status_code == 200
        assert "AdminRoom" in resp.get_data(as_text=True)
    finally:
        s.query(db.CronJob).filter_by(uuid=ju).delete()
        s.query(db.Chatroom).filter_by(uuid=room_uuid).delete()
        s.commit()
