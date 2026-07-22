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
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok", "audit": "OK"})

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
            reason="answer", action=AssistantActionName.REPLY, args={"message": "ok", "audit": "OK"})

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
            args={"message": "done", "audit": "OK"}),
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
        assert activities == ["deciding step 0", "deciding step 1"]
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
