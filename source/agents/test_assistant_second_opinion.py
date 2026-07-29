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
    SECOND_OPINION_SYSTEM_PROMPT,
    AssistantStepDecision,
    SecondOpinionVerdict,
    problem_texts,
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


def _gated_step(run_uuid):
    """The step the reviewer gated: the first step the MODEL decided. Every
    call a run makes is a row now, the loop's own included (the language
    classifier and the acceptance criteria lead every run, recorded even when
    skipped for want of a model group), so position 0 is no longer the decide
    step."""
    return next(s for s in db.list_assistant_steps(run_uuid) if not s.code_driven)


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
        {"problems": [{"category": "identity_mismatch",
                       "text": "uses miles, operator is metric"}],
         "approved": False}
    )
    assert verdict.approved is False
    assert problem_texts(verdict.problems) == ["uses miles, operator is metric"]


def test_verdict_problems_default_to_empty():
    verdict = SecondOpinionVerdict.model_validate({"approved": True})
    assert verdict.approved is True
    assert verdict.problems == []


def test_verdict_parses_categorized_problems():
    verdict = SecondOpinionVerdict.model_validate({
        "problems": [{"category": "identity_mismatch",
                      "text": "assumes US units"}],
        "approved": False,
    })
    assert verdict.problems[0].category == "identity_mismatch"
    assert verdict.problems[0].text == "assumes US units"


def test_a_bare_string_problem_normalizes_to_other():
    """Legacy payloads and any model that answers with plain strings still
    parse — they land in `other` rather than failing the call."""
    verdict = SecondOpinionVerdict.model_validate(
        {"problems": ["uses miles, operator is metric"], "approved": False})
    assert verdict.problems[0].category == "other"
    assert verdict.problems[0].text == "uses miles, operator is metric"


def test_each_rejection_ground_names_its_category_tag():
    """The taxonomy is the rejection bar the prompt already sets — each ground
    carries the tag the verdict must use, so the reviewer is labelling, not
    classifying afresh."""
    for category in ("not_asked", "identity_mismatch", "logic_error",
                     "sandbox_infeasible", "reason_mismatch"):
        assert category in SECOND_OPINION_SYSTEM_PROMPT


def test_problem_texts_reads_both_shapes():
    """One helper renders problems wherever they are shown, so a legacy inline
    payload of strings and a new payload of objects both display as text."""
    assert problem_texts(["plain string"]) == ["plain string"]
    assert problem_texts([{"category": "logic_error", "text": "wrong constant"}]) == [
        "wrong constant"]
    assert problem_texts([]) == []


# --- the review record --------------------------------------------------------


def _reviewed_run(room, monkeypatch, sandbox_calls, review, approved):
    """Run one gated turn with the review seam returning `review`; yields the
    recorded rows for the run."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _python_run("print(12 * 5280)"), _reply("done")
    )
    monkeypatch.setattr(
        AssistantAgent, "_second_opinion",
        lambda self, decision, *, reasoning, messages: (approved, review),
    )
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    return db.list_second_opinion_reviews(agent._run.uuid)


def test_a_rejection_is_recorded_as_its_own_row(room, monkeypatch, sandbox_calls):
    rows = _reviewed_run(
        room, monkeypatch, sandbox_calls,
        {"approved": False, "group_from": "second_opinion",
         "problems": [{"category": "identity_mismatch",
                       "text": "the operator profile is metric"}]},
        approved=False)
    [row] = rows
    assert row.verdict == "rejected"
    assert row.action == "python_run"
    assert row.categories == ["identity_mismatch"]
    assert row.group_from == "second_opinion"
    assert row.step_index == 0
    assert row.step_uuid is not None       # bound to the gated step


def test_an_approval_is_recorded_as_its_own_row(room, monkeypatch, sandbox_calls):
    rows = _reviewed_run(
        room, monkeypatch, sandbox_calls,
        {"approved": True, "problems": []}, approved=True)
    [row] = rows
    assert row.verdict == "approved"
    assert row.problems == []


def test_an_approval_carrying_problems_keeps_them(room, monkeypatch, sandbox_calls):
    """Approved-with-problems is the right-answer-wrong-reasons signal; it must
    be distinguishable from a clean approval."""
    rows = _reviewed_run(
        room, monkeypatch, sandbox_calls,
        {"approved": True,
         "problems": [{"category": "identity_mismatch", "text": "assumes US units"}]},
        approved=True)
    [row] = rows
    assert row.verdict == "approved"
    assert row.categories == ["identity_mismatch"]


def test_a_skipped_review_is_not_recorded_as_an_approval(
    room, monkeypatch, sandbox_calls
):
    """The fail-open paths are the reason `verdict` is four-valued."""
    rows = _reviewed_run(
        room, monkeypatch, sandbox_calls,
        {"skipped": "no_model_group"}, approved=True)
    [row] = rows
    assert row.verdict == "skipped"
    assert row.skip_reason == "no_model_group"


def test_a_failed_review_is_recorded_as_an_error(room, monkeypatch, sandbox_calls):
    rows = _reviewed_run(
        room, monkeypatch, sandbox_calls,
        {"error": "RuntimeError: all models failed"}, approved=True)
    [row] = rows
    assert row.verdict == "error"
    assert "all models failed" in row.error


def test_the_step_stores_only_a_pointer_to_the_review(
    room, monkeypatch, sandbox_calls
):
    """The row is the source of truth; the step's observation keeps just the
    uuid so the payload is not stored twice."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _python_run("print(12 * 5280)"), _reply("done"))
    monkeypatch.setattr(
        AssistantAgent, "_second_opinion",
        lambda self, decision, *, reasoning, messages: (
            True, {"approved": True, "problems": [], "system_prompt": "sys",
                   "user_prompt": "usr", "group_from": "second_opinion"}),
    )
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    [row] = db.list_second_opinion_reviews(agent._run.uuid)
    payload = _gated_step(agent._run.uuid).observation["data"]["second_opinion"]
    assert payload == {"review_uuid": str(row.uuid)}
    assert row.system_prompt == "sys" and row.user_prompt == "usr"


