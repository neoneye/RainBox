"""Tests for the assistant's per-call model slots.

One `assistant.*` binding per model call the assistant makes, each resolving
through `<slot> -> assistant.default`. The slot names are the same strings the
calls label themselves with on /activity, which is what lets an operator bind a
row and then read that row's cost back.
"""

from uuid import UUID, uuid4

import pytest

import db
import agents.model_groups as mg
from agents.assistant import AssistantAgent
from agents.config import (
    ASSISTANT_ACCEPTANCE_CRITERIA_UUID,
    ASSISTANT_DECIDE_UUID,
    ASSISTANT_DEFAULT_UUID,
    ASSISTANT_MEMORY_FILTER_UUID,
    ASSISTANT_REPLY_AUDIT_UUID,
    ASSISTANT_REQUEST_SUMMARY_UUID,
    ASSISTANT_RESPONSE_LANGUAGE_CLASSIFIER_UUID,
    ASSISTANT_RUN_SUMMARIZER_UUID,
    ASSISTANT_SECOND_OPINION_UUID,
    ASSISTANT_UUID,
    agent_config,
    role_name,
)

# Every slot except the default, which is the thing they all fall back to.
STEP_SLOTS = [
    ASSISTANT_DECIDE_UUID,
    ASSISTANT_ACCEPTANCE_CRITERIA_UUID,
    ASSISTANT_REQUEST_SUMMARY_UUID,
    ASSISTANT_MEMORY_FILTER_UUID,
    ASSISTANT_SECOND_OPINION_UUID,
    ASSISTANT_REPLY_AUDIT_UUID,
    ASSISTANT_RESPONSE_LANGUAGE_CLASSIFIER_UUID,
    ASSISTANT_RUN_SUMMARIZER_UUID,
]


def _bind(monkeypatch, bound: dict[UUID, UUID]):
    """Pretend `bound` (agent uuid -> group uuid) is what /agentmodel holds.
    Each group has one member, named after its group so a test can tell which
    group answered from the member list alone."""
    monkeypatch.setattr(
        mg.db, "get_agent_model_binding",
        lambda agent_uuid: (
            type("B", (), {"model_group_uuid": bound[agent_uuid]})()
            if agent_uuid in bound else None))
    monkeypatch.setattr(
        mg.db, "get_model_group_member_uuids", lambda group_uuid: [group_uuid])


# --- the roster ---------------------------------------------------------------


def test_the_slot_roster_is_exactly_the_assistants_calls():
    """Locked deliberately: a new model call in the assistant needs a slot, and
    a slot with no call behind it is a control that binds nothing."""
    assert {n for n in agent_config if n.startswith("assistant.")} == {
        "assistant.default",
        "assistant.decide",
        "assistant.acceptance_criteria",
        "assistant.request_summary",
        "assistant.memory_filter",
        "assistant.second_opinion",
        "assistant.reply_audit",
        "assistant.response_language_classifier",
        "assistant.run_summarizer",
    }


def test_slot_names_are_the_activity_caller_tags():
    """The design's load-bearing claim: /agentmodel and /activity use ONE
    vocabulary, so the row an operator binds is the row they read the cost of.
    `_caller_tag` builds the /activity label from the agent name + purpose."""
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                           send=lambda _: None)
    for purpose in ("decide", "acceptance_criteria", "request_summary",
                    "memory_filter", "second_opinion", "reply_audit",
                    "response_language_classifier"):
        assert agent._caller_tag(purpose) in agent_config, purpose


def test_every_slot_requires_structured_output():
    """Every call behind these slots parses into a Pydantic model, so a group
    that does not promise structured output must not be offerable for one."""
    for name, entry in agent_config.items():
        if name.startswith("assistant."):
            assert entry.get("requires_structured_output") is True, name


def test_role_name_maps_a_slot_uuid_back_to_its_name():
    assert role_name(ASSISTANT_DECIDE_UUID) == "assistant.decide"
    assert role_name(ASSISTANT_DEFAULT_UUID) == "assistant.default"
    assert role_name(uuid4()) is None


