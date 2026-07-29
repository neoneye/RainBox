"""Tests for the acceptance-criteria step: a code-driven step 0 establishes
the reply's constraints (processing preferences, formatting, assumptions)
before the decide loop starts, injects them as
<acceptance_criteria_markdown> directly after <current_request> in every decide
step, and supports mid-run revision — code-driven after a flagged preference
write, model-requested via the `acceptance_criteria` catalog action.

Deterministic: the criteria live-model seam (`_request_acceptance_criteria`)
is stubbed alongside the decide seam (`scripted_decisions`) / the
structured-completion capture, so the ordering, budget, trace, and prompt
properties are exercised without a model.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

import db
from agents.assistant import (
    AcceptanceCriteria,
    AssistantActionName,
    AssistantAgent,
    AssistantObservation,
    AssistantStepDecision,
    AssistantTurnStep,
)
from agents.assistant_fakes import scripted_decisions
from agents.config import ASSISTANT_UUID

KEYS = ("profile.current", "qa.facts_invalidated_at",
        "profile.current_changed_at", "assistant.formatting_guide")


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    saved = {}
    for key in KEYS:
        row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
        saved[key] = row.value if row is not None else None
    try:
        yield app
    finally:
        db.db.session.rollback()
        for key, value in saved.items():
            row = db.db.session.query(db.AppSetting).filter_by(key=key).one_or_none()
            if row is not None:
                row.value = value
        db.db.session.commit()
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    """A chatroom with the assistant and one ambiguous conversion request."""
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(
        f"ac-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "convert 1053737172 feet")
    try:
        yield chatroom
    finally:
        db.db.session.rollback()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _criteria(marker: str) -> AcceptanceCriteria:
    """A distinguishable criteria set; `marker` shows up in the rendered JSON."""
    return AcceptanceCriteria(
        processing=f"target unit: meters ({marker})",
        formatting="numbers: dot decimal, no thousand separators",
        assumptions="convert target not stated; assuming meters",
    )


def _reply(message: str = "About 321179090 meters.") -> AssistantStepDecision:
    return AssistantStepDecision(
        reason="ready to answer", action=AssistantActionName.REPLY,
        args={"message": message})


def _probe(i: int) -> AssistantStepDecision:
    """A deterministic non-terminal decide step: the unknown argument makes it
    a validation failure, which consumes a decide step without dispatching
    anything (no embeddings, no model)."""
    return AssistantStepDecision(
        reason="probe", action=AssistantActionName.MEMORY_QUERY,
        args={"bogus": f"q{i}"})


def _stub_criteria_seam(agent, results, calls=None):
    """Replace the criteria live-model seam with a scripted queue. `results`
    entries are AcceptanceCriteria or an Exception to raise. `calls` (when
    given) records {"system_prompt": ..., "user_prompt": ...} per call."""
    queue = list(results)

    def fake(*, system_prompt, user_prompt):
        assert queue, "more criteria calls than scripted"
        if calls is not None:
            calls.append({"system_prompt": system_prompt,
                          "user_prompt": user_prompt})
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    agent._request_acceptance_criteria = fake
    return queue


def _capture_decides(agent, decisions):
    """Route decide steps through _structured_completion (so prompts are
    built) and capture each decide call's prompts as
    {"system": ..., "user": ...}."""
    queue = list(decisions)
    prompts = []

    def fake_completion(*, system_prompt, user_prompt, response_model,
                        validator=None):
        assert queue, "more decide calls than scripted"
        prompts.append({"system": system_prompt, "user": user_prompt})
        return queue.pop(0)

    agent._structured_completion = fake_completion
    return prompts


def _steps(run_uuid):
    return (
        db.db.session.query(db.AssistantStep)
        .filter(db.AssistantStep.run_uuid == run_uuid)
        .order_by(db.AssistantStep.id)
        .all()
    )


# --- always on ----------------------------------------------------------------


def test_criteria_run_on_every_turn_with_no_switch_to_turn_them_off(app_ctx):
    """The criteria shipped behind `assistant.acceptance_criteria` while they
    proved out. The switch is gone: with it off a small model would decide for
    itself what the reply should look like, and guess wrong — imperial units
    for a metric operator being the case that ended the experiment. Every turn
    now establishes the constraints, and the section is in every prompt."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(
        f"ac-always-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "convert 1053737172 feet")
    agent = _agent()
    calls = []
    _stub_criteria_seam(agent, [_criteria("step0")], calls)
    prompts = _capture_decides(agent, [_reply()])
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert result["status"] == "finished"
        assert len(calls) == 1                                   # the step-0 call
        assert "<acceptance_criteria_markdown" in prompts[0]["user"]
        # The system prompt ranks the section, so the model treats it as
        # binding rather than as one more piece of context.
        assert "acceptance_criteria_markdown" in prompts[0]["system"]
        # …and the revision action is always available to the model.
        assert "- acceptance_criteria:" in agent._action_catalog()
        assert AssistantActionName.ACCEPTANCE_CRITERIA in agent._caps
    finally:
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_no_setting_exists_for_the_criteria(app_ctx):
    """The /settings page must not offer a switch for this."""
    import pytest as _pytest

    with _pytest.raises(db.settings.UnknownSetting):
        db.get_setting("assistant.acceptance_criteria")


