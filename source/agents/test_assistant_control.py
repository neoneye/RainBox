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
        db.db.session.rollback()
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
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _query(q="anything"):
    return AssistantStepDecision(reason="look", action=AssistantActionName.QUERY_MEMORY,
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
        db.db.session.query(AssistantStep)
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
    run = db.db.session.get(AssistantRun, result["assistant_run_uuid"])
    assert run.status == "stopped"
    assert run.final_summary and "stopped by operator" in run.final_summary
    phases = [(s.action, s.phase) for s in _steps(run.uuid)]
    # Step 0's work is intact; a control step records the stop.
    assert ("query_memory", "observed") in phases
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
    assert ("query_memory", "observed") in phases  # step 0 intact
    assert ("reply", "final") in phases        # continued to a terminal reply
    # The redirect was marked applied (no longer pending).
    assert db.list_pending_controls(run_id) == []


def test_heartbeat_reports_progress_activity(app_ctx):
    agent = _agent()
    agent._activity = "running query_memory"
    extra = agent._heartbeat_extra()
    assert extra["activity"] == "running query_memory"
