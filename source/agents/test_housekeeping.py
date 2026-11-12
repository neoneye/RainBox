"""Tests for the housekeeping role and its queue: db/housekeeping.py and
agents/housekeeping.py. No model is called; the summarizer's call is faked."""
from __future__ import annotations

import re
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


# --- room chunks -------------------------------------------------------------

def _room_with_messages(made: dict, n: int):
    human = db.get_human_user()
    room = db.create_chatroom(f"hkc-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    made["rooms"].append(room.uuid)
    for i in range(n):
        sender = human.uuid if i % 2 == 0 else ASSISTANT_UUID
        db.post_chat_message(room.uuid, sender, f"message number {i} about the weather")
    return room


def _reply(first: int, n: int = 10, topic: str = "weather"):
    from agents.room_chunks import ChunkPart, ChunkSummary

    return ChunkSummary(
        parts=[ChunkPart(first_message=first, last_message=first + n - 1,
                         summary="questions about the weather",
                         result="a forecast was given", follow_up="none",
                         topics=[topic])],
        subjects=["weather questions"], topics=["Weather Talk"])


def _checks(prompt: str, wrong: tuple[int, ...] = ()):
    """A check with one entry per part of the index in `prompt`. The parts
    at the positions in `wrong` have a result that does not match."""
    from agents.room_chunks import ChunkCheck, PartCheck

    firsts = [int(n or 0) for n in re.findall(r"^Part (?:m(\d+) to|with no)", prompt, re.M)]
    return ChunkCheck(parts=[
        PartCheck(first_message=first, reasoning=f"read part {k}",
                  request_matches=True, result_matches=k not in wrong)
        for k, first in enumerate(firsts)])


def _fake_chunk_model(monkeypatch, reply=None, check=None):
    """Stands in for the structured calls. `reply(first)` builds the summary;
    by default one part covering the chunk the prompt names. `check(prompt)`
    builds the check of it; by default one that finds nothing wrong. Returns
    the summary calls' prompts; the checks' prompts are its `.checks`."""
    from agents import model_groups
    from agents.room_chunks import ChunkCheck

    class Prompts(list):
        checks: list[str]

    prompts = Prompts()
    prompts.checks = []

    def fake(agent_name, models, system_prompt, user_prompt, response_model, *,
             observer=None, **_kw):
        if response_model is ChunkCheck:
            prompts.checks.append(user_prompt)
            return (check(user_prompt) if check else _checks(user_prompt)), models[0]
        prompts.append(user_prompt)
        first = int(re.search(r"The chunk, m(\d+) to", user_prompt).group(1))
        if observer is not None:
            observer.started(models[0], "model-a", 60.0)
            observer.progress(models[0], "model-a", None, '{"parts": [')
        return (reply(first) if reply else _reply(first)), models[0]

    member = uuid4()
    monkeypatch.setattr(model_groups, "structured_llm_call", fake)
    monkeypatch.setattr(model_groups, "resolve_assistant_model_uuids",
                        lambda _slot: ([member], "assistant.room_summarizer"))
    monkeypatch.setattr(hk, "_is_local_model", lambda _m: True)
    monkeypatch.setattr(db, "resolved_model_kwargs", lambda _m: ("ollama", "model-a", {}))
    return prompts


V = 11   # agents.room_chunks.PROMPT_VERSION


def test_chunks_are_cut_in_order_and_wait_for_their_context(app_ctx):
    room = _room_with_messages(app_ctx, 23)
    # 23 messages, 10 per chunk, 2 of context: the second chunk ends at
    # message 19 and needs 20 and 21 after it; a third would need 30 to 31.
    due = db.due_room_chunks(room.uuid, 10, 2, V, limit=10)
    assert [d["seq"] for d in due] == [0, 1]
    ids = [m["id"] for m in db.counted_room_messages(room.uuid)]
    assert (due[0]["first_id"], due[0]["last_id"]) == (ids[0], ids[9])
    assert (due[1]["first_id"], due[1]["last_id"]) == (ids[10], ids[19])
    assert len(db.due_room_chunks(room.uuid, 10, 2, V, limit=1)) == 1
    # Other settings are another series, cut from the first message again.
    assert [d["seq"] for d in db.due_room_chunks(room.uuid, 5, 0, V, limit=10)] == [0, 1, 2, 3]
    assert len(db.due_room_chunks(room.uuid, 10, 4, V, limit=10)) == 1


def test_progress_and_thinking_rows_are_not_counted(app_ctx):
    room = _room_with_messages(app_ctx, 4)
    db.post_chat_message(room.uuid, ASSISTANT_UUID, "", kind="progress")
    db.post_chat_message(room.uuid, ASSISTANT_UUID, "hmm", kind="thinking")
    assert len(db.counted_room_messages(room.uuid)) == 4


def test_a_run_end_queues_two_chunks_and_each_is_summarized_once(app_ctx, monkeypatch):
    room = _room_with_messages(app_ctx, 45)
    prompts = _fake_chunk_model(monkeypatch)
    agent = _agent(monkeypatch)
    agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": False,
                           "run_uuid": None})
    assert prompts == []                                # no run, no request: nothing
    monkeypatch.setattr(hk, "CHUNK_JOBS_PER_REQUEST", 2)
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})
    assert result["jobs"] == {"done": 2}
    chunks = db.list_room_chunks(room.uuid, 10, 2, V)
    try:
        assert [(c.seq, c.message_count, c.summary, c.prompt_version) for c in chunks] == [
            (0, 10, "weather questions", V), (1, 10, "weather questions", V)]
        assert chunks[0].topics == ["weather-talk"] and chunks[0].model == "model-a"
        assert chunks[0].problems == [] and chunks[0].job_uuid is not None
        # Each summary was checked once, found right, and not written again.
        assert chunks[0].audit == {"attempts": 1} and len(prompts.checks) == 2
        assert prompts.checks[0].startswith(prompts[0] + "\n\nThe index to check:\n")
        assert chunks[1].parts == [{
            "first_message": 10, "last_message": 19, "requests": 1,
            "summary": "questions about the weather", "topics": ["weather"],
            "result": "a forecast was given",
            # Four more messages inside the part, then the next chunk's first.
            "reactions": ["pushed_for_more"] * 4 + ["moved_on"],
            "calibration": "uncontested",
            "calibration_basis": "the human went on, and did not say whether it was right",
            "audit": {"request": True, "result": True, "reasoning": "read part 0"},
            "signals": {"replies": 5, "repeats": 0, "bare": 0, "runs": {}, "first_run": None, "last_run": None,
                        "criteria_not_met": 0, "revisions": 0, "up": 0, "down": 0}}]
        # The first chunk has no context before it; the second has two each side.
        assert "not part of it:\n(none)" in prompts[0]
        assert "[m8 human] message number 8" in prompts[1]
        assert "[m21 assistant]" in prompts[1] and "[m22" not in prompts[1]
        # The model reads both sides.
        assert "[m11 assistant] message number 11" in prompts[1]
        # The series continues after the stored chunks, and nothing is redone.
        assert [d["seq"] for d in db.due_room_chunks(room.uuid, 10, 2, V, 10)] == [2, 3]
        assert agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})[
            "jobs"] == {"done": 2}
        assert len(prompts) == 4
    finally:
        db.session.query(db.RoomChunk).filter_by(room_uuid=room.uuid).delete()
        db.session.commit()


