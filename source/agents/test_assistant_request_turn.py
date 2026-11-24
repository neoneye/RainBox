"""The request_turn prompt layout (chat/prompt_layout.py): the request leaves
tier 0 in every call, travels as its own final message in the calls whose
subject it is (decide, acceptance criteria), and sits as a section just
before the subject in the calls that judge something else.

The single_turn layout is covered by test_assistant_prompt_tiers.py; these
tests only build prompts, so they never touch a model."""
import pytest

from agents.assistant import (
    ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS,
    REQUEST_MESSAGE_SECTION,
    AssistantActionName,
    AssistantStepDecision,
    AssistantTurnStep,
    _rewrite_for_request_turn,
)
from agents.test_assistant_prompt_tiers import (  # noqa: F401 — fixture
    fully_populated_agent,
    section_order,
)
from chat.prompt_layout import (
    REQUEST_TURN,
    REQUEST_TURN_ACKNOWLEDGEMENT,
    SINGLE_TURN,
)

MULTILINE_REQUEST = "convert these:\n  12 ft\n  3 mi"


@pytest.fixture
def agent(fully_populated_agent):  # noqa: F811
    fully_populated_agent._prompt_layout = REQUEST_TURN
    return fully_populated_agent


def _messages(text: str = MULTILINE_REQUEST) -> list[dict]:
    return [
        {"sender_type": "human", "text": "what is 2+2"},
        {"sender_type": "agent", "text": "4"},
        {"sender_type": "human", "text": text},
    ]


def test_decide_sends_the_request_verbatim_as_the_final_turn(agent):
    context, turns = agent._build_decide_turns(
        messages=_messages(), scratchpad=[], step_index=0)

    assert turns == [
        {"role": "assistant", "content": REQUEST_TURN_ACKNOWLEDGEMENT},
        {"role": "user", "content": MULTILINE_REQUEST},
    ]
    order = section_order(context)
    assert "current_user_request" not in order
    # History leads now: it is the tier-0 invariant every call shares.
    assert order[0] == "conversation_history_xml"
    # No quote to re-anchor: the request is the next message.
    assert "12 ft" not in context


def test_decide_instructions_name_the_request_message(agent):
    context, _turns = agent._build_decide_turns(
        messages=_messages(), scratchpad=[], step_index=0)

    assert REQUEST_MESSAGE_SECTION in context
    assert "<source rank=\"2\">current_user_request</source>" not in context
    assert "the request message (the final user message)" in context
    decision = context[context.index("<decision_request"):]
    assert "the request message, the final user message" in decision


def test_criteria_sends_the_request_as_the_final_turn(agent):
    context, turns = agent._build_acceptance_criteria_turns(_messages())

    assert turns[-1] == {"role": "user", "content": MULTILINE_REQUEST}
    assert "current_user_request" not in section_order(context)
    assert ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS in context
    assert REQUEST_MESSAGE_SECTION in context


def test_single_turn_sends_no_extra_turns(fully_populated_agent):  # noqa: F811
    fully_populated_agent._prompt_layout = SINGLE_TURN
    context, turns = fully_populated_agent._build_decide_turns(
        messages=_messages(), scratchpad=[], step_index=0)

    assert turns == []
    assert section_order(context)[0] == "current_user_request"
    assert REQUEST_MESSAGE_SECTION not in context


def test_judging_calls_place_the_request_just_before_their_subject(agent):
    messages = _messages()
    decision = AssistantStepDecision(
        reason="sum", action=AssistantActionName.PYTHON_RUN,
        args={"code": "print(1)"})
    second = section_order(agent._build_second_opinion_prompt(
        decision, reasoning=None, messages=messages))
    audit = section_order(agent._build_reply_audit_prompt(
        "it is 4", messages=messages, scratchpad=[]))
    classifier = section_order(agent._build_response_language_classifier_prompt(
        messages, {"data": {}}))

    for order in (second, audit, classifier):
        assert order[0] == "conversation_history_xml"
        assert order.count("current_user_request") == 1
    assert second[second.index("current_user_request") + 1] == "proposed_step"
    assert audit[audit.index("current_user_request") + 1] == "proposed_reply"
    assert (classifier[classifier.index("current_user_request") + 1]
            == "classification_request")


def test_recall_filter_prefix_ends_with_the_request(agent):
    order = section_order(agent._recall_filter_prefix(_messages()))

    assert order[0] == "conversation_history_xml"
    assert order[-1] == "current_user_request"


def test_the_calls_that_read_both_roles_share_the_history_head(agent):
    """The request left tier 0 in every call at once, so the calls' prompts
    still open with the same bytes: the history, then the identity block.
    The criteria call reads the user's messages only and so opens with its
    own history."""
    messages = _messages()
    decide, _ = agent._build_decide_turns(
        messages=messages, scratchpad=[], step_index=0)
    criteria, _ = agent._build_acceptance_criteria_turns(messages)
    audit = agent._build_reply_audit_prompt(
        "it is 4", messages=messages, scratchpad=[])
    head = decide[:decide.index("<user_expertise_yaml")]

    assert audit.startswith(head)
    assert not criteria.startswith(head)
    assert 'role="assistant"' not in criteria


def test_long_request_is_cut_and_described_in_the_context(agent):
    agent.CURRENT_REQUEST_MAX_CHARS = 400
    agent._long_request_summary_markdown = "a long list of distances"
    request = "x" * 1000 + "MIDDLE" + "y" * 1000

    context, turns = agent._build_decide_turns(
        messages=_messages(request), scratchpad=[], step_index=0)

    sent = turns[-1]["content"]
    assert len(sent) < len(request)
    assert "MIDDLE" not in sent
    order = section_order(context)
    assert order.index("current_user_request_cut") < order.index(
        "decision_request")
    assert order.index("current_user_request_summary_markdown") == (
        order.index("current_user_request_cut") + 1)
    assert 'original_chars="2006"' in context


def test_the_request_section_appears_once_after_a_scratchpad_step(agent):
    step = AssistantTurnStep(
        step_index=0, action="memory_query", args={"query": "x"},
        status="ok", observation="nothing", reason="look")
    context, turns = agent._build_decide_turns(
        messages=_messages(), scratchpad=[step], step_index=1)

    assert "current_user_request" not in section_order(context)
    assert turns[-1]["content"] == MULTILINE_REQUEST


def test_a_reworded_instruction_fails_loudly():
    with pytest.raises(ValueError, match="expected one"):
        _rewrite_for_request_turn("nothing to rewrite", (("absent", "x"),))
