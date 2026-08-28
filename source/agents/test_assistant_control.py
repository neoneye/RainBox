"""Loop-level steerability (Phase 6): a /stop control stops the run at a step
boundary with a clean trace; a /redirect is consumed before the next step;
heartbeats carry progress.
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, AssistantStep
from agents.assistant import AssistantActionName, AssistantAgent, AssistantStepDecision
from agents.config import ASSISTANT_UUID


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.session.rollback()
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"ctl-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "do some work")
    try:
        yield chatroom.uuid
    finally:
        db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _query(q="anything"):
    return AssistantStepDecision(reason="look", action=AssistantActionName.MEMORY_QUERY,
                                 args={"query": q})


def _reply(m="done"):
    return AssistantStepDecision(reason="answer", action=AssistantActionName.REPLY,
                                 args={"message": m})


def _decider_that_inserts_control(agent, command, payload=None):
    """First model call inserts a control for the live run, then returns a
    non-terminal step; subsequent calls reply. The control is therefore pending
    when the loop reaches the *next* step boundary."""
    calls = {"n": 0}

    def decide(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            db.create_assistant_control(run_uuid=agent._run.uuid, command=command,
                                        payload=payload or {})
            return _query()
        return _reply()

    decide.calls = calls  # type: ignore[attr-defined]
    return decide


def _steps(run_id):
    return (
        db.session.query(AssistantStep)
        .filter(AssistantStep.run_uuid == run_id)
        .order_by(AssistantStep.id)
        .all()
    )


def _agent_messages(room_uuid):
    return [m for m in db.list_room_messages(room_uuid)
            if m["sender_type"] == "agent" and m["kind"] == "message"]


def test_stop_at_step_boundary_leaves_clean_trace(room):
    agent = _agent()
    agent._decide_next_step = _decider_that_inserts_control(agent, "stop")

    result = agent.handle(uuid4(), {"room_uuid": str(room)})

    assert result["status"] == "stopped"
    run = db.session.get(AssistantRun, result["assistant_run_uuid"])
    assert run.status == "stopped"
    assert run.final_summary and "stopped by operator" in run.final_summary
    phases = [(s.action, s.phase) for s in _steps(run.uuid)]
    # Step 0's work is intact; a control step records the stop.
    assert ("memory_query", "observed") in phases
    assert ("stop", "control") in phases
    # The model was asked once (step 0); the stop prevented a second decision.
    assert agent._decide_next_step.calls["n"] == 1
    # A single clean stop message, no normal reply.
    msgs = _agent_messages(room)
    assert len(msgs) == 1
    assert "Stopped" in msgs[0]["text"]


def test_redirect_consumed_before_next_step(room):
    agent = _agent()
    agent._decide_next_step = _decider_that_inserts_control(
        agent, "redirect", payload={"instruction": "focus on the build logs"}
    )

    result = agent.handle(uuid4(), {"room_uuid": str(room)})

    assert result["status"] == "finished"
    run_id = result["assistant_run_uuid"]
    phases = [(s.action, s.phase) for s in _steps(run_id)]
    assert ("redirect", "control") in phases   # the redirect was applied
    assert ("memory_query", "observed") in phases  # step 0 intact
    assert ("reply", "final") in phases        # continued to a terminal reply
    # The redirect was marked applied (no longer pending).
    assert db.list_pending_controls(run_id) == []


def test_heartbeat_reports_progress_activity(app_ctx):
    agent = _agent()
    agent._activity = "running memory_query"
    extra = agent._heartbeat_extra()
    assert extra["activity"] == "running memory_query"


def test_heartbeat_reads_no_orm_state_off_the_beating_thread(app_ctx, room):
    """The heartbeat runs on a background thread, and a Flask app context is
    pushed on the MAIN thread only (agents/__main__). SQLAlchemy expires every
    loaded instance on commit, so reading `self._run.uuid` there turns into a
    database round trip, which flask-sqlalchemy routes through `current_app` —
    and the beat dies with "Working outside of application context".

    Seen live as a run that kept logging "heartbeat send failed; still beating"
    while a cold model held the main thread inside one long call, with nothing
    touching the run to refresh it. Three silent beats is the supervisor's
    60s watchdog, so a healthy turn gets SIGKILLed.
    """
    import threading
    from sqlalchemy import inspect as sa_inspect

    agent = _agent()
    agent._run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room, agent_uuid=ASSISTANT_UUID)
    run_uuid = str(agent._run.uuid)

    # The state the failure needs, and the one every commit produces.
    db.session.commit()
    assert "uuid" in sa_inspect(agent._run).unloaded, (
        "precondition: the run must be expired for this to test anything")

    result: dict = {}

    def beat_off_thread() -> None:
        try:
            result["extra"] = agent._heartbeat_extra()
        except Exception as exc:                      # noqa: BLE001 — recorded
            result["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=beat_off_thread)
    thread.start()
    thread.join(timeout=10)

    assert "error" not in result, result.get("error")
    assert result["extra"]["assistant_run_uuid"] == run_uuid