def test_the_setting_switches_room_summaries_off_for_run_ends(app_ctx, monkeypatch):
    room = _room_with_messages(app_ctx, 30)
    run = _run(app_ctx)
    _fake_summary(monkeypatch)
    prompts = _fake_chunk_model(monkeypatch)
    agent = _agent(monkeypatch)
    settings = {"housekeeping.room_summaries": False,
                "housekeeping.room_chunk_size": 10, "housekeeping.room_chunk_overlap": 2}
    monkeypatch.setattr(db, "get_setting", lambda key: settings[key])
    payload = {"run_uuid": str(run.uuid), "room_uuid": str(room.uuid)}
    assert agent.handle(uuid4(), payload)["jobs"] == {"done": 1}      # the run summary
    assert prompts == []
    settings["housekeeping.room_summaries"] = True
    try:
        assert agent.handle(uuid4(), payload)["jobs"] == {"done": 2}  # two chunks per run end
        assert len(db.list_room_chunks(room.uuid, 10, 2, V)) == 2
    finally:
        db.session.query(db.RoomChunk).filter_by(room_uuid=room.uuid).delete()
        db.session.commit()


def test_what_is_off_in_a_reply_is_stored_with_the_chunk(app_ctx, monkeypatch):
    from agents.room_chunks import ChunkPart, ChunkSummary

    room = _room_with_messages(app_ctx, 12)
    _fake_chunk_model(monkeypatch, lambda first: ChunkSummary(
        parts=[ChunkPart(first_message=40, last_message=44, summary="x" * 130,
                         result="", follow_up="none", topics=[])],
        subjects=[], topics=["a", "b", "c", "d"]))
    agent = _agent(monkeypatch)
    agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})
    (chunk,) = db.list_room_chunks(room.uuid, 10, 2, V)
    try:
        assert chunk.problems == [
            "part 1: messages 40-44 are not inside the chunk",
            "part 1: 130 characters", "part 1 result: empty", "part 1: no topics",
            "chunk: empty", "chunk: 4 topics, kept 3"]
        assert chunk.parts[0]["first_message"] is None and chunk.topics == ["a", "b", "c"]
    finally:
        db.session.query(db.RoomChunk).filter_by(room_uuid=room.uuid).delete()
        db.session.commit()


