"""The /assistant page — run-centric inspector over the assistant trace.

Renders recent runs, the selected run's step timeline with each write-intent
inline, and the state-appropriate lifecycle buttons (confirm/reject/undo,
stop/redirect) wired to the existing endpoints. Read-only data; the buttons are
the only writes.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
import webapp  # noqa: F401 — registers all views (incl. /assistant) on the app
from db import AssistantRun
from webapp.assistant_views import _bucket_runs, _format_duration
from webapp.core import app as flask_app


def test_format_duration():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert _format_duration(base, base + timedelta(seconds=2.34)) == "2.3s"
    assert _format_duration(base, base + timedelta(seconds=65)) == "1m 5s"
    assert _format_duration(base, base + timedelta(hours=1, minutes=30)) == "1h 30m"
    assert _format_duration(base, None) is None      # still running
    assert _format_duration(None, base) is None


class _FakeRun:
    """Minimal stand-in for an AssistantRun — _bucket_runs only reads
    status/summary."""
    def __init__(self, status, outcome=None):
        self.status = status
        self.summary = {"outcome": outcome} if outcome else None


def test_bucket_runs_files_each_run_under_matching_facets():
    running = _FakeRun("running")
    stopped = _FakeRun("stopped")
    resolved = _FakeRun("finished", outcome="resolved")
    failed = _FakeRun("failed")
    partial = _FakeRun("finished", outcome="partial")
    runs = [running, stopped, resolved, failed, partial]
    f = {b["name"]: b for b in _bucket_runs(runs)}
    assert f["Recent"]["runs"] == runs                  # holds all
    assert running in f["Running"]["runs"] and f["Running"]["count"] == 1
    assert stopped in f["Stopped"]["runs"]
    assert resolved in f["Resolved"]["runs"]
    # failed run + a 'partial' outcome both count as unresolved
    assert failed in f["Unresolved"]["runs"] and partial in f["Unresolved"]["runs"]
    assert f["Unresolved"]["count"] == 2
    # facets overlap: a running run is ALSO under Recent
    assert running in f["Recent"]["runs"]


@pytest.fixture
def app_ctx():
    application = db.make_app()
    db.init_db(application)
    ctx = application.app_context()
    ctx.push()
    try:
        yield application
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _room():
    human = db.get_human_user()
    assert human is not None
    return db.create_chatroom(f"as-view-{uuid4().hex[:8]}", human.uuid, [])


def _cleanup(run_uuid, room_uuid) -> None:
    # assistant_step / assistant_write_intent cascade off assistant_run.
    db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run_uuid).delete()
    db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room_uuid).delete()
    db.db.session.commit()


def test_runs_list_renders(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        resp = client.get("/assistant")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The virtual status folders render…
        for folder in ("Recent", "Running", "Stopped", "Resolved", "Unresolved"):
            assert f'class="as-fname">{folder}<' in body
        # …and the run appears (uuid-addressed) under them.
        assert f"?id={run.uuid}" in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_timeline_shows_step_with_inline_intent_and_undo(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="kanban_move_task", reason="move it")
    db.settle_assistant_step(step, phase="observed", observation_preview="moved the task")
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="kanban_move_task",
        payload={"task_uuid": "t"}, preview_text="move", room_uuid=room.uuid,
        agent_uuid=run.agent_uuid, state="completed",
        result={"undo": {"capability": "kanban_delete_task", "payload": {}}})
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "kanban_move_task" in body            # step action + intent capability
        assert "moved the task" in body              # observation rendered
        # a completed log-and-undo intent (carries an undo record) → Undo button
        assert f"/chat/api/assistant/write-intents/{intent.uuid}/undo" in body
        # not a proposed intent → no confirm/reject
        assert f"/write-intents/{intent.uuid}/confirm" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_proposed_intent_shows_confirm_and_reject(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="set_reminder", reason="schedule")
    db.settle_assistant_step(step, phase="observed", observation_preview="proposed")
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="set_reminder",
        payload={"text": "x", "when": "2026-06-24T09:00"}, preview_text="fires …",
        room_uuid=room.uuid, agent_uuid=run.agent_uuid)  # default state=proposed
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert f"/chat/api/assistant/write-intents/{intent.uuid}/confirm" in body
        assert f"/chat/api/assistant/write-intents/{intent.uuid}/reject" in body
        # proposed → not undoable
        assert f"/write-intents/{intent.uuid}/undo" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_completed_intent_without_undo_has_no_action(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="activate_memory", reason="activate")
    db.settle_assistant_step(step, phase="observed", observation_preview="done")
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="activate_memory",
        payload={"memory_uuid": "m"}, preview_text="activated", room_uuid=room.uuid,
        agent_uuid=run.agent_uuid, state="completed", result={})  # no undo record
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert f"/write-intents/{intent.uuid}/undo" not in body
        assert f"/write-intents/{intent.uuid}/confirm" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_stop_redirect_only_for_running_run(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())  # status=running
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert f"/chat/api/assistant/runs/{run.uuid}/stop" in body
        assert "ppRedirect(" in body
        # Once finished, the live-only controls disappear.
        db.finish_run(run, "finished")
        body2 = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert f"/chat/api/assistant/runs/{run.uuid}/stop" not in body2
    finally:
        _cleanup(run.uuid, room.uuid)


def test_trigger_block_at_top_and_verdict_at_bottom(app_ctx, client):
    room = _room()
    human = db.get_human_user()
    db.post_chat_message(room.uuid, human.uuid, "please mark the task done")
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished", final_summary="all done — the verdict")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        # Trigger block shows the triggering message + a link into chat.
        assert "Trigger" in body
        assert "please mark the task done" in body
        # links into chat AND anchors on the specific triggering message
        assert f"/chat?id={run.room_uuid}&msg=" in body
        # The verdict (final_summary) is present and sits BELOW the trigger.
        assert "Verdict" in body and "all done — the verdict" in body
        assert body.index("Verdict") > body.index("Trigger")
    finally:
        _cleanup(run.uuid, room.uuid)


def test_run_is_addressable_and_shown_by_uuid(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        # Addressable only by uuid via ?id=; the kebab offers Copy id.
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert str(run.uuid) in body
        assert "Copy id" in body
        assert "Select a run" not in body          # a run is selected
        # Only a uuid ?id= resolves: a non-uuid value and the old ?run= don't.
        assert "Select a run" in client.get(
            "/assistant?id=not-a-uuid").get_data(as_text=True)
        assert "Select a run" in client.get(
            f"/assistant?run={run.uuid}").get_data(as_text=True)
        # The runs list links address runs by uuid.
        listing = client.get("/assistant").get_data(as_text=True)
        assert f"?id={run.uuid}" in listing
    finally:
        _cleanup(run.uuid, room.uuid)


def test_run_summary_renders_in_list_and_detail(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    db.set_run_summary(run, {
        "trigger": "file the weekly report", "obstacles": ["the disk was full"],
        "outcome": "partial"})
    try:
        listing = client.get("/assistant").get_data(as_text=True)
        assert "file the weekly report" in listing   # the summary trigger in the leaf
        detail = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "the disk was full" in detail          # obstacle in the detail pane
        assert "partial" in detail                    # outcome badge
    finally:
        _cleanup(run.uuid, room.uuid)


def test_unsummarized_run_shows_pending(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")  # no summary set
    try:
        detail = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "Not yet summarized" in detail
    finally:
        _cleanup(run.uuid, room.uuid)


def test_leaf_shows_run_and_fail_indicators(app_ctx, client):
    room = _room()
    running = db.start_assistant_run(  # status=running
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    failed = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(failed, "failed")
    try:
        body = client.get("/assistant").get_data(as_text=True)
        assert 'class="as-ind run"' in body    # in-progress indicator (⏳)
        assert 'class="as-ind fail"' in body   # failed indicator (✗)
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.uuid.in_([running.uuid, failed.uuid])
        ).delete(synchronize_session=False)
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == room.uuid).delete()
        db.db.session.commit()


def test_step_token_counts_render_in_timeline(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    with_tok = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="query_memory", reason="r",
        input_tokens=412, output_tokens=87, duration_ms=5100)
    db.settle_assistant_step(with_tok, phase="observed", observation_preview="ok")
    # a control step has no counts
    db.append_assistant_step(run_uuid=run.uuid, step_index=1, phase="control", action="stop")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "in 412 tok · out 87 tok · took 5.1s" in body   # the step with metrics
        # exactly one metrics line (the control step shows none)
        assert body.count('class="toks"') == 1
    finally:
        _cleanup(run.uuid, room.uuid)


def test_step_model_renders_as_a_link(app_ctx, client):
    mc = db.create_model_config("qwen-2.5-7b", {})
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="query_memory", reason="r",
        model_uuid=mc.uuid)
    db.settle_assistant_step(step, phase="observed", observation_preview="ok")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        # the model name links to its /models config page
        assert f'href="/models?id={mc.uuid}"' in body
        assert "qwen-2.5-7b" in body
    finally:
        _cleanup(run.uuid, room.uuid)
        db.db.session.query(db.ModelConfig).filter(db.ModelConfig.uuid == mc.uuid).delete()
        db.db.session.commit()


def test_selected_run_has_kebab_with_actions(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())  # status=running
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        # The kebab on the selected run carries its uuid/room/status.
        assert f"asKebab(event, '{run.uuid}', '{run.room_uuid}', 'running')" in body
        # The menu offers Copy id + Open in chat, and a Stop for a running run.
        assert "Copy id" in body
        assert "Open in chat" in body
        assert f"/chat/api/assistant/runs/' + uuid + '/stop" in body  # Stop target (JS)
    finally:
        _cleanup(run.uuid, room.uuid)


def test_nav_link_present(app_ctx, client):
    body = client.get("/assistant").get_data(as_text=True)
    assert 'href="/assistant"' in body and ">Assistant<" in body
