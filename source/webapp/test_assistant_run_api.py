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
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run_id).delete()
        db.db.session.commit()


def test_run_endpoint_returns_run_and_steps(client):
    flask_client, app = client
    with app.app_context():
        # Real room: the terminal `observed` step posts an anchor chat row.
        human = db.get_human_user()
        chatroom = db.create_chatroom(f"runapi-{uuid4().hex[:8]}", human.uuid, [])
        run = db.start_assistant_run(
            journal_id=uuid4(), room_uuid=chatroom.uuid, agent_uuid=uuid4(), step_limit=6
        )
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=0, phase="running",
            action="query_memory", reason="look it up", args={"query": "git status"},
        )
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=0, phase="observed",
            action="query_memory", observation_preview="Working tree clean.",
        )
        run_id = run.uuid
    try:
        resp = flask_client.get(f"/chat/api/assistant/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["run"]["uuid"] == str(run_id)
        assert body["run"]["status"] == "running"
        phases = [s["phase"] for s in body["steps"]]
        assert phases == ["running", "observed"]
        observed = body["steps"][1]
        assert observed["action"] == "query_memory"
        assert observed["observation_preview"] == "Working tree clean."
        # The first step carries the structured args verbatim.
        assert body["steps"][0]["args"] == {"query": "git status"}
    finally:
        _cleanup_run(app, run_id)


def test_run_endpoint_404_for_unknown_run(client):
    flask_client, _app = client
    resp = flask_client.get("/chat/api/assistant/runs/999999999")
    assert resp.status_code == 404


def test_chat_page_renders_debug_assistant_from_text(client):
    """debug-assistant rows render from their self-contained text (no pointer
    fetch) — the page handles the kind and shows the text verbatim."""
    flask_client, _app = client
    html = flask_client.get("/chat").get_data(as_text=True)
    assert "debug-assistant" in html
    assert "/chat/api/assistant/runs/" not in html  # the fetch-renderer is gone
