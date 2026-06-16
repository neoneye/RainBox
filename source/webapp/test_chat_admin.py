"""Tests for the chat Flask-Admin views/formatters (webapp/core.py).

Runs against the test DB (rainbox_claude, pinned by conftest). Uses real
chatroom folders created via db.create_chatroom_folder and tears them down.
"""
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
def chat_folder(app_ctx):
    parent = db.create_chatroom_folder("Admin parent chat folder")
    child = db.create_chatroom_folder("Admin child chat folder",
                                      parent_uuid=parent.uuid)
    child_uuid = child.uuid
    parent_uuid = parent.uuid
    try:
        yield child_uuid
    finally:
        db.delete_chatroom_folder(child_uuid)
        db.delete_chatroom_folder(parent_uuid)


def test_admin_chatroom_folder_renders(chat_folder):
    """Render the ChatroomFolder admin list with real rows: the folder name, the
    deep link, and the parent_uuid resolving to the parent folder's name."""
    resp = flask_app.test_client().get("/admin/chatroomfolder/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Admin child chat folder" in body
    assert "Admin parent chat folder" in body        # parent_uuid renders the name
    assert f'/chat?id={chat_folder}' in body          # deep link to the page
