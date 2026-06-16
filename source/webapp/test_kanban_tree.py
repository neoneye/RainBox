"""Tests for the /kanban folder tree: schema, load/version/validate/save,
folder create + reparenting delete.

Hits the live local Postgres (conftest pins every pytest run to
rainbox_claude). Each test cleans up the rows it creates.
"""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db import KanbanBoardFolder


def _u(s):
    return UUID(s)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        # Close any read transaction so its ACCESS SHARE locks don't block
        # the next test's init_db ALTERs (single-process lock self-deadlock).
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def board(app_ctx):
    """A fresh board; deleted with all its rows after."""
    b = db.kanban_create_board("Tree test board")
    try:
        yield b
    finally:
        db.kanban_delete_board(_u(b["uuid"]))


def _new_folder(name="F", parent=None):
    """Create a folder directly; returns its dict. Caller cleans up."""
    return db.kanban_create_folder(name, parent_uuid=parent)


def test_folder_table_and_board_column_exist(app_ctx):
    # The folder table is creatable and round-trips.
    f = KanbanBoardFolder(uuid=uuid4(), name="schema check")
    db.db.session.add(f)
    db.db.session.commit()
    got = db.db.session.execute(
        sa.select(KanbanBoardFolder).where(KanbanBoardFolder.uuid == f.uuid)
    ).scalar_one()
    assert got.name == "schema check"
    assert got.parent_uuid is None and got.position == 0
    db.db.session.delete(got)
    db.db.session.commit()
    # The board carries a folder_uuid column (null by default).
    col = db.db.session.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='kanban_board' AND column_name='folder_uuid'"
    )).first()
    assert col is not None


def test_create_folder_defaults_and_position(app_ctx):
    a = db.kanban_create_folder("Alpha")
    b = db.kanban_create_folder("Beta")
    try:
        assert a["name"] == "Alpha" and a["parentId"] is None
        # Relative, not absolute (shared rainbox_claude DB may hold other root
        # folders): the second sibling is appended right after the first.
        assert UUID(a["uuid"]) and isinstance(a["position"], int)
        assert b["position"] == a["position"] + 1
    finally:
        db.kanban_delete_folder(_u(a["uuid"]))
        db.kanban_delete_folder(_u(b["uuid"]))


def test_create_folder_requires_name(app_ctx):
    with pytest.raises(db.KanbanError):
        db.kanban_create_folder("   ")


def test_delete_folder_reparents_children_and_keeps_boards(app_ctx):
    parent = db.kanban_create_folder("parent")
    child = db.kanban_create_folder("child", parent_uuid=_u(parent["uuid"]))
    board = db.kanban_create_board("filed board")
    try:
        # File the board + a sub-board-folder under `child`.
        db.kanban_save_tree(
            folders=[{"uuid": parent["uuid"], "name": "parent", "parentId": None},
                     {"uuid": child["uuid"], "name": "child", "parentId": parent["uuid"]}],
            boards=[{"uuid": board["uuid"], "folderId": child["uuid"]}],
        )
        # Delete the middle folder `child`: its board reparents up to `parent`,
        # the board (and its columns/tasks) survives.
        assert db.kanban_delete_folder(_u(child["uuid"])) is True
        tree = db.kanban_load_tree()
        folder_ids = {f["uuid"] for f in tree["folders"]}
        assert child["uuid"] not in folder_ids and parent["uuid"] in folder_ids
        moved = next(b for b in tree["boards"] if b["uuid"] == board["uuid"])
        assert moved["folderId"] == parent["uuid"]
        assert db.kanban_load_board(_u(board["uuid"])) is not None  # board kept
        assert db.kanban_delete_folder(_u(child["uuid"])) is False  # already gone
    finally:
        db.kanban_delete_board(_u(board["uuid"]))
        db.kanban_delete_folder(_u(parent["uuid"]))


def test_load_tree_shape(board):
    f = db.kanban_create_folder("Inbox")
    try:
        tree = db.kanban_load_tree()
        assert isinstance(tree["version"], str) and tree["version"]
        folder = next(x for x in tree["folders"] if x["uuid"] == f["uuid"])
        assert set(folder) == {"uuid", "name", "description", "parentId", "position"}
        b = next(x for x in tree["boards"] if x["uuid"] == board["uuid"])
        assert set(b) == {"uuid", "name", "folderId", "position", "taskCount"}
        assert b["folderId"] is None and b["taskCount"] == 0
    finally:
        db.kanban_delete_folder(_u(f["uuid"]))


def test_tree_version_excludes_board_name_and_taskcount(board):
    """A board rename (board PUT) and a new task (agent op) must NOT bump the
    tree version — those are not structural tree fields, or the tree would 409
    on every background board/task change."""
    bu = _u(board["uuid"])
    v0 = db.kanban_load_tree()["version"]
    # Rename the board via the board-contents save: tree version unchanged.
    db.kanban_save_board(bu, {**board, "name": "renamed"})
    assert db.kanban_load_tree()["version"] == v0
    # Add a task via the board-contents save: tree version unchanged.
    todo = board["columns"][0]["uuid"]
    after = db.kanban_load_board(bu)
    db.kanban_save_board(bu, {**after,
        "tasks": [{"uuid": str(uuid4()), "columnUuid": todo, "title": "t",
                   "description": "", "agentUuid": None}]})
    assert db.kanban_load_tree()["version"] == v0