# --- the two-link chain -------------------------------------------------------


@pytest.mark.parametrize("slot", STEP_SLOTS, ids=role_name)
def test_an_unbound_slot_falls_back_to_the_default(slot, monkeypatch):
    """Binding only assistant.default configures the whole assistant."""
    default_group = uuid4()
    _bind(monkeypatch, {ASSISTANT_DEFAULT_UUID: default_group})
    assert mg.resolve_assistant_model_group(slot) == (
        default_group, "assistant.default")


@pytest.mark.parametrize("slot", STEP_SLOTS, ids=role_name)
def test_a_bound_slot_beats_the_default(slot, monkeypatch):
    """Binding one slot moves exactly one call."""
    slot_group, default_group = uuid4(), uuid4()
    _bind(monkeypatch,
          {slot: slot_group, ASSISTANT_DEFAULT_UUID: default_group})
    assert mg.resolve_assistant_model_group(slot) == (
        slot_group, role_name(slot))


def test_a_slot_bound_to_an_empty_group_falls_through(monkeypatch):
    """A group with no members cannot answer a call, so it is not an answer —
    the same rule the generic resolver applies to every chain."""
    empty_group, default_group = uuid4(), uuid4()
    monkeypatch.setattr(
        mg.db, "get_agent_model_binding",
        lambda agent_uuid: type("B", (), {"model_group_uuid": (
            empty_group if agent_uuid == ASSISTANT_DECIDE_UUID
            else default_group)})())
    monkeypatch.setattr(
        mg.db, "get_model_group_member_uuids",
        lambda group_uuid: [] if group_uuid == empty_group else [uuid4()])
    _group, label = mg.resolve_assistant_model_group(ASSISTANT_DECIDE_UUID)
    assert label == "assistant.default"


def test_nothing_bound_anywhere_resolves_to_nothing(monkeypatch):
    """Not an error: each call site turns this into its own outcome — the loop
    raises, the optional calls record a skipped step."""
    _bind(monkeypatch, {})
    assert mg.resolve_assistant_model_group(ASSISTANT_DECIDE_UUID) == (
        None, None)
    assert mg.resolve_assistant_model_uuids(ASSISTANT_DECIDE_UUID) == (
        None, None)


def test_the_default_does_not_chain_to_itself(monkeypatch):
    """The default is the end of the chain. Asking for it consults exactly one
    binding — a second lookup would be the same row answering twice."""
    asked: list[UUID] = []

    def record(agent_uuid):
        asked.append(agent_uuid)
        return None

    monkeypatch.setattr(mg.db, "get_agent_model_binding", record)
    assert mg.resolve_assistant_model_group(ASSISTANT_DEFAULT_UUID) == (
        None, None)
    assert asked == [ASSISTANT_DEFAULT_UUID]


# --- the call sites -----------------------------------------------------------


def test_the_decide_call_runs_on_the_decide_slot(monkeypatch):
    """The loop's own call is the hot path — one per step against everyone
    else's one per turn — so it is the slot an operator moves first."""
    decide_group, default_group = uuid4(), uuid4()
    _bind(monkeypatch,
          {ASSISTANT_DECIDE_UUID: decide_group,
           ASSISTANT_DEFAULT_UUID: default_group})
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: [group_uuid])
    seen: dict = {}

    def fake_completion(*, candidate_model_uuids=None, purpose=None, **_kw):
        seen.update(models=candidate_model_uuids, purpose=purpose)
        raise RuntimeError("stop after the model choice")

    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                           send=lambda _: None)
    agent.setup()
    monkeypatch.setattr(agent, "_structured_completion", fake_completion)
    monkeypatch.setattr(agent, "_build_user_prompt", lambda **_kw: "u")
    monkeypatch.setattr(agent, "_system_prompt", lambda: "s")
    with pytest.raises(RuntimeError):
        agent._decide_next_step(messages=[], scratchpad=[], step_index=0)
    assert seen == {"models": [decide_group], "purpose": "decide"}
    # setup() resolved the default, which is what the paths that never name a
    # slot fall back to.
    assert agent.model_group_uuid == default_group


