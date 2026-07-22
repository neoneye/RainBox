"""Tests for the AssistantAgent bounded loop (PR 2).

Deterministic: the only live-model seam (`_decide_next_step`) is replaced with a
scripted sequence via `agents.assistant_fakes.scripted_decisions`, so the loop,
step cap, validation, terminal posting, and (in-memory) trace shape are
exercised without LM Studio or a model binding.

Trace persistence to dedicated tables is PR 3; here the trace lives in
`agent._steps` (a list the loop appends to), which PR 3 will swap for durable
rows. Non-terminal read actions are PR 4; in PR 2 only `reply` and
`ask_clarifying_question` are enabled, so any other action is a validation
failure.
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, AssistantStep
from agents.assistant import AssistantActionName, AssistantAgent, AssistantStepDecision
from agents.assistant_fakes import scripted_decisions
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
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    """A chatroom with the assistant as a member, plus one human message.

    Yields (room_uuid, message_uuid). Cleaned up on teardown.
    """
    human = db.get_human_user()
    assert human is not None
    name = f"assistant-test-{uuid4().hex[:8]}"
    chatroom = db.create_chatroom(name, human.uuid, [ASSISTANT_UUID])
    msg = db.post_chat_message(chatroom.uuid, human.uuid, "hello assistant")
    try:
        yield chatroom.uuid, msg.uuid
    finally:
        # Drop trace rows (assistant_step cascades from assistant_run) and the
        # room (chat messages, incl. debug-assistant pointers, cascade).
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None
    )


def _reply(message: str) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="ready to answer",
        action=AssistantActionName.REPLY,
        args={"1_specification": "en, metric", "2_message": message, "3_audit": "OK"},
    )


def _ask(question: str) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="need more info",
        action=AssistantActionName.ASK_CLARIFYING_QUESTION,
        args={"question": question},
    )


def _query_memory(query: str) -> AssistantStepDecision:
    # Not enabled in PR 2 -> always a validation failure, used to drive the loop
    # without ever terminating.
    return AssistantStepDecision(
        reason="look it up",
        action=AssistantActionName.MEMORY_QUERY,
        args={"query": query},
    )


def _agent_messages(room_uuid):
    return [
        m
        for m in db.list_room_messages(room_uuid)
        if m["sender_type"] == "agent" and m["kind"] == "message"
    ]


def _phases(agent: AssistantAgent) -> list[str]:
    return [s["phase"] for s in agent._steps]


# --- terminal actions ---------------------------------------------------------


def test_step_records_token_usage_and_model_from_the_decide_call(room):
    """The token counts + model uuid the structured-completion seam exposes (via
    _last_usage / _last_model_uuid) are stored on the step that decision produced."""
    room_uuid, message_uuid = room
    agent = _agent()
    model_uuid = uuid4()

    def decider(**_kwargs):
        # what base.py would set
        agent._last_usage = {"input": 412, "output": 87, "ms": 5100}
        agent._last_model_uuid = model_uuid
        agent._last_response_text = (
            '{"reason":"ready to answer","action":"reply",'
            '"args":{"message":"done"}}'
        )
        return _reply("done")

    agent._decide_next_step = decider
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    final = db.list_assistant_steps(agent._run.uuid)[-1]
    assert final.input_tokens == 412 and final.output_tokens == 87
    assert final.duration_ms == 5100
    assert final.model_uuid == model_uuid
    assert final.model_response == agent._last_response_text


def test_step_records_and_posts_model_reasoning(room):
    """The native reasoning text the structured-completion seam exposes (via
    _last_reasoning) is stored on the step that decision produced AND posted
    into the room as a kind="thinking" row, so /assistant and /chat both show
    what a reasoning model was thinking."""
    room_uuid, message_uuid = room
    agent = _agent()

    def decider(**_kwargs):
        # what base.py would set for a reasoning model
        agent._last_reasoning = "The operator greeted me; a short reply suffices."
        return _reply("Hello!")

    agent._decide_next_step = decider
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    final = db.list_assistant_steps(agent._run.uuid)[-1]
    assert final.reasoning == "The operator greeted me; a short reply suffices."
    thinking = [
        m for m in db.list_room_messages(room_uuid) if m["kind"] == "thinking"
    ]
    assert [m["text"] for m in thinking] == [
        "The operator greeted me; a short reply suffices."
    ]


def test_non_reasoning_model_leaves_no_reasoning_trace(room):
    """A non-reasoning model exposes no reasoning text; the step's reasoning
    stays NULL and no thinking row is posted (the scripted seam, like base.py
    on a reasoning-free call, leaves _last_reasoning as None)."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_reply("Hello!"))
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    final = db.list_assistant_steps(agent._run.uuid)[-1]
    assert final.reasoning is None
    assert not [
        m for m in db.list_room_messages(room_uuid) if m["kind"] == "thinking"
    ]


