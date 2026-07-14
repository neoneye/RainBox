"""The assistant-run summarizer: turns a completed run into a stored
{trigger, obstacles, outcome} digest via one structured call. Model-free — the
`_structured_call` seam is monkeypatched.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

import db
from agents.assistant_run_summarizer import (
    AssistantRunSummarizerAgent,
    RunSummary,
    _find_repeated_calls,
)
from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID, agent_config
from db import AssistantRun


def _step(idx: int, action: str | None, args: dict | None = None):
    return SimpleNamespace(step_index=idx, action=action, args=args or {},
                           phase="observed", error=None, observation_preview=None)


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
    db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run_id).delete()
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
        run_uuid=run.uuid, step_index=0, action="kanban_task_column", reason="move it")
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
        assert "kanban_task_column" in captured["prompt"]
        assert "no such task" in captured["prompt"]
        # The digest is stored on the run, with a timestamp.
        fresh = db.get_assistant_run(run.uuid)
        assert fresh is not None and fresh.summary is not None
        assert fresh.summary["trigger"] == "move a kanban task to Done"
        assert fresh.summary["obstacles"] == ["the task did not exist"]
        assert fresh.summary["outcome"] == "failed"
        assert "summarized_at" in fresh.summary
    finally:
        _cleanup(run.uuid, room.uuid)


def test_missing_run_returns_not_ok_without_calling_the_model(app_ctx):
    agent = _agent()

    def boom(*_a, **_k):
        raise AssertionError("the model must not be called for a missing run")

    agent._structured_call = boom  # type: ignore[method-assign]
    result = agent.handle(uuid4(), {"run_uuid": str(uuid4())})
    assert result["ok"] is False


def test_find_repeated_calls_groups_identical_calls():
    steps = [_step(i, "kanban_read", {"board_uuid": "b1"}) for i in range(6)]
    groups = _find_repeated_calls(steps)
    assert len(groups) == 1
    g = groups[0]
    assert g["action"] == "kanban_read"
    assert g["count"] == 6
    assert g["indices"] == [0, 1, 2, 3, 4, 5]
    assert g["similarity"] == 1.0


def test_find_repeated_calls_is_arg_order_independent_and_fuzzy():
    # Same call, keys in different order + a tiny value tweak — still one cluster.
    steps = [
        _step(0, "memory_query", {"q": "where are my keys", "limit": 5}),
        _step(1, "memory_query", {"limit": 5, "q": "where are my keys"}),
        _step(2, "memory_query", {"q": "where are my keyz", "limit": 5}),
    ]
    groups = _find_repeated_calls(steps)
    assert len(groups) == 1
    assert groups[0]["count"] == 3
    assert groups[0]["similarity"] >= 0.85


def test_find_repeated_calls_ignores_distinct_calls():
    steps = [
        _step(0, "kanban_read", {"board_uuid": "b1"}),
        _step(1, "memory_query", {"q": "x"}),
        _step(2, "kanban_task_create", {"title": "t"}),
    ]
    assert _find_repeated_calls(steps) == []


def test_find_repeated_calls_same_action_different_args_not_grouped():
    steps = [
        _step(0, "memory_query", {"q": "where are my keys today please"}),
        _step(1, "memory_query", {"q": "what time is the dentist appointment"}),
    ]
    assert _find_repeated_calls(steps) == []


def test_find_repeated_calls_ignores_actionless_steps():
    steps = [_step(0, None), _step(1, "stop"), _step(2, None)]
    # 'stop' appears once; the two action-less control steps are skipped.
    assert _find_repeated_calls(steps) == []


def test_prompt_includes_repeated_call_hint_with_score():
    agent = _agent()
    steps = [_step(i, "kanban_read", {"board_uuid": "b1"}) for i in range(6)]
    prompt = agent._build_prompt(SimpleNamespace(status="stopped"), steps, None)
    assert "Possible repeated calls" in prompt
    assert "kanban_read called 6×" in prompt
    assert "% similar" in prompt


def test_prompt_omits_hint_for_distinct_calls():
    agent = _agent()
    steps = [_step(0, "kanban_read", {"board_uuid": "b1"}),
             _step(1, "memory_query", {"q": "x"})]
    prompt = agent._build_prompt(SimpleNamespace(status="finished"), steps, None)
    assert "repeated calls" not in prompt.lower()
