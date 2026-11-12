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


def test_a_jobs_page_follows_its_own_job_and_no_other(client):
    c, made = client
    job, _ = _job(_run(made))
    url = f"/assistant/housekeeping/api/revision?job={job.uuid}"
    assert f'data-revision-url="{url}"' in c.get(
        f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    before = c.get(url).get_json()["revision"]
    # Another job running and writing its reply so far moves the log, not this page.
    other, attempt = _job(_run(made), result=None)
    db.checkpoint_housekeeping_attempt(attempt.uuid, response_preview="more of a reply")
    assert c.get(url).get_json()["revision"] == before
    # Its own job running again does.
    db.start_housekeeping_attempt(job)
    assert c.get(url).get_json()["revision"] != before
    assert c.get("/assistant/housekeeping/api/revision?job=nope").status_code == 400


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


def test_a_rooms_page_shows_its_chunks_and_offers_the_rest(client):
    from agents.config import ASSISTANT_UUID, HOUSEKEEPING_UUID
    from agents.room_chunks import content_hash

    c, made = client
    human = db.get_human_user()
    room = db.create_chatroom(f"hkr-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    made["rooms"].append(room.uuid)
    for i in range(34):
        db.post_chat_message(room.uuid, human.uuid if i % 2 == 0 else ASSISTANT_UUID,
                             f"message number {i}")
    messages = db.counted_room_messages(room.uuid)
    chunk = db.store_room_chunk(
        room_uuid=room.uuid, chunk_size=10, overlap=2, seq=0,
        first_message_id=messages[0]["id"], last_message_id=messages[9]["id"],
        message_count=10, content_hash=content_hash(messages[:10]),
        summary="questions about numbers", topics=["numbers"], problems=[],
        parts=[{"first_message": 0, "last_message": 5, "requests": 3,
                "summary": "which number comes next", "topics": ["sequences"],
                "result": "the wrong number was given twice",
                "reactions": ["asked_again", "asked_again", "moved_on"], "calibration": "overconfident",
                "calibration_basis": "it answered, and the human kept asking",
                "signals": {"replies": 3, "runs": {"resolved": 2, "failed": 1},
                            "criteria_not_met": 1, "revisions": 4, "up": 0, "down": 1}},
               {"first_message": 6, "last_message": 9, "requests": 1,
                "summary": "how numbers are written", "topics": ["notation"],
                "audit": {"request": True, "result": False,
                          "reasoning": "the reply was about another number"}}],
        audit={"attempts": 2, "rewritten": [{
            "first_message": 0, "last_message": 5, "summary": "which number is odd",
            "result": "seven", "reasoning": "seven answers a later request"}]},
        model="model-a", prompt_version=9)
    try:
        body = c.get(f"/assistant/housekeeping/room/{room.uuid}").get_data(as_text=True)
        assert "Messages 0&ndash;9" in body and "questions about numbers" in body
        assert '<span class="hk-chip">numbers</span>' in body
        assert "m0&ndash;m5" in body and "which number comes next" in body
        assert "&times;3" in body and '<span class="hk-chip">notation</span>' in body
        assert "&rarr; the wrong number was given twice" in body
        assert 'hk-cal-overconfident' in body and "asked again \u00d72" in body
        assert "it answered, and the human kept asking" in body
        for label in ("2 resolved", "1 failed", "criteria not met ×1", "4 revisions", "▼ 1"):
            assert label in body, label
        assert "[m3 agent] message number 3" in body
        assert "messages changed since" not in body
        # 34 messages: chunks 1 and 2 are due (the third needs messages 30-31 after it).
        assert "Summarize the rest: 2 chunks" in body
        # The check: what it still doubts on a part, and what the first answer had.
        assert "the result may not match the messages: the reply was about another number" in body
        assert "The check found 1 part of the first answer wrong" in body
        assert "which number is odd &rarr; seven" in body
        assert "seven answers a later request" in body

        # An edit after the summary is shown, and the summary is left alone.
        db.session.query(db.ChatMessage).filter_by(id=messages[2]["id"]).update(
            {"text": "edited"})
        db.session.commit()
        assert "messages changed since" in c.get(
            f"/assistant/housekeeping/room/{room.uuid}").get_data(as_text=True)

        # Another series has no chunks, and gets no button: it is not current.
        other = c.get(f"/assistant/housekeeping/room/{room.uuid}?size=5&overlap=0").get_data(as_text=True)
        assert "No chunk of this room is summarized yet" in other
        assert "Summarize the rest" not in other

        assert c.post(f"/assistant/housekeeping/room/{room.uuid}/summarize").status_code == 302
        item = db.session.query(Inbox).filter(Inbox.agent_uuid == HOUSEKEEPING_UUID).one()
        assert '"room_chunks": true' in item.payload
        assert c.get(f"/assistant/housekeeping/room/{uuid4()}").status_code == 404
    finally:
        db.session.query(Inbox).filter(Inbox.agent_uuid == HOUSEKEEPING_UUID).delete()
        db.session.query(db.RoomChunk).filter_by(uuid=chunk.uuid).delete()
        db.session.commit()