# --- step 0: one call, before the loop, outside the budget --------------------


def test_criteria_call_made_once_per_run_before_the_first_decide(room):
    agent = _agent()
    order = []
    calls = []

    def fake_criteria(*, system_prompt, user_prompt):
        order.append("criteria")
        calls.append(user_prompt)
        return _criteria("step0")

    agent._request_acceptance_criteria = fake_criteria
    real_scripted = scripted_decisions(_probe(0), _reply())

    def decide(**kwargs):
        order.append("decide")
        return real_scripted(**kwargs)

    agent._decide_next_step = decide
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    assert order[0] == "criteria"          # before any decide step
    assert order.count("criteria") == 1    # once per run
    # NOT the action catalog: this step plans constraints, not actions.
    assert "Available actions" not in calls[0]
    assert "memory_query" not in calls[0]


def test_criteria_section_renders_directly_after_current_request(room):
    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0")])
    prompts = _capture_decides(agent, [_reply()])
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    prompt = prompts[0]["user"]
    assert "<acceptance_criteria_markdown" in prompt
    assert "target unit: meters (step0)" in prompt
    assert (prompt.index("</current_request>")
            < prompt.index("<acceptance_criteria_markdown")
            < prompt.index("<conversation_history"))


def test_system_prompt_ranks_the_criteria_just_below_the_request(room):
    """The decide system prompt lists acceptance_criteria_markdown directly below
    current_request and carries the code-owned authority sentence, so the model
    treats the criteria as binding rather than as one more piece of context.
    The module constant is the un-swapped literal and mentions neither — the
    two variants stay readable exactly as the model receives them."""
    from agents.assistant import ASSISTANT_SYSTEM_PROMPT

    assert "acceptance_criteria_markdown" not in ASSISTANT_SYSTEM_PROMPT
    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0")])
    prompts = _capture_decides(agent, [_reply()])
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    system = prompts[0]["system"]
    assert ('<source rank="3">reply_language_markdown' in system)
    assert ('<source rank="4">acceptance_criteria_markdown' in system)
    assert '<source rank="2">current_request</source>' in system
    assert "acceptance_criteria_markdown is the established plan" in system
    # The other sources are still all ranked (shifted, not dropped).
    assert '<source rank="7">conversation_history_xml (context only)</source>' in system


def test_step0_consumes_none_of_the_step_limit(room):
    """A run can still take STEP_LIMIT decide steps after the criteria call;
    the criteria row carries its own index outside the decide numbering."""
    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0")])
    probes = [_probe(i) for i in range(agent.STEP_LIMIT - 1)]
    agent._decide_next_step = scripted_decisions(*probes, _reply())
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"  # all STEP_LIMIT decides fit
    rows = _steps(result["assistant_run_uuid"])
    criteria_rows = [s for s in rows if s.action == "acceptance_criteria"]
    assert len(criteria_rows) == 1
    assert criteria_rows[0].phase == "observed"
    assert criteria_rows[0].step_index == 0
    # Neither the criteria row nor the reply audit is one of the decide
    # steps: all STEP_LIMIT decide rows exist alongside them.
    decide_rows = [s for s in rows if not s.code_driven]
    assert [s.step_index for s in decide_rows] == list(range(agent.STEP_LIMIT))
    # The step-0 call's prompts are persisted like any other step's.
    assert criteria_rows[0].user_prompt and "convert 1053737172 feet" in (
        criteria_rows[0].user_prompt)


