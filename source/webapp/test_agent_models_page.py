"""Tests for the /agentmodel page: only agents whose class consumes a
model-group binding (Agent.uses_model_group, default True) are listed; agents
that opted out (direct_chat, workspace_shell, query) are hidden and their
bindings can't be posted."""

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
    for kind in ("direct_chat", "workspace_shell", "query"):
        assert resolve_agent_class(kind).uses_model_group is False, kind
    # A kind not in the class table falls back to ModelGroupAgent -> True.
    assert resolve_agent_class("dreamer").uses_model_group is True


def test_legacy_snake_case_path_redirects(client):
    # The page moved from /agent_models to /agentmodel (matching the other
    # single-word pages); old links redirect and keep their query string.
    test_client, _app = client
    resp = test_client.get("/agent_models?saved=1")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/agentmodel?saved=1")


def test_page_hides_agents_that_dont_use_model_groups(client):
    test_client, _app = client
    resp = test_client.get("/agentmodel")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # `assistant` is hidden for a different reason than the others: it runs
    # LLM calls, but each one binds through its own assistant.* slot, so a row
    # for the agent itself would be a control nothing reads.
    for hidden in ("direct_chat", "workspace_shell", "assistant"):
        assert str(agent_config[hidden]["uuid"]) not in body, hidden
    assert str(agent_config["query"]["uuid"]) not in body
    # Model-group consumers are still there (query_router also guards against
    # an over-eager 'query' substring filter).
    for shown in ("dreamer", "router", "assistant.decide", "query_router"):
        assert str(agent_config[shown]["uuid"]) in body, shown


def test_post_binding_rejected_for_opted_out_agent(client):
    test_client, _app = client
    resp = test_client.post(
        "/agentmodel",
        data={"agent_uuid": str(DIRECT_CHAT_UUID), "model_group": ""},
    )
    assert resp.status_code == 400


def test_dotted_rows_are_grouped_under_one_heading(client):
    """Nine assistant rows read as one family with one heading, not as nine
    rows an operator has to recognize as related."""
    test_client, _app = client
    body = test_client.get("/agentmodel").get_data(as_text=True)
    assert body.count('<tr class="section">') == 1
    assert '<span class="section-title">assistant.*</span>' in body


def test_an_unassigned_slot_shows_what_it_inherits(client):
    """"none assigned" would read as "this call does not happen"; the row runs
    on assistant.default, and says so."""
    test_client, app = client
    with app.app_context():
        db.set_agent_model_binding(
            agent_config["assistant.decide"]["uuid"], None)
    body = test_client.get("/agentmodel").get_data(as_text=True)
    assert "&rarr; assistant.default" in body


def test_the_default_row_inherits_from_nothing(client):
    """The end of the chain. A `.default` that pointed at itself would be a
    fallback that never resolves."""
    from webapp.agent_views import _fallback_name

    assert _fallback_name("assistant.default") == ""
    assert _fallback_name("assistant.decide") == "assistant.default"
    assert _fallback_name("dreamer") == ""
    # A dotted name whose family has no default inherits nothing rather than
    # naming a row that does not exist.
    assert _fallback_name("nosuchfamily.step") == ""
