"""Tests for webapp/assistant_overview_api.py — the JSON API behind
/assistant-overview.

HTTP goes through the real app (webapp.core.app); seeding uses a db.make_app()
context — both hit rainbox_claude (conftest). Each test tags its runs' summary
so the q-filtered response sees only its own rows."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import db
from db import AssistantRun, AssistantStep
from webapp.core import app


def _seed(created, summary_trigger, *, outcome=None, status="finished", n_steps=3):
    a = db.make_app()
    with a.app_context():
        run = AssistantRun(
            uuid=uuid4(), journal_id=uuid4(), room_uuid=uuid4(),
            agent_uuid=uuid4(), status=status, step_limit=6,
            started_at=datetime.now(UTC),
            finished_at=None if status in ("running", "stopping")
            else datetime.now(UTC) + timedelta(seconds=12),
            summary={"trigger": summary_trigger, "outcome": outcome},
        )
        db.db.session.add(run)
        for i in range(n_steps):
            db.db.session.add(AssistantStep(
                uuid=uuid4(), run_uuid=run.uuid, step_index=i, phase="observed"))
        db.db.session.commit()
        created.append(run.uuid)
        return run.uuid


def _cleanup(created):
    a = db.make_app()
    with a.app_context():
        for ru in created:
            db.db.session.query(AssistantRun).filter(
                AssistantRun.uuid == ru).delete()
        db.db.session.commit()


def test_runs_endpoint_shape():
    created = []
    tag = uuid4().hex[:8]
    try:
        rid = _seed(created, f"candy {tag}", outcome="resolved")
        out = app.test_client().get(
            f"/assistant-overview/api/runs?q={tag}").get_json()
        assert out["ok"] is True
        assert out["total"] == 1
        assert out["page"] == 1 and out["pages"] == 1
        row = out["runs"][0]
        assert row["uuid"] == str(rid)
        assert row["summary"] == f"candy {tag}"
        assert row["status_kind"] == "resolved"
        assert row["status_label"] == "Resolved"
        assert row["steps"] == 3
        assert row["step_limit"] == 6
        assert row["duration"]  # finished → a duration string
        assert set(out["counts"]) == {
            "all", "running", "stopped", "resolved", "unresolved"}
    finally:
        _cleanup(created)


def test_running_run_null_duration_and_pinned():
    created = []
    tag = uuid4().hex[:8]
    try:
        _seed(created, f"done {tag}", outcome="resolved", status="finished")
        _seed(created, f"live {tag}", status="running", n_steps=1)
        out = app.test_client().get(
            f"/assistant-overview/api/runs?q={tag}").get_json()
        assert out["runs"][0]["status_kind"] == "running"
        assert out["runs"][0]["duration"] is None
    finally:
        _cleanup(created)


def test_pending_summary_is_null():
    created = []
    tag = uuid4().hex[:8]
    try:
        # uuid match (not summary) so a null trigger still surfaces.
        rid = _seed(created, None, status="finished")
        out = app.test_client().get(
            f"/assistant-overview/api/runs?q={str(rid)[:8]}").get_json()
        assert out["runs"][0]["summary"] is None
    finally:
        _cleanup(created)


def test_pagination_clamps_and_paginates():
    created = []
    tag = uuid4().hex[:8]
    try:
        for i in range(7):
            _seed(created, f"item {i} {tag}", outcome="resolved")
        out = app.test_client().get(
            f"/assistant-overview/api/runs?q={tag}&per_page=5&page=2").get_json()
        assert out["total"] == 7
        assert out["pages"] == 2
        assert out["per_page"] == 5
        assert len(out["runs"]) == 2
    finally:
        _cleanup(created)


def test_range_filters_by_recency():
    created = []
    tag = uuid4().hex[:8]
    try:
        # One recent run; one well outside any picker window.
        _seed(created, f"recent {tag}", outcome="resolved")
        a = db.make_app()
        with a.app_context():
            old = AssistantRun(
                uuid=uuid4(), journal_id=uuid4(), room_uuid=uuid4(),
                agent_uuid=uuid4(), status="finished", step_limit=6,
                started_at=datetime.now(UTC) - timedelta(days=5),
                finished_at=datetime.now(UTC) - timedelta(days=5),
                summary={"trigger": f"old {tag}", "outcome": "resolved"})
            db.db.session.add(old)
            db.db.session.commit()
            created.append(old.uuid)
        out = app.test_client().get(
            f"/assistant-overview/api/runs?q={tag}&range=24h").get_json()
        assert out["total"] == 1
        assert out["runs"][0]["summary"] == f"recent {tag}"
        assert out["counts"]["all"] == 1  # counts honor the range too
        # Any time sees both.
        allout = app.test_client().get(
            f"/assistant-overview/api/runs?q={tag}&range=all").get_json()
        assert allout["total"] == 2
    finally:
        _cleanup(created)


def test_bad_range_param_is_400():
    resp = app.test_client().get("/assistant-overview/api/runs?range=bogus")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_bad_page_param_is_400():
    resp = app.test_client().get("/assistant-overview/api/runs?page=abc")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_bad_status_param_is_400():
    resp = app.test_client().get("/assistant-overview/api/runs?status=bogus")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
