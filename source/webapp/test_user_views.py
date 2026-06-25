"""The /user page — an identity card for a chat participant (the human operator
or an agent), addressed by uuid via ?id=. Identity-only for now."""

from uuid import uuid4

import pytest

import db
import webapp  # noqa: F401 — registers all views (incl. /user) on the app
from agents.config import ASSISTANT_UUID, agent_config
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


def test_human_shows_operator_purpose(app_ctx, client):
    human = db.get_human_user()
    assert human is not None
    body = client.get(f"/user?id={human.uuid}").get_data(as_text=True)
    assert human.name in body
    assert "human" in body                 # the type chip
    assert "Operator" in body              # the human's purpose line
    assert str(human.uuid) in body


def test_agent_shows_its_config_description(app_ctx, client):
    agent = db.get_chat_user(ASSISTANT_UUID)
    assert agent is not None               # seeded one chat_user per agent
    body = client.get(f"/user?id={agent.uuid}").get_data(as_text=True)
    assert agent.name in body
    assert "agent" in body
    # purpose is pulled from agents/config.py by uuid
    purpose = next(e["description"] for e in agent_config.values()
                   if e["uuid"] == ASSISTANT_UUID)
    assert purpose[:30] in body


def test_unknown_and_invalid_ids_render_not_found(app_ctx, client):
    assert "User not found" in client.get(
        f"/user?id={uuid4()}").get_data(as_text=True)
    assert "User not found" in client.get(
        "/user?id=not-a-uuid").get_data(as_text=True)
    assert "User not found" in client.get("/user").get_data(as_text=True)