def test_room_messages_go_to_local_models_only(app_ctx, monkeypatch):
    room = _room_with_messages(app_ctx, 12)
    prompts = _fake_chunk_model(monkeypatch)
    monkeypatch.setattr(hk, "_is_local_model", lambda _m: False)
    agent = _agent(monkeypatch)
    assert agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})[
        "jobs"] == {"failed": 2}
    assert prompts == [] and db.list_room_chunks(room.uuid, 10, 2, V) == []
    (job,) = [r["job"] for r in db.list_housekeeping_jobs(room_uuid=room.uuid)[0]]
    assert "local model" in job.outcome


def test_the_worker_rests_after_a_burst_of_calls(app_ctx, monkeypatch):
    room = _room_with_messages(app_ctx, 45)
    _fake_chunk_model(monkeypatch)
    agent = _agent(monkeypatch)
    agent.BURST_CALLS = 2
    agent.COOLDOWN_SECONDS = 0.05
    rests: list[int] = []
    cool = agent._cool_down_if_due

    def counting():
        before = agent._burst
        cool()
        if before and agent._burst == 0:
            rests.append(before)

    monkeypatch.setattr(agent, "_cool_down_if_due", counting)
    try:
        assert agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})[
            "jobs"] == {"done": 4}
        # A chunk is two calls, the summary and its check: a rest after each
        # of the first three, and the fourth ends the request.
        assert rests == [2, 2, 2]
    finally:
        db.session.query(db.RoomChunk).filter_by(room_uuid=room.uuid).delete()
        db.session.commit()


def _part(first, last, summary, *topics, result="done", read="none"):
    from agents import room_chunks as rc

    return rc.ChunkPart(first_message=first, last_message=last, summary=summary,
                        result=result, follow_up=read, topics=list(topics))


def _asked(*texts):
    """A chunk in which each request is followed by one reply."""
    out = []
    for text in texts:
        out += [{"id": len(out) + 1, "sender": "human", "text": text},
                {"id": len(out) + 2, "sender": "agent", "text": "a reply"}]
    return out