def test_the_inline_payload_survives_when_the_row_cannot_be_written(
    room, monkeypatch, sandbox_calls
):
    """Recording is best-effort. If it fails the trace must still carry the
    review — a lost telemetry row must not also blind the inspector."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _python_run("print(12 * 5280)"), _reply("done"))
    monkeypatch.setattr(
        AssistantAgent, "_second_opinion",
        lambda self, decision, *, reasoning, messages: (
            True, {"approved": True, "problems": [], "user_prompt": "usr"}),
    )
    def boom(**_kwargs):
        raise RuntimeError("telemetry is down")
    monkeypatch.setattr(db, "record_second_opinion_review", boom)
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    payload = _gated_step(agent._run.uuid).observation["data"]["second_opinion"]
    assert payload["user_prompt"] == "usr"      # the full payload, not a pointer
    assert "review_uuid" not in payload


def test_the_rejection_listing_shows_the_text_not_the_raw_object(
    room, monkeypatch, sandbox_calls
):
    """The observation the model reads back must be readable prose, not a dict
    repr, now that problems are objects."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _python_run("print(12 * 5280)"), _reply("giving up"))
    monkeypatch.setattr(
        AssistantAgent, "_second_opinion",
        lambda self, decision, *, reasoning, messages: (
            False,
            {"approved": False,
             "problems": [{"category": "identity_mismatch",
                           "text": "convert to meters"}]},
        ),
    )
    agent.handle(
        uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})
    gated = _gated_step(agent._run.uuid)
    assert "- convert to meters" in gated.observation["text"]
    assert "category" not in gated.observation["text"]


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
    gated = _gated_step(agent._run.uuid)
    assert gated.phase == "failed"
    assert "second_opinion rejected" in gated.observation["text"]
    assert "convert to meters" in gated.observation["text"]
    [row] = db.list_second_opinion_reviews(agent._run.uuid)
    assert gated.observation["data"]["second_opinion"] == {
        "review_uuid": str(row.uuid)}
    assert row.verdict == "rejected"
    assert problem_texts(row.problems) == [
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
    gated = _gated_step(agent._run.uuid)
    assert gated.phase == "observed"
    assert gated.observation["ok"] is True
    # The observation points at the review row, which carries the verdict.
    [row] = db.list_second_opinion_reviews(agent._run.uuid)
    assert gated.observation["data"]["second_opinion"] == {
        "review_uuid": str(row.uuid)}
    assert row.verdict == "approved" and row.problems == []


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

    def fake_call(agent_name, model_uuids, system_prompt, user_prompt, model,
                  usage_out=None):
        prompts.append((system_prompt, user_prompt))
        if usage_out is not None:
            usage_out.update({"input": 400, "output": 20, "ms": 1500})
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
    # Same section convention as the main prompt: the task leads (bare tag),
    # supporting context follows, the local-time anchor closes.
    assert "<current_request>" in user_prompt
    assert (user_prompt.index("<current_request>")
            < user_prompt.index("<proposed_step")
            < user_prompt.index("<verdict_request>")
            < user_prompt.index("<user_settings_json")
            < user_prompt.index("<operator_profile")
            < user_prompt.index("<current_local_time>"))
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
    # Dumped to plain dicts — the payload rides in a JSONB column.
    assert review["problems"] == [
        {"category": "other", "text": "converts to miles, operator is metric"}]


def test_the_review_reports_its_own_cost(monkeypatch):
    """The gate runs a real model call per gated step; its tokens and time are
    recorded nowhere else, so the payload carries them to the review row."""
    _approved, review, _ = _review(
        monkeypatch, verdict=SecondOpinionVerdict(approved=True))
    assert review["usage"] == {"input": 400, "output": 20, "ms": 1500}


def test_the_review_records_when_it_ran(monkeypatch):
    """A duration with no start cannot be placed: the /assistant Model calls
    timeline had every other call on the run's span and the second opinion
    sitting outside it, marked "not timed". Both paths stamp the start."""
    from datetime import UTC, datetime

    for kwargs in ({"verdict": SecondOpinionVerdict(approved=True)},
                   {"error": RuntimeError("all models in the group failed")}):
        _approved, review, _ = _review(monkeypatch, **kwargs)
        requested_at = review["requested_at"]
        assert (datetime.now(UTC) - requested_at).total_seconds() < 60


def test_a_failed_review_still_reports_what_it_spent(monkeypatch):
    """A call that failed open still burned tokens; dropping them would
    under-report the run."""
    _approved, review, _ = _review(
        monkeypatch, error=RuntimeError("all models in the group failed"))
    assert review["usage"]["ms"] == 1500


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
