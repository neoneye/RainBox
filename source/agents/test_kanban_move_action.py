"""The kanban_move action moves a task and returns its inverse (undo) op."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_move_kanban_task,
)
from agents.assistant_fakes import scripted_decisions
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
def board(app_ctx):
    b = db.kanban_create_board("move board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n} for n in ("To do", "Done")]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Ship it", "description": "d"}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    try:
        yield data
    finally:
        db.kanban_delete_board(bu)


def _ctx(room_uuid=None):
    return AssistantActionContext(
        journal_id=None, room_uuid=room_uuid or uuid4(),
        agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def test_capability_is_log_and_undo_write():
    cap = CAPABILITIES[AssistantActionName.KANBAN_MOVE]
    assert cap.write is True
    assert cap.tier == "log_and_undo"
    assert cap.required_args == ("task_uuid", "column_uuid")


def test_move_executes_and_returns_inverse(board):
    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][1]["uuid"]
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": task["uuid"], "column_uuid": done}
    )
    assert obs.ok is True
    # Task actually moved.
    assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done
    # Inverse points back at the original column.
    assert obs.data["undo"] == {
        "capability": "kanban_move",
        "payload": {"task_uuid": task["uuid"], "column_uuid": todo},
    }


def test_move_rejects_missing_task(app_ctx):
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": str(uuid4()), "column_uuid": str(uuid4())}
    )
    assert obs.ok is False


def test_move_rejects_column_not_on_board(board):
    task = board["tasks"][0]
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": task["uuid"], "column_uuid": str(uuid4())}
    )
    assert obs.ok is False


def test_undo_moves_task_back_and_marks_undone(board):
    from agents.assistant_writes import undo_write_intent

    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6)
    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][1]["uuid"]
    db.kanban_move_task(UUID(task["uuid"]), UUID(done), actor=str(ASSISTANT_UUID))
    intent = db.create_write_intent(
        run_id=run.id, step_index=0, capability_name="kanban_move",
        payload={"task_uuid": task["uuid"], "column_uuid": done},
        preview_text="kanban_move: …", room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
        state="completed",
        result={"undo": {"capability": "kanban_move",
                         "payload": {"task_uuid": task["uuid"], "column_uuid": todo}}},
    )
    try:
        obs = undo_write_intent(intent.uuid)
        assert obs.ok is True
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == todo
        assert db.get_write_intent(intent.uuid).state == "undone"
        # Second undo is refused (already undone, not completed).
        assert undo_write_intent(intent.uuid).ok is False
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_id == run.id).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.id == run.id).delete()
        db.db.session.commit()


def test_undo_refuses_unknown_intent(app_ctx):
    from agents.assistant_writes import undo_write_intent
    assert undo_write_intent(uuid4()).ok is False


def test_move_via_loop_lands_completed_undo_ledger(board):
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"mv-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "move it to done")
    task = board["tasks"][0]
    done = board["columns"][1]["uuid"]

    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="move", action=AssistantActionName.KANBAN_MOVE,
                              args={"task_uuid": task["uuid"], "column_uuid": done}),
        AssistantStepDecision(reason="done", action=AssistantActionName.REPLY,
                              args={"message": "moved"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        # Task moved.
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done
        # Exactly one ledger row, completed, never proposed, with a working inverse.
        intents = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid
        ).all()
        assert len(intents) == 1
        assert intents[0].state == "completed"
        assert intents[0].result["undo"]["payload"]["column_uuid"] == board["columns"][0]["uuid"]
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
