"""The chat agents' prompt in each layout (chat/prompt_layout.py). Retrieval
is stubbed at `_context_parts`, so these build prompts without a database."""
from uuid import uuid4

import pytest

import agents.chat_context as chat_context
from agents.chat_context import (
    CHAT_EXCHANGE_TURNS_SHAPE,
    CHAT_REQUEST_TURN_SHAPE,
    CHAT_SINGLE_TURN_SHAPE,
    build_chat_prompt,
    chat_system_prompt,
)
from agents.chat_structured import CHAT_SYSTEM_PROMPT
from agents.chat_unstructured import UNSTRUCTURED_CHAT_SYSTEM_PROMPT
from chat.prompt_layout import (
    EXCHANGE_TURNS,
    EXCHANGE_TURNS_ACKNOWLEDGEMENT,
    REQUEST_TURN,
    REQUEST_TURN_ACKNOWLEDGEMENT,
    SINGLE_TURN,
)

MESSAGES = [
    {"sender_name": "user", "text": "hi"},
    {"sender_name": "chatagent", "text": "Hi!"},
    {"sender_name": "user", "text": "two lines:\nsecond"},
]


@pytest.fixture(autouse=True)
def stub_retrieval(monkeypatch):
    monkeypatch.setattr(
        chat_context, "_context_parts",
        lambda *a, **k: ("PROFILE", "Curated facts\n- SEED", "MEMORY",
                         "the query", []))


def _build(layout):
    return build_chat_prompt(
        MESSAGES, agent_uuid=uuid4(), room_uuid=uuid4(), layout=layout)


def test_single_turn_is_one_message_ending_in_the_current_message():
    prompt = _build(SINGLE_TURN)

    assert prompt.followup_turns == []
    text = prompt.user_prompt
    assert text.index("PROFILE") < text.index("MEMORY") < text.index("<user> hi")
    assert text.endswith("Current message:\n<user> two lines:\\nsecond")


def test_request_turn_sends_the_current_message_verbatim():
    prompt = _build(REQUEST_TURN)

    assert prompt.followup_turns == [
        {"role": "assistant", "content": REQUEST_TURN_ACKNOWLEDGEMENT},
        {"role": "user", "content": "two lines:\nsecond"},
    ]
    assert "two lines" not in prompt.user_prompt
    assert "Current message:" not in prompt.user_prompt


def test_request_turn_orders_the_context_most_stable_first():
    text = _build(REQUEST_TURN).user_prompt

    profile, history = text.index("PROFILE"), text.index("<chatagent> Hi!")
    seed, memory = text.index("SEED"), text.index("MEMORY")
    sender = text.index("The Current message follows")
    assert profile < history < seed < memory < sender
    # Two fences: the transcript sits between the profile and the recall.
    assert text.count("PROFILE") == 1
    assert text.count("</") >= 2


@pytest.mark.parametrize(
    "base", [CHAT_SYSTEM_PROMPT, UNSTRUCTURED_CHAT_SYSTEM_PROMPT])
def test_system_prompt_describes_the_layout(base):
    assert chat_system_prompt(base, SINGLE_TURN) == base
    swapped = chat_system_prompt(base, REQUEST_TURN)
    assert CHAT_SINGLE_TURN_SHAPE not in swapped
    assert CHAT_REQUEST_TURN_SHAPE in swapped


def test_a_system_prompt_without_the_shape_paragraph_fails_loudly():
    with pytest.raises(ValueError):
        chat_system_prompt("no paragraph here", REQUEST_TURN)


def test_exchange_turns_repeats_this_agents_latest_exchange():
    agent_uuid = uuid4()
    messages = [
        {"sender_name": "user", "sender_type": "human", "text": "hi"},
        {"sender_name": "chatagent", "sender_type": "agent",
         "sender_uuid": str(agent_uuid), "text": "Hi!"},
        {"sender_name": "user", "sender_type": "human", "text": "more"},
    ]
    prompt = build_chat_prompt(messages, agent_uuid=agent_uuid,
                               room_uuid=uuid4(), layout=EXCHANGE_TURNS)
    assert prompt.followup_turns == [
        {"role": "assistant", "content": EXCHANGE_TURNS_ACKNOWLEDGEMENT},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "more"},
    ]
    # Someone else's reply is not this agent's turn.
    other = build_chat_prompt(messages, agent_uuid=uuid4(),
                              room_uuid=uuid4(), layout=EXCHANGE_TURNS)
    assert other.followup_turns == [
        {"role": "assistant", "content": REQUEST_TURN_ACKNOWLEDGEMENT},
        {"role": "user", "content": "more"},
    ]


@pytest.mark.parametrize("base", [CHAT_SYSTEM_PROMPT, UNSTRUCTURED_CHAT_SYSTEM_PROMPT])
def test_exchange_turns_system_prompt_describes_the_exchange(base):
    prompt = chat_system_prompt(base, EXCHANGE_TURNS)
    assert CHAT_EXCHANGE_TURNS_SHAPE in prompt
    assert CHAT_SINGLE_TURN_SHAPE not in prompt
