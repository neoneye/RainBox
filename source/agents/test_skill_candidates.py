"""S3: assistant propose_skill (log-and-undo, inert candidate) + activate_skill
(confirm-tier). The candidates-are-inert contract is tested directly. Model-free;
the skills overlay is pointed at a tmp dir via the customize.dir setting."""

from uuid import uuid4

import pytest

import db
import skills
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_activate_skill,
    _action_propose_skill,
)
from agents.assistant_fakes import scripted_decisions
from agents.assistant_writes import execute_write_intent
from agents.config import ASSISTANT_UUID
from db import AssistantRun, AssistantWriteIntent


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


@pytest.fixture
def overlay(app_ctx, tmp_path):
    old = db.get_setting("customize.dir")
    db.set_setting("customize.dir", str(tmp_path))
    try:
        yield tmp_path / "skills"
    finally:
        db.set_setting("customize.dir", old)


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def test_capability_flags():
    assert CAPABILITIES[AssistantActionName.PROPOSE_SKILL].tier == "log_and_undo"
    assert CAPABILITIES[AssistantActionName.ACTIVATE_SKILL].tier == "confirm"
    assert CAPABILITIES[AssistantActionName.SKILL_DELETE].prompt_exposed is False


def test_propose_writes_inert_candidate_with_inverse(overlay):
    obs = _action_propose_skill(_ctx(), {
        "skill_id": "widget-howto", "title": "Widget howto zorp",
        "body": "Inspect zorp widgets carefully."})
    assert obs.ok is True
    f = overlay / "widget-howto.md"
    assert f.exists()
    text = f.read_text()
    assert "status: candidate" in text and "created_by: assistant" in text
    assert obs.data["undo"] == {"capability": "skill_delete",
                                "payload": {"skill_id": "widget-howto"}}


def test_candidate_is_inert_until_activated(overlay):
    _action_propose_skill(_ctx(), {
        "skill_id": "zorp-skill", "title": "Zorp workflow",
        "body": "When handling zorp, do the zorp dance."})
    block, injected = skills.build_skill_block("how do I handle zorp")
    assert "Zorp workflow" not in block and injected == []   # candidate: inert
    assert _action_activate_skill(_ctx(), {"skill_id": "zorp-skill"}).ok is True
    block2, injected2 = skills.build_skill_block("how do I handle zorp")
    assert "Zorp workflow" in block2 and injected2          # active: injected


def test_propose_via_loop_then_undo_deletes(overlay):
    from agents.assistant_writes import undo_write_intent
    chatroom = _room()
    db.post_chat_message(chatroom.uuid, db.get_human_user().uuid, "learn this")
    agent = _agent(scripted_decisions(
        AssistantStepDecision(reason="propose", action=AssistantActionName.PROPOSE_SKILL,
                              args={"skill_id": "loop-skill", "title": "Loop skill",
                                    "body": "do the thing"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "proposed"})))
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert (overlay / "loop-skill.md").exists()
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        assert intent.state == "completed"
        assert undo_write_intent(intent.uuid).ok is True
        assert not (overlay / "loop-skill.md").exists()     # undo deleted it
    finally:
        _cleanup_room(chatroom)


def test_activate_is_confirm_tier(overlay):
    _action_propose_skill(_ctx(), {"skill_id": "conf-skill", "title": "Conf", "body": "x"})
    chatroom = _room()
    db.post_chat_message(chatroom.uuid, db.get_human_user().uuid, "activate it")
    agent = _agent(scripted_decisions(
        AssistantStepDecision(reason="activate", action=AssistantActionName.ACTIVATE_SKILL,
                              args={"skill_id": "conf-skill"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "proposed"})))
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        assert intent.state == "proposed"
        assert "status: candidate" in (overlay / "conf-skill.md").read_text()  # not active inline
        assert execute_write_intent(intent.uuid).ok is True
        assert "status: active" in (overlay / "conf-skill.md").read_text()
    finally:
        _cleanup_room(chatroom)


def test_model_cannot_invoke_skill_delete(overlay):
    _action_propose_skill(_ctx(), {"skill_id": "keep-skill", "title": "Keep", "body": "x"})
    chatroom = _room()
    db.post_chat_message(chatroom.uuid, db.get_human_user().uuid, "delete it")
    agent = _agent(scripted_decisions(
        AssistantStepDecision(reason="del", action=AssistantActionName.SKILL_DELETE,
                              args={"skill_id": "keep-skill"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "done"})))
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert (overlay / "keep-skill.md").exists()         # guard blocked the delete
    finally:
        _cleanup_room(chatroom)


def test_bad_id_and_duplicate_and_no_overlay_rejected(overlay, app_ctx):
    assert _action_propose_skill(_ctx(), {"skill_id": "Bad Id!", "title": "t", "body": "b"}).ok is False
    _action_propose_skill(_ctx(), {"skill_id": "dup", "title": "t", "body": "b"})
    assert _action_propose_skill(_ctx(), {"skill_id": "dup", "title": "t", "body": "b"}).ok is False
    db.set_setting("customize.dir", None)  # no overlay
    assert _action_propose_skill(_ctx(), {"skill_id": "noov", "title": "t", "body": "b"}).ok is False


def _agent(decide):
    a = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    a._decide_next_step = decide
    return a


def _room():
    human = db.get_human_user()
    return db.create_chatroom(f"skill-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])


def _cleanup_room(chatroom):
    db.db.session.query(AssistantWriteIntent).filter(
        AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
    db.db.session.query(AssistantRun).filter(
        AssistantRun.room_uuid == chatroom.uuid).delete()
    db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
    db.db.session.commit()
