"""The assistant-run summarizer: turns a completed run into a stored
{trigger, obstacles, outcome} digest via one structured call. Model-free — the
`_structured_call` seam is monkeypatched.
"""

from uuid import uuid4

import pytest

import db
from agents.assistant_run_summarizer import AssistantRunSummarizerAgent, RunSummary
from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID, agent_config
from db import AssistantRun


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


def _agent() -> AssistantRunSummarizerAgent:
    return AssistantRunSummarizerAgent(
        agent_uuid=ASSISTANT_RUN_SUMMARIZER_UUID,
        name="assistant_run_summarizer", send=lambda _: None,
    )


def _room():
    human = db.get_human_user()
    assert human is not None
    return db.create_chatroom(f"summ-{uuid4().hex[:8]}", human.uuid, [])


def _cleanup(run_id: int, room_uuid=None) -> None:
    db.db.session.query(AssistantRun).filter(AssistantRun.id == run_id).delete()
    if room_uuid is not None:
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
    db.db.session.commit()


def test_requires_structured_output():
    assert agent_config["assistant_run_summarizer"].get("requires_structured_output") is True


def test_summarizes_run_into_summary_column(app_ctx):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_id=run.id, step_index=0, action="kanban_move_task", reason="move it")
    db.settle_assistant_step(step, phase="failed", error="no such task")
    db.finish_run(run, "failed")
    agent = _agent()
    captured: dict = {}

    def fake_call(user_prompt, validator=None):
        captured["prompt"] = user_prompt
        return RunSummary(
            trigger="move a kanban task to Done",
            obstacles=["the task did not exist"],
            outcome="failed",
        )

    agent._structured_call = fake_call  # type: ignore[method-assign]
    try:
        result = agent.handle(uuid4(), {"run_uuid": str(run.uuid)})
        assert result["ok"] is True
        # The prompt was built from the run's trigger + step trace (the failed step).
        assert "kanban_move_task" in captured["prompt"]
        assert "no such task" in captured["prompt"]
        # The digest is stored on the run, with a timestamp.
        fresh = db.get_assistant_run_by_uuid(run.uuid)
        assert fresh is not None and fresh.summary is not None
        assert fresh.summary["trigger"] == "move a kanban task to Done"
        assert fresh.summary["obstacles"] == ["the task did not exist"]
        assert fresh.summary["outcome"] == "failed"
        assert "summarized_at" in fresh.summary
    finally:
        _cleanup(run.id, room.uuid)


def test_missing_run_returns_not_ok_without_calling_the_model(app_ctx):
    agent = _agent()

    def boom(*_a, **_k):
        raise AssertionError("the model must not be called for a missing run")

    agent._structured_call = boom  # type: ignore[method-assign]
    result = agent.handle(uuid4(), {"run_uuid": str(uuid4())})
    assert result["ok"] is False
