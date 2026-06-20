"""db.kanban_get_task: a public single-task reader returning the task brief
(including the current columnUuid), used by the assistant's kanban-move undo."""

from uuid import UUID, uuid4

import pytest

import db


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
    b = db.kanban_create_board("get-task board")
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


def test_get_task_returns_brief_with_current_column(board):
    task = board["tasks"][0]
    out = db.kanban_get_task(UUID(task["uuid"]))
    assert out is not None
    assert out["uuid"] == task["uuid"]
    assert out["columnUuid"] == board["columns"][0]["uuid"]
    assert out["title"] == "Ship it"


def test_get_task_returns_none_for_unknown(app_ctx):
    assert db.kanban_get_task(uuid4()) is None