def test_step0_row_is_flagged_code_driven_and_the_revision_is_not(room):
    """The loop chose step 0, so its row says so: `code_driven` is what lets a
    reader tell an action the model picked from a call the code made. Without
    it the row's code-written action/reason read as a model decision — the
    inspector rendered exactly that, hiding the criteria the call returned."""
    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0"), _criteria("revised")])
    revise = AssistantStepDecision(
        reason="the operator named a unit mid-run",
        action=AssistantActionName.ACCEPTANCE_CRITERIA, args={})
    _capture_decides(agent, [revise, _reply()])
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    rows = _steps(result["assistant_run_uuid"])
    step0 = next(s for s in rows if s.action == "acceptance_criteria"
                 and s.step_index == 0)
    assert step0.code_driven is True
    # The model asking for a revision IS a decision — that row stays False, so
    # its decide dump keeps rendering.
    revision = next(s for s in rows if s.action == "acceptance_criteria"
                    and s.reason == "the operator named a unit mid-run")
    assert revision.code_driven is False
    # The reply the model decided on, likewise.
    assert next(s for s in rows if s.action == "reply").code_driven is False
    # The reply audit is the loop's own call too (the classifier is covered in
    # test_response_language_classifier).
    audit = [s for s in rows if s.action == AssistantAgent.REPLY_AUDIT_ACTION]
    assert audit and all(s.code_driven for s in audit)


def test_criteria_call_sees_formatting_guide_despite_gated_switch(room):
    """The formatting guide is a declared INPUT of the criteria call, rendered
    from the criteria snapshot profile regardless of the separate
    assistant.formatting_guide switch (which only gates the decide-prompt
    injection) — otherwise a run with that switch off would establish its
    criteria without the derived defaults (metric -> Celsius, separators)."""
    db.set_setting("assistant.formatting_guide", False)
    germany = next(e for e in db.profile_templates_entries()
                   if e["name"] == "Germany")["uuid"]
    db.set_current_profile(germany)
    agent = _agent()
    calls = []
    _stub_criteria_seam(agent, [_criteria("step0")], calls)
    prompts = _capture_decides(agent, [_reply()])
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert "Use these defaults unless the current request" in calls[0]["user_prompt"]
    assert "- Temperature: Celsius" in calls[0]["user_prompt"]
    # The decide prompt stays gated: no formatting_guide section there.
    assert "<formatting_guide" not in prompts[0]["user"]


# --- fail-open ----------------------------------------------------------------


def test_failed_criteria_call_is_fail_open(room):
    agent = _agent()
    _stub_criteria_seam(agent, [RuntimeError("model exploded")])
    prompts = _capture_decides(agent, [_reply()])
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"                          # run proceeds
    assert "<acceptance_criteria_markdown" not in prompts[0]["user"]  # no section
    rows = _steps(result["assistant_run_uuid"])
    failed = [s for s in rows if s.action == "acceptance_criteria"]
    assert len(failed) == 1 and failed[0].phase == "failed"
    assert "model exploded" in (failed[0].error or "")


# --- the system prompt's scope -----------------------------------------------


def test_system_prompt_excludes_response_language():
    prompt = AssistantAgent._acceptance_criteria_system_prompt()
    assert "response_language" not in prompt
    assert "Language rules" not in prompt
    assert "processing" in prompt
    assert "formatting" in prompt
    assert "assumptions" in prompt


def test_schema_contains_only_non_language_criteria():
    schema = AcceptanceCriteria.model_json_schema()
    assert schema["required"] == ["processing", "formatting", "assumptions"]
    assert set(schema["properties"]) == {
        "processing", "formatting", "assumptions"}


def test_every_criterion_is_a_required_non_empty_string():
    """Prose, not a list, and no empty exit: a list of terse fragments let a
    small model return `[]` for `formatting` on the theory that the
    formatting guide applies itself downstream. A field with nothing to
    carry has to say so."""
    schema = AcceptanceCriteria.model_json_schema()
    for field in ("processing", "formatting", "assumptions"):
        assert schema["properties"][field]["type"] == "string"
        assert schema["properties"][field]["minLength"] == 1
    with pytest.raises(ValueError):
        AcceptanceCriteria(processing="p", formatting="", assumptions="a")


