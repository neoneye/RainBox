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


def _cleanup_run(run_uuid) -> None:
    # assistant_step has an ON DELETE CASCADE FK to assistant_run.
    db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run_uuid).delete()
    db.db.session.commit()


def test_start_assistant_run_creates_running_row(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        assert run.uuid is not None
        assert run.status == "running"
        assert run.step_limit == 6
        assert run.finished_at is None
    finally:
        _cleanup_run(run.uuid)


def test_append_step_is_committed_before_the_next_append(app_ctx):
    """Trace-before-action durability: a `running` row is committed as soon as
    append_assistant_step returns — before the action's observation is recorded —
    so a kill mid-action still leaves the last committed step."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=0, phase="running",
            action="query_qa", reason="look it up", args={"query": "git status"},
        )
        # Simulate another reader (fresh state) mid-action: the running row is
        # already durable, before any "observed" row exists.
        db.db.session.expire_all()
        running = (
            db.db.session.query(AssistantStep)
            .filter(AssistantStep.run_uuid == run.uuid, AssistantStep.phase == "running")
            .all()
        )
        assert len(running) == 1
        assert running[0].action == "query_qa"
        assert running[0].args == {"query": "git status"}
    finally:
        _cleanup_run(run.uuid)


def test_failed_step_records_error_and_is_queryable_by_phase(app_ctx):
    # A real room: a `failed` step posts a terminal anchor (chat row), which needs
    # an existing room to NOTIFY.
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"trace-fail-{uuid4().hex[:8]}", human.uuid, [])
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=chatroom.uuid, agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=0, phase="failed",
            action="query_qa", error="boom: kaboom",
        )
        # Queryable by phase/action without scanning chat history.
        failed = (
            db.db.session.query(AssistantStep)
            .filter(AssistantStep.run_uuid == run.uuid, AssistantStep.phase == "failed")
            .all()
        )
        assert len(failed) == 1
        assert failed[0].error == "boom: kaboom"
    finally:
        _cleanup_run(run.uuid)


def test_append_posts_self_contained_debug_assistant_trace(app_ctx):
    """The inline anchor's text IS the full readable trace (action/reason/args/
    observation) — self-contained, so it matches what's shown and copied. It lands
    at the terminal transition (planned posts nothing), one per step."""
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"trace-ptr-{uuid4().hex[:8]}", human.uuid, [])
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=chatroom.uuid, agent_uuid=uuid4(), step_limit=6
    )
    try:
        # `planned` posts NO anchor (the observation doesn't exist yet).
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=0, phase="planned",
            action="query_memory", reason="look it up", args={"query": "the-query"},
        )
        assert not [m for m in db.list_room_messages(chatroom.uuid)
                    if m["kind"] == "debug-assistant"]
        db.append_assistant_step(
            run_uuid=run.uuid, step_index=0, phase="observed",
            action="query_memory", reason="look it up", args={"query": "the-query"},
            observation_preview="found the fact",
        )
        rows = [
            m for m in db.list_room_messages(chatroom.uuid)
            if m["kind"] == "debug-assistant"
        ]
        assert len(rows) == 1  # exactly one anchor per step, at its terminal phase
        assert rows[0]["content_type"] == "json"
        state = json.loads(rows[0]["text"])          # the full step state as JSON
        assert state["step"] == 0
        assert state["action"] == "query_memory"
        assert state["reason"] == "look it up"
        assert state["args"] == {"query": "the-query"}  # args in full
        assert state["observation"] == "found the fact"
        assert "run_id" not in state                 # not a pointer
    finally:
        _cleanup_run(run.uuid)
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def test_open_and_append_persist_token_counts(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        opened = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="query_memory", reason="r",
            input_tokens=412, output_tokens=87)
        assert opened.input_tokens == 412 and opened.output_tokens == 87
        # append (terminal single-insert) carries them too; default is None.
        plain = db.append_assistant_step(
            run_uuid=run.uuid, step_index=1, phase="control", action="stop")
        assert plain.input_tokens is None and plain.output_tokens is None
    finally:
        _cleanup_run(run.uuid)


def test_open_then_settle_is_one_mutable_row(app_ctx):
    """A normal action step is a single row: open inserts it at `running`
    (durable before the action), settle UPDATEs the SAME row in place to its
    terminal phase — no second row appended."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"trace-settle-{uuid4().hex[:8]}", human.uuid, [])
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=chatroom.uuid, agent_uuid=uuid4(), step_limit=6
    )
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="query_memory",
            reason="look it up", args={"query": "the-query"},
        )
        assert step.uuid is not None
        assert step.phase == "running"
        # Opening posts no chat anchor (the observation doesn't exist yet).
        assert not [m for m in db.list_room_messages(chatroom.uuid)
                    if m["kind"] == "debug-assistant"]

        settled = db.settle_assistant_step(
            step, phase="observed", observation_preview="found the fact",
        )
        # Same row, mutated — not a new one.
        assert settled.id == step.id
        assert settled.uuid == step.uuid
        rows = db.list_assistant_steps(run.uuid)
        assert len(rows) == 1
        assert rows[0].phase == "observed"
        assert rows[0].observation_preview == "found the fact"
        # Exactly one terminal anchor, posted at settle.
        anchors = [m for m in db.list_room_messages(chatroom.uuid)
                   if m["kind"] == "debug-assistant"]
        assert len(anchors) == 1
        state = json.loads(anchors[0]["text"])
        assert state["action"] == "query_memory"
        assert state["observation"] == "found the fact"
    finally:
        _cleanup_run(run.uuid)
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def test_unsettled_open_step_remains_a_durable_running_row(app_ctx):
    """Crash visibility: a step opened but never settled stays as a single
    `running` row carrying action/reason/args (the trace-before-action durability
    the append-only design used to give)."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="query_qa",
            reason="look it up", args={"query": "git status"},
        )
        db.db.session.expire_all()  # simulate a fresh reader after a crash
        rows = db.list_assistant_steps(run.uuid)
        assert len(rows) == 1
        assert rows[0].phase == "running"
        assert rows[0].action == "query_qa"
        assert rows[0].args == {"query": "git status"}
    finally:
        _cleanup_run(run.uuid)


def test_write_intent_binds_step_uuid(app_ctx):
    """A write intent references its producing step by uuid — the sole step
    pointer (the old (run_id, step_index) soft pointer is gone)."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=2, action="kanban_move_task", reason="move it",
        )
        intent = db.create_write_intent(
            run_uuid=run.uuid, step_uuid=step.uuid,
            capability_name="kanban_move_task", payload={"task_uuid": "t"},
            preview_text="move", room_uuid=run.room_uuid, agent_uuid=run.agent_uuid,
            state="completed", result={"undo": {"x": 1}},
        )
        assert intent.step_uuid == step.uuid
    finally:
        _cleanup_run(run.uuid)


