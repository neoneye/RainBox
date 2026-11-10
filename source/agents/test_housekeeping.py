"""Tests for the housekeeping role and its queue: db/housekeeping.py and
agents/housekeeping.py. No model is called; the summarizer's call is faked."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from agents import housekeeping as hk
from agents.assistant_run_summarizer import AssistantRunSummarizerAgent, RunSummary
from agents.config import ASSISTANT_UUID, HOUSEKEEPING_UUID
from db import AssistantRun, HousekeepingAttempt, HousekeepingJob
from db.models import Inbox, Journal


def _clear_jobs() -> None:
    db.session.query(HousekeepingAttempt).delete()
    db.session.query(HousekeepingJob).delete()
    db.session.commit()


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    made: dict = {"runs": [], "rooms": []}
    _clear_jobs()
    try:
        yield made
    finally:
        db.session.rollback()
        _clear_jobs()
        for run_uuid in made["runs"]:
            db.session.query(AssistantRun).filter(AssistantRun.uuid == run_uuid).delete()
        for room_uuid in made["rooms"]:
            db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
        db.session.query(Inbox).filter(Inbox.agent_uuid == HOUSEKEEPING_UUID).delete()
        db.session.commit()
        ctx.pop()


def _run(made: dict, *, finished: bool = True) -> AssistantRun:
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(f"hk-{uuid4().hex[:8]}", human.uuid, [])
    made["rooms"].append(room.uuid)
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    if finished:
        db.finish_run(run, "finished")
    made["runs"].append(run.uuid)
    return run


def _agent(monkeypatch, *, idle: bool = True) -> hk.HousekeepingAgent:
    agent = hk.HousekeepingAgent(HOUSEKEEPING_UUID, "housekeeping", lambda _m: None)
    agent.POLL_SECONDS = 0.01
    agent.PROGRESS_SECONDS = 0.0
    if idle:
        monkeypatch.setattr(db, "turn_activity", lambda **_kw: False)
        monkeypatch.setattr(agent, "_probe", lambda: None)
    monkeypatch.setattr(AssistantRunSummarizerAgent, "setup", lambda self: None)
    # The sweep has a test of its own. Here it would pick up whatever runs
    # other tests left in the shared database and run them as jobs.
    monkeypatch.setattr(db, "sweep_due_housekeeping_jobs", lambda: 0)
    return agent


def _fake_summary(monkeypatch, behaviour=None) -> list[str]:
    calls: list[str] = []

    def fake(self, user_prompt, validator=None, followup_turns=None):
        calls.append(user_prompt)
        if behaviour is not None:
            behaviour(self, len(calls))
        return RunSummary(trigger="a question", obstacles=[], outcome="resolved")

    monkeypatch.setattr(AssistantRunSummarizerAgent, "_structured_call", fake)
    return calls


# --- the queue ---------------------------------------------------------------

def test_the_same_work_is_one_job_however_often_it_is_asked_for(app_ctx):
    run = _run(app_ctx)
    first, created = db.ensure_housekeeping_job(db.RUN_SUMMARY_JOB, run_uuid=run.uuid)
    again, created_again = db.ensure_housekeeping_job(db.RUN_SUMMARY_JOB, run_uuid=run.uuid)
    assert created and not created_again
    assert first.uuid == again.uuid and first.tier == 2 and first.state == "waiting"


def test_next_job_is_tier_one_first_then_an_opened_run_then_the_oldest(app_ctx):
    old, new, urgent = _run(app_ctx), _run(app_ctx), _run(app_ctx)
    a, _ = db.ensure_housekeeping_job(db.RUN_SUMMARY_JOB, run_uuid=old.uuid)
    b, _ = db.ensure_housekeeping_job(db.RUN_SUMMARY_JOB, run_uuid=new.uuid)
    a.created_at = datetime.now(UTC) - timedelta(minutes=5)
    db.session.commit()
    assert db.next_housekeeping_job().uuid == a.uuid

    assert db.bump_run_housekeeping(new.uuid) == 1
    assert db.next_housekeeping_job().uuid == b.uuid
    assert db.bump_run_housekeeping(new.uuid) == 0      # already at the front

    c, _ = db.ensure_housekeeping_job("verdict_detection", run_uuid=urgent.uuid)
    c.tier = 1
    db.session.commit()
    assert db.next_housekeeping_job().uuid == c.uuid


def test_sweep_finds_recent_finished_runs_with_no_summary_and_no_job(app_ctx):
    due, summarized, queued, old, active = (
        _run(app_ctx), _run(app_ctx), _run(app_ctx), _run(app_ctx),
        _run(app_ctx, finished=False))
    db.set_run_summary(summarized, {"trigger": "t", "obstacles": [], "outcome": "resolved"})
    db.ensure_housekeeping_job(db.RUN_SUMMARY_JOB, run_uuid=queued.uuid)
    old.finished_at = datetime.now(UTC) - timedelta(days=3)
    db.session.commit()

    db.sweep_due_housekeeping_jobs()
    with_job = {j.run_uuid for j in db.session.query(HousekeepingJob).all()}
    assert due.uuid in with_job and queued.uuid in with_job
    assert not {summarized.uuid, old.uuid, active.uuid} & with_job
    db.sweep_due_housekeeping_jobs()                    # nothing is found twice
    assert len(db.list_run_housekeeping_jobs(due.uuid)) == 1


def test_turn_activity_is_other_agents_work_and_never_housekeepings_own(app_ctx):
    # Uncommitted on purpose: the session sees its own changes, and the
    # fixture rolls them back, so the shared database is left as it was.
    db.session.query(Inbox).delete()
    db.session.query(AssistantRun).filter(
        AssistantRun.status.in_(("running", "stopping"))).delete()
    db.session.query(Journal).filter(Journal.state == "processing").delete()
    db.session.flush()
    assert db.turn_activity() is False

    db.session.add(Inbox(agent_uuid=HOUSEKEEPING_UUID, payload="{}"))
    db.session.flush()
    assert db.turn_activity() is False

    item = Inbox(agent_uuid=ASSISTANT_UUID, payload="{}")
    db.session.add(item)
    db.session.flush()
    assert db.turn_activity() is True
    db.session.delete(item)

    room_uuid = uuid4()
    stale = AssistantRun(room_uuid=room_uuid, agent_uuid=ASSISTANT_UUID, status="running",
                         started_at=datetime.now(UTC) - timedelta(hours=2))
    db.session.add(stale)
    db.session.flush()
    assert db.turn_activity() is False                  # a row a dead worker left
    db.session.add(AssistantRun(room_uuid=room_uuid, agent_uuid=ASSISTANT_UUID,
                                status="running"))
    db.session.flush()
    assert db.turn_activity() is True


def test_a_job_left_running_by_a_dead_worker_goes_back_to_waiting(app_ctx):
    run = _run(app_ctx)
    job, _ = db.ensure_housekeeping_job(db.RUN_SUMMARY_JOB, run_uuid=run.uuid)
    attempt = db.start_housekeeping_attempt(job)
    assert db.recover_running_housekeeping_jobs("worker gone") == 1
    db.session.refresh(job)
    db.session.refresh(attempt)
    assert job.state == "waiting"
    assert (attempt.result, attempt.error) == ("failed", "worker gone")
    assert attempt.ended_at is not None


# --- the worker --------------------------------------------------------------

def test_a_finished_run_gets_one_summary_job_that_runs_to_done(app_ctx, monkeypatch):
    run = _run(app_ctx)
    calls = _fake_summary(monkeypatch)
    agent = _agent(monkeypatch)
    payload = {"run_uuid": str(run.uuid), "room_uuid": str(run.room_uuid)}

    result = agent.handle(uuid4(), payload)
    assert result == {"ok": True, "jobs": {"done": 1}}
    (job,) = db.list_run_housekeeping_jobs(run.uuid)
    assert (job.job_type, job.state, job.outcome) == ("run_summary", "final", "done")
    assert job.room_uuid == run.room_uuid and job.finished_at is not None
    (attempt,) = db.list_housekeeping_attempts(job.uuid)
    assert attempt.result == "done" and attempt.model_ms is not None
    db.session.refresh(run)
    assert run.summary["outcome"] == "resolved"

    # The same item again runs nothing: the job already has its result.
    assert agent.handle(uuid4(), payload) == {"ok": True, "jobs": {}}
    assert len(calls) == 1
    # Asked for by the operator, it runs again as a new attempt of the same job.
    assert agent.handle(uuid4(), {**payload, "force": True})["jobs"] == {"done": 1}
    assert len(calls) == 2
    assert len(db.list_housekeeping_attempts(job.uuid)) == 2


def test_a_failed_job_is_retried_once_and_then_final(app_ctx, monkeypatch):
    run = _run(app_ctx)

    def boom(_self, _n):
        raise RuntimeError("model unavailable")

    calls = _fake_summary(monkeypatch, boom)
    agent = _agent(monkeypatch)
    result = agent.handle(uuid4(), {"run_uuid": str(run.uuid)})
    assert result["jobs"] == {"failed": 2}
    assert len(calls) == 2
    (job,) = db.list_run_housekeeping_jobs(run.uuid)
    assert job.state == "final"
    assert job.outcome == "failed: RuntimeError: model unavailable"
    assert [a.result for a in db.list_housekeeping_attempts(job.uuid)] == ["failed", "failed"]
    assert db.run_housekeeping_flags([run.uuid]) == {run.uuid: "failed"}

    # Enqueued again by the operator, it gets its retry back.
    db.requeue_housekeeping_job(job)
    assert (job.state, job.outcome, job.retries) == ("waiting", None, 0)


def test_a_run_that_is_gone_is_skipped_not_failed(app_ctx, monkeypatch):
    _fake_summary(monkeypatch)
    agent = _agent(monkeypatch)
    missing = uuid4()
    assert agent.handle(uuid4(), {"run_uuid": str(missing)})["jobs"] == {"done": 1}
    (job,) = db.list_run_housekeeping_jobs(missing)
    assert (job.state, job.outcome) == ("final", "skipped: run not found")


def test_a_new_turn_kills_the_call_and_the_job_runs_again_when_idle(app_ctx, monkeypatch):
    run = _run(app_ctx)
    turn = {"active": False}

    def slow_first_time(summarizer, n):
        if n == 1:
            turn["active"] = True               # a message arrives mid-call
            with summarizer.call_guard():
                time.sleep(5)                   # the blocked read
            raise AssertionError("the call was not interrupted")

    calls = _fake_summary(monkeypatch, slow_first_time)
    agent = _agent(monkeypatch, idle=False)
    monkeypatch.setattr(agent, "_probe", lambda: (lambda: turn["active"]))
    waits: list[int] = []

    def activity(**_kw):
        # The turn ends after the worker has asked a few times.
        if turn["active"]:
            waits.append(1)
            if len(waits) >= 3:
                turn["active"] = False
        return turn["active"]

    monkeypatch.setattr(db, "turn_activity", activity)
    started = time.monotonic()
    result = agent.handle(uuid4(), {"run_uuid": str(run.uuid)})
    assert time.monotonic() - started < 4       # killed, not slept through
    assert result["jobs"] == {"killed": 1, "done": 1}
    assert len(calls) == 2
    (job,) = db.list_run_housekeeping_jobs(run.uuid)
    assert (job.state, job.outcome, job.retries) == ("final", "done", 0)
    assert [a.result for a in db.list_housekeeping_attempts(job.uuid)] == \
        ["killed: turn", "done"]


def test_a_turn_seen_during_a_shielded_write_raises_when_the_shield_ends():
    seen = {"turn": False}
    with hk.TurnWatch(lambda: seen["turn"], poll_seconds=0.01) as watch:
        with pytest.raises(hk.Preempted):
            with watch.interruptible():
                with watch.shielded():
                    seen["turn"] = True
                    time.sleep(0.2)             # the watcher sees it; no raise here
                    reached = True
        assert reached
        with pytest.raises(hk.Preempted):       # and the next window refuses to open
            with watch.interruptible():
                pass


def test_progress_and_model_are_written_to_the_running_attempt(app_ctx, monkeypatch):
    run = _run(app_ctx)

    def stream(summarizer, _n):
        summarizer._model_attempt_started(uuid4(), "model-a", 60.0)
        summarizer._model_attempt_progress(uuid4(), "model-a", None, '{"trigger": "a')

    _fake_summary(monkeypatch, stream)
    agent = _agent(monkeypatch)
    agent.handle(uuid4(), {"run_uuid": str(run.uuid)})
    (job,) = db.list_run_housekeeping_jobs(run.uuid)
    (attempt,) = db.list_housekeeping_attempts(job.uuid)
    assert attempt.model == "model-a"
    assert attempt.response_preview == '{"trigger": "a'