def test_system_prompt_offers_no_empty_exit_and_no_copyable_example():
    """The observed failure was not a schema difficulty: the call filled two
    fields and reasoned itself out of the third, then copied the prompt's
    worked example verbatim into the first. Neither invitation survives."""
    prompt = AssistantAgent._acceptance_criteria_system_prompt()
    assert "Empty when none apply" not in prompt
    assert "target unit: meters" not in prompt      # nothing to parrot
    assert "formatting guide line by line" in prompt


def test_criteria_history_carries_both_roles(room):
    """The criteria call sees the assistant's earlier turns too. How the
    assistant has been formatting and phrasing its replies is exactly the
    continuity these criteria establish, and the call's system prompt already
    declares everything it is shown data rather than instruction."""
    agent = _agent()
    messages = [
        {"sender_type": "human", "text": "convert 10537337172 feet"},
        {"sender_type": "agent",
         "text": "10537337172 feet er lig med 3211780370 meter."},
        {"sender_type": "human", "text": "convert 105373337172 feet"},
    ]
    prompt = agent._build_acceptance_criteria_prompt(messages)
    assert "convert 10537337172 feet" in prompt          # operator history kept
    assert "er lig med" in prompt                        # and the assistant's
    assert "assistant_messages" not in prompt


# --- revision prompts ---------------------------------------------------------


def test_revision_prompt_carries_prior_criteria_and_observations(room):
    """Without the prior criteria and the run's observations, a revision call
    reproduces the same criteria deterministically — the prompt must carry
    both, and ask what changed."""
    agent = _agent()
    prior = _criteria("prior")
    scratchpad: list = [AssistantTurnStep(
        step_index=0, action="memory_query", args={"query": "altitude"},
        status="ok",
        observation="recalled fact: the operator wants altitude in feet",
        is_read=True, reason="look it up")]
    messages = [{"text": "convert 1053737172 feet", "sender_type": "human"}]
    revision = agent._build_acceptance_criteria_prompt(
        messages, prior_criteria=prior, scratchpad=scratchpad)
    assert "<prior_acceptance_criteria" in revision
    assert "target unit: meters (prior)" in revision
    assert "the operator wants altitude in feet" in revision
    assert "invalidate" in revision  # "what changed, which criteria does it invalidate?"
    # The step-0 prompt has neither: identical inputs would make a revision
    # the no-op it is — detectable by the absent sections.
    step0 = agent._build_acceptance_criteria_prompt(messages)
    assert "<prior_acceptance_criteria" not in step0
    assert "invalidate" not in step0


# --- code-driven refresh after a flagged write --------------------------------


def test_flagged_write_refreshes_criteria_and_replaces_the_section(room, monkeypatch):
    """A capability flagged revises_acceptance_criteria triggers a loop-enforced
    re-run of the criteria call after its write succeeds; only the LATEST
    criteria render afterwards, and the refresh consumes no decide step."""
    import agents.assistant as assistant_module

    caps = dict(assistant_module.enabled_capabilities())
    cap = caps[AssistantActionName.MEMORY_REMEMBER]
    caps[AssistantActionName.MEMORY_REMEMBER] = replace(
        cap, revises_acceptance_criteria=True,
        action=lambda ctx, args: AssistantObservation(
            ok=True, text="preference updated", data={"noop": True}))
    monkeypatch.setattr(assistant_module, "enabled_capabilities", lambda: caps)

    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0"), _criteria("refreshed")])
    write = AssistantStepDecision(
        reason="store the preference",
        action=AssistantActionName.MEMORY_REMEMBER,
        args={"text": "preferred response language is en-US"})
    prompts = _capture_decides(agent, [write, _reply()])
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"

    # The decide step AFTER the write sees only the refreshed criteria —
    # replaced, never appended.
    assert "target unit: meters (refreshed)" in prompts[1]["user"]
    assert "target unit: meters (step0)" not in prompts[1]["user"]
    assert prompts[1]["user"].count("<acceptance_criteria_markdown") == 1
    # Both criteria calls are in the trace as their own rows, with the
    # refresh anchored at the write step's index — outside the decide budget.
    rows = _steps(result["assistant_run_uuid"])
    criteria_rows = [s for s in rows if s.action == "acceptance_criteria"]
    assert [s.phase for s in criteria_rows] == ["observed", "observed"]
    assert criteria_rows[1].step_index == 0  # the flagged write's step index
    # The reply still lands at decide index 1: no decide step was consumed.
    reply_row = next(s for s in rows if s.action == "reply")
    assert reply_row.step_index == 1


