"""The assistant's memory_forget capability (log-and-undo): reject a memory by
uuid or text so it stops being recalled, executing immediately and reversibly
(undo reactivates it). Searches active AND candidate memories (a just-remembered
memory must be forgettable). Mirrors `remember` (its inverse)."""

from uuid import uuid4

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
    _action_reactivate_memory,
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


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID,
        step_index=0,
    )


def _claim(text, status="active"):
    return db.create_memory_claim(
        scope="global", kind="fact", text=text, confidence=1.0,
        status=status, sensitivity="private", subject="forget-test")


def test_forget_capability_is_log_and_undo():
    cap = CAPABILITIES[AssistantActionName.MEMORY_FORGET]
    assert cap.write is True and cap.tier == "log_and_undo"
    assert cap.dry_run is False and cap.prompt_exposed is True


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


def test_forget_observation_links_to_the_memory_page(app_ctx):
    """The reply surfaces a /memory?id=<uuid> link so the operator can inspect
    (and reactivate) the just-forgotten claim."""
    c = _claim(f"link me {uuid4().hex[:6]}")
    try:
        obs = _action_forget_memory(_ctx(), {"memory_uuid": str(c.uuid)})
        assert obs.data["link"] == f"/memory?id={c.uuid}"
    finally:
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()


def test_forget_returns_an_undo_record_that_reactivates(app_ctx):
    """Log-and-undo: forget carries an inverse op pointing at memory_reactivate,
    so undo restores the exact claim it rejected."""
    c = _claim(f"undo me {uuid4().hex[:6]}")
    try:
        obs = _action_forget_memory(_ctx(), {"memory_uuid": str(c.uuid)})
        undo = obs.data["undo"]
        assert undo["capability"] == "memory_reactivate"
        assert undo["payload"]["memory_uuid"] == str(c.uuid)
        # Replaying the inverse restores the active claim.
        back = _action_reactivate_memory(_ctx(), undo["payload"])
        assert back.ok is True
        assert db.get_memory_claim(c.uuid).status == "active"
    finally:
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()


def test_reactivate_refuses_a_claim_that_is_not_rejected(app_ctx):
    """Version guard: undo can't clobber a memory that changed since forget —
    reactivate only flips a still-rejected claim back."""
    c = _claim(f"still active {uuid4().hex[:6]}")  # never forgotten
    try:
        obs = _action_reactivate_memory(_ctx(), {"memory_uuid": str(c.uuid)})
        assert obs.ok is False
        assert db.get_memory_claim(c.uuid).status == "active"  # untouched
    finally:
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()


def test_forget_no_match_fails(app_ctx):
    obs = _action_forget_memory(_ctx(), {"text": f"nonexistent {uuid4().hex}"})
    assert obs.ok is False


def test_forget_needs_uuid_or_text(app_ctx):
    obs = _action_forget_memory(_ctx(), {})
    assert obs.ok is False


def test_forget_via_loop_executes_inline_and_is_undoable(app_ctx):
    """End-to-end through the ReAct loop: 'forget X' rejects the memory inline
    (no confirm step) and records a completed, undoable write-intent — mirroring
    remember. This is the bug fix: forget used to only *propose*, which the model
    could not terminate, looping to the step limit."""
    from agents.assistant_writes import undo_write_intent

    text = f"I prefer pasta {uuid4().hex[:6]}"
    c = _claim(text, status="candidate")
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"forget-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, f"forget {text}")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="forget", action=AssistantActionName.MEMORY_FORGET,
                              args={"text": text}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "done"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        assert intent.state == "completed"                       # log-and-undo: inline
        assert db.get_memory_claim(c.uuid).status == "rejected"  # gone immediately
        # the reply carries a /memory link so the operator can inspect it
        reply = db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == chatroom.uuid,
            db.ChatMessage.kind == "message",
            db.ChatMessage.sender_uuid == ASSISTANT_UUID).order_by(
            db.ChatMessage.id.desc()).first()
        assert f"/memory?id={c.uuid}" in reply.text
        # Reversible: undo reactivates it.
        assert undo_write_intent(intent.uuid).ok is True
        assert db.get_memory_claim(c.uuid).status == "active"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.query(MemoryClaim).filter_by(uuid=c.uuid).delete()
        db.db.session.commit()