def test_settle_joins_a_request_asked_again_and_makes_parts_cover_the_chunk():
    from agents import room_chunks as rc

    chunk = _asked("compute the square root of 1999", "compute the square root of 1999",
                   "try again", "compute the square root of 2999", "which boards exist")
    reply = rc.ChunkSummary(
        parts=[_part(20, 20, "compute a square root", "sqrt", "number",
                     result="step limit reached", read="retried"),
               _part(22, 22, "compute it once more", "sqrt", read="retried"),
               _part(24, 24, "a retry", "sqrt", "maths", result="a value was given"),
               _part(26, 26, "compute another square root", "sqrt", result="another value"),
               _part(28, 28, "which boards exist", "Kanban Boards")],
        subjects=["square roots", "Square Roots", "boards."], topics=["maths", "kanban"])
    summary, topics, parts, problems = rc.settle(reply, 20, 29, chunk)
    assert (summary, topics, problems) == ("square roots, boards", ["maths", "kanban"], [])
    first, second, third = parts
    # The same request and a bare retry are one part with the first's phrase,
    # and it ends where the request ended up.
    assert (first["first_message"], first["last_message"], first["requests"]) == (20, 25, 3)
    assert first["summary"] == "compute a square root"
    assert first["result"] == "a value was given"
    assert first["reactions"] == ["asked_again", "asked_again", "moved_on"]
    assert first["topics"] == ["sqrt", "number"]
    # The same question about another value keeps its own part and its own
    # answer, though the model gave it the same topic.
    assert (second["first_message"], second["last_message"], second["requests"],
            second["result"]) == (26, 27, 1, "another value")
    assert (third["first_message"], third["last_message"], third["topics"]) == (
        28, 29, ["kanban-boards"])


def test_the_check_is_put_on_its_parts_and_fails_on_request_or_result():
    from agents import room_chunks as rc

    def check(first, request=True, result=True):
        return rc.PartCheck(first_message=first, reasoning=f"about {first}",
                            request_matches=request, result_matches=result)

    parts = [{"first_message": 20, "last_message": 23}, {"first_message": 24, "last_message": 27},
             {"first_message": 28, "last_message": 29}]
    # Numbered 1, 2, 3 by the model: one per part, so they are taken in order.
    failed, problems = rc.settle_audit(rc.ChunkCheck(parts=[
        check(1, result=False), check(2), check(3)]), parts)
    assert failed == [parts[0]] and problems == []
    assert parts[0]["audit"] == {"request": True, "result": False, "reasoning": "about 1"}
    # Fewer checks than parts: matched by message number, the rest unchecked.
    parts = [{"first_message": 20, "last_message": 23}, {"first_message": 24, "last_message": 29}]
    failed, problems = rc.settle_audit(rc.ChunkCheck(parts=[check(24, request=False)]), parts)
    assert failed == [parts[1]] and "audit" not in parts[0]
    assert problems == ["check: 1 checks for 2 parts"]


def test_a_chunk_whose_check_fails_is_written_once_more(app_ctx, monkeypatch):
    room = _room_with_messages(app_ctx, 12)
    # The first answer's check finds its part wrong; the second's finds it
    # wrong too, and that one is kept with the check on it.
    prompts = _fake_chunk_model(monkeypatch, check=lambda p: _checks(p, wrong=(0,)))
    agent = _agent(monkeypatch)
    assert agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})[
        "jobs"] == {"done": 1}
    (chunk,) = db.list_room_chunks(room.uuid, 10, 2, V)
    try:
        assert len(prompts) == 2 and len(prompts.checks) == 2
        assert prompts[1].startswith(prompts[0] + "\n\nAn earlier answer for this chunk")
        assert "- Part m0 to m9: read part 0" in prompts[1]
        assert chunk.audit == {"attempts": 2, "rewritten": [{
            "first_message": 0, "last_message": 9, "summary": "questions about the weather",
            "result": "a forecast was given", "reasoning": "read part 0"}]}
        assert chunk.parts[0]["audit"]["result"] is False
        assert agent._burst == 4                        # every call counts toward the rest
    finally:
        db.session.query(db.RoomChunk).filter_by(room_uuid=room.uuid).delete()
        db.session.commit()


def test_a_check_no_model_delivers_leaves_the_summary(app_ctx, monkeypatch):
    room = _room_with_messages(app_ctx, 12)

    def no_check(_prompt):
        raise RuntimeError("every member failed")

    prompts = _fake_chunk_model(monkeypatch, check=no_check)
    agent = _agent(monkeypatch)
    assert agent.handle(uuid4(), {"room_uuid": str(room.uuid), "room_chunks": True})[
        "jobs"] == {"done": 1}
    (chunk,) = db.list_room_chunks(room.uuid, 10, 2, V)
    try:
        assert len(prompts) == 1 and chunk.summary == "weather questions"
        assert chunk.audit == {"attempts": 1, "error": "check: every member failed"}
        assert "audit" not in chunk.parts[0]
    finally:
        db.session.query(db.RoomChunk).filter_by(room_uuid=room.uuid).delete()
        db.session.commit()