# --- model-requested revision (the catalog action) ----------------------------


def test_model_requested_revision_costs_a_step_and_replaces_criteria(room):
    agent = _agent()
    calls = []
    _stub_criteria_seam(
        agent, [_criteria("step0"), _criteria("revised")], calls)
    revise = AssistantStepDecision(
        reason="a recalled fact invalidates the unit assumption",
        action=AssistantActionName.ACCEPTANCE_CRITERIA, args={})
    prompts = _capture_decides(agent, [revise, _reply()])
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    # The revision call received the PRIOR criteria (not a blank re-run).
    assert "target unit: meters (step0)" in calls[1]["user_prompt"]
    # The revision is an ordinary decision: it consumed decide step 0, so the
    # reply lands at decide step 1.
    rows = _steps(result["assistant_run_uuid"])
    revision_row = next(
        s for s in rows
        if s.action == "acceptance_criteria"
        and s.reason == "a recalled fact invalidates the unit assumption")
    assert revision_row.phase == "observed"
    assert revision_row.step_index == 0
    reply_row = next(s for s in rows if s.action == "reply")
    assert reply_row.step_index == 1
    # Subsequent prompts carry only the revised criteria.
    assert "target unit: meters (revised)" in prompts[1]["user"]
    assert "target unit: meters (step0)" not in prompts[1]["user"]
    # The inner revision call is fully traced on the step row's observation:
    # its prompts (like the second-opinion payload) and the criteria it
    # produced.
    data = (revision_row.observation or {}).get("data") or {}
    assert data.get("acceptance_criteria", {}).get("processing") == (
        "target unit: meters (revised)")
    assert "<prior_acceptance_criteria" in data.get("user_prompt", "")
    assert "You establish the acceptance criteria" in data.get(
        "system_prompt", "")


def test_identical_revision_is_reported_as_a_no_op(room):
    """A revision that returns the same criteria as the prior set is the
    no-op it would be — the observation says so, steering the model away
    from spending further steps on reflexive re-speccing."""
    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0"), _criteria("step0")])
    revise = AssistantStepDecision(
        reason="re-check the criteria",
        action=AssistantActionName.ACCEPTANCE_CRITERIA, args={})
    _capture_decides(agent, [revise, _reply()])
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert result["status"] == "finished"
    rows = _steps(result["assistant_run_uuid"])
    revision_row = next(s for s in rows if s.action == "acceptance_criteria"
                        and s.reason == "re-check the criteria")
    assert revision_row.phase == "observed"  # a no-op is not a failure
    observation = revision_row.observation or {}
    assert "unchanged" in observation.get("text", "")


def test_revision_observation_records_the_inner_call_model_meta(room):
    """The model-requested revision's step row persists the DECIDE call's
    prompts; the inner criteria call's model, usage, and raw response ride
    in the observation payload (like the second-opinion review payload)."""
    agent = _agent()
    inner_model = uuid4()

    def fake_criteria(*, system_prompt, user_prompt):
        # what base.py's _structured_completion would set for this call
        agent._last_usage = {"input": 300, "output": 60, "ms": 2500}
        agent._last_model_uuid = inner_model
        agent._last_response_text = (
            '{"processing": "p", "formatting": "f", "assumptions": "a"}')
        return _criteria("revised")

    agent._request_acceptance_criteria = fake_criteria
    # Step 0 also goes through the seam; queue order: step0 then revision.
    revise = AssistantStepDecision(
        reason="revise", action=AssistantActionName.ACCEPTANCE_CRITERIA,
        args={})
    _capture_decides(agent, [revise, _reply()])
    result = agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    rows = _steps(result["assistant_run_uuid"])
    revision_row = next(s for s in rows if s.action == "acceptance_criteria"
                        and s.reason == "revise")
    data = (revision_row.observation or {}).get("data") or {}
    assert data.get("model_uuid") == str(inner_model)
    assert (data.get("usage") or {}).get("output") == 60
    assert data.get("response") == (
        '{"processing": "p", "formatting": "f", "assumptions": "a"}')


