"""A wake turn: the assistant is started by a `wake` row (chat/wake.py), not
by a message from the user. The notice sits where the request would, every
call that reads the request is told what it is, and the row never joins the
conversation."""

from unittest.mock import patch
from uuid import uuid4

import pytest

import db
from agents.assistant import WAKE_SECTION
from agents.config import ASSISTANT_UUID
from agents.test_assistant_acceptance_criteria import (
    _agent, _capture_decides, _criteria, _reply, _stub_criteria_seam,
)
from chat.prompt_layout import EXCHANGE_TURNS, SINGLE_TURN
from chat.wake import WAKE_KIND, continue_meta, continue_notice


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.session.rollback()
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    """A room where the user asked and the assistant answered."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(
        f"wake-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "hvad er en fjord")
    db.post_chat_message(chatroom.uuid, ASSISTANT_UUID, "En fjord er en havarm.")
    try:
        yield chatroom
    finally:
        db.session.rollback()
        db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == chatroom.uuid).delete()
        db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.session.commit()


def _wake(room, index=1, count=1):
    return db.post_chat_message(
        room.uuid, db.get_human_user().uuid, continue_notice(index, count),
        kind=WAKE_KIND, meta=continue_meta(index, count))


def _turn(room, message_uuid, layout=SINGLE_TURN):
    agent = _agent()
    criteria_calls = []
    _stub_criteria_seam(agent, [_criteria("step0")], criteria_calls)
    prompts = _capture_decides(agent, [_reply("Og de er dannet af gletsjere.")])
    with patch("agents.assistant.current_prompt_layout", return_value=layout):
        result = agent.handle(uuid4(), {
            "room_uuid": str(room.uuid), "message_uuid": str(message_uuid)})
    return agent, criteria_calls, prompts, result


def test_the_notice_takes_the_request_s_place_and_every_call_is_told(room):
    wake = _wake(room, 2, 3)
    _agent_, criteria_calls, prompts, _result = _turn(room, wake.uuid)
    decide, criteria = prompts[0]["user"], criteria_calls[0]["user_prompt"]
    for prompt in (decide, criteria):
        assert WAKE_SECTION in prompt
        assert "<current_user_request>Wake-up notice:" in prompt
        assert "wake-up 2 of 3" in prompt
        assert "There is no request from the user this turn" in prompt
    # The conversation is what it was: the notice is not a message in it.
    history = decide.split("<conversation_history_xml>")[1].split(
        "</conversation_history_xml>")[0]
    assert "hvad er en fjord" in history and "En fjord er en havarm." in history
    assert "Wake-up notice" not in history
    assert "answer that request" not in decide


def test_a_turn_a_message_started_says_nothing_about_waking(room):
    human = db.get_human_user()
    asked = db.post_chat_message(room.uuid, human.uuid, "og en vig?")
    _agent_, criteria_calls, prompts, _result = _turn(room, asked.uuid)
    assert "woken" not in prompts[0]["user"]
    assert "woken" not in criteria_calls[0]["user_prompt"]


def test_an_earlier_wake_is_not_read_by_a_later_turn(room):
    _wake(room)
    human = db.get_human_user()
    asked = db.post_chat_message(room.uuid, human.uuid, "og en vig?")
    _agent_, _criteria_calls, prompts, _result = _turn(room, asked.uuid)
    assert "Wake-up notice" not in prompts[0]["user"]


def test_the_run_names_the_wake_as_what_started_it(room):
    wake = _wake(room)
    _agent_, _calls, _prompts, result = _turn(room, wake.uuid)
    run = db.get_assistant_run(result["assistant_run_uuid"])
    trigger = db.get_run_trigger_message(run)
    assert trigger["uuid"] == str(wake.uuid)
    assert db.assistant_run_for_request(wake.uuid).uuid == run.uuid


def test_the_reply_language_is_read_from_the_user_s_latest_message(room):
    """The notice is system text in one fixed language; classifying it would
    answer a Danish conversation in the notice's language."""
    wake = _wake(room)
    agent = _agent()
    seen = []
    agent._run_response_language_classifier = (
        lambda *, step_index, messages, profile: seen.append(messages))
    _stub_criteria_seam(agent, [_criteria("step0")])
    _capture_decides(agent, [_reply()])
    agent.handle(uuid4(), {
        "room_uuid": str(room.uuid), "message_uuid": str(wake.uuid)})
    assert seen[0][-1]["text"] == "hvad er en fjord"


def test_the_exchange_layout_sends_the_notice_as_the_final_message(room):
    wake = _wake(room)
    # The room as the turn reads it, taken before the turn adds its reply.
    everything = db.list_room_messages(room.uuid)
    rows = ([m for m in everything if m["kind"] == "message"]
            + [m for m in everything if m["kind"] == WAKE_KIND])
    agent, _calls, _prompts, _result = _turn(room, wake.uuid, EXCHANGE_TURNS)
    anchor = agent._request_anchor(agent._wake)
    assert "the final user message of this call is the wake-up notice" in anchor
    _user, turns = agent._build_decide_turns(
        messages=rows, scratchpad=[], step_index=0)
    assert turns[-1] == {"role": "user", "content": continue_notice(1, 1)}
    # The exchange before it is the user's message and the reply to it.
    assert [t["content"] for t in turns[1:3]] == [
        "hvad er en fjord", "En fjord er en havarm."]


def _read_decision(query):
    from agents.assistant import AssistantActionName, AssistantStepDecision
    return AssistantStepDecision(
        reason="look", action=AssistantActionName.MEMORY_QUERY,
        args={"query": query})


def test_a_wake_turn_gets_one_read(room):
    """Nobody asked anything, so no read answers a wake-up; told to say
    something new, the model kept searching in other words."""
    from agents.assistant import enabled_capabilities
    agent = _agent()
    agent._caps = enabled_capabilities()
    agent._wake = {"text": continue_notice(1, 1)}
    agent._turn_reads = {}
    agent._decide_validator()(_read_decision("fjords"))     # the first read runs
    agent._turn_reads = {"memory_query:fjords": 0}
    validate = agent._decide_validator()
    with pytest.raises(ValueError, match="has had its read"):
        validate(_read_decision("glaciers"))
    validate(_reply())                                      # a reply is never refused


def test_a_turn_a_message_started_reads_as_often_as_it_needs(room):
    from agents.assistant import enabled_capabilities
    agent = _agent()
    agent._caps = enabled_capabilities()
    agent._wake = None
    agent._turn_reads = {"memory_query:fjords": 0}
    agent._decide_validator()(_read_decision("glaciers"))
