"""Tests for the 'backup' cron action type (db.cron).

Hits the live local Postgres. The firing tests insert a cron job directly (not
via cron_save_tree, so the real tree is untouched) and tear down every row they
create — cron_job, cron_run, and cron-room chat messages — so the suite is
non-destructive. Backups are written into pytest's tmp_path, which auto-cleans.
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import CRON_ROOM_UUID, ChatMessage, CronJob, CronRun, Inbox


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
def settings_unset(app_ctx):
    """Force the backup.* settings to unset for a deterministic baseline, then
    restore the operator's real values. Needed because these tests run against
    the shared live DB where the operator may have configured backup.repo etc.;
    a test must never depend on (or destroy) that config."""
    keys = ("backup.repo", "backup.age_recipient", "backup.git_push")
    sel = db.db.session.query(db.AppSetting).filter(db.AppSetting.key.in_(keys))
    before = {r.key: r.value for r in sel.all()}
    for row in sel.all():
        row.value = None
    db.db.session.commit()
    try:
        yield
    finally:
        for row in db.db.session.query(db.AppSetting).filter(db.AppSetting.key.in_(keys)).all():
            row.value = before.get(row.key)
        db.db.session.commit()


@pytest.fixture
def firing(app_ctx):
    """Register a backup CronJob and tear down everything it creates."""
    s = db.db.session
    base_msg = s.query(sa.func.max(ChatMessage.id)).filter(
        ChatMessage.room_uuid == CRON_ROOM_UUID).scalar() or 0
    job_uuids: list = []

    def add_job(**kw):
        kw.setdefault("uuid", uuid4())
        kw.setdefault("name", "Backup")
        kw.setdefault("enabled", True)
        kw.setdefault("cron_expr", "30 3 * * *")
        kw.setdefault("timezone", "localtime")
        kw.setdefault("action_type", "backup")
        job = CronJob(**kw)
        s.add(job)
        s.commit()
        job_uuids.append(job.uuid)
        return job

    try:
        yield add_job
    finally:
        for ju in job_uuids:
            s.execute(sa.delete(CronRun).where(CronRun.cron_uuid == ju))
            s.execute(sa.delete(CronJob).where(CronJob.uuid == ju))
        s.execute(sa.delete(ChatMessage).where(
            ChatMessage.room_uuid == CRON_ROOM_UUID, ChatMessage.id > base_msg))
        s.commit()


def _cron_texts():
    return [m.text for m in db.db.session.query(ChatMessage)
            .filter_by(room_uuid=CRON_ROOM_UUID).all()]


@pytest.fixture
def age_recipient(tmp_path_factory, monkeypatch):
    """Generate a throwaway age keypair and configure rainbox with the PUBLIC
    key via the env var. Returns the recipient string."""
    import re
    import subprocess

    identity = tmp_path_factory.mktemp("agekey") / "identity.txt"
    out = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    recipient = re.search(r"(age1[0-9a-z]+)", out.stderr + out.stdout).group(1)
    monkeypatch.setenv("RAINBOX_BACKUP_AGE_RECIPIENT", recipient)
    return recipient


def test_fire_backup_job_writes_file_to_command_path(firing, tmp_path, age_recipient):
    job = firing(command=str(tmp_path), name="Nightly")
    run = db.fire_cron_job(job, trigger="manual")

    assert run.cron_uuid == job.uuid
    assert job.last_fired_at is not None
    # A real encrypted dump landed under the configured destination.
    backups = list((tmp_path / "rainbox_database").rglob("*.zstd.age"))
    assert len(backups) == 1 and backups[0].stat().st_size > 0
    assert backups[0].read_bytes().startswith(b"age-encryption.org/")
    assert any('▶ backed up "Nightly"' in t for t in _cron_texts())


def test_fire_backup_job_uses_env_when_command_empty(firing, tmp_path, monkeypatch, age_recipient, settings_unset):
    monkeypatch.setenv("RAINBOX_BACKUP_REPO", str(tmp_path))
    job = firing(command="", name="EnvBackup")
    db.fire_cron_job(job, trigger="scheduled")
    assert list((tmp_path / "rainbox_database").rglob("*.zstd.age"))


def test_fire_backup_job_without_destination_posts_error(firing, monkeypatch, settings_unset):
    monkeypatch.delenv("RAINBOX_BACKUP_REPO", raising=False)
    job = firing(command="", name="NoDest")
    db.fire_cron_job(job, trigger="manual")
    # The job still records a run, but the failure is reported as an event line.
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 1
    assert any('✖ "NoDest" failed to fire' in t and "no backup destination" in t
               for t in _cron_texts())


def test_fire_backup_job_without_recipient_posts_error(firing, tmp_path, monkeypatch):
    """Fail-closed via the cron path: a destination but no public key -> error
    event, no file written (never a plaintext fallback)."""
    monkeypatch.delenv("RAINBOX_BACKUP_AGE_RECIPIENT", raising=False)
    monkeypatch.delenv("RAINBOX_BACKUP_AGE_RECIPIENTS_FILE", raising=False)
    job = firing(command=str(tmp_path), name="NoKey")
    db.fire_cron_job(job, trigger="manual")
    assert any('✖ "NoKey" failed to fire' in t and "no age recipient" in t
               for t in _cron_texts())
    assert not (tmp_path / "rainbox_database").exists()


def test_cron_backup_uses_db_settings_not_just_env(firing, tmp_path, tmp_path_factory, monkeypatch):
    """The cron path resolves recipient/destination through db.get_setting, so a
    value set in the DB (with env unset) drives the backup via db.settings."""
    import re
    import subprocess

    monkeypatch.delenv("RAINBOX_BACKUP_AGE_RECIPIENT", raising=False)
    monkeypatch.delenv("RAINBOX_BACKUP_REPO", raising=False)
    identity = tmp_path_factory.mktemp("agekey") / "identity.txt"
    out = subprocess.run(["age-keygen", "-o", str(identity)],
                         capture_output=True, text=True, check=True)
    recipient = re.search(r"(age1[0-9a-z]+)", out.stderr + out.stdout).group(1)

    # Snapshot the live operator values so we restore them exactly — never
    # clobber the shared DB's real backup config to NULL on teardown.
    keys = ("backup.age_recipient", "backup.repo")
    before = {r.key: r.value for r in db.db.session.query(db.AppSetting).filter(
        db.AppSetting.key.in_(keys)).all()}

    db.set_setting("backup.age_recipient", recipient)   # DB, not env
    db.set_setting("backup.repo", str(tmp_path))
    try:
        job = firing(command="", name="DbCfg")          # no per-job override
        db.fire_cron_job(job, trigger="manual")
        backups = list((tmp_path / "rainbox_database").rglob("*.zstd.age"))
        assert len(backups) == 1
        assert backups[0].read_bytes().startswith(b"age-encryption.org/")
    finally:
        for row in db.db.session.query(db.AppSetting).filter(
                db.AppSetting.key.in_(keys)).all():
            row.value = before.get(row.key)
        db.db.session.commit()


def test_cron_tick_fires_due_backup(firing, tmp_path, age_recipient):
    from datetime import timedelta

    past = datetime.now(UTC) - timedelta(minutes=1)
    job = firing(command=str(tmp_path), next_run_at=past, name="DueBackup")
    fired = db.cron_tick()
    assert fired >= 1
    assert list((tmp_path / "rainbox_database").rglob("*.zstd.age"))


def test_validate_and_save_tree_accepts_backup(app_ctx):
    """A 'backup' action survives tree validation and the save/load round-trip."""
    # Validation accepts it (no raise).
    db.validate_cron_tree([], [{
        "uuid": str(uuid4()), "name": "B", "enabled": True, "folderId": None,
        "cron": "30 3 * * *", "timezone": "UTC", "type": "backup",
        "command": "/tmp/x", "target": "", "message": "",
    }])
    # Save/load preserves the type (and doesn't coerce it to "message").
    # Snapshot the tree first so this whole-tree replace leaves the DB as found.
    s = db.db.session
    fsnap = [_row(r) for r in s.execute(sa.select(db.CronFolder)).scalars().all()]
    jsnap = [_row(r) for r in s.execute(sa.select(db.CronJob)).scalars().all()]
    try:
        ju = str(uuid4())
        db.cron_save_tree([], [{
            "uuid": ju, "name": "B", "enabled": True, "folderId": None,
            "cron": "30 3 * * *", "timezone": "UTC", "type": "backup",
            "command": "/tmp/x", "target": "", "message": "",
        }])
        out = db.cron_load_tree()
        assert [j["type"] for j in out["jobs"]] == ["backup"]
        assert out["jobs"][0]["command"] == "/tmp/x"
    finally:
        s.execute(sa.delete(db.CronJob))
        s.execute(sa.delete(db.CronFolder))
        for row in fsnap:
            s.add(db.CronFolder(**row))
        for row in jsnap:
            s.add(db.CronJob(**row))
        s.commit()


def test_seed_cron_defaults_idempotent(app_ctx):
    """init_db already seeded the System folder + backup job; calling again
    creates no duplicates and never overwrites operator edits (e.g. if the job
    has been enabled in the UI, seed must leave that alone)."""
    from db.cron import BACKUP_CRON_JOB_UUID, SYSTEM_CRON_FOLDER_UUID

    job_before = db.db.session.query(db.CronJob).filter_by(uuid=BACKUP_CRON_JOB_UUID).one()
    enabled_before = job_before.enabled

    db.seed_cron_defaults()  # second call
    folders = db.db.session.query(db.CronFolder).filter_by(uuid=SYSTEM_CRON_FOLDER_UUID).all()
    jobs = db.db.session.query(db.CronJob).filter_by(uuid=BACKUP_CRON_JOB_UUID).all()
    assert len(folders) == 1 and len(jobs) == 1            # no duplicates
    assert jobs[0].action_type == "backup"
    assert jobs[0].enabled is enabled_before               # operator state preserved


def _row(r):
    return {c.name: getattr(r, c.name) for c in r.__table__.columns if c.name != "id"}
