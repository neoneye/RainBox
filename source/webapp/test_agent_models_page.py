"""Tests for the /agent_models page: only agents whose class consumes a
model-group binding (Agent.uses_model_group, default True) are listed; agents
that opted out (direct_chat, workspace_shell, query, conversation) are hidden
and their bindings can't be posted."""

import pytest

import db
from agents.config import DIRECT_CHAT_UUID, agent_config, resolve_agent_class


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


def test_uses_model_group_flags():
    """The opt-out is a class trait: default True on the base class, False on
    the agents that never read a binding."""
    from agents.base import Agent, ModelGroupAgent

    assert Agent.uses_model_group is True
    assert ModelGroupAgent.uses_model_group is True
    for kind in ("direct_chat", "workspace_shell", "query", "conversation"):
        assert resolve_agent_class(kind).uses_model_group is False, kind
    # A kind not in the class table falls back to ModelGroupAgent -> True.
    assert resolve_agent_class("dreamer").uses_model_group is True


def test_page_hides_agents_that_dont_use_model_groups(client):
    test_client, _app = client
    resp = test_client.get("/agent_models")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for hidden in ("direct_chat", "workspace_shell", "conversation"):
        assert str(agent_config[hidden]["uuid"]) not in body, hidden
    assert str(agent_config["query"]["uuid"]) not in body
    # Model-group consumers are still there (query_router also guards against
    # an over-eager 'query' substring filter).
    for shown in ("dreamer", "router", "assistant", "query_router"):
        assert str(agent_config[shown]["uuid"]) in body, shown


def test_post_binding_rejected_for_opted_out_agent(client):
    test_client, _app = client
    resp = test_client.post(
        "/agent_models",
        data={"agent_uuid": str(DIRECT_CHAT_UUID), "model_group": ""},
    )
    assert resp.status_code == 400


def test_persona_roles_follow_their_agent_kind_class(client):
    """persona_egon runs chat_unstructured (a model-group agent), so it stays
    listed even though its role name has no class-table entry."""
    test_client, _app = client
    resp = test_client.get("/agent_models")
    assert str(agent_config["persona_egon"]["uuid"]) in resp.get_data(as_text=True)
