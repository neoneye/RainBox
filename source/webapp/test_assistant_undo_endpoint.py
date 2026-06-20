"""POST /chat/api/assistant/write-intents/<uuid>/undo reverts a kanban move."""

from uuid import uuid4

import pytest

import db
from webapp.chat_api import app as flask_app


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


def test_undo_endpoint_unknown_intent_returns_ok_false(app_ctx, client):
    resp = client.post(f"/chat/api/assistant/write-intents/{uuid4()}/undo")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
