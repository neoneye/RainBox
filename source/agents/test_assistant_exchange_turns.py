"""The exchange_turns prompt layout (chat/prompt_layout.py): request_turn, with
the user's previous message and the assistant's reply to it repeated as turns
of their own before the request — so a follow-up too short to name its
subject reads right after the exchange it continues. These tests only build
prompts, so they never touch a model."""
from uuid import uuid4

import pytest

from agents.assistant import EXCHANGE_TURNS_SECTION, REQUEST_MESSAGE_SECTION
from agents.test_assistant_prompt_tiers import (  # noqa: F401 — fixture
    fully_populated_agent,
    section_order,
)
from chat.prompt_layout import (
    EXCHANGE_TURNS,
    EXCHANGE_TURNS_ACKNOWLEDGEMENT,
    REQUEST_TURN,
    REQUEST_TURN_ACKNOWLEDGEMENT,
    latest_exchange,
    request_turns,
)

ASSISTANT = uuid4()
OTHER_AGENT = uuid4()


@pytest.fixture
def agent(fully_populated_agent):  # noqa: F811
    fully_populated_agent._prompt_layout = EXCHANGE_TURNS
    fully_populated_agent.agent_uuid = ASSISTANT
    return fully_populated_agent


def _messages(replier=ASSISTANT) -> list[dict]:
    return [
        {"sender_type": "human", "text": "what is the capital of Peru"},
        {"sender_type": "agent", "sender_uuid": str(ASSISTANT), "text": "Lima"},
        {"sender_type": "human", "text": "tell me about the moon"},
        {"sender_type": "agent", "sender_uuid": str(replier),
         "text": "It orbits the earth."},
        {"sender_type": "human", "text": "more details"},
    ]


def test_decide_repeats_the_latest_exchange_before_the_request(agent):
    context, turns = agent._build_decide_turns(
        messages=_messages(), scratchpad=[], step_index=0)

    assert turns == [
        {"role": "assistant", "content": EXCHANGE_TURNS_ACKNOWLEDGEMENT},
        {"role": "user", "content": "tell me about the moon"},
        {"role": "assistant", "content": "It orbits the earth."},
        {"role": "user", "content": "more details"},
    ]
    # The history is unchanged: it still ends in the exchange, so every call
    # of the turn — those that send no turns included — shares one head.
    history = context[context.index("<conversation_history_xml>"):
                      context.index("</conversation_history_xml>")]
    assert "tell me about the moon" in history
    assert "It orbits the earth." in history
    assert section_order(context)[0] == "conversation_history_xml"


def test_the_instructions_say_what_the_exchange_turns_are(agent):
    context, _turns = agent._build_decide_turns(
        messages=_messages(), scratchpad=[], step_index=0)
    assert REQUEST_MESSAGE_SECTION in context
    assert EXCHANGE_TURNS_SECTION in context

    criteria, turns = agent._build_acceptance_criteria_turns(_messages())
    assert EXCHANGE_TURNS_SECTION in criteria
    assert turns[1:] == [
        {"role": "user", "content": "tell me about the moon"},
        {"role": "assistant", "content": "It orbits the earth."},
        {"role": "user", "content": "more details"},
    ]


def test_a_reply_from_another_agent_is_not_repeated(agent):
    """In a group room the message before the request may be someone else's;
    it is not this model's turn, so the shape falls back to request_turn's."""
    _context, turns = agent._build_decide_turns(
        messages=_messages(replier=OTHER_AGENT), scratchpad=[], step_index=0)
    assert turns == request_turns("more details")


def test_request_turn_is_unchanged(agent):
    agent._prompt_layout = REQUEST_TURN
    context, turns = agent._build_decide_turns(
        messages=_messages(), scratchpad=[], step_index=0)
    assert turns == [
        {"role": "assistant", "content": REQUEST_TURN_ACKNOWLEDGEMENT},
        {"role": "user", "content": "more details"},
    ]
    assert EXCHANGE_TURNS_SECTION not in context


def test_a_long_reply_is_shortened_as_in_the_history(agent):
    limit = agent.HISTORY_MESSAGE_MAX_CHARS
    messages = _messages()
    messages[-2] = {**messages[-2], "text": "x" * (limit * 2)}
    _context, turns = agent._build_decide_turns(
        messages=messages, scratchpad=[], step_index=0)
    assert len(turns[2]["content"]) < limit * 2
    assert turns[2]["content"].startswith("x")


def test_latest_exchange_needs_the_user_then_this_agent():
    msgs = _messages()
    assert latest_exchange(msgs, ASSISTANT) == (msgs[2], msgs[3])
    assert latest_exchange(msgs, OTHER_AGENT) is None
    assert latest_exchange(msgs[-2:], ASSISTANT) is None
    two_humans = msgs[:3] + [{"sender_type": "human", "text": "x"}, msgs[-1]]
    assert latest_exchange(two_humans, ASSISTANT) is None
