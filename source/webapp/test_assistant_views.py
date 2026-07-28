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
from webapp.assistant_views import _format_duration
from webapp.core import app as flask_app


def test_format_duration():
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert _format_duration(base, base + timedelta(seconds=2.34)) == "2.3s"
    assert _format_duration(base, base + timedelta(seconds=65)) == "1m 5s"
    assert _format_duration(base, base + timedelta(hours=1, minutes=30)) == "1h 30m"
    assert _format_duration(base, None) is None      # still running
    assert _format_duration(None, base) is None


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


def test_assistant_page_has_no_tree_and_points_to_overview(app_ctx, client):
    resp = client.get("/assistant")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The left tree is gone…
    assert "as-tree" not in body
    assert "as-folder" not in body
    # …and the empty state points at the overview (the run finder).
    assert "/assistant-overview" in body
    assert "No run selected" in body


def test_timeline_shows_step_with_inline_intent_and_undo(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="kanban_task_column", reason="move it")
    db.settle_assistant_step(step, phase="observed", observation_preview="moved the task")
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="kanban_task_column",
        payload={"task_uuid": "t"}, preview_text="move", room_uuid=room.uuid,
        agent_uuid=run.agent_uuid, state="completed",
        result={"undo": {"capability": "kanban_task_delete", "payload": {}}})
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "kanban_task_column" in body            # step action + intent capability
        assert "moved the task" in body              # observation rendered
        # a completed log-and-undo intent (carries an undo record) → Undo button
        assert f"/chat/api/assistant/write-intents/{intent.uuid}/undo" in body
        # not a proposed intent → no confirm/reject
        assert f"/write-intents/{intent.uuid}/confirm" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_response_language_classifier_has_action_description(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid,
        step_index=0,
        phase="observed",
        action="response_language_classifier",
        reason="English request; preferred British variant.",
        observation_preview='{"languages":[{"code":"en-GB","score":5}],"audit":"OK"}',
    )
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "response_language_classifier" in body
        assert "determine which language(s) the reply should use" in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_step_is_anchored_and_has_permalink(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="look")
    db.settle_assistant_step(step, phase="observed", observation_preview="ok")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert f'id="step-{step.uuid}"' in body          # anchor target
        assert f'href="#step-{step.uuid}"' in body        # permalink
    finally:
        _cleanup(run.uuid, room.uuid)


def test_undone_intent_is_marked_in_the_timeline(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="kanban_task_column", reason="r")
    db.settle_assistant_step(step, phase="observed", observation_preview="moved")
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="kanban_task_column",
        payload={"task_uuid": "t"}, preview_text="move", room_uuid=room.uuid,
        agent_uuid=run.agent_uuid, state="completed",
        result={"undo": {"capability": "kanban_task_delete", "payload": {}}})
    db.set_write_intent_state(intent, "undone")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "↩ undone" in body                  # the persistent undone badge
        assert 'class="intent undone"' in body      # styled distinctly
        # an already-undone intent offers no Undo button
        assert f"/write-intents/{intent.uuid}/undo" not in body
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
        run_uuid=run.uuid, step_index=0, action="memory_activate", reason="activate")
    db.settle_assistant_step(step, phase="observed", observation_preview="done")
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="memory_activate",
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


def test_verdict_shows_the_full_reply_not_the_truncated_summary(app_ctx, client):
    room = _room()
    agent_uuid = uuid4()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=agent_uuid)
    full_reply = "FULL-REPLY " + ("blah " * 100)      # > 200 chars
    db.post_chat_message(room.uuid, agent_uuid, full_reply)
    db.finish_run(run, "finished", final_summary=full_reply[:200])
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert full_reply.strip() in body            # the whole reply, not just [:200]
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
        # "Started by" block shows who triggered it + the message + a chat link.
        assert "Started by" in body
        assert "please mark the task done" in body
        # the operator name links to their /user page
        assert f"/user?id={human.uuid}" in body
        # links into chat AND anchors on the specific triggering message
        assert f"/chat?id={run.room_uuid}&msg=" in body
        # The verdict (final_summary) is present and sits BELOW the trigger.
        assert "Verdict" in body and "all done — the verdict" in body
        assert body.index("Verdict") > body.index("Started by")
    finally:
        _cleanup(run.uuid, room.uuid)


