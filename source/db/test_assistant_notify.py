"""The assistant live-update NOTIFYs: run/step/model-checkpoint writes emit a
chat_events payload keyed by `assistant_run_uuid` (and carrying NO `room_uuid`,
which is how chat clients know to ignore it) so the /assistant page can refresh
the run it is showing.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

import db
from db import AssistantRun
from db.models import CHAT_NOTIFY_CHANNEL


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


@pytest.fixture
def listener():
    """An autocommit connection LISTENing on chat_events (same DSN as the app).
    Matches the pattern in db/test_chat_progress.py."""
    conn = psycopg.connect(db.psycopg_dsn(), autocommit=True)
    conn.execute(f"LISTEN {CHAT_NOTIFY_CHANNEL}")
    try:
        yield conn
    finally:
        conn.close()


def _assistant_events(conn, count: int = 1, timeout: float = 2.0):
    """Collect NOTIFY payloads until `count` assistant-keyed ones arrived (a
    step settle also posts a room-keyed debug-assistant chat NOTIFY — those are
    filtered out here)."""
    out = []
    for note in conn.notifies(timeout=timeout):
        payload = json.loads(note.payload)
        if "assistant_run_uuid" in payload:
            out.append(payload)
            if len(out) >= count:
                break
    return out


def _cleanup_run(run_uuid, room_uuid=None) -> None:
    db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run_uuid).delete()
    if room_uuid is not None:
        db.db.session.query(db.ChatMessage).filter(
            db.ChatMessage.room_uuid == room_uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
    db.db.session.commit()


def _room():
    human = db.get_human_user()
    assert human is not None
    return db.create_chatroom(f"as-notify-{uuid4().hex[:8]}", human.uuid, [])


def test_run_lifecycle_notifies(app_ctx, listener):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        events = _assistant_events(listener)
        assert events == [{"assistant_run_uuid": str(run.uuid), "event": "run"}]

        db.finish_run(run, "finished", final_summary="did the thing")
        events = _assistant_events(listener)
        assert events == [{"assistant_run_uuid": str(run.uuid), "event": "run"}]
    finally:
        _cleanup_run(run.uuid)


def test_step_open_and_settle_notify(app_ctx, listener):
    # A real room: the settle posts a debug-assistant chat row into it.
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4(), step_limit=6
    )
    _assistant_events(listener)  # drain the run-start event
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="memory_query",
            reason="look it up", args={"query": "x"},
        )
        events = _assistant_events(listener)
        assert events == [{"assistant_run_uuid": str(run.uuid), "event": "step"}]
        # No room_uuid: the payload must stay invisible to chat clients.
        assert "room_uuid" not in events[0]

        db.settle_assistant_step(
            step, phase="observed", observation_preview="found it"
        )
        events = _assistant_events(listener)
        assert events == [{"assistant_run_uuid": str(run.uuid), "event": "step"}]
    finally:
        _cleanup_run(run.uuid, room.uuid)


def test_model_checkpoint_notifies(app_ctx, listener):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    _assistant_events(listener)
    model_uuid = uuid4()
    try:
        db.checkpoint_assistant_call(
            run, step_index=0, system_prompt="s", user_prompt="u",
            requested_at=datetime.now(UTC), model_group_uuid=None,
        )
        db.checkpoint_assistant_model_attempt(
            run, model_uuid=model_uuid, model_name="test-model", timeout_seconds=10.0
        )
        _assistant_events(listener)  # drain the call-checkpoint event
        db.checkpoint_assistant_model_progress(
            run, model_uuid=model_uuid, reasoning="thinking...", response_text=None
        )
        events = _assistant_events(listener)
        assert {"assistant_run_uuid": str(run.uuid), "event": "model"} in events
    finally:
        _cleanup_run(run.uuid)
