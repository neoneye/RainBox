"""The 'working on it' progress row is posted at enqueue time (see
webapp._maybe_trigger_chat_agents) so the operator sees it before the agent
process spawns. The assistant's terminal reply must reap it when the real reply
lands."""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, ChatMessage
from agents.assistant import AssistantActionName, AssistantAgent, AssistantStepDecision
from agents.config import ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE


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


def _progress_count(room_uuid):
    return db.db.session.query(ChatMessage).filter_by(
        room_uuid=room_uuid, kind="progress").count()


def test_enqueue_time_progress_survives_the_run_and_is_reaped(app_ctx):
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"prog-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "what kanban boards do you see")
    # The enqueue-time progress bubble (posted by the webapp before the agent
    # spawns); handle() must leave it visible through the run, then the reply
    # reaps it.
    db.set_setting("qa.facts_invalidated_at", None)  # no invalidation marker this turn
    db.post_chat_message(chatroom.uuid, ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE, kind="progress")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    seen = {}

    def fake_decide(**_kwargs):
        # By the time the model is consulted, the operator already has a signal.
        seen["progress_during_first_call"] = _progress_count(chatroom.uuid)
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok"})

    agent._decide_next_step = fake_decide
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert seen["progress_during_first_call"] >= 1   # picked-up signal was already visible
        assert _progress_count(chatroom.uuid) == 0        # reaped by the real reply
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_facts_marker_does_not_leave_the_operator_without_a_progress_signal(app_ctx):
    """The facts-invalidation notice is kind='message' — a terminal kind whose
    side effect reaps the sender's progress rows, including the enqueue-time
    'working on it' bubble. handle() must re-post the bubble right after the
    marker, so the operator keeps a signal through the (long) model calls."""
    from datetime import UTC, datetime

    human = db.get_human_user()
    chatroom = db.create_chatroom(f"prog-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "what games have I played")
    db.post_chat_message(chatroom.uuid, ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE, kind="progress")
    # Fresh invalidation stamp -> the marker WILL post this turn.
    db.set_setting("qa.facts_invalidated_at", datetime.now(UTC).isoformat())
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    seen = {}

    def fake_decide(**_kwargs):
        seen["progress_during_first_call"] = _progress_count(chatroom.uuid)
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok"})

    agent._decide_next_step = fake_decide
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        marker_posted = any(
            (m.get("meta") or {}).get("facts_invalidation")
            for m in db.list_room_messages(chatroom.uuid)
        )
        assert marker_posted, "precondition: the facts marker posted this turn"
        assert seen["progress_during_first_call"] >= 1, (
            "the marker reaped the working bubble and nothing re-posted it — "
            "no progress signal during the model call"
        )
        assert _progress_count(chatroom.uuid) == 0  # final reply reaps as usual
    finally:
        db.set_setting("qa.facts_invalidated_at", None)
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_each_step_boundary_emits_immediate_liveness(app_ctx):
    """Completed steps reset the watchdog; it must not become a whole-run timer."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(
        f"step-heartbeat-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "answer this")
    db.set_setting("qa.facts_invalidated_at", None)
    sent = []
    agent = AssistantAgent(
        agent_uuid=ASSISTANT_UUID, name="assistant", send=sent.append)
    agent.HEARTBEAT_INTERVAL = 999
    decisions = iter([
        AssistantStepDecision(
            reason="invalid first try", action=AssistantActionName.REPLY, args={}),
        AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY,
            args={"message": "done"}),
    ])
    agent._decide_next_step = lambda **_kwargs: next(decisions)
    try:
        result = agent._handle_with_heartbeat(
            uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert result["status"] == "finished"
        activities = [
            message.get("activity") for message in sent
            if message.get("status") == "heartbeat"
        ]
        # Every phase of the turn reports itself, including the calls made
        # before and after the decide loop — a run held up in the classifier
        # or the audit is working, not hung, and the watchdog must be able to
        # tell. Both decide steps appear, so a completed step resets the timer
        # rather than the whole run sharing one silence budget.
        assert activities == [
            "classifying response language",
            "establishing acceptance criteria",
            "deciding step 0",
            "deciding step 1",
            "auditing the reply",
        ]
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_one_progress_row_carries_the_run_state_and_links_to_the_trace(app_ctx):
    """A run used to narrate itself with a chat row per step — a `thinking`
    bubble and a `debug-assistant` bubble each time — so one turn buried the
    conversation under a dozen rows. The room now carries ONE progress row,
    rewritten in place: where the run is, what it has cost, and a link to
    /assistant where the full trace already lives."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"prog-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "how many kanban boards?")
    db.set_setting("qa.facts_invalidated_at", None)
    db.post_chat_message(
        chatroom.uuid, ASSISTANT_UUID, ASSISTANT_WORKING_NOTICE, kind="progress")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    seen = {}

    def fake_decide(**_kwargs):
        # what base.py sets for a real decide call, so the row has a cost to show
        agent._last_usage = {"input": 900, "output": 60, "ms": 4000}
        if "rows" not in seen:
            rows = [m for m in db.list_room_messages(chatroom.uuid)
                    if m["kind"] == "progress"]
            seen["rows"] = rows
            seen["kinds"] = {m["kind"] for m in db.list_room_messages(chatroom.uuid)}
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok"})

    agent._decide_next_step = fake_decide
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        run_uuid = result["assistant_run_uuid"]
        # Exactly one progress row while the run works — not one per step.
        assert len(seen["rows"]) == 1
        text = seen["rows"][0]["text"]
        assert f"/assistant?id={run_uuid}" in text     # inspect the run from chat
        assert "Step " in text
        assert "LLM call" in text and "in " in text and "out " in text
        # The bubbles it replaced are gone.
        assert "thinking" not in seen["kinds"]
        assert "debug-assistant" not in seen["kinds"]
        # …and the reply reaps the progress row as any terminal post does.
        assert _progress_count(chatroom.uuid) == 0
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_progress_row_reports_the_calls_made_before_the_first_decide(app_ctx):
    """By the first decide call the run has already made a model call — the
    language classifier. Its cost is in the row from the start, so a run that is
    slow BEFORE the decide loop opens does not read as idle."""
    from agents.assistant import ResponseLanguageClassification, ResponseLanguageItem

    human = db.get_human_user()
    chatroom = db.create_chatroom(f"prog-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "hello")
    db.set_setting("qa.facts_invalidated_at", None)
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._request_response_language_classification = lambda **_: (
        ResponseLanguageClassification(
            reason="English request.",
            languages=[ResponseLanguageItem(code="en-US", score=5)],
            audit="OK"))
    seen = {}

    def fake_decide(**_kwargs):
        rows = [m for m in db.list_room_messages(chatroom.uuid) if m["kind"] == "progress"]
        seen["text"] = rows[0]["text"] if rows else ""
        return AssistantStepDecision(
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok"})

    agent._decide_next_step = fake_decide
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert "1 LLM call ·" in seen["text"]    # the classifier, already counted
        assert "deciding step 0" in seen["text"]
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_the_reply_keeps_a_pointer_to_the_run_that_produced_it(app_ctx):
    """The progress row carries the run link while the turn works, but it is
    reaped the moment the reply lands — and a reply worth questioning is
    exactly when the operator wants the trace. The answer keeps the pointer, in
    `meta` rather than in the text: the text is the answer, it is what Copy
    yields, and it is what the model reads back as conversation next turn."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"prog-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "how many kanban boards?")
    db.set_setting("qa.facts_invalidated_at", None)
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = lambda **_: AssistantStepDecision(
        reason="answer", action=AssistantActionName.REPLY,
        args={"message": "You have 3 kanban boards."})
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        run_uuid = result["assistant_run_uuid"]
        reply = [m for m in db.list_room_messages(chatroom.uuid)
                 if m["sender_type"] == "agent" and m["kind"] == "message"][-1]
        assert reply["meta"]["assistant_run_uuid"] == run_uuid
        assert reply["text"] == "You have 3 kanban boards."   # the answer alone
        assert run_uuid not in reply["text"]
        assert _progress_count(chatroom.uuid) == 0            # bubble still reaped
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