def test_terminal_run_enqueues_a_summary(room):
    """When a run reaches a terminal state, the assistant enqueues the
    assistant_run_summarizer (off the critical path) carrying this run's uuid."""
    from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID
    from db.models import Inbox

    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_reply("Done."))
    result = agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    run_uuid = result["assistant_run_uuid"]
    items = (
        db.db.session.query(Inbox)
        .filter(Inbox.agent_uuid == ASSISTANT_RUN_SUMMARIZER_UUID)
        .all()
    )
    assert any(run_uuid in (i.payload or "") for i in items)


def test_reply_action_posts_one_message_and_finishes(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_reply("Working tree is clean."))

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    posts = _agent_messages(room_uuid)
    assert len(posts) == 1
    assert posts[0]["text"] == "Working tree is clean."
    assert result["status"] == "finished"
    # A terminal step is a single row (no separate planned transition).
    assert _phases(agent) == ["final"]


def test_ask_clarifying_question_is_terminal_and_posts(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_ask("Which repository do you mean?"))

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    posts = _agent_messages(room_uuid)
    assert len(posts) == 1
    assert posts[0]["text"] == "Which repository do you mean?"
    assert result["status"] == "finished"
    assert _phases(agent) == ["final"]


# --- loop control -------------------------------------------------------------


