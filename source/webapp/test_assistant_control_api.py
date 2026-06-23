"""HTTP tests for the /stop and /redirect control endpoints."""

from uuid import uuid4

import pytest

import db
from db import AssistantControl, AssistantRun


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


def _run(app):
    with app.app_context():
        return db.start_assistant_run(
            journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
        ).uuid


def _cleanup(app, run_id):
    with app.app_context():
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run_id).delete()
        db.db.session.commit()


def test_stop_endpoint_inserts_control_and_flags_stopping(client):
    flask_client, app = client
    run_id = _run(app)
    try:
        resp = flask_client.post(f"/chat/api/assistant/runs/{run_id}/stop")
        assert resp.status_code == 200
        with app.app_context():
            controls = db.list_pending_controls(run_id)
            assert [c.command for c in controls] == ["stop"]
            assert db.get_assistant_run(run_id).status == "stopping"
    finally:
        _cleanup(app, run_id)


def test_redirect_endpoint_inserts_redirect_control(client):
    flask_client, app = client
    run_id = _run(app)
    try:
        resp = flask_client.post(
            f"/chat/api/assistant/runs/{run_id}/redirect",
            json={"instruction": "focus on the failing test"},
        )
        assert resp.status_code == 200
        with app.app_context():
            controls = db.list_pending_controls(run_id)
            assert len(controls) == 1
            assert controls[0].command == "redirect"
            assert controls[0].payload["instruction"] == "focus on the failing test"
    finally:
        _cleanup(app, run_id)


def test_redirect_requires_instruction(client):
    flask_client, app = client
    run_id = _run(app)
    try:
        resp = flask_client.post(f"/chat/api/assistant/runs/{run_id}/redirect", json={})
        assert resp.status_code == 400
    finally:
        _cleanup(app, run_id)


def test_stop_unknown_run_404(client):
    flask_client, _app = client
    assert flask_client.post("/chat/api/assistant/runs/999999999/stop").status_code == 404