def test_a_parts_signals_are_counted_from_the_records_of_its_replies():
    from agents import room_chunks as rc

    chunk = [
        {"id": 1, "sender": "human", "text": "count the letters"},
        {"id": 2, "sender": "agent", "text": "two", "signals": {
            "run": "resolved", "criteria": "not met", "revisions": 3, "vote": "downvote"}},
        {"id": 3, "sender": "human", "text": "that is wrong"},
        {"id": 4, "sender": "agent", "text": "three", "signals": {
            "run": "resolved", "criteria": "met", "revisions": 1, "vote": "upvote"}},
        {"id": 5, "sender": "human", "text": "which boards exist"},
        {"id": 6, "sender": "agent", "text": "two boards", "signals": {"run": "partial"}},
    ]
    reply = rc.ChunkSummary(
        parts=[_part(40, 43, "count letters", "letters", read="corrected"),
               _part(44, 45, "list boards", "boards")],
        subjects=["letters", "boards"], topics=["misc"])
    _s, _t, parts, _p = rc.settle(reply, 40, 45, chunk)
    assert parts[0]["signals"] == {
        "replies": 2, "repeats": 0, "bare": 1, "runs": {"resolved": 2}, "first_run": "resolved",
        "last_run": "resolved", "criteria_not_met": 1,
        "revisions": 4, "up": 1, "down": 1}
    assert parts[1]["signals"]["runs"] == {"partial": 1}
    assert (parts[0]["calibration"], parts[0]["calibration_basis"]) == (
        "overconfident", "a reply was voted down")
    assert parts[1]["calibration"] == "unclear"

    prompt = rc.build_prompt([], chunk, [], 40)["user"]
    assert ("[m41 assistant] two\n    signals for m41: run resolved; "
            "criteria not met; voted down by the human") in prompt
    assert "signals for m43: run resolved; criteria met after 1 revision; voted up" in prompt


@pytest.mark.parametrize("reactions, requests, signals, expected", [
    # A vote down or a correction outranks everything else.
    (["moved_on"], 1, {"down": 1, "up": 1, "last_run": "resolved"}, "overconfident"),
    (["corrected"], 1, {"runs": {"resolved": 1}, "last_run": "resolved"}, "overconfident"),
    # Asked again, and delivered only then.
    (["asked_again", "moved_on"], 1,
     {"bare": 1, "runs": {"partial": 1, "resolved": 1}, "first_run": "partial",
      "last_run": "resolved"}, "underconfident"),
    # The request repeated in its text, after an answer that did not settle it.
    (["asked_again", "asked_again", "moved_on"], 1,
     {"repeats": 2, "runs": {"resolved": 1, "partial": 2}, "first_run": "resolved",
      "last_run": "partial", "criteria_not_met": 2}, "overconfident"),
    # A bare follow-up read as thanks is not asking again.
    (["thanked"], 1, {"bare": 1, "runs": {"resolved": 1}, "last_run": "resolved"}, "uncontested"),
    # Three different questions joined into one part are not asking again.
    (["pushed_for_more", "moved_on"], 3,
     {"runs": {"resolved": 3}, "first_run": "resolved", "last_run": "resolved"}, "uncontested"),
    # Asked again and never delivered: a failure, not a matter of confidence.
    (["asked_again"], 1, {"bare": 1, "runs": {"failed": 2}, "first_run": "failed",
                          "last_run": "failed"}, "unclear"),
    (["thanked"], 1, {"up": 1, "runs": {"partial": 1}, "last_run": "partial"}, "calibrated"),
    (["not_shown"], 1, {"runs": {"resolved": 1}, "last_run": "resolved"}, "unclear"),
    # Going on without a word is not agreement: only a vote up is.
    (["moved_on"], 1, {"runs": {"resolved": 1}, "last_run": "resolved"}, "uncontested"),
    (["pushed_for_more"], 1, {"runs": {"failed": 1}, "last_run": "failed"}, "unclear"),
    (["moved_on"], 1, {}, "uncontested"),          # a reply with no run behind it
])
def test_calibration_is_decided_by_rule(reactions, requests, signals, expected):
    from agents import room_chunks as rc

    label, basis = rc.calibrate(
        {"reactions": reactions, "requests": requests, "signals": signals})
    assert label == expected and basis


