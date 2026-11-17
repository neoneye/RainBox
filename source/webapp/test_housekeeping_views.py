"""The Housekeeping page, a job's page, and the run page's After the turn group."""
from __future__ import annotations

import html
import re
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
    assert '<a href="/assistant/housekeeping" class="pp-active" title="Background work after a turn: summaries of runs and chatrooms">Housekeeping</a>' in body
    assert "No housekeeping jobs" in body and "idle" in body
    # The bar counts the jobs that have not run yet, as one number.
    assert '<span class="k">Pending</span><span class="v">0</span>' in body


def test_the_log_lists_a_job_with_its_room_run_and_outcome(client):
    c, made = client
    run = _run(made)
    job, _ = _job(run)
    body = c.get("/assistant/housekeeping").get_data(as_text=True)
    assert f'data-href="/assistant/housekeeping/{job.uuid}"' in body
    assert "assistant summary" in body
    # The log names no model: a job's models are on its own page.
    assert "model-a" not in body and "<th>Model</th>" not in body
    assert '<th class="hk-right">Time</th>' in body
    # The row is the one thing to click. The room, its chunks and the run are
    # linked from the job's page.
    assert f'/assistant?id={run.uuid}' not in body and f'/chat?id={run.room_uuid}' not in body
    page = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    assert f'/assistant?id={run.uuid}' in page and f'/chat?id={run.room_uuid}' in page
    assert f'/assistant/housekeeping/room/{run.room_uuid}' in page
    # A job that is done carries no pill, here or on its own page. A pill
    # is for what is still going or went wrong.
    assert 'class="hk-pill done"' not in body
    assert 'class="hk-pill done"' not in page
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
    assert f'running <a href="/assistant/housekeeping/{job.uuid}">assistant summary</a>' in log
    page = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    assert "response so far" in page and "{&#34;trigger&#34;: &#34;a que" in page
    assert 'data-final="1"' not in page
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
                   response_text='{"outcome": "resolved"}',
                   prompt_tokens=1193, completion_tokens=37, prefill_ms=848,
                   decode_ms=700, total_ms=1600, cached_tokens_reported=0)
    db.session.add(call)
    db.session.commit()
    made["calls"].append(call.uuid)
    db.finish_housekeeping_attempt(job, second, "done")
    db.settle_housekeeping_job(job, "final", outcome="done")

    body = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
    assert 'data-label="start"' in body and "killed: turn" in body
    assert 'data-label="attempt 2"' in body and "You summarize a run." in body
    # The timeline is in the order things happened: the first attempt, where
    # it was killed, the second attempt, and its model call. The page opens
    # on the call.
    rows = re.findall(r'<button type="button" class="wf-row( on)?" data-key="(.)[^"]*"', body)
    assert rows == [("", "a"), ("", "e"), ("", "a"), (" on", "c")]
    assert '<span class="wf-name kind-end">killed</span>' in body
    # A start holds what was known when it began: not how the attempt ended.
    start = body[body.index(f'data-key="a{killed.uuid}">'):]
    start = start[:start.index('<div class="ev-pane')]
    assert "killed: turn" not in start and "model call" not in start
    assert f'<div class="ev-pane on" data-key="c{call.uuid}">' in body
    # A final job's page says so, and its script stops asking for changes.
    assert '<div class="hk-dash" data-final="1">' in body
    # The band at the top holds the job as a whole: how it stands, its
    # calls, its time and its attempts.
    dash = body[body.index('<div class="hk-dash"'):body.index("<h3>Timeline</h3>")]
    assert '<div class="dval-big dstatus-done">done</div>' in dash
    assert re.search(r'LLM calls</div>\s*<div class="dval-big">1</div>', dash)
    assert re.search(r'Decision calls</div>\s*<div class="dval-big">0</div>', dash)
    assert "model 1.6s" in dash and "total " in dash and '>decision ' not in dash
    assert re.search(r'Attempts</div>\s*<div class="dval">2', dash)
    # Finished stands under Created, in one cell, so the two are compared.
    assert re.search(r'Created</div>\s*<div class="dval">.*?</div>\s*(\{#.*?#\}\s*)?'
                     r'<div class="dlabel">Finished</div>', dash, re.S)
    assert "if (box.querySelector('[data-final]')) { clearInterval(timer); return; }" in body
    # The page takes the window's width, as a run's page does.
    assert '<div class="hk-wrap hk-full">' in body
    assert body.count('class="ev-pane') == 4
    assert "Run status: finished" in body and "resolved" in body
    assert f"/activity/call/{call.uuid}" in body
    # The call carries its figures on a line under its header, as a call's
    # meta line reads on /assistant.
    header = body[body.index(f'data-key="c{call.uuid}">'):]
    # Each prompt is a collapsed block with its character count, and every
    # block can be copied. The reply is JSON, so it has the raw | pretty
    # switch.
    assert "system prompt (20 chars)" in body and "user prompt (20 chars)" in body
    card = header[:header.index("</pre></div>")]
    assert card.count('class="ev-copy"') == 3
    assert card.count('data-view="pretty"') == 1
    assert f'data-k="a{second.uuid}-m1"' in body
    kpis = body[body.index('<div class="ev-kpis">'):]
    kpis = kpis[:kpis.index("</div>")]
    for text in ("in 1193", "out 37", "cached 0", "prefill 0.8s", "decode 0.7s",
                 "769 tok/s", "took 1.6s"):
        assert text in kpis
    # The heading says on hover what the job is for. No line repeats where
    # its result went.
    assert '<div class="dlabel" title="Summarizes one run of the personal assistant' in body
    assert "<dt>Wrote</dt>" not in body
    assert "Enqueue again" not in body
    # The run's page links the summarizer's step to this job.
    run_page = c.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
    assert f'<a href="/assistant/housekeeping/{job.uuid}">housekeeping ↗</a>' in run_page
    kpis = run_page[run_page.index(f'<a href="/assistant/housekeeping/{job.uuid}">housekeeping ↗</a>'):]
    assert "housekeeping ↗" in kpis and kpis.index("housekeeping ↗") < kpis.index("model-a")
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
    assert f'<a href="/assistant/housekeeping/{job.uuid}">assistant summary</a>' in body
    db.session.refresh(job)
    assert job.bumped_at is not None        # opening the page asked for it first
    other = _run(made)
    assert "After the turn" not in c.get(f"/assistant?id={other.uuid}").get_data(as_text=True)
    # A job that is done has no line: its call is a step on the timeline.
    _job(other)
    assert "After the turn" not in c.get(f"/assistant?id={other.uuid}").get_data(as_text=True)


