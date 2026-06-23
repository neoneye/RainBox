"""Tests for the assistant control channel (Phase 6): stop/redirect rows the
operator inserts and the running loop consumes at a step boundary.
"""

from uuid import uuid4

import pytest

import db
from db import AssistantControl, AssistantRun


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def run(app_ctx):
    r = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        yield r
    finally:
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == r.uuid).delete()
        db.db.session.commit()


def test_create_control_is_pending(run):
    c = db.create_assistant_control(run_uuid=run.uuid, command="stop")
    assert c.state == "pending"
    assert c.command == "stop"


def test_list_pending_controls_in_order(run):
    a = db.create_assistant_control(run_uuid=run.uuid, command="redirect",
                                    payload={"instruction": "focus on git"})
    b = db.create_assistant_control(run_uuid=run.uuid, command="stop")
    pending = db.list_pending_controls(run.uuid)
    assert [c.id for c in pending] == [a.id, b.id]


def test_mark_control_applied_excludes_it_from_pending(run):
    c = db.create_assistant_control(run_uuid=run.uuid, command="stop")
    db.mark_control_state(c, "applied", note="stopped at step 1")
    assert c.state == "applied"
    assert c.applied_at is not None
    assert db.list_pending_controls(run.uuid) == []


def test_run_status_allows_stopping(run):
    # The widened CHECK admits the transient 'stopping' state.
    db.finish_run(run, "stopping")
    db.db.session.refresh(run)
    assert run.status == "stopping"


def test_control_cascades_when_run_deleted(app_ctx):
    r = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    c = db.create_assistant_control(run_uuid=r.uuid, command="stop")
    cid = c.id
    db.db.session.query(AssistantRun).filter(AssistantRun.uuid == r.uuid).delete()
    db.db.session.commit()
    assert db.db.session.get(AssistantControl, cid) is None
