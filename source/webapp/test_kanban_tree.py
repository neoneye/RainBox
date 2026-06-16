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
