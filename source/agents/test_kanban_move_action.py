"""The kanban_move_task action moves a task and returns its inverse (undo) op."""

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
    cap = CAPABILITIES[AssistantActionName.KANBAN_MOVE_TASK]
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
    # Inverse points back at the original column; expect_column pins the
    # destination so undo refuses if the task moves again first.
    assert obs.data["undo"] == {
        "capability": "kanban_move_task",
        "payload": {"task_uuid": task["uuid"], "column_uuid": todo,
                    "expect_column": done},
    }


def test_move_rejects_missing_task(app_ctx):
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": str(uuid4()), "column_uuid": str(uuid4())}
    )
    assert obs.ok is False


def test_move_resolves_column_by_name(board):
    """Run 17: the operator names a column ('In progress'); the model couldn't map
    it to a uuid. The action resolves a column NAME (case-insensitive) too."""
    task = board["tasks"][0]
    done = board["columns"][1]["uuid"]  # "Done"
    obs = _action_move_kanban_task(_ctx(), {"task_uuid": task["uuid"], "column_uuid": "done"})
    assert obs.ok is True
    assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done


def test_move_to_current_column_is_flagged_not_silent_noop(board):
    """Run 17: the model targeted the column the task was already in; the move was
    a no-op but reported 'Moved'. A no-op must be flagged, not claimed as success."""
    task = board["tasks"][0]
    todo = board["columns"][0]["uuid"]  # the task's current column
    obs = _action_move_kanban_task(_ctx(), {"task_uuid": task["uuid"], "column_uuid": todo})
    assert obs.ok is False
    assert "different" in obs.text.lower()        # states the destination≠source rule
    assert "'To do'" in obs.text                  # names the source column
    assert "'Done'" in obs.text                   # offers the real alternative
    assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == todo  # unchanged


def test_move_unknown_column_lists_available(board):
    task = board["tasks"][0]
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": task["uuid"], "column_uuid": "Nonexistent"})
    assert obs.ok is False
    assert "To do" in obs.text and "Done" in obs.text  # guides the model


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
        run_id=run.id, capability_name="kanban_move_task",
        payload={"task_uuid": task["uuid"], "column_uuid": done},
        preview_text="kanban_move_task: …", room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
        state="completed",
        result={"undo": {"capability": "kanban_move_task",
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
        AssistantStepDecision(reason="move", action=AssistantActionName.KANBAN_MOVE_TASK,
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


def test_undo_refused_if_task_moved_since(board):
    """Position-aware undo: if the task moved after the assistant's move, undoing
    must not yank it from where it now sits. Refuse and leave it put."""
    from agents.assistant_writes import undo_write_intent
    from db import AssistantRun, AssistantWriteIntent

    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][1]["uuid"]
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6)
    obs = _action_move_kanban_task(_ctx(), {"task_uuid": task["uuid"], "column_uuid": done})
    intent = db.create_write_intent(
        run_id=run.id, capability_name="kanban_move_task",
        payload={"task_uuid": task["uuid"], "column_uuid": done},
        preview_text="kanban_move_task", room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
        state="completed", result={"undo": obs.data["undo"]})
    # Someone else moves the task back to To do after the assistant's move.
    db.kanban_move_task(UUID(task["uuid"]), UUID(todo), actor="human")
    try:
        out = undo_write_intent(intent.uuid)
        assert out.ok is False
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == todo  # left put
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_id == run.id).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.id == run.id).delete()
        db.db.session.commit()