def test_verdict_chip_carries_the_outcome_not_the_lifecycle_status(app_ctx, client):
    """The Verdict card's chip is the verdict — Resolved/Unresolved — matching
    the dashboard's headline status. It must never show the lifecycle status
    ("Finished" = the loop terminated), which reads as success on a run that
    resolved nothing."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished", final_summary="I could not complete it")
    db.set_run_summary(run, {
        "trigger": "file the report", "obstacles": ["no access"],
        "outcome": "partial"})
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert '<span class="outcome out-unresolved">Unresolved</span>' in body
        assert "out-finished" not in body      # the green lifecycle chip is gone
    finally:
        _cleanup(run.uuid, room.uuid)


def test_verdict_chip_is_resolved_when_the_run_resolved(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished", final_summary="done")
    db.set_run_summary(run, {
        "trigger": "file the report", "obstacles": [], "outcome": "resolved"})
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert '<span class="outcome out-resolved">Resolved</span>' in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_run_is_addressable_and_shown_by_uuid(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        # Addressable only by uuid via ?id=; the header kebab offers Copy run id.
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert str(run.uuid) in body
        assert "Copy run id" in body
        assert f"asKebab(event, '{run.uuid}'" in body  # kebab wired to this run
        assert "No run selected" not in body           # a run is selected
        # Only a uuid ?id= resolves: a non-uuid value and the old ?run= don't.
        assert "No run selected" in client.get(
            "/assistant?id=not-a-uuid").get_data(as_text=True)
        assert "No run selected" in client.get(
            f"/assistant?run={run.uuid}").get_data(as_text=True)
    finally:
        _cleanup(run.uuid, room.uuid)


def test_run_summary_renders_in_detail(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    db.set_run_summary(run, {
        "trigger": "file the weekly report", "obstacles": ["the disk was full"],
        "outcome": "partial"})
    try:
        detail = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "file the weekly report" in detail     # summary trigger in the dashboard
        assert "the disk was full" in detail          # obstacle in the detail pane
        assert "Unresolved" in detail                 # 'partial' outcome → dashboard status
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


def test_unsummarized_failed_run_shows_failure_reason(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "killed", final_summary="worker exited with code 9")
    try:
        detail = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "worker exited with code 9" in detail
        assert "Not yet summarized" not in detail
    finally:
        _cleanup(run.uuid, room.uuid)


def test_step_token_counts_render_in_timeline(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    with_tok = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="r",
        input_tokens=412, output_tokens=87, duration_ms=5100)
    db.settle_assistant_step(with_tok, phase="observed", observation_preview="ok")
    # a control step has no counts
    db.append_assistant_step(run_uuid=run.uuid, step_index=1, phase="control", action="stop")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        # token counts + throughput render as separate gap-separated fields
        # (412+87)/5.1s ≈ 98 tok/s
        assert "in 412" in body and "out 87" in body
        assert "98 tok/s" in body and "took 5.1s" in body
        # exactly one step metrics line (the control step shows none)
        assert body.count('title="Input tokens') == 1
    finally:
        _cleanup(run.uuid, room.uuid)


def test_run_dashboard_aggregates_status_steps_time_tokens(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    s1 = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="r",
        input_tokens=400, output_tokens=50, duration_ms=3000)
    db.settle_assistant_step(s1, phase="observed", observation_preview="ok")
    s2 = db.open_assistant_step(
        run_uuid=run.uuid, step_index=1, action="reply", reason="r2",
        input_tokens=100, output_tokens=20, duration_ms=2100)
    db.settle_assistant_step(s2, phase="observed", observation_preview="done")
    db.finish_run(run, "finished")
    db.set_run_summary(run, {"trigger": "t", "obstacles": [], "outcome": "resolved"})
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert 'class="dash"' in body
        assert "Resolved" in body                       # status column
        assert '<div class="dval-big">2</div>' in body  # step count
        assert "in 500" in body and "out 70" in body    # accumulated tokens
        assert "112 tok/s" in body                       # throughput, in the Tokens column
        assert "model 5.1s" in body                      # accumulated model (LLM) time
        assert "total " in body                          # start→finish time
        assert "action " in body                          # time outside the model
    finally:
        _cleanup(run.uuid, room.uuid)


def test_step_model_renders_as_a_link(app_ctx, client):
    mc = db.create_model_config("qwen-2.5-7b", {})
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="r",
        model_uuid=mc.uuid)
    db.settle_assistant_step(step, phase="observed", observation_preview="ok")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        # the model name links to its /model config page
        assert f'href="/model?id={mc.uuid}"' in body
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
        # The kebab on the selected run carries its uuid/status/journal id.
        assert f"asKebab(event, '{run.uuid}', 'running', '{run.journal_id}')" in body
        # The menu offers Copy run id / Copy journal id / View as markdown, and a
        # Stop for a running run.
        assert "Copy run id" in body
        assert "Copy journal id" in body
        assert "View as markdown" in body
        assert f"/chat/api/assistant/runs/' + uuid + '/stop" in body  # Stop target (JS)
    finally:
        _cleanup(run.uuid, room.uuid)


def test_nav_link_present(app_ctx, client):
    # The nav's Assistant menu reaches the overview (the run finder) and the
    # second-opinion list; the inspector page itself is reached by clicking a
    # row in the overview.
    body = client.get("/assistant").get_data(as_text=True)
    assert 'href="/assistant-overview"' in body
    assert 'href="/second-opinion"' in body
    assert "Assistant &#9662;" in body


def test_markdown_export_serializes_the_run(app_ctx, client):
    room = _room()
    human = db.get_human_user()
    db.post_chat_message(room.uuid, human.uuid, "please file the report")
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="look it up",
        args={"query": "report"}, input_tokens=120, output_tokens=15, duration_ms=2000)
    db.settle_assistant_step(step, phase="observed", observation_preview="found it")
    db.finish_run(run, "finished", final_summary="all done — the verdict")
    db.set_run_summary(run, {
        "trigger": "file the weekly report", "obstacles": ["the disk was full"],
        "outcome": "resolved"})
    try:
        resp = client.get(f"/assistant/{run.uuid}/markdown")
        assert resp.status_code == 200
        assert resp.mimetype == "text/plain"
        md = resp.get_data(as_text=True)
        # Section headers and key content from the detail pane.
        assert md.startswith(f"# Assistant run {run.uuid}")   # full uuid for DB lookups
        assert "## Summary" in md and "file the weekly report" in md
        assert "### Obstacles" in md and "- the disk was full" in md
        assert "## Run" in md and "please file the report" in md
        assert "## Timeline" in md
        assert "Step 1 of 1 — memory_query" in md   # action + its description
        assert '"query": "report"' in md             # action args block
        assert "found it" in md                       # observation
        assert "## Verdict — Resolved" in md and "all done — the verdict" in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_markdown_verdict_header_carries_the_outcome(app_ctx, client):
    """The markdown twin of the Verdict chip: the outcome, not the lifecycle
    status. A run that finished without resolving reads "Unresolved"."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished", final_summary="I could not complete it")
    db.set_run_summary(run, {
        "trigger": "file the report", "obstacles": ["no access"],
        "outcome": "partial"})
    try:
        md = client.get(
            f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "## Verdict — Unresolved" in md
        assert "## Verdict — Finished" not in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_markdown_export_unknown_run_is_404(app_ctx, client):
    assert client.get("/assistant/not-a-uuid/markdown").status_code == 404
    assert client.get(f"/assistant/{uuid4()}/markdown").status_code == 404


def test_query_memory_data_renders_as_table_with_tooltips():
    """The memory_query step's structured data renders as a compact counts table
    (short headers + explanatory tooltips), not a raw JSON blob."""
    from webapp.assistant_views import ASSISTANT_TEMPLATE, _step_md
    # Table markup + tooltips + short headers in the HTML template.
    for tip in ["number of QA static items", "number of QA dynamic items",
                "number of memory items"]:
        assert f'title="{tip}"' in ASSISTANT_TEMPLATE
    # truncated / omitted carry an explanatory tooltip (what + how to recover).
    assert "per-fact cap (tagged truncate1200)" in ASSISTANT_TEMPLATE
    assert "exceeded the 11000-char budget" in ASSISTANT_TEMPLATE
    assert "io-data" in ASSISTANT_TEMPLATE
    for hdr in ["QA static", "QA dynamic"]:
        assert hdr in ASSISTANT_TEMPLATE
    # Markdown mirror (_step_md) renders the same counts as a table row.
    class _Step:  # all fields default to None except the two we set
        action = "memory_query"
        observation = {"ok": True, "data": {"qa_static": 3, "qa_dynamic": 0,
                        "memory": 6, "truncated": 0, "omitted": 0}}
        def __getattr__(self, name):
            return None
    md = "\n".join(_step_md(_Step(), {}, {}))
    assert "| QA static | QA dynamic | memory | truncated | omitted |" in md
    assert "| 3 | 0 | 6 | 0 | 0 |" in md


def test_step_reasoning_renders_collapsed_in_timeline_and_markdown(app_ctx, client):
    """A step's captured model reasoning shows as a collapsed "model reasoning"
    block on the page and a **model reasoning** section in the markdown export;
    a step without reasoning (non-reasoning model) renders neither."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="look it up",
        reasoning="the operator wants git state, memory holds that")
    db.settle_assistant_step(step, phase="observed", observation_preview="found it")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "model reasoning" in body
        assert "the operator wants git state, memory holds that" in body
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "**model reasoning**" in md
        assert "the operator wants git state, memory holds that" in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_step_without_reasoning_has_no_reasoning_block(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="look it up")
    db.settle_assistant_step(step, phase="observed", observation_preview="found it")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "model reasoning" not in body
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "**model reasoning**" not in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_interrupted_step_shows_partial_model_response(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid,
        step_index=0,
        phase="failed",
        action=None,
        error="worker killed",
        model_response='{"reason":"enough evidence","action":"rep',
    )
    db.finish_run(run, "killed")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "partial model response" in body
        assert "enough evidence" in body
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "**partial model response**" in md
        assert "enough evidence" in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_page_live_refreshes_via_sse_not_polling(app_ctx, client):
    """The page rides the chat_events SSE stream and filters on
    assistant_run_uuid; recurring timers are banned (chat-frontend-rules)."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "new EventSource('/chat/stream')" in body
        assert "assistant_run_uuid" in body
        assert f"'{run.uuid}'" in body
        assert "setInterval" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_in_flight_model_call_card(app_ctx, client):
    """A running run with an active_call checkpoint shows the streamed
    partial reasoning/response; a settled run never shows the card."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    model_uuid = uuid4()
    db.checkpoint_assistant_call(
        run, step_index=0, system_prompt="s", user_prompt="u",
        requested_at=datetime.now(UTC), model_group_uuid=None)
    db.checkpoint_assistant_model_attempt(
        run, model_uuid=model_uuid, model_name="live-model", timeout_seconds=10.0)
    db.checkpoint_assistant_model_progress(
        run, model_uuid=model_uuid,
        reasoning="pondering the request", response_text='{"reason": "part')
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "model call in progress" in body
        assert "pondering the request" in body
        assert "live-model" in body

        db.finish_run(run, "finished")
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "model call in progress" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def _second_opinion_step(run, *, approved: bool, problems=None):
    """One settled python_run step whose observation data carries the
    second-opinion review payload the loop stores."""
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="python_run",
        reason="compute the conversion", args={"code": "print(12 * 0.3048)"})
    review = {
        "approved": approved, "problems": problems or [],
        "group_from": "second_opinion", "model_uuid": str(uuid4()),
        "system_prompt": "You are a second-opinion reviewer.",
        "user_prompt": "<python_program>print(12 * 0.3048)</python_program>",
        "reasoning": "The operator is metric; the conversion factor is right.",
        "response": '{"problems": [], "approved": %s}' % (
            "true" if approved else "false"),
    }
    if approved:
        db.settle_assistant_step(
            step, phase="observed", observation_preview="3.6576",
            observation={"ok": True, "text": "3.6576",
                         "data": {"duration_seconds": 0.01,
                                  "second_opinion": review}})
    else:
        text = "second_opinion rejected this python_run"
        db.settle_assistant_step(
            step, phase="failed", observation_preview=text,
            observation={"ok": False, "text": text,
                         "data": {"second_opinion": review}},
            error=text)
    return step


def test_second_opinion_renders_before_the_action_call(app_ctx, client):
    """Chronological order: the review ran before the program executed, so its
    block sits between the model response and the action call — and the
    action-result data no longer repeats the payload."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _second_opinion_step(run, approved=True)
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "second opinion" in body
        assert body.index("second opinion") < body.index("action call")
        assert "approved: true" in body
        assert "group: second_opinion" in body
        # The reviewer's own model request, collapsed like the decide call's.
        assert "You are a second-opinion reviewer." in body
        assert "&lt;python_program&gt;print(12 * 0.3048)&lt;/python_program&gt;" in body
        # Its reasoning channel (collapsed) and verbatim response.
        assert "The operator is metric; the conversion factor is right." in body
        assert "&#34;approved&#34;: true" in body or '"approved": true' in body
        # Stripped from the action-result data pre; the rest of the data stays.
        assert '"second_opinion"' not in body
        assert '"duration_seconds": 0.01' in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_second_opinion_rejection_shows_problems(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _second_opinion_step(
        run, approved=False,
        problems=["the operator profile is metric; convert to meters"])
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "approved: false" in body
        assert "- the operator profile is metric; convert to meters" in body
    finally:
        _cleanup(run.uuid, room.uuid)


def _pointer_review_step(run, *, verdict="rejected", problems=None):
    """A gated step whose observation carries only a pointer, with the review
    itself in its own row — the current shape."""
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="python_run",
        reason="compute the conversion", args={"code": "print(12 * 0.3048)"})
    row = db.record_second_opinion_review(
        run_uuid=run.uuid, step_uuid=step.uuid, step_index=0,
        action="python_run", verdict=verdict, group_from="second_opinion",
        model_uuid=uuid4(), problems=problems or [],
        system_prompt="You are a second-opinion reviewer.",
        user_prompt="<python_program>print(12 * 0.3048)</python_program>",
        reasoning="The operator is metric.", response='{"approved": false}')
    text = "second_opinion rejected this python_run"
    db.settle_assistant_step(
        step, phase="failed", observation_preview=text,
        observation={"ok": False, "text": text,
                     "data": {"second_opinion": {"review_uuid": str(row.uuid)}}},
        error=text)
    return step, row


def test_inspector_resolves_the_review_pointer(app_ctx, client):
    """The payload lives in its own row now; the trace must render it the same
    as when it was stored inline."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _pointer_review_step(run, problems=[
        {"category": "identity_mismatch", "text": "convert to meters"}])
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "second opinion" in body
        assert "approved: false" in body
        assert "group: second_opinion" in body
        assert "You are a second-opinion reviewer." in body
        assert "- convert to meters" in body
        assert "The operator is metric." in body
        # The raw pointer is never shown as the action-result data.
        assert "review_uuid" not in body
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "**second opinion**" in md
        assert "- convert to meters" in md
        assert "review_uuid" not in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_skipped_review_row_renders_its_reason(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="python_run")
    row = db.record_second_opinion_review(
        run_uuid=run.uuid, step_uuid=step.uuid, step_index=0,
        action="python_run", verdict="skipped", skip_reason="no_model_group")
    db.settle_assistant_step(
        step, phase="observed", observation_preview="3.6576",
        observation={"ok": True, "text": "3.6576",
                     "data": {"second_opinion": {"review_uuid": str(row.uuid)}}})
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "review skipped: no_model_group" in body
        # A skipped review never approved anything, so no verdict badge.
        assert "approved: true" not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_legacy_inline_payload_still_renders(app_ctx, client):
    """Runs from before the row existed keep their payload inline and have no
    pointer; they must not go blank."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _second_opinion_step(
        run, approved=False,
        problems=["the operator profile is metric; convert to meters"])
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "approved: false" in body
        assert "- the operator profile is metric; convert to meters" in body
        assert "You are a second-opinion reviewer." in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_dangling_pointer_does_not_break_the_trace(app_ctx, client):
    """The row is gone (or was never written); the step must still render."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="python_run")
    db.settle_assistant_step(
        step, phase="observed", observation_preview="3.6576",
        observation={"ok": True, "text": "3.6576",
                     "data": {"second_opinion": {"review_uuid": str(uuid4())}}})
    db.finish_run(run, "finished")
    try:
        resp = client.get(f"/assistant?id={run.uuid}")
        assert resp.status_code == 200
        assert "3.6576" in resp.get_data(as_text=True)
    finally:
        _cleanup(run.uuid, room.uuid)


def test_dashboard_counts_the_gate_as_part_of_the_run_cost(app_ctx, client):
    """The review is a real model call made on the run's behalf. Counting only
    assistant_step rows under-reported tokens and model time on every gated
    run."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="python_run",
        input_tokens=100, output_tokens=10, duration_ms=2000)
    db.record_second_opinion_review(
        run_uuid=run.uuid, step_uuid=step.uuid, step_index=0,
        action="python_run", verdict="approved",
        input_tokens=50, output_tokens=5, duration_ms=1000)
    db.settle_assistant_step(step, phase="observed", observation_preview="ok")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "in 150" in body and "out 15" in body
        # 2000ms of step + 1000ms of review.
        assert "3.0s" in body
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "in 150" in md and "out 5" not in md.split("in 150")[0]
    finally:
        _cleanup(run.uuid, room.uuid)


def test_categorized_problems_render_as_text(app_ctx, client):
    """Problems are {category, text} objects now. Both the inspector and the
    markdown export must show the sentence, not the raw object."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _second_opinion_step(run, approved=False, problems=[
        {"category": "identity_mismatch",
         "text": "the operator profile is metric; convert to meters"}])
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "- the operator profile is metric; convert to meters" in body
        assert "identity_mismatch" not in body      # the tag is not the finding
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "- the operator profile is metric; convert to meters" in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_markdown_export_mirrors_the_second_opinion_block(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _second_opinion_step(run, approved=True)
    db.finish_run(run, "finished")
    try:
        md = client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "**second opinion** · approved: true" in md
        assert md.index("**second opinion**") < md.index("**action call**")
        assert "You are a second-opinion reviewer." in md
        assert "<python_program>print(12 * 0.3048)</python_program>" in md
        assert "_reasoning_" in md
        assert "The operator is metric; the conversion factor is right." in md
        assert "_response_" in md and '{"problems": [], "approved": true}' in md
        assert '"second_opinion"' not in md
    finally:
        _cleanup(run.uuid, room.uuid)