@pytest.mark.parametrize("texts, read, after, expected", [
    # A few words after a reply are what the model read them as, and a nudge
    # when it read nothing.
    (["tell me about the lighthouse", "more"], "none", ["which boards exist"],
     ["pushed_for_more", "moved_on"]),
    (["tell me about the lighthouse", "once more"], "retried", ["which boards exist"],
     ["asked_again", "moved_on"]),
    (["tell me about the lighthouse", "good"], "thanked", ["which boards exist"],
     ["thanked", "moved_on"]),
    # A retry the model read in a part with one follow-up is that message,
    # whatever its length.
    (["tell me about the lighthouse", "do that again and look in the 1999 notes"],
     "retried", ["which boards exist"], ["asked_again", "moved_on"]),
    # With two to choose from its reading does not say which, so neither is.
    (["tell me about the lighthouse", "and who built the lighthouse keeper's house",
      "what colour is the lighthouse keeper's house"],
     "retried", ["which boards exist"], ["pushed_for_more", "pushed_for_more", "moved_on"]),
    # The same request typed again is asking again whatever the model read,
    # also when it is the first message after the part.
    (["tell me about the lighthouse", "tell me about the lighthouse"], "none",
     ["which boards exist"], ["asked_again", "moved_on"]),
    (["tell me about the lighthouse"], "none", ["tell me about the lighthouse, from memory"],
     ["asked_again"]),
    # A correction is the model's to read.
    (["tell me about the lighthouse", "it was built of brick and not of stone"], "corrected",
     ["which boards exist"], ["pushed_for_more", "corrected", "moved_on"]),
    # Only a room that ends there shows no reaction.
    (["tell me about the lighthouse"], "none", [], ["not_shown"]),
])
def test_reactions_are_read_from_the_messages(texts, read, after, expected):
    from agents import room_chunks as rc

    chunk = _asked(*texts)
    last = 40 + len(chunk) - 1
    reply = rc.ChunkSummary(parts=[_part(40, last, "the lighthouse", "lighthouse", read=read)],
                            subjects=["the lighthouse"], topics=["places"])
    _s, _t, parts, _p = rc.settle(reply, 40, last, chunk, _asked(*after))
    assert parts[0]["reactions"] == expected and "read" not in parts[0]


def test_a_part_inside_the_chunk_is_followed_by_the_next_parts_request():
    from agents import room_chunks as rc

    chunk = _asked("tell me about the lighthouse", "which boards exist")
    reply = rc.ChunkSummary(
        parts=[_part(40, 41, "the lighthouse", "lighthouse"), _part(42, 43, "boards", "boards")],
        subjects=["a lighthouse", "boards"], topics=["misc"])
    _s, _t, parts, _p = rc.settle(reply, 40, 43, chunk, [])
    assert [p["reactions"] for p in parts] == [["moved_on"], ["not_shown"]]


@pytest.mark.parametrize("next_request, expected", [
    # The reply's own wording held up to it, in two kinds of quotation mark.
    ('who is "the keeper" here', "questioned"),
    ("who is «the keeper» here", "questioned"),
    # The human's own words in quotation marks are not the reply's.
    ('search for "the lighthouse" once more in the notes of 1999', "moved_on"),
    ("which boards exist", "moved_on"),
])
def test_a_next_message_that_quotes_the_reply_back_is_negative_feedback(next_request, expected):
    from agents import room_chunks as rc

    chunk = [{"id": 1, "sender": "human", "text": "tell me about the lighthouse"},
             {"id": 2, "sender": "agent", "text": "The keeper built it in 1999.",
              "signals": {"run": "resolved"}},
             {"id": 3, "sender": "human", "text": next_request},
             {"id": 4, "sender": "agent", "text": "a reply"}]
    reply = rc.ChunkSummary(
        parts=[_part(40, 41, "the lighthouse", "lighthouse"),
               _part(42, 43, "a question", "question")],
        subjects=["the lighthouse", "a question"], topics=["misc"])
    _s, _t, parts, _p = rc.settle(reply, 40, 43, chunk, [])
    assert parts[0]["reactions"] == [expected]
    assert (parts[0]["calibration"] == "overconfident") == (expected == "questioned")


