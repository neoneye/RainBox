"""Assistant kanban_board_create (log-and-undo; undo deletes the board) and its
internal, non-model-invocable kanban_board_delete inverse. The caller supplies
only a name — the store assigns the board uuid, so a caller can never pick (and
collide) one. This is the gap behind "create a board named X" failing because
kanban_create (a TASK creator) demanded a board_uuid."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_create_kanban_board,
)
from agents.assistant_fakes import scripted_decisions
from agents.assistant_writes import undo_write_intent
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


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0)


def test_capabilities_board_create_exposed_delete_internal():
    create = CAPABILITIES[AssistantActionName.KANBAN_BOARD_CREATE]
    assert create.write is True and create.tier == "log_and_undo" and create.prompt_exposed is True
    assert "board_uuid" not in create.required_args  # caller never supplies a uuid
    delete = CAPABILITIES[AssistantActionName.KANBAN_BOARD_DELETE]
    assert delete.prompt_exposed is False


def test_create_board_assigns_uuid_and_returns_delete_inverse(app_ctx):
    name = f"Basic todos {uuid4().hex[:6]}"
    bu = None
    try:
        obs = _action_create_kanban_board(_ctx(), {"title": name})
        assert obs.ok is True
        bu = obs.data["board_uuid"]
        UUID(bu)  # a real uuid was assigned by the store
        board = db.kanban_load_board(UUID(bu))
        assert board is not None and board["name"] == name
        assert board["columns"]  # default columns were created
        assert obs.data["link"] == f"/kanban?id={bu}"
        assert obs.data["undo"] == {
            "capability": "kanban_board_delete", "payload": {"board_uuid": bu}}
    finally:
        if bu:
            db.kanban_delete_board(UUID(bu))


def test_create_board_requires_title(app_ctx):
    obs = _action_create_kanban_board(_ctx(), {"title": "   "})
    assert obs.ok is False


def test_create_board_via_loop_and_undo(app_ctx):
    name = f"Basic todos {uuid4().hex[:6]}"
    human = db.get_human_user()
    room = db.create_chatroom(f"kb-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(room.uuid, human.uuid, f"create a kanban board named {name}")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="make the board",
                              action=AssistantActionName.KANBAN_BOARD_CREATE,
                              args={"title": name}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"1_message": "done", "2_audit": "OK"}))
    bu = None
    try:
        agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == room.uuid).one()
        assert intent.state == "completed"
        bu = intent.result["undo"]["payload"]["board_uuid"]
        assert db.kanban_load_board(UUID(bu)) is not None  # board exists
        # the reply links to the new board
        reply = db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == room.uuid,
            db.ChatMessage.kind == "message",
            db.ChatMessage.sender_uuid == ASSISTANT_UUID).order_by(
            db.ChatMessage.id.desc()).first()
        assert f"/kanban?id={bu}" in reply.text
        # undo removes it
        assert undo_write_intent(intent.uuid).ok is True
        assert db.kanban_load_board(UUID(bu)) is None
    finally:
        if bu and db.kanban_load_board(UUID(bu)) is not None:
            db.kanban_delete_board(UUID(bu))
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == room.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == room.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()
