"""Tests for the kanban Flask-Admin views/formatters (webapp/core.py).

Runs against the test DB (rainbox_claude, pinned by conftest). Uses a real
board created via db.kanban_create_board and tears it down.
"""
from uuid import UUID, uuid4

import pytest

import db
from webapp.core import app as flask_app


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()  # release read locks before the next init_db ALTERs
        ctx.pop()


@pytest.fixture
def board(app_ctx):
    b = db.kanban_create_board("Admin board", "for the admin tests")
    todo = b["columns"][0]["uuid"]
    task_uuid = str(uuid4())
    db.kanban_save_board(UUID(b["uuid"]), {**b, "tasks": [{
        "uuid": task_uuid, "columnUuid": todo, "title": "Admin task",
        "description": "", "agentUuid": None,
    }]})
    db.kanban_append_event(UUID(task_uuid), "note", actor="human", detail="hello")
    try:
        yield db.kanban_load_board(UUID(b["uuid"]))
    finally:
        db.kanban_delete_board(UUID(b["uuid"]))


@pytest.fixture
def board_folder(app_ctx):
    parent = db.kanban_create_folder("Admin parent folder")
    child = db.kanban_create_folder("Admin child folder",
                                    parent_uuid=UUID(parent["uuid"]))
    try:
        yield child
    finally:
        db.kanban_delete_folder(UUID(child["uuid"]))
        db.kanban_delete_folder(UUID(parent["uuid"]))


def test_admin_kanban_board_folder_renders(board_folder):
    """Render the KanbanBoardFolder admin list with real rows: the folder name,
    the deep link, and the parent_uuid resolving to the parent folder's name."""
    resp = flask_app.test_client().get("/admin/kanbanboardfolder/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Admin child folder" in body
    assert "Admin parent folder" in body            # parent_uuid renders the name
    assert f'/kanban?id={board_folder["uuid"]}' in body  # deep link to the page


@pytest.mark.parametrize("path", [
    "/admin/kanbanboard/", "/admin/kanbancolumn/",
    "/admin/kanbantask/", "/admin/kanbantaskevent/",
])
def test_admin_kanban_pages_render(board, path):
    """Render each kanban admin list end-to-end with real rows (catches
    formatter import/runtime errors that unit tests on the helpers miss)."""
    resp = flask_app.test_client().get(path)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    if path == "/admin/kanbanboard/":
        assert "Admin board" in body
        assert f'/kanban?board={board["uuid"]}' in body  # deep link to the page
    if path == "/admin/kanbantask/":
        assert "Admin task" in body
        assert "Admin board" in body      # board_uuid renders with the board name
        assert "To do" in body            # column_uuid renders with the column name
    if path == "/admin/kanbantaskevent/":
        assert "note" in body and "hello" in body
        assert "Admin task" in body       # task_uuid renders with the task title
        assert "human" in body            # non-uuid actor renders verbatim
