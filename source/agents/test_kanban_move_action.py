"""The kanban_move action moves a task and returns its inverse (undo) op."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    _action_move_kanban_task,
)
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
