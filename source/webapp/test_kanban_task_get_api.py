"""GET /kanban/api/tasks/<uuid> — used by the deep link `/kanban?id=<task>` to
resolve which board to open before popping the task overlay."""

from uuid import UUID, uuid4

import pytest

import db
import webapp  # noqa: F401 — registers the kanban api routes
from webapp.core import app as flask_app


@pytest.fixture
def app_ctx():
    application = db.make_app()
    db.init_db(application)
    ctx = application.app_context()
    ctx.push()
    try:
        yield application
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def test_get_task_returns_board_uuid(app_ctx, client):
    b = db.kanban_create_board("task-get board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": "To do"}]
    fresh["tasks"] = []
    db.kanban_save_board(bu, fresh)
    created = db.kanban_create_task(bu, UUID(fresh["columns"][0]["uuid"]),
                                    title="deep link me", actor="test")
    try:
        resp = client.get(f"/kanban/api/tasks/{created['uuid']}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["task"]["uuid"] == created["uuid"]
        assert body["task"]["boardUuid"] == str(bu)
    finally:
        db.kanban_delete_board(bu)


def test_get_task_404_for_unknown(app_ctx, client):
    resp = client.get(f"/kanban/api/tasks/{uuid4()}")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_get_task_400_for_bad_uuid(app_ctx, client):
    resp = client.get("/kanban/api/tasks/not-a-uuid")
    assert resp.status_code == 400
