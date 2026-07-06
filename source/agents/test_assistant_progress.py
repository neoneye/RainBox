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