def test_over_consumed_scripted_seam_raises_clearly(room):
    """If the loop asks for more model decisions than were scripted, the seam
    raises a clear AssertionError rather than hanging or silently passing."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions()  # nothing scripted

    with pytest.raises(AssertionError, match="more decisions than were scripted"):
        agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})


def test_step_cap_stops_after_step_limit(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent.step_limit = 3
    # Exactly step_limit non-terminal (disabled) decisions: if the loop asked
    # for a 4th the scripted seam would raise; if it stopped early the queue
    # would be unused. Passing proves it ran exactly step_limit times.
    agent._decide_next_step = scripted_decisions(
        _query_memory("a"), _query_memory("b"), _query_memory("c")
    )

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "stopped"
    # Each step is one row that settles running->observed in place; no terminal
    # "final" (the run hit the step cap). So three observed steps, not nine rows.
    assert _phases(agent) == ["observed", "observed", "observed"]
    # The user still gets a message explaining the run stopped.
    assert len(_agent_messages(room_uuid)) == 1


def test_invalid_args_produce_traceable_failed_step_not_a_crash(room):
    room_uuid, message_uuid = room
    agent = _agent()
    # First decision is a reply with no message (invalid); the loop must record
    # a failed step and continue, then the valid reply terminates the run.
    bad = AssistantStepDecision(
        reason="oops", action=AssistantActionName.REPLY, args={}
    )
    agent._decide_next_step = scripted_decisions(bad, _reply("Now I can answer."))

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    # Step 0 fails validation (single failed row), step 1 is the terminal reply.
    assert _phases(agent) == ["failed", "final"]
    failed = agent._steps[0]
    assert failed["error"] and "message" in failed["error"]
    posts = _agent_messages(room_uuid)
    assert len(posts) == 1
    assert posts[0]["text"] == "Now I can answer."


# --- the production decision seam ---------------------------------------------


def test_decide_next_step_calls_structured_completion_with_decision_model(app_ctx):
    """The real _decide_next_step (not the scripted fake) must route through the
    extracted _structured_completion with the AssistantStepDecision schema and a
    prompt that carries the structured messages."""
    agent = _agent()
    captured: dict = {}

    def fake_completion(*, system_prompt, user_prompt, response_model, validator=None):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        captured["response_model"] = response_model
        return _reply("ok")

    agent._structured_completion = fake_completion
    decision = agent._decide_next_step(
        messages=[{
            "sender_type": "human",
            "text": "what is the git status?",
            "timestamp": "2026-07-13 17:00",
        }],
        scratchpad=[],
        step_index=0,
    )

    assert decision.action is AssistantActionName.REPLY
    assert captured["response_model"] is AssistantStepDecision
    assert captured["system_prompt"].strip()
    assert "git status" in captured["user_prompt"]


def test_handle_raises_on_missing_room_uuid(app_ctx):
    agent = _agent()
    with pytest.raises(ValueError, match="room_uuid"):
        agent.handle(uuid4(), {"message_uuid": str(uuid4())})


# --- registration -------------------------------------------------------------


def test_assistant_is_registered_as_structured_responder():
    from agents.config import agent_config
    from webapp.chat_api import CHAT_RESPONDER_UUIDS

    entry = agent_config["assistant"]
    assert entry["uuid"] == ASSISTANT_UUID
    assert entry.get("requires_structured_output") is True
    assert entry.get("requires_function_calling") is not True
    assert ASSISTANT_UUID in CHAT_RESPONDER_UUIDS


def test_assistant_agent_is_a_model_group_agent_not_structured():
    """Spec: the assistant is a specialized ModelGroupAgent (it makes multiple
    structured calls inside one handle()), not a one-shot StructuredLLMAgent."""
    from agents.base import ModelGroupAgent, StructuredLLMAgent

    assert issubclass(AssistantAgent, ModelGroupAgent)
    assert not issubclass(AssistantAgent, StructuredLLMAgent)


# --- durable trace (PR 3) -----------------------------------------------------


def _steps_for(run_id):
    return (
        db.db.session.query(AssistantStep)
        .filter(AssistantStep.run_uuid == run_id)
        .order_by(AssistantStep.id)
        .all()
    )


def test_loop_persists_run_and_steps_to_tables(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_reply("Working tree is clean."))

    jid = uuid4()
    result = agent.handle(jid, {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    run = db.db.session.get(AssistantRun, result["assistant_run_uuid"])
    assert run is not None
    assert run.status == "finished"
    assert run.journal_id == jid
    assert run.room_uuid == room_uuid

    steps = _steps_for(run.uuid)
    assert [s.phase for s in steps] == ["final"]
    assert [s.action for s in steps] == ["reply"]


def test_journal_result_is_summary_not_full_trace(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_reply("Done."))

    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    # The journal result points at the run + carries a short summary; it is not
    # the trace itself.
    assert result["assistant_run_uuid"] is not None
    assert result["final_summary"] == "Done."
    assert "steps" not in result


def test_killed_mid_run_leaves_last_committed_step_and_marks_run_failed(room):
    """If a later step crashes, the already-committed steps remain visible and
    the run is marked failed (not left stuck in 'running')."""
    room_uuid, message_uuid = room
    agent = _agent()

    calls = {"n": 0}

    def flaky(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # An invalid reply (missing message) -> planned+failed, committed —
            # a stable first step independent of which read actions are enabled.
            return AssistantStepDecision(
                reason="oops", action=AssistantActionName.REPLY, args={}
            )
        raise RuntimeError("model exploded")

    agent._decide_next_step = flaky

    with pytest.raises(RuntimeError, match="model exploded"):
        agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    runs = (
        db.db.session.query(AssistantRun)
        .filter(AssistantRun.room_uuid == room_uuid)
        .all()
    )
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "failed"
    steps = _steps_for(run.uuid)
    phases = [s.phase for s in steps]
    # Step 0's failed row survived the crash; a terminal failed row records the
    # exception raised while deciding step 1. One row per step now.
    assert phases == ["failed", "failed"]
    # The terminal failed row points at the logical step where it failed (the
    # model raised while deciding step 1), not a row count.
    assert steps[-1].phase == "failed"
    assert steps[-1].step_index == 1
    assert run.summary["outcome"] == "failed"
    assert "RuntimeError: model exploded" in run.summary["obstacles"]


def test_handled_model_failure_persists_last_prompts(room):
    room_uuid, message_uuid = room
    agent = _agent()
    db.post_progress(room_uuid, agent.agent_uuid, "Working on it")

    def failed_decide(**_kwargs):
        agent._last_system_prompt = "system policy used for failed call"
        agent._last_user_prompt = "user data used for failed call"
        raise TimeoutError("structured stream exceeded 45s")

    agent._decide_next_step = failed_decide
    with pytest.raises(TimeoutError):
        agent.handle(
            uuid4(),
            {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)},
        )

    step = db.list_assistant_steps(agent._run.uuid)[-1]
    assert step.system_prompt == "system policy used for failed call"
    assert step.user_prompt == "user data used for failed call"
    assert "45s" in step.error
    assert agent._run.summary["outcome"] == "failed"
    messages = db.list_room_messages(room_uuid)
    assert not any(message["kind"] == "progress" for message in messages)
    notices = [message for message in messages if message["kind"] == "notice"]
    assert len(notices) == 1
    assert "I stopped before completing this request" in notices[0]["text"]
    assert f"/assistant?id={agent._run.uuid}" in notices[0]["text"]