def test_list_assistant_runs_is_newest_first(app_ctx):
    r1 = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    r2 = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        runs = db.list_assistant_runs(limit=50)
        ids = [r.uuid for r in runs]
        # r2 was created after r1, so it sorts ahead of it.
        assert ids.index(r2.uuid) < ids.index(r1.uuid)
    finally:
        _cleanup_run(r1.uuid)
        _cleanup_run(r2.uuid)


def test_list_write_intents_for_run_buckets_by_step(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        step = db.open_assistant_step(
            run_uuid=run.uuid, step_index=0, action="kanban_move_task", reason="m")
        linked = db.create_write_intent(
            run_uuid=run.uuid, step_uuid=step.uuid, capability_name="kanban_move_task",
            payload={}, preview_text="p", room_uuid=run.room_uuid,
            agent_uuid=run.agent_uuid, state="completed", result={"undo": {}})
        unlinked = db.create_write_intent(
            run_uuid=run.uuid, capability_name="kanban_move_task", payload={},
            preview_text="p", room_uuid=run.room_uuid, agent_uuid=run.agent_uuid,
            state="completed", result={"undo": {}})  # step_uuid NULL
        intents = db.list_write_intents_for_run(run.uuid)
        assert {i.uuid for i in intents} == {linked.uuid, unlinked.uuid}
        by_step = [i for i in intents if i.step_uuid == step.uuid]
        assert [i.uuid for i in by_step] == [linked.uuid]
    finally:
        _cleanup_run(run.uuid)


def test_set_run_summary_round_trips_and_stamps_time(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        assert run.summary is None
        db.set_run_summary(run, {"trigger": "t", "obstacles": ["o"], "outcome": "partial"})
        fresh = db.get_assistant_run(run.uuid)
        assert fresh is not None
        assert fresh.summary["trigger"] == "t"
        assert fresh.summary["obstacles"] == ["o"]
        assert fresh.summary["outcome"] == "partial"
        assert fresh.summary["summarized_at"]  # stamped by the helper
    finally:
        _cleanup_run(run.uuid)


def test_get_assistant_run(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        got = db.get_assistant_run(run.uuid)
        assert got is not None and got.uuid == run.uuid
        assert db.get_assistant_run(uuid4()) is None  # unknown uuid
    finally:
        _cleanup_run(run.uuid)


def test_get_run_final_reply_returns_the_full_agent_reply(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(f"reply-{uuid4().hex[:8]}", human.uuid, [])
    agent_uuid = uuid4()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=agent_uuid)
    long_text = "VERDICT " + ("word " * 200)          # well past the 200-char summary
    db.post_chat_message(room.uuid, agent_uuid, long_text)   # the agent's reply
    db.finish_run(run, "finished", final_summary=long_text[:200])
    running = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=agent_uuid)
    try:
        reply = db.get_run_final_reply(run)
        assert reply is not None
        assert reply["text"] == long_text                      # full, not truncated
        assert reply["id"] is not None                         # int id for the chat anchor
        assert db.get_run_final_reply(running) is None          # not finished → no verdict
    finally:
        _cleanup_run(run.uuid)
        _cleanup_run(running.uuid)
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def test_get_run_trigger_message_returns_latest_human_message(app_ctx):
    human = db.get_human_user()
    assert human is not None
    room = db.create_chatroom(f"trig-{uuid4().hex[:8]}", human.uuid, [])
    db.post_chat_message(room.uuid, human.uuid, "an earlier message")
    db.post_chat_message(room.uuid, human.uuid, "please do the thing")
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    try:
        trig = db.get_run_trigger_message(run)
        assert trig is not None
        assert trig["text"] == "please do the thing"   # the latest before start
        assert trig["sender_name"] == human.name
        assert trig["sender_uuid"] == str(human.uuid)  # links to /user
        assert isinstance(trig["id"], int)             # the chat-anchor id
        # A run in a room with no human message has no trigger.
        empty = db.start_assistant_run(
            journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
        try:
            assert db.get_run_trigger_message(empty) is None
        finally:
            _cleanup_run(empty.uuid)
    finally:
        _cleanup_run(run.uuid)
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def test_finish_run_sets_terminal_status_and_summary(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.finish_run(run, "finished", final_summary="all done")
        db.db.session.expire_all()
        reloaded = db.db.session.get(AssistantRun, run.uuid)
        assert reloaded.status == "finished"
        assert reloaded.final_summary == "all done"
        assert reloaded.finished_at is not None
    finally:
        _cleanup_run(run.uuid)


def test_get_assistant_run_returns_row_or_none(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        assert db.get_assistant_run(run.uuid) is not None
        assert db.get_assistant_run(uuid4()) is None
    finally:
        _cleanup_run(run.uuid)


def test_init_db_twice_preserves_sentinel_assistant_run(app_ctx):
    """New trace tables are created by create_all and never wiped by a re-init."""
    sentinel_jid = uuid4()
    sentinel = db.start_assistant_run(
        journal_id=sentinel_jid, room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        db.init_db(app_ctx)
        db.init_db(app_ctx)  # second call must also succeed
        db.db.session.expire_all()
        reloaded = db.db.session.get(AssistantRun, sentinel.uuid)
        assert reloaded is not None, "init_db erased existing assistant_run rows"
        assert reloaded.journal_id == sentinel_jid
    finally:
        _cleanup_run(sentinel.uuid)
