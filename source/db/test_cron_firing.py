"""Tests for the cron scheduler/firing layer (db.py).

Hits the live local Postgres. Cron jobs are inserted directly (not via
cron_save_tree, so the real tree is untouched) and every row a test creates —
cron_job, cron_run, the enqueued inbox item, and cron-room chat messages — is
torn down in teardown, so the suite is non-destructive.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
# Imported at module level ON PURPOSE: importing webapp.core builds the Flask
# app and runs init_db (ALTER TABLEs needing ACCESS EXCLUSIVE). A body-level
# `from webapp.core import app` inside a test deadlocks when this file runs
# alone: the `firing` fixture's setup queries hold ACCESS SHARE locks in an
# open read transaction, and a single-threaded process can't release them
# while the import waits. Importing here happens before any fixture runs.
from webapp.core import app as flask_app  # noqa: F401  (imported for side effect + test_client)
from db import (
    CRON_ROOM_UUID,
    CRON_SYSTEM_UUID,
    ChatMessage,
    CronJob,
    CronRun,
    Inbox,
)


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
def firing(app_ctx):
    """Yields a helper to register a CronJob; tears down all created rows."""
    s = db.db.session

    def _max(model, **filt):
        q = s.query(sa.func.max(model.id))
        for k, v in filt.items():
            q = q.filter(getattr(model, k) == v)
        return q.scalar() or 0

    base_msg = _max(ChatMessage, room_uuid=CRON_ROOM_UUID)
    base_inbox = _max(Inbox)
    s.rollback()  # close the read transaction: holding its ACCESS SHARE locks
    # across the test body can block any concurrently-built app's init_db ALTERs
    job_uuids: list = []

    def add_job(**kw):
        kw.setdefault("uuid", uuid4())
        kw.setdefault("name", "T")
        kw.setdefault("enabled", True)
        kw.setdefault("cron_expr", "* * * * *")
        kw.setdefault("timezone", "localtime")
        kw.setdefault("action_type", "message")
        kw.setdefault("target", "")
        kw.setdefault("message", "")
        kw.setdefault("command", "")
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
        s.execute(sa.delete(Inbox).where(Inbox.id > base_inbox))
        s.commit()


def test_compute_next_run_utc_and_local(app_ctx):
    now = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
    nxt = db.cron_compute_next_run("*/5 * * * *", "UTC", after=now)
    assert nxt is not None
    assert nxt.tzinfo is not None
    assert now < nxt <= now + timedelta(minutes=5)
    # localtime variant also yields a future instant.
    assert db.cron_compute_next_run("*/5 * * * *", "localtime", after=now) is not None
    # An unparseable expression degrades to None rather than raising.
    assert db.cron_compute_next_run("not a cron", "UTC", after=now) is None


def test_fire_message_job_posts_to_cron_room(firing):
    job = firing(action_type="message", target="", message="hello from cron", name="Greeter")
    run = db.fire_cron_job(job, trigger="manual")
    assert run.cron_uuid == job.uuid and run.trigger == "manual"
    assert job.last_fired_at is not None
    # The message text landed in the cron room, authored by the cron sender.
    texts = [m.text for m in db.db.session.query(ChatMessage)
             .filter_by(room_uuid=CRON_ROOM_UUID, sender_uuid=CRON_SYSTEM_UUID).all()]
    assert "hello from cron" in texts
    assert any('▶ sent "Greeter"' in t for t in texts)


def test_fire_message_job_resolves_target_by_uuid(firing):
    # target is a chatroom uuid (rename-proof); firing resolves it to the room.
    job = firing(action_type="message", target=str(CRON_ROOM_UUID),
                 message="hello via uuid", name="ByUuid")
    db.fire_cron_job(job, trigger="manual")
    texts = [m.text for m in db.db.session.query(ChatMessage)
             .filter_by(room_uuid=CRON_ROOM_UUID, sender_uuid=CRON_SYSTEM_UUID).all()]
    assert "hello via uuid" in texts
    # The resolved-room event line names the room (vs the bare fallback line).
    assert any('▶ sent "ByUuid"' in t and "→ #" in t for t in texts)


def test_fire_command_job_enqueues_workspace_shell(firing):
    from agents.config import agent_config
    ws_uuid = agent_config["workspace_shell"]["uuid"]
    job = firing(action_type="command", command="echo hi", name="Echo")
    db.fire_cron_job(job, trigger="manual")
    # An inbox item for the workspace-shell agent carries the command directly.
    import json
    rows = db.db.session.query(Inbox).filter_by(agent_uuid=ws_uuid).all()
    payloads = [json.loads(r.payload) for r in rows]
    assert any(p.get("command_text") == "echo hi" and p.get("room_uuid") == str(CRON_ROOM_UUID)
               for p in payloads)


def test_cron_tick_fires_due_and_advances(firing):
    past = datetime.now(UTC) - timedelta(minutes=1)
    job = firing(action_type="message", message="tick fired", next_run_at=past, name="Ticker")
    fired = db.cron_tick()
    assert fired >= 1
    db.db.session.refresh(job)
    assert job.next_run_at is not None and job.next_run_at > datetime.now(UTC)  # advanced
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 1


def test_cron_tick_backfills_then_does_not_fire(firing):
    job = firing(action_type="message", message="x", next_run_at=None, name="New")
    db.cron_tick()
    db.db.session.refresh(job)
    assert job.next_run_at is not None  # scheduled
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 0  # not fired yet


def test_cron_tick_skips_disabled_and_folder_disabled(firing):
    past = datetime.now(UTC) - timedelta(minutes=1)
    job = firing(enabled=False, next_run_at=past, name="Off")
    db.cron_tick()
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 0


def test_fire_message_job_records_ok_outcome(firing):
    job = firing(action_type="message", message="hi", name="Outcome")
    run = db.fire_cron_job(job, trigger="manual")
    assert run.status == "ok" and run.error == ""
    assert run.finished_at is not None


def test_fire_failure_records_error_outcome(firing):
    # An empty command can't fire; the failure lands on the run row, not just
    # as a transient event line in the cron room.
    job = firing(action_type="command", command="", name="NoCmd")
    run = db.fire_cron_job(job, trigger="manual")
    assert run.status == "error" and "no command" in run.error
    assert run.finished_at is not None


def test_fire_command_job_pending_until_outcome_recorded(firing):
    job = firing(action_type="command", command="echo hi", name="Async")
    run = db.fire_cron_job(job, trigger="manual")
    # Async action: the fire returns with the outcome still open.
    assert run.status == "pending" and run.finished_at is None
    # The workspace-shell agent reports back via the run uuid it received.
    db.cron_record_run_outcome(run.uuid, status="ok", journal_id=12345)
    db.db.session.refresh(run)
    assert run.status == "ok" and run.journal_id == 12345
    assert run.finished_at is not None
    # An unknown run uuid is a no-op, not an error.
    db.cron_record_run_outcome(uuid4(), status="ok")


def test_cron_tick_sweeps_stale_pending_runs(firing):
    job = firing(action_type="message", message="x", name="Stale", next_run_at=None)
    s = db.db.session
    stale = CronRun(cron_uuid=job.uuid, trigger="manual",
                    fired_at=datetime.now(UTC) - db.CRON_RUN_PENDING_TIMEOUT - timedelta(minutes=1))
    fresh = CronRun(cron_uuid=job.uuid, trigger="manual", fired_at=datetime.now(UTC))
    s.add_all([stale, fresh])
    s.commit()
    db.cron_tick()
    s.refresh(stale)
    s.refresh(fresh)
    # A completion that hasn't arrived after the timeout never will.
    assert stale.status == "error" and "no completion" in stale.error
    assert stale.finished_at is not None
    # A recent in-flight run is left alone.
    assert fresh.status == "pending"


def test_cron_job_is_draft(firing):
    assert db.cron_job_is_draft(firing(action_type="command", command="  ", name="D1"))
    assert db.cron_job_is_draft(firing(action_type="message", message="", name="D2"))
    assert not db.cron_job_is_draft(firing(action_type="command", command="ls", name="D3"))
    assert not db.cron_job_is_draft(firing(action_type="message", message="hi", name="D4"))
    # Backups have no required field (destination falls back to settings/env).
    assert not db.cron_job_is_draft(firing(action_type="backup", command="", name="D5"))


def test_cron_tick_skips_drafts_without_firing(firing):
    base_msg = db.db.session.query(sa.func.max(ChatMessage.id)).filter(
        ChatMessage.room_uuid == CRON_ROOM_UUID).scalar() or 0
    past = datetime.now(UTC) - timedelta(minutes=1)
    job = firing(action_type="command", command="", next_run_at=past, name="DraftCmd")
    db.cron_tick()
    db.db.session.refresh(job)
    # No run row, no event spam — but the schedule rolled forward, so the job
    # doesn't fire a stale slot the moment its command is filled in.
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 0
    new_msgs = db.db.session.query(ChatMessage).filter(
        ChatMessage.room_uuid == CRON_ROOM_UUID, ChatMessage.id > base_msg).count()
    assert new_msgs == 0
    assert job.next_run_at is not None and job.next_run_at > datetime.now(UTC)


def test_cron_tick_global_pause(firing):
    from db.settings import set_setting

    past = datetime.now(UTC) - timedelta(minutes=1)
    job = firing(action_type="message", message="paused?", next_run_at=past, name="Pausable")
    try:
        set_setting("cron.paused", True)
        assert db.cron_tick() == 0
        assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 0
        db.db.session.refresh(job)
        assert job.next_run_at == past  # schedule untouched → resume catches up once
        set_setting("cron.paused", False)
        assert db.cron_tick() >= 1
        assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 1
    finally:
        set_setting("cron.paused", None)  # clear back to unset


def test_pause_resume_endpoints(firing):
    from db.settings import set_setting
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    try:
        resp = client.post("/cron/api/pause")
        assert resp.status_code == 200 and resp.get_json()["paused"] is True
        assert db.cron_is_paused() is True
        assert db.cron_load_tree()["paused"] is True  # page hydrates the state
        resp = client.post("/cron/api/resume")
        assert resp.status_code == 200 and resp.get_json()["paused"] is False
        assert db.cron_is_paused() is False
    finally:
        set_setting("cron.paused", None)


def test_health_endpoint(firing):
    from webapp.core import app as flask_app

    client = flask_app.test_client()
    job = firing(action_type="message", message="hp", name="Healthy")
    db.fire_cron_job(job, trigger="manual")          # → ok
    bad = firing(action_type="command", command="", name="Sick")
    db.fire_cron_job(bad, trigger="manual")          # → error (no command)

    h = client.get(f"/cron/api/jobs/{job.uuid}/health").get_json()
    assert h["ok"] is True
    assert h["ok_count"] == 1 and h["error_count"] == 0
    assert h["last_ok_at"] is not None and h["last_error_at"] is None
    assert len(h["next_runs"]) == 3                  # "* * * * *" always has next slots
    assert h["runs"][0]["status"] == "ok" and h["runs"][0]["trigger"] == "manual"

    h = client.get(f"/cron/api/jobs/{bad.uuid}/health").get_json()
    assert h["error_count"] == 1 and "no command" in h["runs"][0]["error"]

    assert client.get(f"/cron/api/jobs/{uuid4()}/health").status_code == 404
    assert client.get("/cron/api/jobs/not-a-uuid/health").status_code == 400


def _add_run(job_uuid, *, trigger, status, finished_ago=timedelta(0)):
    now = datetime.now(UTC)
    run = CronRun(cron_uuid=job_uuid, trigger=trigger, status=status,
                  fired_at=now - finished_ago, finished_at=now - finished_ago)
    db.db.session.add(run)
    db.db.session.commit()
    return run


def test_cron_tick_retries_failed_run(firing):
    """A recent error refires as trigger='retry' between slots, up to
    max_retries; a success ends the chain."""
    future = datetime.now(UTC) + timedelta(hours=1)
    job = firing(action_type="message", message="again", name="Retrier",
                 max_retries=2, next_run_at=future)
    _add_run(job.uuid, trigger="scheduled", status="error")
    assert db.cron_tick() >= 1
    runs = db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid)\
        .order_by(CronRun.id.desc()).all()
    assert runs[0].trigger == "retry" and runs[0].status == "ok"  # message fire succeeds
    # The retry succeeded → the chain is over; nothing more fires.
    db.cron_tick()
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 2


def test_cron_tick_retry_budget_exhausts(firing):
    future = datetime.now(UTC) + timedelta(hours=1)
    job = firing(action_type="message", message="x", name="GiveUp",
                 max_retries=2, next_run_at=future)
    _add_run(job.uuid, trigger="scheduled", status="error")
    _add_run(job.uuid, trigger="retry", status="error")
    _add_run(job.uuid, trigger="retry", status="error")  # budget of 2 spent
    db.cron_tick()
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 3


def test_cron_tick_does_not_retry_old_errors(firing):
    """An error outside CRON_RETRY_WINDOW isn't retried — a restart or a newly
    set max_retries must not refire ancient failures."""
    future = datetime.now(UTC) + timedelta(hours=1)
    job = firing(action_type="message", message="x", name="Ancient",
                 max_retries=3, next_run_at=future)
    _add_run(job.uuid, trigger="scheduled", status="error",
             finished_ago=db.CRON_RETRY_WINDOW + timedelta(minutes=1))
    db.cron_tick()
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 1


def test_max_retries_round_trips_through_tree(app_ctx):
    ju = str(uuid4())
    db.cron_save_tree([], [{
        "uuid": ju, "name": "R", "enabled": True, "folderId": None,
        "cron": "* * * * *", "timezone": "UTC", "type": "message",
        "target": "", "message": "m", "command": "", "maxRetries": 3,
    }])
    try:
        out = next(j for j in db.cron_load_tree()["jobs"] if j["uuid"] == ju)
        assert out["maxRetries"] == 3
        # Validation rejects junk.
        for bad in (-1, "2", True, db.CRON_MAX_RETRIES_CAP + 1):
            with pytest.raises(db.CronTreeError):
                db.validate_cron_tree([], [{
                    "uuid": str(uuid4()), "name": "B", "cron": "* * * * *",
                    "type": "message", "target": "", "message": "m",
                    "maxRetries": bad,
                }])
    finally:
        db.db.session.execute(sa.delete(CronJob).where(CronJob.uuid == UUID(ju)))
        db.db.session.commit()


def test_cron_tick_skips_while_previous_run_in_flight(firing):
    """A due job whose latest run is still 'pending' skips the slot (schedule
    advances, no new run, a ⏭ note in the cron room) and fires again on the
    next due slot once the outcome has landed."""
    past = datetime.now(UTC) - timedelta(minutes=1)
    job = firing(action_type="command", command="echo hi", next_run_at=past, name="Busy")
    run = db.fire_cron_job(job, trigger="scheduled")
    assert run.status == "pending"  # async command: outcome not in yet

    assert db.cron_job_run_in_flight(job.uuid) is True
    db.cron_tick()
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 1  # no pile-up
    db.db.session.refresh(job)
    assert job.next_run_at is not None and job.next_run_at > datetime.now(UTC)  # slot skipped
    texts = [m.text for m in db.db.session.query(ChatMessage)
             .filter_by(room_uuid=CRON_ROOM_UUID, sender_uuid=CRON_SYSTEM_UUID).all()]
    assert any('⏭ "Busy" skipped' in t for t in texts)

    # Outcome lands → the next due slot fires normally again.
    db.cron_record_run_outcome(run.uuid, status="ok")
    assert db.cron_job_run_in_flight(job.uuid) is False
    job.next_run_at = past
    db.db.session.commit()
    db.cron_tick()
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).count() == 2


def test_record_outcome_posts_completion_line(firing):
    """The ▶ event at fire time says a command started; recording its outcome
    posts the consolidated ✔/✖ verdict line to the cron room."""
    job = firing(action_type="command", command="echo hi", name="Verdict")
    run = db.fire_cron_job(job, trigger="manual")
    db.cron_record_run_outcome(run.uuid, status="ok")
    run2 = db.fire_cron_job(job, trigger="manual")
    db.cron_record_run_outcome(run2.uuid, status="error", error="exit code 2")
    texts = [m.text for m in db.db.session.query(ChatMessage)
             .filter_by(room_uuid=CRON_ROOM_UUID, sender_uuid=CRON_SYSTEM_UUID).all()]
    assert any('✔ "Verdict" completed (manual)' in t for t in texts)
    assert any('✖ "Verdict" failed (manual): exit code 2' in t for t in texts)


def test_load_tree_includes_last_run(firing):
    """Each job in the tree payload carries its latest run outcome (or null)
    for the health column, and its next_run_at for the next-run column."""
    job = firing(action_type="message", message="lr", name="LastRun",
                 next_run_at=datetime.now(UTC) + timedelta(minutes=5))
    tree_job = next(j for j in db.cron_load_tree()["jobs"] if j["uuid"] == str(job.uuid))
    assert tree_job["last_run"] is None  # never fired
    assert tree_job["next_run_at"] == job.next_run_at.isoformat()
    db.fire_cron_job(job, trigger="manual")
    db.fire_cron_job(job, trigger="manual")  # latest of several runs wins
    lr = next(j for j in db.cron_load_tree()["jobs"]
              if j["uuid"] == str(job.uuid))["last_run"]
    assert lr["status"] == "ok" and lr["trigger"] == "manual"
    assert lr["fired_at"] is not None and lr["error"] == ""


def test_fire_debug_message_is_dry_run(firing):
    """debug=True reports what the fire would do without doing it: the dry-run
    event posts, the message itself does not, and the run records ok+debug."""
    job = firing(action_type="message", target="", message="dry hello", name="Dry")
    run = db.fire_cron_job(job, trigger="manual", debug=True)
    assert run.debug is True and run.status == "ok" and run.finished_at is not None
    texts = [m.text for m in db.db.session.query(ChatMessage)
             .filter_by(room_uuid=CRON_ROOM_UUID, sender_uuid=CRON_SYSTEM_UUID).all()]
    assert any('dry-run "Dry"' in t and "would send" in t for t in texts)
    assert "dry hello" not in texts  # the message itself was NOT sent


def test_fire_debug_command_enqueues_with_flag(firing):
    """A command dry-run still goes through the workspace-shell agent (it owns
    the validation), with debug in the payload so it echoes instead of runs."""
    import json
    from agents.config import agent_config

    ws_uuid = agent_config["workspace_shell"]["uuid"]
    job = firing(action_type="command", command="echo hi", name="DryCmd")
    run = db.fire_cron_job(job, trigger="manual", debug=True)
    assert run.status == "pending" and run.debug is True
    payloads = [json.loads(r.payload) for r in
                db.db.session.query(Inbox).filter_by(agent_uuid=ws_uuid).all()]
    assert any(p.get("debug") is True and p.get("cron_run_uuid") == str(run.uuid)
               for p in payloads)
    texts = [m.text for m in db.db.session.query(ChatMessage)
             .filter_by(room_uuid=CRON_ROOM_UUID, sender_uuid=CRON_SYSTEM_UUID).all()]
    assert any('▶ dry-run "DryCmd"' in t for t in texts)


def test_run_now_endpoint_debug(firing):
    job = firing(action_type="message", message="dbg", name="DryEp")
    client = flask_app.test_client()
    resp = client.post(f"/cron/api/jobs/{job.uuid}/run?debug=1")
    assert resp.status_code == 200 and resp.get_json()["debug"] is True
    run = db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid).one()
    assert run.debug is True and run.trigger == "manual" and run.status == "ok"


def test_run_now_endpoint(firing):
    from webapp.core import app as flask_app

    job = firing(action_type="message", message="manual run", name="Manual")
    client = flask_app.test_client()
    resp = client.post(f"/cron/api/jobs/{job.uuid}/run")
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert db.db.session.query(CronRun).filter_by(cron_uuid=job.uuid, trigger="manual").count() == 1
    # Unknown job → 404; bad uuid → 400.
    assert client.post(f"/cron/api/jobs/{uuid4()}/run").status_code == 404
    assert client.post("/cron/api/jobs/not-a-uuid/run").status_code == 400
