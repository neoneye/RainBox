"""HTTP API tests for the assistant-run read endpoint that backs the chat UI's
inline trace rendering (PR 5).

The chat page renders a debug-assistant pointer row by fetching the run's steps
from GET /chat/api/assistant/runs/<run_id>; these tests pin that contract.
Uses the live local Postgres (rainbox_claude via conftest).
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


def _cleanup_run(app, run_id: int) -> None:
    with app.app_context():
        db.db.session.query(AssistantRun).filter(AssistantRun.id == run_id).delete()
        db.db.session.commit()


def test_run_endpoint_returns_run_and_steps(client):
    flask_client, app = client
    with app.app_context():
        run = db.start_assistant_run(
            journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
        )
        db.append_assistant_step(
            run_id=run.id, step_index=0, phase="running",
            action="query_qa", reason="look it up", args={"query": "git status"},
        )
        db.append_assistant_step(
            run_id=run.id, step_index=0, phase="observed",
            action="query_qa", observation_preview="Working tree clean.",
        )
        run_id = run.id
    try:
        resp = flask_client.get(f"/chat/api/assistant/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["run"]["id"] == run_id
        assert body["run"]["status"] == "running"
        phases = [s["phase"] for s in body["steps"]]
        assert phases == ["running", "observed"]
        observed = body["steps"][1]
        assert observed["action"] == "query_qa"
        assert observed["observation_preview"] == "Working tree clean."
        # The first step carries the structured args verbatim.
        assert body["steps"][0]["args"] == {"query": "git status"}
    finally:
        _cleanup_run(app, run_id)


def test_run_endpoint_404_for_unknown_run(client):
    flask_client, _app = client
    resp = flask_client.get("/chat/api/assistant/runs/999999999")
    assert resp.status_code == 404


def test_chat_page_includes_assistant_trace_renderer(client):
    """The /chat page ships the JS that renders debug-assistant pointer rows."""
    flask_client, _app = client
    html = flask_client.get("/chat").get_data(as_text=True)
    assert "debug-assistant" in html
    assert "/chat/api/assistant/runs/" in html