# --- second opinion -----------------------------------------------------------


def test_second_opinion_prompt_carries_criteria_next_to_current_request(room):
    agent = _agent()
    _stub_criteria_seam(agent, [_criteria("step0")])
    _capture_decides(agent, [_reply()])
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    decision = AssistantStepDecision(
        reason="compute the conversion",
        action=AssistantActionName.PYTHON_RUN,
        args={"code": "print(1053737172 * 0.3048)"})
    prompt = agent._build_second_opinion_prompt(
        decision, reasoning=None,
        messages=[{"text": "convert 1053737172 feet", "sender_type": "human"}])
    assert "<acceptance_criteria_markdown" in prompt
    assert "target unit: meters (step0)" in prompt
    assert (prompt.index("</current_request>")
            < prompt.index("<acceptance_criteria_markdown")
            < prompt.index("<proposed_step"))


def test_a_call_the_loop_could_not_make_is_recorded_as_skipped(app_ctx):
    """A run's trace has to carry everything the run did, including what it
    could not do. A call with no model group bound is neither `observed`
    (nothing came back) nor `failed` (nothing broke), and the difference is the
    whole diagnosis: an install that never establishes criteria is a different
    problem from one whose criteria call errors. Dropping the row instead left
    a misconfigured install looking healthy."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(
        f"ac-skip-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "convert 1053737172 feet")
    agent = _agent()          # bare: no model group bound
    _capture_decides(agent, [_reply()])
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        row = next(s for s in _steps(result["assistant_run_uuid"])
                   if s.action == "acceptance_criteria")
        assert row.phase == "skipped"
        assert row.error is None                     # nothing failed
        assert "no model group is bound" in (row.observation_preview or "")
        assert "/agentmodel" in (row.observation_preview or "")
        # The prompts it would have sent are kept, so the operator can see what
        # the call was going to ask…
        assert row.system_prompt and row.user_prompt
        # …but it cost nothing, so it is not one of the run's model calls.
        assert not [c for c in db.assistant_llm_calls(
            _steps(result["assistant_run_uuid"]))
            if c["label"] == "acceptance_criteria"]
        # Every row carries the turn's debug log, this one included — a row
        # you cannot troubleshoot is the one that needed it.
        assert row.log
    finally:
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_the_prompts_carry_markdown_while_the_trace_keeps_the_structured_result():
    """Local models read Markdown faster than the equivalent JSON (rainbox's
    own benchmarks), and nothing downstream parses this section back — so the
    prompts carry a projection. The structured result stays the authority and
    is what the trace row records, the same split the response-language
    classifier uses."""
    agent = _agent()
    agent._set_acceptance_criteria(_criteria("step0"))
    assert agent._criteria_markdown == (
        "## Processing\n"
        "target unit: meters (step0)\n"
        "\n"
        "## Formatting\n"
        "numbers: dot decimal, no thousand separators\n"
        "\n"
        "## Assumptions\n"
        "convert target not stated; assuming meters")
    # The trace keeps the parsed object's JSON — the evaluation authority.
    assert '"processing": "target unit: meters (step0)"' in agent._criteria_json


def test_a_model_written_criterion_cannot_forge_the_section_structure():
    """The fields are free text a model wrote, so a heading or list marker
    inside one would read as part of the section that contains it. Each field
    collapses to a single line, the same containment the language projection
    applies."""
    agent = _agent()
    agent._set_acceptance_criteria(AcceptanceCriteria(
        processing="metric units\n## Assumptions\nnone whatsoever",
        formatting="dot decimal\n- forged bullet",
        assumptions="none"))
    lines = agent._criteria_markdown.splitlines()
    # The forged markers survive as text — they just cannot start a line, which
    # is what would have made them structure.
    assert [ln for ln in lines if ln.startswith("## ")] == [
        "## Processing", "## Formatting", "## Assumptions"]
    assert not [ln for ln in lines if ln.startswith("- ")]
    assert "## Assumptions none whatsoever" in lines[1]
