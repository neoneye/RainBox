"""Tests for the second-opinion gate: an independent LLM review of a gated
action (currently python_run) that runs BEFORE dispatch. A rejection becomes
the step's failed observation and the program never executes; an approval
dispatches and carries the verdict in observation.data.

Deterministic: the decide seam is scripted (`scripted_decisions`), the review
seam is either monkeypatched at the agent method (loop tests) or exercised for
real with `agents.query_filter_router.structured_llm_call` monkeypatched
(unit tests), and the Python sandbox is replaced with a recording fake.
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun
from agents.assistant import (
    CAPABILITIES,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    SecondOpinionVerdict,
)
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
    msg = db.post_chat_message(
        chatroom.uuid, human.uuid, "how much is 12 feet?")
    try:
        yield chatroom.uuid, msg.uuid
    finally:
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


def _python_run(code: str) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="compute the conversion",
        action=AssistantActionName.PYTHON_RUN,
        args={"code": code},
    )


def _reply(message: str) -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="done", action=AssistantActionName.REPLY, args={"message": message}
    )


@pytest.fixture
def sandbox_calls(monkeypatch):
    """Replace the Pyodide sandbox with a recording fake; yields the list of
    code strings that were actually executed."""
    from tools.python_sandbox import sandbox

    calls: list[str] = []

    def fake_run_python(code: str, **_kwargs):
        calls.append(code)
        return sandbox.SandboxResult(
            ok=True, stdout="3.6576\n", duration_seconds=0.01
        )

    monkeypatch.setattr(sandbox, "run_python", fake_run_python)
    return calls


# --- the capability flag ------------------------------------------------------


def test_python_run_is_second_opinion_gated():
    assert CAPABILITIES[AssistantActionName.PYTHON_RUN].second_opinion is True


def test_the_gate_is_scoped_to_python_run_only():
    """Lock the gated surface: widening it is a deliberate registry change,
    not an accident."""
    gated = {a.value for a, cap in CAPABILITIES.items() if cap.second_opinion}
    assert gated == {"python_run"}


# --- the verdict schema -------------------------------------------------------


def test_verdict_parses_from_structured_output_json():
    verdict = SecondOpinionVerdict.model_validate(
        {"problems": ["uses miles, operator is metric"], "approved": False}
    )
    assert verdict.approved is False
    assert verdict.problems == ["uses miles, operator is metric"]


def test_verdict_problems_default_to_empty():
    verdict = SecondOpinionVerdict.model_validate({"approved": True})
    assert verdict.approved is True
    assert verdict.problems == []


# --- the loop gate ------------------------------------------------------------


def test_rejection_blocks_execution_and_feeds_the_critique_back(
    room, monkeypatch, sandbox_calls
):
    """A rejected python_run never reaches the sandbox; the step fails with the
    reviewer's problems in the observation, and the model can then revise."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _python_run("print(12 * 5280)"), _reply("giving up")
    )
    monkeypatch.setattr(
        AssistantAgent,
        "_second_opinion",
        lambda self, decision, *, reasoning, messages: (
            False,
            {"approved": False,
             "problems": ["the operator profile is metric; convert to meters"]},
        ),
    )
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert sandbox_calls == []
    steps = db.list_assistant_steps(agent._run.uuid)
    gated = steps[0]
    assert gated.phase == "failed"
    assert "second_opinion rejected" in gated.observation["text"]
    assert "convert to meters" in gated.observation["text"]
    assert gated.observation["data"]["second_opinion"]["problems"] == [
        "the operator profile is metric; convert to meters"
    ]


def test_approval_runs_the_program_and_records_the_verdict(
    room, monkeypatch, sandbox_calls
):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _python_run("print(12 * 0.3048)"), _reply("3.66 meters")
    )
    monkeypatch.setattr(
        AssistantAgent,
        "_second_opinion",
        lambda self, decision, *, reasoning, messages: (
            True, {"approved": True, "problems": []}
        ),
    )
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    assert sandbox_calls == ["print(12 * 0.3048)"]
    steps = db.list_assistant_steps(agent._run.uuid)
    gated = steps[0]
    assert gated.phase == "observed"
    assert gated.observation["ok"] is True
    assert gated.observation["data"]["second_opinion"] == {
        "approved": True, "problems": []
    }


