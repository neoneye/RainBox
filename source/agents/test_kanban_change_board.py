"""The kanban_task_change_board action moves a task to another board: the
column carries over by name unless an explicit column pins the landing spot,
and the undo record moves the task back to its original board + column."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    _action_change_kanban_task_board,
)
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


def _make_board(name: str, columns: tuple[str, ...], tasks: int = 0):
    b = db.kanban_create_board(name)
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n} for n in columns]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][1]["uuid"],
                       "title": f"Task {i}", "description": "d"}
                      for i in range(tasks)]
    db.kanban_save_board(bu, fresh)
    return db.kanban_load_board(bu)


@pytest.fixture
def boards(app_ctx):
    """A source board with one task in 'In progress', and a target board that
    also has an 'In progress' column (different position)."""
    src = _make_board("src board", ("To do", "In progress", "Done"), tasks=1)
    dst = _make_board("dst board", ("Inbox", "Review", "In progress"))
    try:
        yield src, dst
    finally:
        db.kanban_delete_board(UUID(src["uuid"]))
        db.kanban_delete_board(UUID(dst["uuid"]))


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(),
        agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def _col(board, name):
    return next(c for c in board["columns"] if c["name"] == name)


def test_capability_is_log_and_undo_write():
    cap = CAPABILITIES[AssistantActionName.KANBAN_TASK_CHANGE_BOARD]
    assert cap.write is True
    assert cap.read is False
    assert cap.tier == "log_and_undo"
    assert cap.prompt_exposed is True
    assert cap.required_args == ("task_uuid", "board_uuid")
    assert cap.optional_args == frozenset({"column_uuid"})


def test_change_board_preserves_column_by_name(boards):
    src, dst = boards
    task = src["tasks"][0]
    obs = _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": dst["uuid"]})
    assert obs.ok is True
    after = db.kanban_get_task(UUID(task["uuid"]))
    assert after["boardUuid"] == dst["uuid"]
    assert after["columnUuid"] == _col(dst, "In progress")["uuid"]
    assert "'dst board'" in obs.text and "'In progress'" in obs.text
    # The undo record restores the original board AND column.
    assert obs.data["undo"]["capability"] == "kanban_task_change_board"
    assert obs.data["undo"]["payload"] == {
        "task_uuid": task["uuid"], "board_uuid": src["uuid"],
        "column_uuid": _col(src, "In progress")["uuid"],
        "expect_board": dst["uuid"]}


def test_change_board_without_name_match_falls_back(app_ctx):
    src = _make_board("fb src", ("To do", "In progress", "Done"), tasks=1)
    dst = _make_board("fb dst", ("Inbox", "Later"))
    try:
        task = src["tasks"][0]  # in 'In progress' (position 1)
        obs = _action_change_kanban_task_board(
            _ctx(), {"task_uuid": task["uuid"], "board_uuid": dst["uuid"]})
        assert obs.ok is True
        after = db.kanban_get_task(UUID(task["uuid"]))
        # No 'In progress' on the target: same position (1) wins.
        assert after["columnUuid"] == _col(dst, "Later")["uuid"]
    finally:
        db.kanban_delete_board(UUID(src["uuid"]))
        db.kanban_delete_board(UUID(dst["uuid"]))


def test_explicit_column_name_overrides_carry_over(boards):
    src, dst = boards
    task = src["tasks"][0]
    obs = _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": dst["uuid"],
                 "column_uuid": "review"})  # case-insensitive name
    assert obs.ok is True
    after = db.kanban_get_task(UUID(task["uuid"]))
    assert after["columnUuid"] == _col(dst, "Review")["uuid"]


def test_unknown_explicit_column_lists_target_columns(boards):
    src, dst = boards
    task = src["tasks"][0]
    obs = _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": dst["uuid"],
                 "column_uuid": "Nonexistent"})
    assert obs.ok is False
    assert "'Inbox'" in obs.text and "'Review'" in obs.text
    # Nothing moved.
    assert db.kanban_get_task(UUID(task["uuid"]))["boardUuid"] == src["uuid"]


def test_same_board_is_refused_and_points_to_task_column(boards):
    src, _ = boards
    task = src["tasks"][0]
    obs = _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": src["uuid"]})
    assert obs.ok is False
    assert "kanban_task_column" in obs.text


def test_invalid_and_missing_targets(boards):
    src, dst = boards
    task = src["tasks"][0]
    assert _action_change_kanban_task_board(
        _ctx(), {"task_uuid": "nope", "board_uuid": dst["uuid"]}).ok is False
    assert _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": "nope"}).ok is False
    assert _action_change_kanban_task_board(
        _ctx(), {"task_uuid": str(uuid4()), "board_uuid": dst["uuid"]}).ok is False
    assert _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": str(uuid4())}).ok is False


def test_undo_restores_board_and_column_and_marks_undone(boards):
    from agents.assistant_writes import undo_write_intent

    src, dst = boards
    task = src["tasks"][0]
    original_column = _col(src, "In progress")["uuid"]
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6)
    obs = _action_change_kanban_task_board(
        _ctx(), {"task_uuid": task["uuid"], "board_uuid": dst["uuid"]})
    assert obs.ok is True
    intent = db.create_write_intent(
        run_uuid=run.uuid, capability_name="kanban_task_change_board",
        payload={"task_uuid": task["uuid"], "board_uuid": dst["uuid"]},
        preview_text="kanban_task_change_board: …",
        room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
        state="completed", result={"undo": obs.data["undo"]},
    )
    try:
        undone = undo_write_intent(intent.uuid)
        assert undone.ok is True
        after = db.kanban_get_task(UUID(task["uuid"]))
        assert after["boardUuid"] == src["uuid"]
        assert after["columnUuid"] == original_column
        assert db.get_write_intent(intent.uuid).state == "undone"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_uuid == run.uuid).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run.uuid).delete()
        db.db.session.commit()


def test_undo_refused_when_task_changed_board_since(app_ctx):
    src = _make_board("eb src", ("To do", "In progress"), tasks=1)
    dst = _make_board("eb dst", ("Inbox",))
    third = _make_board("eb third", ("Inbox",))
    try:
        task = src["tasks"][0]
        obs = _action_change_kanban_task_board(
            _ctx(), {"task_uuid": task["uuid"], "board_uuid": dst["uuid"]})
        assert obs.ok is True
        # The task moves on to a third board before the undo fires.
        db.kanban_move_task_to_board(UUID(task["uuid"]), UUID(third["uuid"]))
        undo = _action_change_kanban_task_board(_ctx(), obs.data["undo"]["payload"])
        assert undo.ok is False
        assert "changed board" in undo.text
        assert db.kanban_get_task(UUID(task["uuid"]))["boardUuid"] == third["uuid"]
    finally:
        db.kanban_delete_board(UUID(src["uuid"]))
        db.kanban_delete_board(UUID(dst["uuid"]))
        db.kanban_delete_board(UUID(third["uuid"]))
