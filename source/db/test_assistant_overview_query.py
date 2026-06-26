"""Tests for db.list_assistant_runs_page + db.assistant_step_counts — the
paginated/filtered/sorted run query behind /assistant-overview.

Live local Postgres (rainbox_claude via conftest). Each test seeds runs whose
summary trigger carries a unique tag, so the q-filtered counts see only its own
rows regardless of what else lives in the table; rows are deleted afterwards
(assistant_step cascades on the run FK).
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
from db import AssistantRun, AssistantStep


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


def _mk_run(created, *, summary_trigger=None, outcome=None, status="finished",
            started=None, n_steps=0):
    """Seed one run (+ n_steps) and remember its uuid for cleanup."""
    started = started or datetime.now(UTC)
    run = AssistantRun(
        uuid=uuid4(), journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(),
        status=status, step_limit=6, started_at=started,
        finished_at=None if status in ("running", "stopping")
        else started + timedelta(seconds=10),
    )
    if summary_trigger is not None or outcome is not None:
        run.summary = {"trigger": summary_trigger, "outcome": outcome}
    db.db.session.add(run)
    for i in range(n_steps):
        db.db.session.add(AssistantStep(
            uuid=uuid4(), run_uuid=run.uuid, step_index=i, phase="observed"))
    db.db.session.commit()
    created.append(run.uuid)
    return run.uuid


def _cleanup(created):
    for ru in created:
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == ru).delete()
    db.db.session.commit()


def test_page_filters_by_summary_substring(app_ctx):
    created = []
    tag = uuid4().hex[:8]
    try:
        _mk_run(created, summary_trigger=f"buy candy {tag}", outcome="resolved")
        _mk_run(created, summary_trigger=f"solve riemann {tag}", outcome="failed")
        runs, total, _counts = db.list_assistant_runs_page(q=f"candy {tag}")
        assert total == 1
        assert runs[0].summary["trigger"] == f"buy candy {tag}"
    finally:
        _cleanup(created)


def test_step_counts_aggregate(app_ctx):
    created = []
    try:
        rid = _mk_run(created, summary_trigger="x", outcome="resolved", n_steps=4)
        other = _mk_run(created, summary_trigger="y", outcome="resolved", n_steps=0)
        counts = db.assistant_step_counts([rid, other])
        assert counts.get(rid) == 4
        assert other not in counts  # zero-step runs are absent
    finally:
        _cleanup(created)


def test_running_runs_pinned_first(app_ctx):
    created = []
    tag = uuid4().hex[:8]
    old = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        _mk_run(created, summary_trigger=f"done {tag}", outcome="resolved",
                status="finished", started=old + timedelta(days=2))
        _mk_run(created, summary_trigger=f"live {tag}", status="running",
                started=old)
        runs, _t, _c = db.list_assistant_runs_page(
            q=tag, sort="started", direction="desc")
        assert runs[0].status == "running"  # pinned despite older started_at
    finally:
        _cleanup(created)


def test_status_facet_counts_and_filter(app_ctx):
    created = []
    tag = uuid4().hex[:8]
    try:
        _mk_run(created, summary_trigger=f"a {tag}", status="running")
        _mk_run(created, summary_trigger=f"b {tag}", outcome="resolved",
                status="finished")
        _mk_run(created, summary_trigger=f"c {tag}", status="stopped")
        runs, total, counts = db.list_assistant_runs_page(q=tag, status="running")
        assert counts["all"] == 3
        assert counts["running"] == 1
        assert counts["stopped"] == 1
        assert counts["resolved"] == 1
        assert total == 1  # status filter applied to the returned page
        assert all(r.status in ("running", "stopping") for r in runs)
    finally:
        _cleanup(created)


def test_since_filters_by_started_at(app_ctx):
    created = []
    tag = uuid4().hex[:8]
    now = datetime.now(UTC)
    try:
        _mk_run(created, summary_trigger=f"recent {tag}", outcome="resolved",
                started=now - timedelta(hours=1))
        _mk_run(created, summary_trigger=f"old {tag}", outcome="resolved",
                started=now - timedelta(days=5))
        runs, total, counts = db.list_assistant_runs_page(
            q=tag, since=now - timedelta(hours=3))
        assert total == 1
        assert runs[0].summary["trigger"] == f"recent {tag}"
        assert counts["all"] == 1  # counts honor the time range too
    finally:
        _cleanup(created)


def test_pagination_slices(app_ctx):
    created = []
    tag = uuid4().hex[:8]
    try:
        for i in range(7):
            _mk_run(created, summary_trigger=f"item {i} {tag}", outcome="resolved")
        page1, total, _c = db.list_assistant_runs_page(q=tag, offset=0, limit=5)
        page2, _t2, _c2 = db.list_assistant_runs_page(q=tag, offset=5, limit=5)
        assert total == 7
        assert len(page1) == 5
        assert len(page2) == 2
    finally:
        _cleanup(created)
