"""Tests for the /chat folder/tree HTTP endpoints (webapp/chat_api.py).

Uses the live local Postgres (rainbox_claude via conftest).
"""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import Chatroom, ChatroomFolder


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_get_tree_shape(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    tree = client.get("/chat/api/tree").get_json()
    assert {"folders", "rooms", "version"} <= set(tree)
    assert isinstance(tree["version"], str) and tree["version"]


def test_create_folder_then_appears_in_tree(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    resp = client.post("/chat/api/folders", json={"name": "apitest-folder"})
    assert resp.status_code == 201
    fid = resp.get_json()["id"]
    try:
        tree = client.get("/chat/api/tree").get_json()
        assert any(f["id"] == fid for f in tree["folders"])
    finally:
        db.db.session.execute(sa.delete(ChatroomFolder).where(ChatroomFolder.uuid == fid))
        db.db.session.commit()


def test_put_tree_stale_version_is_409(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    tree = client.get("/chat/api/tree").get_json()
    body = {"folders": [], "rooms": [{"uuid": r["uuid"], "folderId": r["folderId"]}
                                     for r in tree["rooms"]],
            "version": "staleversion0000"}
    resp = client.put("/chat/api/tree", json=body)
    assert resp.status_code == 409
    assert "version" in resp.get_json()  # fresh token returned for re-hydration


def test_put_tree_missing_version_is_400(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    resp = client.put("/chat/api/tree", json={"folders": [], "rooms": []})
    assert resp.status_code == 400


def test_folder_delete_preview_and_delete(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    human = db.get_human_user()
    folder = db.create_chatroom_folder("apitest-del")
    room = db.create_chatroom(f"apidel-{uuid4().hex[:6]}", human.uuid, [])
    db.db.session.execute(sa.update(Chatroom).where(Chatroom.uuid == room.uuid)
                          .values(folder_uuid=folder.uuid))
    db.db.session.commit()
    room_uuid = room.uuid  # capture before ORM session is invalidated by HTTP call
    db.post_chat_message(room_uuid, human.uuid, "hi")
    preview = client.get(f"/chat/api/folders/{folder.uuid}/delete-preview").get_json()
    assert preview["room_count"] == 1 and preview["message_count"] == 1
    resp = client.delete(f"/chat/api/folders/{folder.uuid}")
    assert resp.status_code == 200
    db.db.session.expire_all()  # flush ORM identity-map cache after HTTP-layer delete
    assert db.db.session.execute(
        sa.select(Chatroom).where(Chatroom.uuid == room_uuid)).scalar_one_or_none() is None


def test_folder_delete_unknown_is_404(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    resp = client.delete(f"/chat/api/folders/{uuid4()}")
    assert resp.status_code == 404


def test_room_delete_preview(app_ctx):
    from webapp.core import app as flask_app
    client = flask_app.test_client()
    human = db.get_human_user()
    room = db.create_chatroom(f"apirp-{uuid4().hex[:6]}", human.uuid, [])
    db.post_chat_message(room.uuid, human.uuid, "x")
    try:
        preview = client.get(f"/chat/api/rooms/{room.uuid}/delete-preview").get_json()
        assert preview["message_count"] == 1 and preview["room_name"] == room.name
    finally:
        db.delete_chatroom(room.uuid)
