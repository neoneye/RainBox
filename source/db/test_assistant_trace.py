"""Tests for the durable assistant trace: assistant_run / assistant_step tables
and the db.start_assistant_run / append_assistant_step / finish_run helpers.

The trace is the *source of truth* (not journal.result, not chat rows). These
tests exercise the helpers directly — the loop wiring is tested in
agents/test_assistant.py.
"""

import json
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


def _cleanup_run(run_id: int) -> None:
    # assistant_step has an ON DELETE CASCADE FK to assistant_run.
    db.db.session.query(AssistantRun).filter(AssistantRun.id == run_id).delete()
    db.db.session.commit()


def test_start_assistant_run_creates_running_row(app_ctx):
    run = db.start_assistant_run(
        journal_id=123, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        assert run.id is not None
        assert run.status == "running"
        assert run.step_limit == 6
        assert run.finished_at is None
    finally:
        _cleanup_run(run.id)


def test_append_step_is_committed_before_the_next_append(app_ctx):
    """Trace-before-action durability: a `running` row is committed as soon as
    append_assistant_step returns — before the action's observation is recorded —
    so a kill mid-action still leaves the last committed step."""
    run = db.start_assistant_run(
        journal_id=1, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.append_assistant_step(
            run_id=run.id, step_index=0, phase="running",
            action="query_qa", reason="look it up", args={"query": "git status"},
        )
        # Simulate another reader (fresh state) mid-action: the running row is
        # already durable, before any "observed" row exists.
        db.db.session.expire_all()
        running = (
            db.db.session.query(AssistantStep)
            .filter(AssistantStep.run_id == run.id, AssistantStep.phase == "running")
            .all()
        )
        assert len(running) == 1
        assert running[0].action == "query_qa"
        assert running[0].args == {"query": "git status"}
    finally:
        _cleanup_run(run.id)


def test_failed_step_records_error_and_is_queryable_by_phase(app_ctx):
    run = db.start_assistant_run(
        journal_id=1, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.append_assistant_step(
            run_id=run.id, step_index=0, phase="failed",
            action="query_qa", error="boom: kaboom",
        )
        # Queryable by phase/action without scanning chat history.
        failed = (
            db.db.session.query(AssistantStep)
            .filter(AssistantStep.run_id == run.id, AssistantStep.phase == "failed")
            .all()
        )
        assert len(failed) == 1
        assert failed[0].error == "boom: kaboom"
    finally:
        _cleanup_run(run.id)


def test_append_posts_thin_debug_assistant_chat_pointer(app_ctx):
    """The inline anchor is a debug-assistant JSON row carrying only the
    run_id/step_index pointer — never 'progress' (which gets reaped) and never
    the full payload."""
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"trace-ptr-{uuid4().hex[:8]}", human.uuid, [])
    run = db.start_assistant_run(
        journal_id=1, room_uuid=chatroom.uuid, agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.append_assistant_step(
            run_id=run.id, step_index=0, phase="planned",
            action="reply", reason="answer now", args={"message": "hi"},
        )
        rows = [
            m for m in db.list_room_messages(chatroom.uuid)
            if m["kind"] == "debug-assistant"
        ]
        assert len(rows) == 1
        assert rows[0]["content_type"] == "json"
        payload = json.loads(rows[0]["text"])
        assert payload == {"run_id": run.id, "step_index": 0}
        # The pointer must not carry the step payload (args/reason).
        assert "message" not in rows[0]["text"]
    finally:
        _cleanup_run(run.id)
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def test_finish_run_sets_terminal_status_and_summary(app_ctx):
    run = db.start_assistant_run(
        journal_id=1, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.finish_run(run, "finished", final_summary="all done")
        db.db.session.expire_all()
        reloaded = db.db.session.get(AssistantRun, run.id)
        assert reloaded.status == "finished"
        assert reloaded.final_summary == "all done"
        assert reloaded.finished_at is not None
    finally:
        _cleanup_run(run.id)


def test_get_assistant_run_returns_row_or_none(app_ctx):
    run = db.start_assistant_run(
        journal_id=1, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        assert db.get_assistant_run(run.id) is not None
        assert db.get_assistant_run(999999999) is None
    finally:
        _cleanup_run(run.id)


def test_init_db_twice_preserves_sentinel_assistant_run(app_ctx):
    """New trace tables are created by create_all and never wiped by a re-init."""
    sentinel = db.start_assistant_run(
        journal_id=987654, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.init_db(app_ctx)
        db.init_db(app_ctx)  # second call must also succeed
        db.db.session.expire_all()
        reloaded = db.db.session.get(AssistantRun, sentinel.id)
        assert reloaded is not None, "init_db erased existing assistant_run rows"
        assert reloaded.journal_id == 987654
    finally:
        _cleanup_run(sentinel.id)
