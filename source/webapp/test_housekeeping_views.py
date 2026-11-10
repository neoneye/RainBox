"""The Housekeeping page, a job's page, and the run page's After the turn group."""
from __future__ import annotations

from uuid import uuid4

import pytest

import db
from db import AssistantRun, HousekeepingAttempt, HousekeepingJob
from db.models import Inbox, LlmCall
from webapp import app as flask_app


@pytest.fixture
def client():
    with flask_app.app_context():
        made: dict = {"runs": [], "rooms": [], "calls": []}
        db.session.query(HousekeepingAttempt).delete()
        db.session.query(HousekeepingJob).delete()
        db.session.commit()
        try:
            yield flask_app.test_client(), made
        finally:
            db.session.rollback()
            db.session.query(HousekeepingAttempt).delete()
            db.session.query(HousekeepingJob).delete()
            for model, key in ((AssistantRun, "runs"), (db.Chatroom, "rooms"),
                               (LlmCall, "calls")):
                for uuid in made[key]:
                    db.session.query(model).filter(model.uuid == uuid).delete()
            db.session.commit()


def _run(made: dict) -> AssistantRun:
    human = db.get_human_user()
    room = db.create_chatroom(f"hkv-{uuid4().hex[:8]}", human.uuid, [])
    made["rooms"].append(room.uuid)
    run = db.start_assistant_run(journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    made["runs"].append(run.uuid)
    return run


def _job(run, *, result: str | None = "done", outcome: str | None = "done"):
    job, _ = db.ensure_housekeeping_job(
        db.RUN_SUMMARY_JOB, run_uuid=run.uuid, room_uuid=run.room_uuid)
    attempt = db.start_housekeeping_attempt(job)
    db.checkpoint_housekeeping_attempt(
        attempt.uuid, model="model-a", response_preview='{"trigger": "a que')
    if result is not None:
        db.finish_housekeeping_attempt(job, attempt, result)
        db.settle_housekeeping_job(job, "final", outcome=outcome)
    return job, attempt


def test_the_assistant_menu_has_a_housekeeping_item(client):
    c, _ = client
    body = c.get("/assistant/housekeeping").get_data(as_text=True)
    assert '<a href="/assistant/housekeeping" class="pp-active">Housekeeping</a>' in body
    assert "No housekeeping jobs" in body and "idle" in body


def test_the_log_lists_a_job_with_its_room_run_model_and_outcome(client):
    c, made = client
    run = _run(made)
    job, _ = _job(run)
    body = c.get("/assistant/housekeeping").get_data(as_text=True)
    assert f'data-href="/assistant/housekeeping/{job.uuid}"' in body
    assert "run summary" in body and "model-a" in body
    assert f'/assistant?id={run.uuid}' in body and f'/chat?id={run.room_uuid}' in body
    assert '<span class="hk-pill done">done</span>' in body
    # The fragment is the live part alone: no page chrome to swap in twice.
    fragment = c.get("/assistant/housekeeping?fragment=1").get_data(as_text=True)
    assert "hk-log" in fragment and "<title>" not in fragment


def test_filters_narrow_the_log_and_say_so_when_nothing_matches(client):
    c, made = client
    done_job, _ = _job(_run(made))
    failed_job, _ = _job(_run(made), result="failed", outcome="failed: RuntimeError: boom")
    failed = c.get("/assistant/housekeeping?outcome=failed").get_data(as_text=True)
    assert str(failed_job.uuid) in failed and str(done_job.uuid) not in failed
    done = c.get("/assistant/housekeeping?outcome=done").get_data(as_text=True)
    assert str(done_job.uuid) in done and str(failed_job.uuid) not in done
    none = c.get("/assistant/housekeeping?outcome=running").get_data(as_text=True)
    assert "No housekeeping jobs match this filter" in none
    one_room = c.get(f"/assistant/housekeeping?room={done_job.room_uuid}").get_data(as_text=True)
    assert str(done_job.uuid) in one_room and str(failed_job.uuid) not in one_room


def test_a_running_job_shows_at_the_top_and_its_reply_so_far_on_its_page(client):
    c, made = client
    job, _ = _job(_run(made), result=None)
    log = c.get("/assistant/housekeeping").get_data(as_text=True)
    assert f'running <a href="/assistant/housekeeping/{job.uuid}">run summary</a>' in log
    page = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    assert "reply so far" in page and "{&#34;trigger&#34;: &#34;a que" in page
    assert "running for" in page


def test_a_job_page_shows_each_attempt_with_its_prompt_and_reply(client):
    c, made = client
    run = _run(made)
    job, _ = db.ensure_housekeeping_job(
        db.RUN_SUMMARY_JOB, run_uuid=run.uuid, room_uuid=run.room_uuid)
    killed = db.start_housekeeping_attempt(job)
    db.finish_housekeeping_attempt(job, killed, "killed: turn")
    db.settle_housekeeping_job(job, "deferred", deferred_reason="turn")
    second = db.start_housekeeping_attempt(job)
    call = LlmCall(caller=db.SUMMARIZER_CALLER, run_uuid=run.uuid, model="model-a",
                   messages=[{"role": "system", "content": "You summarize a run."},
                             {"role": "user", "content": "Run status: finished"}],
                   response_text='{"outcome": "resolved"}')
    db.session.add(call)
    db.session.commit()
    made["calls"].append(call.uuid)
    db.finish_housekeeping_attempt(job, second, "done")
    db.settle_housekeeping_job(job, "final", outcome="done")

    body = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    assert "Attempt 1" in body and "killed: turn" in body
    assert "Attempt 2" in body and "You summarize a run." in body
    assert "Run status: finished" in body and "resolved" in body
    assert f"/activity/call/{call.uuid}" in body
    assert "the summary on its run" in body
    assert "Enqueue again" not in body
    assert c.get(f"/assistant/housekeeping/{uuid4()}").status_code == 404


def test_a_final_failure_can_be_enqueued_again(client):
    c, made = client
    job, _ = _job(_run(made), result="failed", outcome="failed: RuntimeError: boom")
    assert "Enqueue again" in c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    try:
        resp = c.post(f"/assistant/housekeeping/{job.uuid}/requeue")
        assert resp.status_code == 302
        db.session.refresh(job)
        assert (job.state, job.outcome) == ("waiting", None)
    finally:
        from agents.config import HOUSEKEEPING_UUID
        db.session.query(Inbox).filter(Inbox.agent_uuid == HOUSEKEEPING_UUID).delete()
        db.session.commit()


def test_revision_moves_when_a_job_changes(client):
    c, made = client
    before = c.get("/assistant/housekeeping/api/revision").get_json()["revision"]
    _job(_run(made))
    assert c.get("/assistant/housekeeping/api/revision").get_json()["revision"] != before


def test_the_run_page_lists_its_jobs_after_the_turn_and_moves_them_forward(client):
    c, made = client
    run = _run(made)
    job, _ = db.ensure_housekeeping_job(
        db.RUN_SUMMARY_JOB, run_uuid=run.uuid, room_uuid=run.room_uuid)
    body = c.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
    assert "After the turn" in body
    assert f'<a href="/assistant/housekeeping/{job.uuid}">run summary</a>' in body
    db.session.refresh(job)
    assert job.bumped_at is not None        # opening the page asked for it first
    other = _run(made)
    assert "After the turn" not in c.get(f"/assistant?id={other.uuid}").get_data(as_text=True)