def test_ungated_actions_never_consult_the_reviewer(room, monkeypatch):
    """reply is not second_opinion-gated; the reviewer must not run at all."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(_reply("hello"))

    def explode(self, decision, *, reasoning, messages):
        raise AssertionError("second_opinion consulted for an ungated action")

    monkeypatch.setattr(AssistantAgent, "_second_opinion", explode)
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})


# --- the review call itself ---------------------------------------------------


def _review(monkeypatch, *, verdict=None, error=None, no_group=False):
    """Run _second_opinion with the model-group resolver and the structured
    call monkeypatched; returns (approved, review, prompts) where `prompts`
    captures the (system, user) pair the reviewer model was given."""
    import agents.query_filter_router as qfr

    prompts: list[tuple[str, str]] = []
    resolved = (None, None) if no_group else ([uuid4()], "second_opinion")
    monkeypatch.setattr(
        qfr, "resolve_model_uuids", lambda candidates: resolved
    )

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, model):
        prompts.append((system_prompt, user_prompt))
        if error is not None:
            raise error
        return verdict, model_uuids[0]

    monkeypatch.setattr(qfr, "structured_llm_call", fake_call)
    agent = _agent()
    agent._identity_block = '{"name": "Otto", "country": "Denmark"}'
    agent._profile_block = "prefers metric units"
    approved, review = agent._second_opinion(
        _python_run("print(12 * 0.3048)"),
        reasoning="Feet to an unknown unit; the operator is metric.",
        messages=[{"text": "how much is 12 feet?", "sender_type": "human"}],
    )
    return approved, review, prompts


def test_review_prompt_carries_all_artifacts_under_review(monkeypatch):
    approved, review, prompts = _review(
        monkeypatch, verdict=SecondOpinionVerdict(approved=True)
    )
    assert approved is True
    assert review["approved"] is True and review["group_from"] == "second_opinion"
    [(system_prompt, user_prompt)] = prompts
    assert "second-opinion reviewer" in system_prompt
    assert "how much is 12 feet?" in user_prompt
    assert "compute the conversion" in user_prompt          # stated_reason
    assert "the operator is metric" in user_prompt          # model_reasoning
    assert "print(12 * 0.3048)" in user_prompt              # python_program
    assert "prefers metric units" in user_prompt            # operator_profile
    assert 'action="python_run"' in user_prompt
    # The exact prompts ride in the review payload so the inspector can show
    # the review's model request verbatim.
    assert review["system_prompt"] == system_prompt
    assert review["user_prompt"] == user_prompt
    # No instrumentation events fire through the faked call, so the reasoning
    # stays empty and the response falls back to the parsed verdict's JSON.
    assert review["reasoning"] is None
    assert review["response"] == SecondOpinionVerdict(approved=True).model_dump_json()


def test_review_rejection_returns_the_problems(monkeypatch):
    approved, review, _ = _review(
        monkeypatch,
        verdict=SecondOpinionVerdict(
            approved=False, problems=["converts to miles, operator is metric"]
        ),
    )
    assert approved is False
    assert review["problems"] == ["converts to miles, operator is metric"]


def test_review_failure_fails_open(monkeypatch):
    """The reviewer model being down must not block side-effect-free compute;
    the action runs and the trace records why the check was skipped."""
    approved, review, _ = _review(
        monkeypatch, error=RuntimeError("all models in the group failed")
    )
    assert approved is True
    assert "all models in the group failed" in review["error"]
    # The prompts were built before the failed call; keep them for diagnosis.
    assert "print(12 * 0.3048)" in review["user_prompt"]


def test_no_model_group_anywhere_skips_the_review(monkeypatch):
    approved, review, prompts = _review(monkeypatch, no_group=True)
    assert approved is True
    assert review == {"skipped": "no_model_group"}
    assert prompts == []


def test_reviewer_chain_ignores_the_memory_filter_binding(monkeypatch):
    """Regression: the reviewer once resolved through
    resolve_filter_model_uuids, which prepends the memory_filter scorer
    binding — so a bound memory_filter silently supplied the reviewer model
    (seen live as group_from="memory_filter"). The reviewer consults only its
    own chain; the filter callers keep their memory_filter-first behaviour."""
    import agents.query_filter_router as qfr
    from agents.config import MEMORY_FILTER_UUID, SECOND_OPINION_UUID

    class Binding:
        model_group_uuid = uuid4()

    def only_memory_filter_bound(agent_uuid):
        return Binding() if agent_uuid == MEMORY_FILTER_UUID else None

    monkeypatch.setattr(qfr.db, "get_agent_model_binding", only_memory_filter_bound)
    monkeypatch.setattr(
        qfr.db, "get_model_group_member_uuids", lambda group_uuid: [uuid4()])
    assert qfr.resolve_model_uuids(
        [(SECOND_OPINION_UUID, "second_opinion"), (uuid4(), "own")]
    ) == (None, None)
    _uuids, label = qfr.resolve_filter_model_uuids([(uuid4(), "own")])
    assert label == "memory_filter"