def test_reply_signals_come_from_the_run_the_audit_and_the_vote(app_ctx):
    room = _room_with_messages(app_ctx, 4)
    human = db.get_human_user()
    run = db.start_assistant_run(journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID)
    app_ctx["runs"].append(run.uuid)
    db.finish_run(run, "finished")
    db.set_run_summary(run, {"trigger": "t", "obstacles": [], "outcome": "partial"})
    for i, verdict in enumerate(("revise", "revise", "send")):
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=i, phase="observed", action="reply_audit",
            reason="r", observation_preview='{"verdict": "%s"}' % verdict)
    reply = db.post_chat_message(room.uuid, ASSISTANT_UUID, "an answer",
                                 meta={"assistant_run_uuid": str(run.uuid)})
    plain = db.post_chat_message(room.uuid, ASSISTANT_UUID, "no run behind this")
    made = [db.create_feedback_event(
                room_uuid=room.uuid, message_uuid=reply.uuid, agent_uuid=ASSISTANT_UUID,
                rating=rating, comment=comment, created_by_uuid=human.uuid)
            for rating, comment in (("upvote", None), ("downvote", "wrong count"))]
    try:
        messages = db.attach_reply_signals(room.uuid, db.counted_room_messages(room.uuid))
        by_id = {m["id"]: m["signals"] for m in messages}
        assert by_id[reply.id] == {
            "run": "partial", "criteria": "met", "revisions": 2,
            "vote": "downvote", "vote_comment": "wrong count"}     # the latest vote
        assert by_id[plain.id] is None
        assert all(m["signals"] is None for m in messages if m["sender"] == "human")
    finally:
        db.session.query(db.FeedbackEvent).filter(
            db.FeedbackEvent.uuid.in_([f.uuid for f in made])).delete()
        db.session.commit()


def test_a_subject_without_a_version_is_from_the_first_prompt():
    assert db.parse_chunk_subject("10/2/3:40-49") == {
        "size": 10, "overlap": 2, "version": 1, "seq": 3, "first_id": 40, "last_id": 49}
    assert db.parse_chunk_subject(db.chunk_subject(5, 0, 2, 7, 1, 9)) == {
        "size": 5, "overlap": 0, "version": 2, "seq": 7, "first_id": 1, "last_id": 9}
    assert db.parse_chunk_subject("nonsense") is None


def test_repeats_are_read_from_the_requests_text():
    from agents import room_chunks as rc

    count = "count the number of r in the word strawberry"
    assert rc.count_repeats([count, count + " using python",
                             count + " using python and ignore case"]) == (2, 0)
    assert rc.count_repeats([count, count.upper()]) == (1, 0)
    # Retyped with slips in it, it is still the same request.
    assert rc.count_repeats(["when is the first mention of the harbour in my diary",
                             "when is the frist mention of the harbour i n my diary"]) == (1, 0)
    # Another value is another question.
    assert rc.count_repeats(["what is 1176 feet in metres", "what is 358 inches in metres"]) == (0, 0)
    assert rc.count_repeats(["what is 1176 feet", "what is 358 inches"]) == (0, 0)
    assert rc.count_repeats(["what did I do on 01 august 1999",
                             "what did I do on 01 september 1999"]) == (0, 0)
    # A few words with no request of their own are bare, not repeats.
    assert rc.count_repeats(["tell me about the old harbour", "try again", "thanks"]) == (0, 2)
    assert rc.count_repeats(["again"]) == (0, 0)        # the first is never a follow-up
    assert rc.count_repeats([]) == (0, 0)