def test_a_chunk_jobs_page_shows_what_the_decision_model_was_asked_and_answered(client):
    from agents import room_chunks
    from agents.config import ASSISTANT_UUID
    from llm.systemone import state_digest
    from llm.systemone_questions import follow_up_state

    c, made = client
    human = db.get_human_user()
    room = db.create_chatroom(f"hkd-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    made["rooms"].append(room.uuid)
    for i, text in enumerate(["how tall is the lighthouse", "It is not in the notes.",
                              "look up the height of the lighthouse", "Still nothing.",
                              "which boards exist", "Two boards."]):
        db.post_chat_message(room.uuid, human.uuid if i % 2 == 0 else ASSISTANT_UUID, text)
    messages = db.counted_room_messages(room.uuid)
    job, _ = db.ensure_housekeeping_job(
        db.ROOM_CHUNK_JOB, room_uuid=room.uuid,
        subject=db.chunk_subject(4, 2, room_chunks.PROMPT_VERSION, 0,
                                 messages[0]["id"], messages[3]["id"]))
    first, second = room_chunks.follow_ups(messages[:4], messages[4:6], 0)
    # The requests are made inside an attempt, at its start.
    attempt = db.start_housekeeping_attempt(job)

    def record(state, p, **over):
        return db.record_decision_call(
            job_uuid=job.uuid, slot="assistant.repeat_check", model="decider",
            model_digest="d" * 64, server_version="0.40.16", question_set_version=1,
            bands_version=1, state_digest=state_digest(state),
            questions=[{"name": "retry", "kind": "noul", "version": 1,
                        "instructions": "The next message asks again.", "options": None}],
            answers=[{"name": "retry", "status": "ok", "p": p, "label": "yes" if p >= 0.5 else "no",
                      "probabilities": None, "reason": None,
                      "bands": {"yes": 0.5, "no": 0.5, "choice": 0.8}}],
            status="ok", reason=None, elapsed_ms=120, deadline_ms=20000, **over)

    state = follow_up_state(first["request"], first["next_message"])
    sent = LlmCall(caller="assistant.repeat_check", model="decider", messages=[
        {"role": "state", "content": state},
        {"role": "question", "content": '{"retry": {"instructions": "The next message asks again."}}'}],
        response_text='{"retry": {"p": 0.91}, "usage": {"input_tokens": 131, "output_tokens": 0}}')
    db.session.add(sent)
    db.session.commit()
    made["calls"].append(sent.uuid)
    on_activity = sent.uuid
    rows = [record(state, 0.91, llm_call_uuid=on_activity),
            # The chunk's last request, against the message after the chunk.
            record(follow_up_state(second["request"], second["next_message"]), 0.2),
            # Asked of messages that are no longer what the room holds.
            record({"request": "gone", "reply": "gone", "next_message": "gone"}, 0.12)]
    try:
        body = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
        assert body.count('data-desc="Identify duplicate user messages"') == 3
        # Each request has a row of its own on the timeline, named by the
        # message it is about.
        assert body.count('<span class="wf-name kind-decision">duplicate') == 3
        # Numbered as the summary's prompt numbers the chunk, from m1: the
        # room's m2 is the chunk's m3.
        # The row names the two messages that are compared.
        assert '<span class="wf-name kind-decision">duplicate m1 \u2194 m3</span>' in body
        for r in rows:
            assert f'class="wf-row" data-key="d{r.uuid}"' in body
            assert f'<div class="ev-pane" data-key="d{r.uuid}">' in body
        # A message past the chunk's range says so, and only that one.
        assert body.count(">after the chunk</span>") == 1
        # A request that reached the model links to its row on /activity. One
        # recorded without such a row has no link.
        card = body[body.index("<h3>Inspect</h3>"):]
        assert card.count('href="/activity/call/') == 1
        assert f'href="/activity/call/{on_activity}"' in card
        # A request has a line of figures, as a model call has: what a
        # decision model's call has to show, with no tokens.
        assert f'<a href="/activity/call/{on_activity}">activity \u2197</a>' in card
        for text in (">decider</span>", ">took 120 ms</span>"):
            assert card.count(text) == 3
        # The input tokens are read from the stored call's reply.
        assert card.count(">in 131</span>") == 1 and " chars</span>" not in card
        # A call shows what its page on /activity shows: each message that
        # was sent under its role, then the response.
        assert card.index(">state<") < card.index(">question<") < card.index(">response<")
        assert "The next message asks again." in card and "0.91}" in card
        # Two human messages, and no reply between them.
        assert ("&lt;message1&gt;how tall is the lighthouse&lt;/message1&gt;\n"
                "&lt;message2&gt;look up the height of the lighthouse&lt;/message2&gt;") in card
        # The message's number is the page's, and stands in the call's line.
        assert "(m2)" not in body and 'data-label="duplicate m1 \u2194 m3"' in body
        assert "<b>Question</b>" not in body and "<b>Answer</b>" not in body
        # A call with no row on /activity says so.
        assert card.count("has no row on the Activity page") == 2
        assert second["message"] == 4       # the message after the chunk is a follow-up too
        # The first row is the start. It lists every message the job reads:
        # each one's number, its uuid as a link into the room, who wrote it,
        # and its text. The two after the chunk are listed under their own
        # line and go on from the chunk's numbers, so a message a duplicate
        # row names is in the table.
        pane = body[body.index(f'data-key="a{attempt.uuid}">'):]
        pane = pane[:pane.index('<div class="ev-pane')]
        # The pane has no heading of its own: the table leads.
        assert "<b" not in pane and pane.count('<td class="hk-mono" title="m') == 6
        assert pane.index("The chunk: the messages that are summarized.") < pane.index(
            "Context after the chunk.")
        assert "Context before the chunk." not in pane      # the room begins with the chunk
        assert '<td class="hk-mono" title="m4 in the room">m5</td>' in pane
        assert '<td class="hk-mono" title="m0 in the room">m1</td>' in pane
        assert f'msg={messages[2]["uuid"]}' in pane and f'msg={messages[4]["uuid"]}' in pane
        # The uuid's first 8 characters are shown, and the whole of it on hover.
        full = str(messages[2]["uuid"])
        assert f'title="{full}">{full[:8]}</a>' in pane
        assert "<td>user</td>" in pane and "<td>assistant</td>" in pane
        # Each message has the time it was written.
        assert "<th>Date</th>" in pane
        assert f'<td class="hk-mono">{messages[0]["created_at"].strftime("%Y-%m-%d %H:%M:%S")}</td>' in pane
        # A user's row is tinted differently from an assistant's.
        assert pane.count('<tr class="is-user">') == 3 and pane.count('<tr class="is-assistant">') == 3
        assert "look up the height of the lighthouse" in pane
        # No chunk is written yet, so the timeline has no result row.
        assert 'kind-result' not in body
        chunk = db.RoomChunk(
            room_uuid=room.uuid, chunk_size=4, overlap=2, seq=0,
            first_message_id=messages[0]["id"], last_message_id=messages[3]["id"],
            message_count=4, content_hash=room_chunks.content_hash(messages[:4]),
            summary="the height of the lighthouse", topics=["lighthouse"],
            parts=[{"first_message": 0, "last_message": 3, "topics": ["lighthouse"],
                    "summary": "how tall is the lighthouse", "result": "It was not found."}],
            problems=[], prompt_version=room_chunks.PROMPT_VERSION, job_uuid=job.uuid)
        db.session.add(chunk)
        db.session.commit()
        try:
            body = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
        finally:
            db.session.delete(chunk)
            db.session.commit()
        # What the job wrote is the last row, shown as the room's page shows
        # a chunk, with a link to it there.
        names = re.findall(r'<span class="wf-name kind-[a-z]+">([^<]*)</span>', body)
        assert names[-1] == "result" and names.count("result") == 1
        # Its mark is held inside the track, so one at the very end is drawn.
        assert 'class="wf-tick" style="left:min(' in body and 'calc(100% - 2px))' in body
        pane = body[body.index(f'data-key="r{chunk.uuid}">'):]
        # It has no heading of its own, and no list of the messages: those
        # are the start row's table.
        pane = pane[:pane.index("</table>")]
        assert "<b" not in pane and "&rarr; It was not found." in pane
        assert "#chunk-0" in body and "the 4 messages" not in body
        # Numbered from m1, as the summary's prompt numbers the chunk: the
        # room's m0 to m3 are m1 to m4 here.
        assert '<td class="hk-mono">m1&ndash;m4</td>' in pane
    finally:
        for r in rows:
            db.session.delete(r)
        db.session.query(HousekeepingAttempt).filter_by(uuid=attempt.uuid).delete()
        db.session.delete(job)
        db.session.commit()


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
    job, _ = _job(_run(made))
    chunk = db.store_room_chunk(
        room_uuid=room.uuid, chunk_size=10, overlap=2, seq=0,
        first_message_id=messages[0]["id"], last_message_id=messages[9]["id"],
        message_count=10, content_hash=content_hash(messages[:10]),
        summary="questions about numbers", topics=["numbers"], problems=[],
        parts=[{"first_message": 0, "last_message": 5, "requests": 3,
                "summary": "which number comes next", "topics": ["sequences"],
                "result": "the wrong number was given twice",
                "reactions": ["asked_again", "asked_again", "moved_on"], "calibration": "overconfident",
                "asked_again_at": [{"message": 2, "p": 0.91}],
                "calibration_basis": "it answered, and the user kept asking",
                "signals": {"replies": 3, "runs": {"resolved": 2, "failed": 1},
                            "criteria_not_met": 1, "revisions": 4, "up": 0, "down": 1}},
               {"first_message": 6, "last_message": 9, "requests": 1,
                "summary": "how numbers are written", "topics": ["notation"],
                "audit": {"request": True, "result": False,
                          "reasoning": "the reply was about another number"}}],
        audit={"attempts": 2, "repeat_check": {"model": "decider", "asked": 5, "answered": 4},
               "rewritten": [{
            "first_message": 0, "last_message": 5, "summary": "which number is odd",
            "result": "seven", "reasoning": "seven answers a later request"}]},
        model="model-a", prompt_version=17, job_uuid=job.uuid)
    try:
        body = c.get(f"/assistant/housekeeping/room/{room.uuid}").get_data(as_text=True)
        assert "Messages 0&ndash;9" in body and "questions about numbers" in body
        assert "m2 asks again 0.91" in body and "Each of 5 follow-ups was put to the decision model" in body
        assert "4 were answered." in body
        # The card names no model. Which ones wrote it is on the job's page,
        # behind the button in the card's corner.
        chunk_card = body[body.index('id="chunk-0"'):body.index('id="chunk-0-messages"')]
        assert "model-a" not in chunk_card and "decider" not in chunk_card
        assert f'href="/assistant/housekeeping/{job.uuid}"' in chunk_card and ">inspect</a>" in chunk_card
        # The heading links to its own chunk in this series.
        assert 'href="?size=10&amp;overlap=2&amp;prompt=17#chunk-0">Messages 0&ndash;9</a>' in body
        assert '<span class="hk-chip">numbers</span>' in body
        assert "m0&ndash;m5" in body and "which number comes next" in body
        assert "&times;3" in body and '<span class="hk-chip">notation</span>' in body
        assert "&rarr; the wrong number was given twice" in body
        assert 'hk-cal-overconfident' in body and "asked again \u00d72" in body
        assert "it answered, and the user kept asking" in body
        for label in ("2 resolved", "1 failed", "criteria not met ×1", "4 revisions", "▼ 1"):
            assert label in body, label
        assert "[m3 assistant] message number 3" in body
        assert "messages changed since" not in body
        # 34 messages: chunks 1 and 2 are due (the third needs messages 30-31 after it).
        assert "Summarize the rest: 2 chunks" in body
        # The check: what it still doubts on a part, and what the first answer had.
        assert "the result may not match the messages: the reply was about another number" in body
        assert "The check found 1 part of the first answer wrong" in body
        assert "which number is odd &rarr; seven" in body
        assert "seven answers a later request" in body

        # The same chunks as the text a prompt is going to hold, with its size.
        assert f'href="/assistant/housekeeping/room/{room.uuid}?size=10&amp;overlap=2&amp;prompt=17&view=prompt"' in body
        as_prompt = c.get(
            f"/assistant/housekeeping/room/{room.uuid}?view=prompt").get_data(as_text=True)
        assert 'id="chunk-0"' not in as_prompt
        line = ('    &lt;part messages=&#34;0-5&#34; topics=&#34;sequences&#34; asked=&#34;3&#34; '
                'calibration=&#34;overconfident&#34; wanted=&#34;which number comes next&#34; '
                'result=&#34;the wrong number was given twice&#34;/&gt;')
        assert line in as_prompt
        block = as_prompt[as_prompt.index('<pre class="ev-pre" data-clamped="1">'):]
        text = html.unescape(block[block.index(">") + 1:block.index("</pre>")])
        assert text.startswith('<room_earlier authority="context" content_is_data="true">\n  <chunk messages="0-9" ')
        assert text.endswith("  </chunk>\n</room_earlier>")
        assert f"room_earlier ({len(text):,} chars)" in as_prompt
        assert f"<b>{len(text):,} characters</b>" in as_prompt
        # Beside it, what the chunk's ten messages come to: five of the
        # human's and five replies, "message number 0" to "message number 9".
        assert "<td>Your messages</td>" in as_prompt
        cells = [row.split("</td>")[:-1] for row in
                 as_prompt[as_prompt.index("hk-sizes"):].split("<tr>")[2:7]]
        numbers = [[re.sub("<[^>]+>", "", cell) for cell in row[1:]] for row in cells]
        # The sentences alone: the chunk's phrase, and each part's two.
        written = sum(map(len, (
            "questions about numbers", "which number comes next",
            "the wrong number was given twice", "how numbers are written")))
        assert numbers == [["5", "80", ""], ["5", "80", ""], ["10", "160", "100%"],
                           ["", str(written), f"{100 * written / 160:.0f}%"],
                           ["", f"{len(text):,}", f"{100 * len(text) / 160:.0f}%"]]

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


def test_a_log_row_shows_its_start_and_says_the_rest_on_hover():
    from datetime import datetime
    from types import SimpleNamespace

    from webapp.housekeeping_views import _job_times

    start = datetime(1999, 11, 13, 13, 59, 3)
    done = SimpleNamespace(created_at=start,
                           finished_at=datetime(1999, 11, 13, 14, 4, 57))
    assert _job_times(done).split("\n") == [
        "Started: 1999-11-13 13:59:03",
        "Stopped: 1999-11-13 14:04:57",
        "Duration: 5 min 54 s",
    ]
    still_open = SimpleNamespace(created_at=start, finished_at=None)
    assert _job_times(still_open) == "Started: 1999-11-13 13:59:03"


def test_the_log_has_a_button_that_pauses_the_queue_and_one_that_resumes_it(client):
    from agents.config import HOUSEKEEPING_UUID

    c, _made = client
    try:
        body = c.get("/assistant/housekeeping").get_data(as_text=True)
        assert 'name="paused" value="1"' in body and ">Pause</button>" in body
        assert c.post("/assistant/housekeeping/pause", data={"paused": "1"}).status_code == 302
        assert db.get_setting("housekeeping.paused") is True
        body = c.get("/assistant/housekeeping").get_data(as_text=True)
        assert 'name="paused" value="0"' in body and ">Resume</button>" in body
        assert '<span class="v">paused</span>' in body
        # Pausing wakes nobody. Resuming wakes the worker.
        woken = db.session.query(Inbox).filter(Inbox.agent_uuid == HOUSEKEEPING_UUID)
        assert woken.count() == 0
        assert c.post("/assistant/housekeeping/pause", data={"paused": "0"}).status_code == 302
        assert db.get_setting("housekeeping.paused") is False
        assert woken.count() == 1
    finally:
        db.set_setting("housekeeping.paused", None)
        db.session.query(Inbox).filter(Inbox.agent_uuid == HOUSEKEEPING_UUID).delete()
        db.session.commit()


def test_a_chunk_attempt_lists_every_model_call_it_made_in_order(client):
    from datetime import timedelta

    from agents import room_chunks

    assert db.JOB_CHECK_CALLERS[db.ROOM_CHUNK_JOB] == (room_chunks.AUDIT_CALLER,)
    c, made = client
    job, _ = db.ensure_housekeeping_job(
        db.ROOM_CHUNK_JOB, room_uuid=uuid4(), subject="10/2/v17/0/1/10")
    attempt = db.start_housekeeping_attempt(job)
    start = attempt.started_at
    calls = [
        LlmCall(caller=caller, model="model-a", started_at=start + timedelta(seconds=n),
                messages=[{"role": "user", "content": f"prompt of call {n}"}],
                response_text=f"reply of call {n}", total_ms=1000)
        for n, caller in enumerate(
            (db.JOB_CALLERS[db.ROOM_CHUNK_JOB], room_chunks.AUDIT_CALLER,
             db.JOB_CALLERS[db.ROOM_CHUNK_JOB]), 1)]
    db.session.add_all(calls)
    db.session.commit()
    made["calls"] += [call.uuid for call in calls]
    try:
        body = c.get(f"/assistant/housekeeping/{job.uuid}").get_data(as_text=True)
        # Each call has a row of its own on the timeline, in the order the
        # calls were made, and a pane under it.
        names = re.findall(r'<span class="wf-name kind-[a-z]+(?: is-failed)?">([^<]*)</span>', body)
        # The first summary has a check and another summary after it: the
        # check rejected it, and its row is red.
        assert names == ["start", "summary (rejected)", "check", "summary", "model call"]
        assert body.count('<span class="wf-name kind-llm is-failed">') == 1
        assert body.count('class="wf-bar kind-llm is-failed"') == 1
        assert body.count(">rejected by the check</span>") == 1
        # A row's hover is its name, how long it took and when it began. What
        # it is for is the hover on its name in the Inspect header.
        assert re.search(r'title="check &mdash; 1\.0s at \d\d:\d\d:\d\d"', body)
        for text in ("The job begins.", "Asks the model to write the summary",
                     "A summary the check found wrong.", "A second model call that compares",
                     "The model call that is running now"):
            assert f'data-purpose="{text}' in body
        # The Inspect header says what is going on in the selected row, in a
        # few words, and every row carries its own for when it is selected.
        assert '<div class="hk-card hk-inspect">' in body
        for text in ("the job begins", "write the summary of the messages",
                     "compare the summary with the messages",
                     "a summary the check found wrong; it was written again"):
            assert f'data-desc="{text}"' in body
        # A call's pane has no heading line: its figures lead.
        assert "call 1 of 3" not in body and "<b>Summary</b>" not in body
        places = [body.index(text) for text in (
            "reply of call 1", "reply of call 2", "reply of call 3")]
        assert places == sorted(places)
        # A bar is where its call was in the attempt, and as wide as it took.
        assert 'class="wf-bar kind-llm" style="left:' in body
        for call in calls:
            assert f'href="/activity/call/{call.uuid}"' in body
        # Each call's prompts fold under a key of their own.
        assert f'data-k="a{attempt.uuid}-m1"' in body
        assert f'data-k="a{attempt.uuid}-c2-m1"' in body
    finally:
        db.session.query(HousekeepingAttempt).filter_by(uuid=attempt.uuid).delete()
        db.session.delete(job)
        db.session.commit()