def test_a_step_row_records_the_group_its_own_call_used(monkeypatch):
    """The experiment is read off the trace afterwards, so a row must name the
    group THAT call ran on. Recording the agent's ambient group would name the
    wrong one for every call whose slot is bound away from the default."""
    criteria_group, audit_group, default_group = uuid4(), uuid4(), uuid4()
    _bind(monkeypatch, {
        ASSISTANT_ACCEPTANCE_CRITERIA_UUID: criteria_group,
        ASSISTANT_REPLY_AUDIT_UUID: audit_group,
        ASSISTANT_DEFAULT_UUID: default_group,
    })
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: [group_uuid])
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                           send=lambda _: None)
    agent.setup()
    assert agent._slot_group(ASSISTANT_ACCEPTANCE_CRITERIA_UUID) == criteria_group
    assert agent._slot_group(ASSISTANT_REPLY_AUDIT_UUID) == audit_group
    # …and a slot with nothing of its own reports the group it inherits.
    assert agent._slot_group(ASSISTANT_REQUEST_SUMMARY_UUID) == default_group


def test_the_decide_rows_record_the_group_the_decide_call_used(monkeypatch):
    """Its step rows are written after the call returns; re-resolving there
    could answer with a group the call never touched."""
    decide_group, default_group = uuid4(), uuid4()
    _bind(monkeypatch,
          {ASSISTANT_DECIDE_UUID: decide_group,
           ASSISTANT_DEFAULT_UUID: default_group})
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: [group_uuid])
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                           send=lambda _: None)
    agent.setup()
    assert agent._decide_group_uuid is None   # no decide call yet
    monkeypatch.setattr(agent, "_structured_completion",
                        lambda **_kw: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(agent, "_build_user_prompt", lambda **_kw: "u")
    monkeypatch.setattr(agent, "_system_prompt", lambda: "s")
    with pytest.raises(RuntimeError):
        agent._decide_next_step(messages=[], scratchpad=[], step_index=0)
    assert agent._decide_group_uuid == decide_group


def test_a_pinned_group_overrides_every_slot(monkeypatch):
    """What the profile-guidance eval needs: hold the model fixed so a result
    is attributable to the prompt and not to which slot a call happened to
    resolve."""
    pinned, decide_group = uuid4(), uuid4()
    _bind(monkeypatch, {ASSISTANT_DECIDE_UUID: decide_group})
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: [group_uuid])
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant",
                           send=lambda _: None)
    agent.pin_model_group(pinned)
    agent.setup()   # must not undo the pin
    for slot in STEP_SLOTS:
        models, group, label = agent._slot_models(slot)
        assert (models, group, label) == ([pinned], pinned, "pinned")


def test_the_run_summarizer_falls_back_to_the_default(monkeypatch):
    """The one slot that is also a spawned agent. Without its own fallback it
    would be the single call left unconfigured after setting the default."""
    from agents.assistant_run_summarizer import AssistantRunSummarizerAgent

    default_group = uuid4()
    _bind(monkeypatch, {ASSISTANT_DEFAULT_UUID: default_group})
    monkeypatch.setattr(
        db, "get_model_group_member_uuids", lambda group_uuid: [group_uuid])
    agent = AssistantRunSummarizerAgent(
        agent_uuid=ASSISTANT_RUN_SUMMARIZER_UUID,
        name="assistant.run_summarizer", send=lambda _: None)
    agent.setup()
    assert agent.model_group_uuid == default_group
    assert agent.candidate_model_uuids == [default_group]


def test_the_assistant_itself_has_no_binding():
    """`assistant` is the worker (chat identity, class path); its models come
    from the slots. A row of its own would be a control nothing reads."""
    assert AssistantAgent.uses_model_group is False
