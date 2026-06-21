"""The assistant's forget_memory capability (confirm-tier + dry-run): reject a
memory by uuid or text so it stops being recalled. Searches active AND candidate
memories (a just-remembered candidate must be forgettable)."""

from uuid import UUID, uuid4

import pytest

import db
from db import AssistantRun, AssistantWriteIntent, MemoryClaim
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_forget_memory,
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
        db.db.session.rollback()
        ctx.pop()


def _ctx(dry_run=False):
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID,
        step_index=0, dry_run=dry_run,
    )


def _claim(text, status="active"):
    return db.create_memory_claim(
        scope="global", kind="fact", text=text, confidence=1.0,
        status=status, sensitivity="private", subject="forget-test")


def test_forget_capability_is_confirm_with_dry_run():
    cap = CAPABILITIES[AssistantActionName.FORGET_MEMORY]
    assert cap.write is True and cap.tier == "confirm"
    assert cap.dry_run is True and cap.prompt_exposed is True


def test_forget_by_uuid_rejects_the_memory(app_ctx):
    c = _claim(f"forget me {uuid4().hex[:6]}")
    try:
        obs = _action_forget_memory(_ctx(), {"memory_uuid": str(c.uuid)})
        assert obs.ok is True
        assert db.get_memory_claim(c.uuid).status == "rejected"
    finally:
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()


def test_forget_by_text_matches_a_candidate(app_ctx):
    text = f"I prefer pasta {uuid4().hex[:6]}"
    c = _claim(text, status="candidate")  # remember creates candidates
    try:
        obs = _action_forget_memory(_ctx(), {"text": text})
        assert obs.ok is True
        assert db.get_memory_claim(c.uuid).status == "rejected"
    finally:
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()


def test_forget_dry_run_previews_without_rejecting(app_ctx):
    c = _claim(f"keep me for now {uuid4().hex[:6]}")
    try:
        obs = _action_forget_memory(_ctx(dry_run=True), {"memory_uuid": str(c.uuid)})
        assert obs.ok is True and "forget" in obs.text.lower()
        assert db.get_memory_claim(c.uuid).status == "active"  # not mutated
    finally:
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()


def test_forget_no_match_fails(app_ctx):
    obs = _action_forget_memory(_ctx(), {"text": f"nonexistent {uuid4().hex}"})
    assert obs.ok is False


def test_forget_needs_uuid_or_text(app_ctx):
    obs = _action_forget_memory(_ctx(), {})
    assert obs.ok is False


def test_forget_via_loop_proposes_then_confirm_executes(app_ctx):
    from agents.assistant_writes import execute_write_intent

    text = f"I prefer pasta {uuid4().hex[:6]}"
    c = _claim(text, status="candidate")
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"forget-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, f"forget {text}")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="forget", action=AssistantActionName.FORGET_MEMORY,
                              args={"text": text}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "done"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        assert intent.state == "proposed"                      # confirm-tier: not inline
        assert db.get_memory_claim(c.uuid).status == "candidate"
        assert "forget" in intent.preview_text.lower() and text in intent.preview_text
        assert execute_write_intent(intent.uuid).ok is True
        assert db.get_memory_claim(c.uuid).status == "rejected"  # gone after confirm
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()
