"""Tests for assistant_write_intent persistence: the confirm-tier approval row
(proposed -> confirmed -> executing -> completed/failed, plus rejected).
"""

from uuid import uuid4

import pytest

import db
from db import AssistantRun, AssistantWriteIntent


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
        db.db.session.query(AssistantRun).filter(AssistantRun.id == r.id).delete()
        db.db.session.commit()


def test_create_write_intent_is_proposed_with_payload_hash(run):
    intent = db.create_write_intent(
        run_id=run.id, capability_name="activate_memory",
        payload={"memory_uuid": "abc"}, preview_text="activate memory abc",
        room_uuid=run.room_uuid, agent_uuid=run.agent_uuid,
    )
    assert intent.state == "proposed"
    assert intent.payload == {"memory_uuid": "abc"}
    assert intent.payload_hash  # bound to capability + payload
    # The hash is stable for the same capability + payload.
    assert intent.payload_hash == db.write_intent_payload_hash(
        "activate_memory", {"memory_uuid": "abc"}
    )


def test_payload_hash_changes_with_payload_or_capability(run):
    h1 = db.write_intent_payload_hash("activate_memory", {"memory_uuid": "a"})
    h2 = db.write_intent_payload_hash("activate_memory", {"memory_uuid": "b"})
    h3 = db.write_intent_payload_hash("other_cap", {"memory_uuid": "a"})
    assert h1 != h2 and h1 != h3


def test_state_transitions_stamp_timestamps(run):
    intent = db.create_write_intent(
        run_id=run.id, capability_name="activate_memory",
        payload={"x": 1}, preview_text="p", room_uuid=run.room_uuid,
        agent_uuid=run.agent_uuid,
    )
    confirmer = uuid4()
    db.set_write_intent_state(intent, "confirmed", confirmed_by_uuid=confirmer)
    assert intent.state == "confirmed"
    assert intent.confirmed_at is not None
    assert intent.confirmed_by_uuid == confirmer

    db.set_write_intent_state(intent, "executing")
    assert intent.executed_at is not None

    db.set_write_intent_state(intent, "completed", result={"ok": True})
    assert intent.state == "completed"
    assert intent.completed_at is not None
    assert intent.result == {"ok": True}


def test_get_write_intent_roundtrip(run):
    intent = db.create_write_intent(
        run_id=run.id, capability_name="activate_memory",
        payload={"x": 1}, preview_text="p", room_uuid=run.room_uuid,
        agent_uuid=run.agent_uuid,
    )
    again = db.get_write_intent(intent.uuid)
    assert again is not None and again.id == intent.id


def test_intent_cascades_when_run_deleted(app_ctx):
    r = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    intent = db.create_write_intent(
        run_id=r.id, capability_name="c", payload={},
        preview_text="p", room_uuid=r.room_uuid, agent_uuid=r.agent_uuid,
    )
    iid = intent.id
    db.db.session.query(AssistantRun).filter(AssistantRun.id == r.id).delete()
    db.db.session.commit()
    assert db.db.session.get(AssistantWriteIntent, iid) is None


def test_create_write_intent_accepts_completed_state_and_result(run):
    intent = db.create_write_intent(
        run_id=run.id, capability_name="kanban_move_task",
        payload={"task_uuid": "t", "column_uuid": "c"},
        preview_text="kanban_move_task: …",
        room_uuid=run.room_uuid, agent_uuid=run.agent_uuid,
        state="completed", result={"undo": {"capability": "kanban_move_task"}},
    )
    assert intent.state == "completed"
    assert intent.result == {"undo": {"capability": "kanban_move_task"}}
