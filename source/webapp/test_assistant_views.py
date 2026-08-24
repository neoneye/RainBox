"""The /assistant page — run-centric inspector over the assistant trace.

Renders recent runs, the selected run's step timeline with each write-intent
inline, and the state-appropriate lifecycle buttons (confirm/reject/undo,
stop/redirect) wired to the existing endpoints. Read-only data; the buttons are
the only writes.
"""

import html
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
import webapp  # noqa: F401 — registers all views (incl. /assistant) on the app
from db import AssistantRun
from webapp.assistant_views import (
    TRIGGER_PEEK_CHARS,
    TRIGGER_PEEK_LINES,
    _format_duration,
    _trigger_peek,
    _review_meta,
    _review_payload,
)
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


def _steps_region(body: str) -> str:
    """The per-step sections only, with the log above them cut away.

    The log and the step sections both render a run's content, so a
    "renders once" guard has to say WHERE. These guards are about one step
    not printing the same payload under two headings — the bug they were
    written for — not about the page having a second surface.
    """
    marker = 'class="step phase-'
    index = body.find(marker)
    return body[index:] if index >= 0 else body


def _rendered(client, run) -> tuple[str, str]:
    """The run as both renderers show it — the HTML page and the markdown
    export. HTML-unescaped, so an assertion about JSON text reads the same
    either way (and a negative assertion can't pass just because the page
    escaped the quotes)."""
    return (
        html.unescape(client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)),
        html.unescape(
            client.get(f"/assistant/{run.uuid}/markdown").get_data(as_text=True)),
    )


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
        # exactly one step metrics line (the control step shows none). Scoped
        # to the step sections: the log pane above carries its own meta line
        # for the same call, which is a second surface, not a second step.
        assert _steps_region(body).count('title="Input tokens') == 1
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
    # Both say what the number means, not just that it is a limit: the per-fact
    # cap drops a middle and keeps both ends, and the payload budget decides
    # what is ADMITTED rather than capping the observation.
    assert "middle was dropped" in ASSISTANT_TEMPLATE
    assert "both ends kept (tagged truncate1200)" in ASSISTANT_TEMPLATE
    assert "not admitted because they no longer fit" in ASSISTANT_TEMPLATE
    assert "not the whole observation" in ASSISTANT_TEMPLATE
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


def test_code_driven_step_shows_its_real_response_not_a_synthesized_decision(
        app_ctx, client):
    """A code-driven call (criteria, language classifier, reply audit) has no
    decision behind it: its action and reason are labels the loop wrote. Dumping
    those in decision shape made every such row read as "the model chose
    acceptance_criteria" — identical on every run — while the criteria the call
    actually returned went unshown. The row's own response is what renders."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid,
        step_index=0,
        phase="observed",
        action="acceptance_criteria",
        reason="established before step 0 (code-driven)",
        model_response='{"processing": "answer in meters"}',
        code_driven=True,
        observation_preview='{"processing": "answer in meters"}',
    )
    db.finish_run(run, "finished")
    try:
        for text in _rendered(client, run):
            assert "answer in meters" in text
            # No fabricated decision dump…
            assert '"action": "acceptance_criteria"' not in text
            # …and a complete response is not labelled a partial one.
            assert "partial model response" not in text
    finally:
        _cleanup(run.uuid, room.uuid)


def _real_run_shape(run) -> None:
    """The row sequence a plain reply run produces: two calls the loop makes
    before the loop opens, the audit of the finished reply, then the reply the
    model decided. All four carry decide-step index 0 — the code-driven ones
    ride alongside the decide step rather than consuming budget.

    The audit's ROW precedes the reply's — the reply lands only once the audit
    says send — while its CALL went out 20s later. That inversion is the whole
    point of the fixture, so the `requested_at` stamps matter."""
    t0 = datetime(2026, 7, 29, 14, 7, 21, tzinfo=UTC)
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed",
        action="response_language_classifier", reason="the request is in English",
        code_driven=True, model_response='{"languages": [{"code": "en-US"}]}',
        observation_preview="en-US", requested_at=t0,
        input_tokens=900, output_tokens=120, duration_ms=17000)
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed",
        action="acceptance_criteria",
        reason="established before step 0 (code-driven)", code_driven=True,
        model_response='{\n  "processing": "answer in meters"\n}',
        observation_preview='{\n "processing": "answer in meters"\n}',
        requested_at=t0 + timedelta(seconds=17),
        input_tokens=1163, output_tokens=194, duration_ms=20849)
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed",
        action="reply_audit", reason="send", code_driven=True,
        model_response='{"verdict": "send"}', observation_preview='{"verdict": "send"}',
        requested_at=t0 + timedelta(seconds=55))
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="reply", reason="ready to answer",
        args={"message": "About 321179090 meters."},
        requested_at=t0 + timedelta(seconds=35))
    db.settle_assistant_step(step, phase="final", observation_preview="replied")


def test_steps_are_numbered_by_position_and_marked_warm_up_or_follow_up(
        app_ctx, client):
    """Numbering by `step_index` printed "Step 1 of 4" for three rows in a row,
    because the code-driven calls share the decide index they sit beside. The
    timeline numbers positions instead, and says which rows are not decide
    steps: warm-up before the loop opens, follow-up once it has."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _real_run_shape(run)
    db.finish_run(run, "finished")
    try:
        page, md = _rendered(client, run)
        assert page.count("Step 1 of 4") == 1
        assert [n for n in range(1, 5) if f"Step {n} of 4" in page] == [1, 2, 3, 4]
        for text in (page, md):
            # The two pre-loop calls, then the audit of what the model decided.
            assert text.count("warm-up") >= 2
            assert text.count("follow-up") >= 1
        # The markdown headings carry it in reading order — the order the
        # calls RAN. The audit's row was written first, but it audits a reply
        # the decide call had already produced, so it reads last.
        heads = [ln.split(" — ")[:3] for ln in md.splitlines()
                 if ln.startswith("### Step ")]
        assert heads == [
            ["### Step 1 of 4", "warm-up", "response_language_classifier"],
            ["### Step 2 of 4", "warm-up", "acceptance_criteria"],
            # No kind on the one real decide step, so its action sits here.
            ["### Step 3 of 4", "reply", "send the final answer to the user"],
            ["### Step 4 of 4", "follow-up", "reply_audit"],
        ]
        # The catalog summary for `acceptance_criteria` describes the revision
        # the model can request; the loop's own call establishes them.
        assert "establish what a good reply must satisfy" in md
        assert "revise this turn's acceptance criteria" not in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_the_timeline_reads_in_the_same_order_as_the_waterfall(app_ctx, client):
    """One page cannot hold two answers to "which call came first".

    Ordered by row id, the reply audit sat above the decide call whose reply it
    audited — and an audit prompt carries no action list, so the run read as
    one where the model was never offered its actions. The waterfall was laid
    out on the clock all along; the rows now are too."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _real_run_shape(run)
    db.finish_run(run, "finished")
    try:
        steps = db.assistant_trace_steps(run.uuid)
        assert [s.action for s in steps] == [
            "response_language_classifier", "acceptance_criteria",
            "reply", "reply_audit"]
        # Commit order still says otherwise, and still should: the loop's own
        # readers find a running step by walking back from the newest row.
        assert [s.action for s in db.list_assistant_steps(run.uuid)] == [
            "response_language_classifier", "acceptance_criteria",
            "reply_audit", "reply"]
        # And the two surfaces on the page now agree call for call. A step the
        # model chose is labelled by the decision it made; a call the loop
        # issued itself is labelled by what it is, since presenting it as a
        # decision would be a fiction.
        calls = db.assistant_llm_calls(steps)
        assert [c["label"] for c in calls] == [
            s.action if s.code_driven else f"decide → {s.action}" for s in steps]
    finally:
        _cleanup(run.uuid, room.uuid)


def test_code_driven_row_shows_its_payload_once_and_calls_no_action(
        app_ctx, client):
    """Its response IS its result — printing the same JSON under both "model
    response" and "action result" reads as two things having happened. Nor was
    an action called: the loop made the call itself, so an "action call" block
    with empty args is the same fiction the synthesized decision was."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _real_run_shape(run)
    db.finish_run(run, "finished")
    try:
        for text in _rendered(client, run):
            steps_only = _steps_region(text)
            # Indentation differs between the raw response and the stored
            # preview, so the duplicate is caught on content, not bytes.
            assert steps_only.count("answer in meters") == 1
            assert steps_only.count("action call") == 1  # the reply step's
            # The classifier's rendered result is NOT its raw response, so both
            # blocks survive there.
            assert "en-US" in text
    finally:
        _cleanup(run.uuid, room.uuid)


def test_every_model_call_row_shows_the_same_io_meta_fields(app_ctx, client):
    """Every model call in the trace reports its cost the same way, including
    the code-driven ones — a row missing in/out/throughput silently reads as
    free. Both renderers draw from one set of field builders, so what the page
    shows and what the export shows cannot drift apart."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _real_run_shape(run)
    db.finish_run(run, "finished")
    try:
        page, md = _rendered(client, run)
        for text in (page, md):
            # The classifier and the criteria call both priced, same shape.
            assert "in 900" in text and "out 120" in text and "60 tok/s" in text
            assert "in 1163" in text and "out 194" in text and "65 tok/s" in text
    finally:
        _cleanup(run.uuid, room.uuid)


def test_io_meta_line_has_a_single_definition(app_ctx, client):
    """The DRY guarantee, stated as a test: changing a field's wording in the
    builder changes it in the page and the export at once. If either renderer
    grows its own copy of the line, this fails."""
    from webapp import assistant_views as views

    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    _real_run_shape(run)
    db.finish_run(run, "finished")
    original = views._field
    try:
        views._field = lambda text, title, **kw: original(
            f"[{text}]", title, **kw)
        page, md = _rendered(client, run)
        for text in (page, md):
            assert "[in 900]" in text and "[out 120]" in text
            assert "[60 tok/s]" in text and "[took 17.0s]" in text
    finally:
        views._field = original
        _cleanup(run.uuid, room.uuid)


def _run_with_hidden_calls(run, t0):
    """A run with a model call that does NOT map onto a step row: a gated step
    reviewed by the second opinion, whose review lives in its own table. The
    memory_query step and the recall filter it triggered are rows; the review
    is the one call invisible in the step columns."""
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="query_memory", reason="look",
        system_prompt="s", user_prompt="u", requested_at=t0,
        input_tokens=2100, output_tokens=60, duration_ms=9000)
    db.settle_assistant_step(
        step, phase="observed", observation_preview="found",
        observation={"ok": True, "text": "found", "data": {"recall_filter": {
            "mode": "llm", "group_from": "memory_filter", "candidates": []}}})
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed",
        action="recall_filter", reason="score the recalled candidates",
        system_prompt="s", user_prompt="u", code_driven=True,
        requested_at=t0 + timedelta(seconds=10),
        input_tokens=3400, output_tokens=210, duration_ms=12000)
    gated = db.open_assistant_step(
        run_uuid=run.uuid, step_index=1, action="python_run", reason="compute",
        system_prompt="s", user_prompt="u",
        requested_at=t0 + timedelta(seconds=23),
        input_tokens=2600, output_tokens=140, duration_ms=11000)
    db.settle_assistant_step(gated, phase="observed", observation_preview="1")
    db.record_second_opinion_review(
        run_uuid=run.uuid, step_uuid=gated.uuid, step_index=1,
        action="python_run", verdict="approved", group_from="own",
        system_prompt="s", user_prompt="u", response="{}",
        input_tokens=1500, output_tokens=80, duration_ms=6000,
        requested_at=t0 + timedelta(seconds=35))
    # Pin the run's span to the calls, so the layout percentages below are
    # arithmetic rather than a function of when the test happens to run.
    db.finish_run(run, "finished")
    run.started_at = t0
    run.finished_at = t0 + timedelta(seconds=41)
    db.db.session.commit()
    return step, gated


def test_counts_every_llm_call_including_the_ones_with_no_step_row(
        app_ctx, client):
    """Two of a run's model calls ride inside something else: the second
    opinion and the criteria revision. Counting step rows misses them, and
    their seconds then reappear as unexplained "action" time — the exact time
    an operator is hunting for."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    t0 = datetime(2026, 7, 29, 14, 0, 0, tzinfo=UTC)
    _run_with_hidden_calls(run, t0)
    try:
        page, md = _rendered(client, run)
        # 3 step rows, but 4 calls.
        assert "- **Steps:** 3" in md
        assert "- **LLM calls:** 4" in md
        # 9 + 12 + 11 + 6 = 38s of model time, not the 20s the rows alone show.
        assert "model 38.0s" in md
        # …and the inner calls' tokens are in the run's total.
        assert "in 9600" in md and "out 490" in md
        assert "LLM calls" in page and ">4<" in page
    finally:
        _cleanup(run.uuid, room.uuid)


def test_waterfall_places_each_call_on_the_run_span(app_ctx, client):
    """The visualization: one bar per call, offset by when it ran and scaled by
    how long it took. The offsets are what show where the time went — a wide
    gap between two bars is time no model was working."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    t0 = datetime(2026, 7, 29, 14, 0, 0, tzinfo=UTC)
    _run_with_hidden_calls(run, t0)
    try:
        from webapp.assistant_views import _waterfall
        steps = db.list_assistant_steps(run.uuid)
        rows = _waterfall(db.assistant_llm_calls(
            steps, db.list_second_opinion_reviews(run.uuid)), run)
        assert [r["label"] for r in rows] == [
            "decide → query_memory", "recall_filter", "decide → python_run",
            "second opinion"]
        # The span runs from the first call to the last one's end (41s), so the
        # first bar starts at 0 and the recall filter 10s in.
        assert rows[0]["offset_pct"] == 0.0
        assert rows[1]["offset_pct"] == pytest.approx(10 / 41 * 100, abs=0.01)
        assert rows[1]["width_pct"] == pytest.approx(12 / 41 * 100, abs=0.01)
        assert rows[1]["seconds"] == "12.0s"
        # Clicking a bar lands on the step the call belongs to — its own row.
        assert rows[1]["anchor"] == str(steps[1].uuid)
        page, md = _rendered(client, run)
        assert "recall_filter" in page and "recall_filter" in md
        assert "wf-bar" in page
    finally:
        _cleanup(run.uuid, room.uuid)


def test_only_rows_worth_attention_are_coloured_in_the_waterfall():
    """Colour is reserved for the rows a reader should stop on, so the chart
    shows where the time went instead of asking anyone to decode a legend.

    Two kinds earn it, and both are a problem by definition: `rejected` is an
    answer that was thrown away, and `unaccounted` is time nothing measured.
    An `activity` bar is ordinary measured work and stays neutral like a call.
    """
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    named = set(re.findall(r"\.wf-name\.kind-([a-z-]+)", ASSISTANT_TEMPLATE))
    barred = set(re.findall(r"\.wf-bar\.kind-([a-z-]+)", ASSISTANT_TEMPLATE))

    assert named == {"rejected", "unaccounted"}
    assert barred == {"rejected", "unaccounted"}


def test_review_written_before_start_times_still_gets_a_bar(app_ctx, client):
    """Reviews recorded before the gate stamped its start have a duration and
    no start, so the timeline drew every other call and left this one outside
    the span marked "not timed" — reading as if the second opinion had not run
    at all. A review runs between its step's decide call returning and the
    action executing, so it is placed at the moment that step row was opened."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    gated = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="python_run", reason="compute",
        system_prompt="s", user_prompt="u", requested_at=datetime.now(UTC),
        input_tokens=100, output_tokens=10, duration_ms=19172)
    db.settle_assistant_step(gated, phase="observed", observation_preview="ok")
    db.record_second_opinion_review(
        run_uuid=run.uuid, step_uuid=gated.uuid, step_index=0,
        action="python_run", verdict="approved", group_from="own",
        system_prompt="s", user_prompt="u", response="{}",
        input_tokens=1459, output_tokens=10, duration_ms=17497)  # no start
    db.finish_run(run, "finished")
    try:
        steps = db.list_assistant_steps(run.uuid)
        reviews = db.list_second_opinion_reviews(run.uuid)
        assert reviews[0].requested_at is None      # the shape being handled
        call = next(c for c in db.assistant_llm_calls(steps, reviews)
                    if c["label"] == "second opinion")
        assert call["start"] == steps[0].created_at
        page, md = _rendered(client, run)
        assert "second opinion" in page and "17.5s" in page
        assert "not timed" not in page
    finally:
        _cleanup(run.uuid, room.uuid)


def test_call_without_a_recorded_start_is_placed_at_its_row_end(
        app_ctx, client):
    """Rows written before start times were captured still belong on the
    timeline: the response landed when the row was written, so the call ran the
    duration before that."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="final", action="reply",
        reason="done", duration_ms=5000)
    db.finish_run(run, "finished")
    try:
        steps = db.list_assistant_steps(run.uuid)
        call = db.assistant_llm_calls(steps)[0]
        assert call["start"] == steps[0].created_at - timedelta(seconds=5)
    finally:
        _cleanup(run.uuid, room.uuid)


def test_model_chosen_step_still_shows_its_decision(app_ctx, client):
    """The counterpart: a step the model decided keeps its verbatim decision
    dump — that IS the model's response for a decide call."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="acceptance_criteria",
        reason="the operator named a unit mid-run")
    db.settle_assistant_step(step, phase="observed", observation_preview="revised")
    db.finish_run(run, "finished")
    try:
        for text in _rendered(client, run):
            assert '"action": "acceptance_criteria"' in text
            assert "the operator named a unit mid-run" in text
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
        # The card takes the position the timeline will give the row it becomes
        # — not step_index + 1, which the timeline no longer numbers by.
        assert "Step 1<" in body
        _real_run_shape(run)
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "Step 5<" in body

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


def test_live_refresh_keeps_expanded_blocks_open(app_ctx, client):
    """A live run refreshes every few seconds, and the swap re-collapsed
    everything the reader had opened — a prompt closed under them before they
    could read it, so a running run could only be inspected after it finished.
    The open blocks are carried across the swap, keyed by step + role rather
    than by position: a live step grows blocks as it runs (its reasoning, then
    its second opinion), so an index would reopen the wrong one."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="query_memory", reason="look",
        system_prompt="s", user_prompt="u", reasoning="thinking",
        log=[{"label": "profile", "text": "default"}])
    db.settle_assistant_step(step, phase="observed", observation_preview="ok")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        # Every collapsible block is addressable by a stable role…
        for role in ("log", "system", "user", "reasoning"):
            assert f'data-k="{role}"' in body
        # …and the refresh reads them before the swap and reapplies after.
        assert "function detailsKey(d) {" in body
        assert "return (step ? step.id : '') + '/' + d.getAttribute('data-k');" in body
        assert "var open = openDetails(cur);" in body
        assert "function (d) { if (open[detailsKey(d)]) d.open = true; });" in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_skipped_call_reads_as_skipped_not_as_a_silent_row(app_ctx, client):
    """A call the loop could not make is a row like any other, but it must not
    look like one that ran: it carries a `skipped` badge, no empty "model
    response" block, and no bar in the Model calls timeline (it cost nothing)."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    skipped = db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="skipped",
        action="acceptance_criteria", reason="no model group is bound",
        code_driven=True, system_prompt="s", user_prompt="u",
        observation_preview="skipped: no model group is bound",
        requested_at=datetime.now(UTC))
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="final", action="reply",
        reason="ready", duration_ms=4000, input_tokens=100, output_tokens=20)
    db.finish_run(run, "finished")
    try:
        page, md = _rendered(client, run)
        assert ">skipped<" in page
        assert "no model group is bound" in page and "no model group is bound" in md
        # It has no response, so it gets no "model response" block at all —
        # an empty one reads as a call that answered with nothing.
        fragment = page.split(f'id="step-{skipped.uuid}"')[1].split('id="step-')[0]
        assert "model response" not in fragment
        assert "no model group is bound" in fragment       # but its result is there
        assert md.count("**model response**") == 1         # the reply's, only
        # It cost nothing, so it is not one of the run's model calls.
        assert "- **LLM calls:** 1" in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_review_meta_shows_what_the_review_cost():
    """The reviewer's row stored its tokens all along; the line rendered only
    the model and the group, so the gate looked free next to the step it
    gates."""
    fields = _review_meta(
        {"model_uuid": str(uuid4()), "group_from": "second_opinion",
         "usage": {"input": 3100, "output": 120, "ms": 4000}},
        {})
    texts = [f["text"] for f in fields]

    assert "in 3100" in texts
    assert "out 120" in texts
    assert "805 tok/s" in texts
    assert "took 4.0s" in texts


def test_review_meta_survives_a_review_that_recorded_no_usage():
    """A skipped or failed-open review has no model call to cost."""
    fields = _review_meta({"group_from": "own"}, {})

    assert [f["text"] for f in fields] == ["group: own"]


def test_review_payload_carries_the_rows_usage_forward():
    """The row is the source of truth, so the shape it hands the renderer has
    to match the inline payload's."""
    class _Row:
        problems = []
        group_from = "own"
        model_uuid = None
        system_prompt = user_prompt = reasoning = response = None
        skip_reason = error = None
        verdict = "approved"
        input_tokens, output_tokens, duration_ms = 11, 22, 33

    assert _review_payload(_Row())["usage"] == {
        "input": 11, "output": 22, "ms": 33}


def test_trigger_peek_leaves_a_short_message_alone():
    """The common card keeps exactly the shape it had, with no toggle to
    ignore."""
    assert _trigger_peek("convert 12 feet") is None
    assert _trigger_peek("line\n" * TRIGGER_PEEK_LINES) is None


def test_trigger_peek_clamps_by_lines():
    peek = _trigger_peek("\n".join(f"line {i}" for i in range(400)))

    assert peek["head"].count("\n") == TRIGGER_PEEK_LINES - 1
    assert peek["head"].startswith("line 0")
    assert peek["more"] == "show all 400 lines (3,489 characters)"


def test_trigger_peek_clamps_a_single_enormous_line_too():
    """A pasted document can be one line thousands of characters long, which a
    line-count clamp would let through whole."""
    peek = _trigger_peek("x" * 20_000)

    assert len(peek["head"]) <= TRIGGER_PEEK_CHARS + 2  # + the ellipsis
    assert peek["more"] == "show all 1 lines (20,000 characters)"


def test_a_long_trigger_message_renders_collapsed_but_whole(app_ctx, client):
    """Collapsed, not truncated: the reader can still get the whole message,
    and the open state rides the live-refresh swap like every other block."""
    room = _room()
    human = db.get_human_user()
    text = "OPENING LINE\n" + "\n".join(f"pasted line {i}" for i in range(400))
    db.post_chat_message(room.uuid, human.uuid, text)
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        body = html.unescape(
            client.get(f"/assistant?id={run.uuid}").get_data(as_text=True))
        assert 'data-k="trigger"' in body          # carried across a refresh
        assert "show all 401 lines" in body
        assert "OPENING LINE" in body
        assert "pasted line 399" in body           # the whole message is there
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_short_trigger_message_gets_no_toggle(app_ctx, client):
    room = _room()
    human = db.get_human_user()
    db.post_chat_message(room.uuid, human.uuid, "convert 12 feet")
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "convert 12 feet" in body
        assert 'data-k="trigger"' not in body
    finally:
        _cleanup(run.uuid, room.uuid)


def test_the_markdown_export_keeps_the_whole_trigger_message(app_ctx, client):
    """The export is for reading away from the page; there is nothing to
    click, so clamping it would only lose the message."""
    room = _room()
    human = db.get_human_user()
    text = "\n".join(f"pasted line {i}" for i in range(400))
    db.post_chat_message(room.uuid, human.uuid, text)
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        md = client.get(
            f"/assistant/{run.uuid}/markdown").get_data(as_text=True)
        assert "pasted line 0" in md
        assert "pasted line 399" in md
        assert "show all" not in md
    finally:
        _cleanup(run.uuid, room.uuid)


def _recall_filter_step(run, scorer_uuid):
    """A memory_query step and the recall-filter row it produced — the scoring
    call the action makes, recorded like every other call of the turn."""
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query",
        reason="look up what languages the operator knows",
        args={"query": "programming languages"})
    db.settle_assistant_step(
        step, phase="observed", observation_preview="found 2 facts",
        observation={"ok": True, "text": "found 2 facts",
                     "data": {"recall_filter": {
                         "mode": "llm", "group_from": "memory_filter",
                         "scorer_model": "granite4:tiny-h", "candidates": []}}})
    return db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed",
        action="recall_filter",
        reason="score the recalled candidates for relevance "
               "(memory_filter model group)",
        system_prompt="You score recalled candidates for relevance.",
        user_prompt="<candidates>rows go here</candidates>",
        reasoning="Two rows answer the question; the rest are unrelated.",
        model_response='{"items": [{"id": "7cd64094", "direct": 5}]}',
        code_driven=True, requested_at=datetime.now(UTC),
        model_uuid=scorer_uuid,
        input_tokens=3100, output_tokens=216, duration_ms=2500)


def test_recall_filter_renders_through_the_shared_step_machinery(app_ctx, client):
    """The scorer is a model call on a model group, so it renders like every
    other one: the standard request/response exchange, with the prompts, the
    scores it answered, and a LINK to the model that answered — which for this
    call is usually not the assistant's own. It had a bespoke block that could
    show none of that, because the call had no step row to render from."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    scorer = db.create_model_config("granite4:tiny-h", {})
    step = _recall_filter_step(run, scorer.uuid)
    db.finish_run(run, "finished")
    try:
        page, md = _rendered(client, run)
        assert f'id="step-{step.uuid}"' in page
        # A call the loop made, not one the model chose: no step budget spent.
        assert "score what memory_query recalled for relevance" in page
        assert "follow-up" in page
        # The model is a link, like every other answering model on the page.
        assert f'href="/model?id={scorer.uuid}"' in page
        assert "granite4:tiny-h" in md
        assert "You score recalled candidates for relevance." in page
        assert "<candidates>rows go here</candidates>" in page
        # The answer the operator came for: the ids it scored.
        assert "7cd64094" in page and "7cd64094" in md
        assert "in 3100" in page and "out 216" in page
    finally:
        _cleanup(run.uuid, room.uuid)
        db.db.session.query(db.ModelConfig).filter(
            db.ModelConfig.uuid == scorer.uuid).delete()
        db.db.session.commit()


def test_gated_recall_filter_leaves_no_row_because_no_model_ran(app_ctx, client):
    """A filter that never reached a model made no call, so there is no row to
    write: the memory_query step keeps its one-line note in the result data,
    and the trace does not show a step that would report no model and no cost."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="look")
    db.settle_assistant_step(
        step, phase="observed", observation_preview="nothing",
        observation={"ok": True, "text": "nothing", "data": {"recall_filter": {
            "mode": "gated", "reason": "no_model_group"}}})
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert "score what memory_query recalled" not in body
        assert "no_model_group" in body
    finally:
        _cleanup(run.uuid, room.uuid)


# --- action phase timing -----------------------------------------------------
#
# "The action took 33s" is not something an operator can act on. memory_query
# is three unrelated costs — a vector search that calls the embedder, a seed KB
# that may embed its whole registry, and a relevance filter that is a full LLM
# call on another model — and which one dominates changes per query.

def _timed_memory_query_run(room):
    """A finished run whose memory_query step carries a timing payload: two
    phases and two embedder calls, one of which is the bulk of the action."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="memory_query", reason="recall",
        input_tokens=100, output_tokens=20, duration_ms=1000)
    db.settle_assistant_step(step, phase="observed", observation_preview="ok",
                             observation={"ok": True, "text": "facts", "data": {
                                 "qa_static": 3, "qa_dynamic": 0, "memory": 6,
                                 "truncated": 0, "omitted": 0,
                                 "timing": {
                                     "phases": [
                                         {"name": "claim retrieval", "ms": 1200,
                                          "started_at": "2026-08-15T10:00:00+00:00"},
                                         {"name": "recall filter", "ms": 31600,
                                          "started_at": "2026-08-15T10:00:01+00:00"},
                                     ],
                                     "embeddings": {
                                         "count": 2, "ms": 900, "chars": 137,
                                         "models": ["embeddinggemma:300m"],
                                         "calls": [
                                             {"model": "embeddinggemma:300m", "ms": 500,
                                              "chars": 100, "texts": 1,
                                              "preview": ["what languages do I know"],
                                              "requested_at": "2026-08-15T10:00:00+00:00"},
                                             {"model": "embeddinggemma:300m", "ms": 400,
                                              "chars": 37, "texts": 1,
                                              "preview": ["where do I live"],
                                              "requested_at": "2026-08-15T10:00:02+00:00"},
                                         ],
                                         "dropped": 0,
                                     },
                                 }}})
    db.finish_run(run, "finished")
    return run


def test_memory_query_phase_timing_renders_in_both_views(app_ctx, client):
    """The phases render as a table on the page and in the export, so the
    action's own duration is broken into the parts that spent it."""
    room = _room()
    run = _timed_memory_query_run(room)
    try:
        page, md = _rendered(client, run)
        for body in (page, md):
            assert "claim retrieval" in body
            assert "recall filter" in body
            assert "1.2s" in body                  # the vector search
            assert "31.6s" in body                 # the filter's LLM call
        assert "io-timing" in page
        assert "| phase | took | at |" in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_embedder_is_counted_and_named_but_not_folded_into_llm_totals(app_ctx, client):
    """The embedder is a second model on the same runtime, so its calls show —
    as their own waterfall rows, a dashboard line, and a summary under the
    phases. It stays out of the LLM token/throughput totals, which it would
    only dilute: it produces no tokens."""
    room = _room()
    run = _timed_memory_query_run(room)
    try:
        page, md = _rendered(client, run)
        assert "kind-embedding" in page                    # waterfall rows
        assert "embed 0.9s" in page                        # dashboard Time cell
        # The model is named once, under the phases — not on every waterfall
        # row, where it would be the same string repeated down the column.
        assert "2 calls · 0.9s · 137 chars · embeddinggemma:300m" in page
        assert "embed embeddinggemma:300m" not in page
        assert "embed 0.9s (2 calls)" in md
        assert "| embed | embedding |" in md
        # The LLM totals are the step's own, untouched by the two embed calls.
        assert "in 100" in page and "out 20" in page
        steps = db.list_assistant_steps(run.uuid)
        stats = db.assistant_run_stats(steps)
        assert stats["calls"] == 1                         # the decide call only
        assert stats["duration_ms"] == 1000                # no embedder ms
        assert stats["embedding_calls"] == 2
        assert stats["embedding_ms"] == 900
    finally:
        _cleanup(run.uuid, room.uuid)


def test_each_embed_call_shows_the_text_it_was_given(app_ctx, client):
    """Two embed bars of the same length raise one question — same query or
    different ones? — that neither the bars nor the char total can answer, so
    the text has to be somewhere on the page.

    Not on the timeline label, which is a fixed-width column: a query of any
    length would push the timing off the row. It goes in the row's own detail
    pane and in the step's timing table, both of which have room for it.
    """
    room = _room()
    run = _timed_memory_query_run(room)
    try:
        page, md = _rendered(client, run)
        for body in (page, md):
            assert "what languages do I know" in body
            assert "where do I live" in body
        # The label stays the shape of the call, never its content.
        assert 'embed "what languages do I know"' not in page
        assert ">embed<" in page
        # Reachable from the row: the text is inside an event pane, not only
        # down in the timing table.
        panes = page.split('class="ev-pane')
        assert any("what languages do I know" in p for p in panes[1:])
        assert "io-embed-text" in page
    finally:
        _cleanup(run.uuid, room.uuid)


def test_an_embed_row_is_named_by_its_shape_never_by_its_text():
    """The label sits in a fixed-width column beside a bar, so it cannot carry
    a value of unbounded length: a query of any size would push the timing off
    the row. The text goes to the detail, which has room for it.

    A first-run seed populate embeds the whole registry in one call, and its
    size IS the thing worth knowing, so a batch says how many.
    """
    bulk, detail = db.embed_call_label(
        {"texts": 312, "chars": 90000, "preview": ["a fact", "another fact"]})
    assert bulk == "embed 312 texts"
    assert detail == "a fact / another fact"        # still readable in full

    # One text, however long, is just "embed".
    assert db.embed_call_label({"texts": 1, "chars": 26}) == ("embed", "")

    long_query = "x" * 200
    label, detail = db.embed_call_label({"texts": 1, "preview": [long_query]})
    assert label == "embed"
    assert detail == long_query                     # the detail keeps it whole

    short_query = "street address"
    label, detail = db.embed_call_label({"texts": 1, "preview": [short_query]})
    assert label == "embed"
    assert detail == short_query


def test_timing_payload_is_not_dumped_as_json_in_the_result(app_ctx, client):
    """The timing block renders as a table, so it must be stripped from the
    action-result data — left in, it reaches the page as an unreadable JSON
    dump beside the table that already says it."""
    room = _room()
    run = _timed_memory_query_run(room)
    try:
        page, md = _rendered(client, run)
        assert '"started_at"' not in page
        assert '"started_at"' not in md
    finally:
        _cleanup(run.uuid, room.uuid)


# --- rejected attempts -------------------------------------------------------
#
# A decide call whose first response is refused is retried (see
# ModelGroupAgent.REJECTED_RESPONSE_RETRIES). The step records the attempt it
# kept, so the refused ones — real seconds, real tokens — showed up on the
# trace as a gap between two calls with nothing in it.

def _retried_step_run(room):
    """A finished run whose reply step took two attempts: the first refused
    after 18s, the second accepted after 14s."""
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=1, phase="final", action="reply",
        reason="answer it", model_response='{"action":"reply"}',
        input_tokens=9500, output_tokens=200, duration_ms=14104,
        requested_at=datetime(2026, 8, 15, 15, 53, 5, tzinfo=UTC),
        system_prompt="You perform one narrow call.",
        user_prompt="<current_user_request>hi</current_user_request>",
        reasoning="thinking again",
        rejected_attempts=[{
            "model_uuid": str(uuid4()),
            "model_name": "gemma4:e4b",
            "requested_at": "2026-08-15T15:53:05+00:00",
            "ms": 18271,
            "input_tokens": 9400,
            "output_tokens": 682,
            "reasoning": "thinking about it",
            "response": '{"reason":null,"action":null,"args":null}',
            "error": "RejectedResponse: model did not return a valid "
                     "AssistantStepDecision",
            "feedback": [
                {"role": "assistant",
                 "content": '{"reason":null,"action":null,"args":null}'},
                {"role": "user",
                 "content": "<rejected_response>\nfix it\n</rejected_response>"},
            ],
        }])
    db.finish_run(run, "finished")
    return run


def test_a_rejected_attempt_shows_beside_the_response_that_replaced_it(
        app_ctx, client):
    room = _room()
    run = _retried_step_run(room)
    try:
        page, md = _rendered(client, run)
        for body in (page, md):
            assert "rejected response 1 of 1" in body
            assert '{"reason":null,"action":null,"args":null}' in body
            assert "model did not return a valid AssistantStepDecision" in body
            # …and above the decision that replaced it, which both renderers
            # show as the reconstructed decide JSON.
            assert '"action": "reply"' in body
            assert body.index("rejected response 1 of 1") < body.index(
                '"action": "reply"')
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_rejected_attempt_renders_as_a_full_exchange(app_ctx, client):
    """A rejected attempt is an LLM invocation like any other: it was sent a
    request, it thought, it answered. It renders through the same macro as the
    attempt that replaced it — so prompts, reasoning and response show for
    both, or the trace is teaching that one of them is less of a call."""
    room = _room()
    run = _retried_step_run(room)
    try:
        page, md = _rendered(client, run)

        # Both attempts number their request, and each carries the prompts.
        for body in (page, md):
            assert "model request (attempt 1)" in body
            assert "model request (attempt 2)" in body
            steps_only = _steps_region(body)
            assert steps_only.count("system prompt") == 2   # one per attempt
            assert steps_only.count("thinking about it") == 1  # attempt 1's
            assert steps_only.count("thinking again") == 1     # attempt 2's
        # The rejected attempt's collapsible blocks are addressable, like
        # every other block on the page (the live refresh reopens by key).
        assert 'data-k="attempt1-system"' in page
        assert 'data-k="attempt1-reasoning"' in page
        assert 'data-k="reasoning"' in page                 # the kept one
    finally:
        _cleanup(run.uuid, room.uuid)


def test_the_retry_shows_the_turns_the_first_attempt_never_saw(app_ctx, client):
    """What makes the second attempt a different call is what was appended to
    its prompt: its own refused answer, and why it was refused. Attempt 1
    carries none of it; attempt 2 carries both."""
    room = _room()
    run = _retried_step_run(room)
    try:
        page, md = _rendered(client, run)
        for body in (page, md):
            assert "<rejected_response>" in body
            assert _steps_region(body).count("<rejected_response>") == 1
        assert 'data-k="attempt1-turn1"' not in page        # nothing preceded it
        assert 'data-k="turn1"' in page and 'data-k="turn2"' in page
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_rejected_attempt_is_a_call_on_the_waterfall_and_in_the_totals(
        app_ctx, client):
    """The 18 seconds have to land somewhere. As its own row they are model
    time; left out they were "action" time — a gap where nothing ran."""
    room = _room()
    run = _retried_step_run(room)
    try:
        page, md = _rendered(client, run)
        assert "kind-rejected" in page
        assert "reply (rejected)" in page
        assert "| reply (rejected) | rejected |" in md

        steps = db.list_assistant_steps(run.uuid)
        stats = db.assistant_run_stats(steps)
        assert stats["calls"] == 2                          # both attempts
        assert stats["duration_ms"] == 14104 + 18271        # and both durations
        assert stats["input_tokens"] == 9500 + 9400         # and both prompts
    finally:
        _cleanup(run.uuid, room.uuid)


def test_a_step_without_retries_renders_no_rejected_block(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="final", action="reply",
        reason="answer it", model_response='{"action":"reply"}',
        duration_ms=1000)
    db.finish_run(run, "finished")
    try:
        page, md = _rendered(client, run)
        assert "rejected response" not in page
        assert "rejected response" not in md
    finally:
        _cleanup(run.uuid, room.uuid)


def test_the_kept_attempt_starts_where_the_rejected_one_ended(app_ctx, client):
    """Attempts are sequential — the retry goes out only after the previous
    answer was refused. Both bars drawn from `requested_at` (the call's start,
    which is the FIRST attempt's) made the kept one look like it ran alongside
    the attempt it replaced, and put it first in a list ordered by start."""
    room = _room()
    run = _retried_step_run(room)
    try:
        steps = db.list_assistant_steps(run.uuid)
        calls = db.assistant_llm_calls(steps)

        assert [c["label"] for c in calls] == ["reply (rejected)", "decide → reply"]
        rejected, kept = calls
        assert rejected["start"] == datetime(
            2026, 8, 15, 15, 53, 5, tzinfo=UTC).astimezone()
        # 18271ms later, to the millisecond — not the same instant.
        assert kept["start"] - rejected["start"] == timedelta(
            milliseconds=rejected["duration_ms"])
    finally:
        _cleanup(run.uuid, room.uuid)


def test_several_rejections_are_numbered_and_laid_end_to_end(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="final", action="reply",
        reason="answer it", duration_ms=5000,
        requested_at=datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC),
        rejected_attempts=[
            {"requested_at": "2026-08-15T12:00:00+00:00", "ms": 7000,
             "error": "RejectedResponse: nope", "response": "{}"},
            {"requested_at": "2026-08-15T12:00:07+00:00", "ms": 3000,
             "error": "RejectedResponse: nope again", "response": "{}"},
        ])
    db.finish_run(run, "finished")
    try:
        calls = db.assistant_llm_calls(db.list_assistant_steps(run.uuid))
        assert [c["label"] for c in calls] == [
            "reply (rejected 1/2)", "reply (rejected 2/2)", "decide → reply"]
        # …and the kept attempt sits after the LAST rejection, not the first.
        assert calls[-1]["start"] == datetime(
            2026, 8, 15, 12, 0, 10, tzinfo=UTC).astimezone()
    finally:
        _cleanup(run.uuid, room.uuid)


def test_every_gantt_bar_selects_the_event_that_explains_it(app_ctx, client):
    """A bar the reader cannot follow is a number with no provenance.

    The gantt IS the list — there is no second column of the same events, so
    every bar has to name an event the pane below can render, including the
    bars carrying no detail of their own, whose pane says exactly that.
    """
    import re

    room = _room()
    run = _timed_memory_query_run(room)
    try:
        page, _ = _rendered(client, run)
        panes = re.findall(r'class="ev-pane[^"]*" id="(ev-\d+)"', page)
        picked = re.findall(r'class="wf-row ev-pick[^"]*"\s+data-ev="(ev-\d+)"',
                            page)

        assert picked, "no gantt rows rendered"
        for target in picked:
            assert target in panes, f"{target} has no detail pane"
        # One pane per bar, and one bar per event: a second enumeration of the
        # same run is what this layout exists to avoid.
        assert sorted(picked) == sorted(panes)
        assert "log-row" not in page
        # Exactly one pane is open to begin with, or the page opens on a wall
        # of every prompt the run sent.
        assert page.count('class="ev-pane on"') == 1
    finally:
        _cleanup(run.uuid, room.uuid)


def test_the_inspector_names_the_override_a_call_ran_on(app_ctx, client):
    """A step records the OVERRIDE it ran on, not the base config, so looking
    the uuid up as a config alone left the reader eight hex characters. An
    override is named by the model it tunes plus the tuning."""
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    cfg = db.list_model_configs()[0]
    override = db.create_model_config_override(
        cfg.uuid, {}, display_name="t0.15 probe")
    db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="final", action="reply",
        reason="answer", system_prompt="s", user_prompt="u",
        model_response="{}", code_driven=True, model_uuid=override.uuid,
        input_tokens=10, output_tokens=2, duration_ms=1000,
        requested_at=datetime.now(UTC))
    db.finish_run(run, "finished")
    try:
        page, _ = _rendered(client, run)
        assert f"{cfg.model_name} · t0.15 probe" in page
    finally:
        _cleanup(run.uuid, room.uuid)


def test_an_inspector_block_looks_like_every_other_pre():
    """`.as-main pre` already gives every block its box and its size, which is
    why `.trigmsg` needs to declare almost nothing. An inspector block holds
    the same kind of thing — a prompt, a response, a request — so it takes the
    same box rather than restating it in near-miss values: a 4px radius beside
    a 6px one, #f7f8fa beside #f6f8fa.

    Its height is bounded, but not here: a long block is clamped to a few
    lines by clampBlocks, which measures the block's own line-height. See
    test_a_long_block_clamps_instead_of_scrolling_inside_itself.
    """
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    rule = re.search(r"\.as-main \.ev-pre \{[^}]*\}", ASSISTANT_TEMPLATE)
    assert rule, "the .ev-pre rule is gone"
    for restated in ("font-size", "background", "border-radius", "padding"):
        assert restated not in rule.group(0), restated


def test_the_inspector_meta_line_is_right_aligned_like_a_step_s():
    """A step's io-meta sits at the right end of its row. The inspector's meta
    line reports the same things about the same call, so it lands in the same
    place rather than reading as a different kind of row."""
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    rule = re.search(r"\.as-main \.ev-kpis \{[^}]*\}", ASSISTANT_TEMPLATE)
    assert rule, "the .ev-kpis rule is gone"
    assert "justify-content:flex-end" in rule.group(0).replace(" ", "")


def test_the_inspector_meta_line_shares_the_step_meta_styling():
    """The inspector's meta line and a step's io-meta report the same things
    about the same call. They take one rule rather than two sets of values, so
    a monospace face here against a sans-serif one there cannot come back.
    """
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    shared = re.search(r"\.as-main \.step \.io-meta[^{]*\{[^}]*\}",
                       ASSISTANT_TEMPLATE)
    assert shared and ".ev-kpis" in shared.group(0), "ev-kpis has its own rule"

    link = re.search(r"\.as-main \.step \.io-model[^{]*\{[^}]*\}",
                     ASSISTANT_TEMPLATE)
    assert link and ".ev-kpi a" in link.group(0), "the model link diverged"

    # Whatever ev-kpis still declares must not restate the shared look.
    own = re.search(r"\.as-main \.ev-kpis \{[^}]*\}", ASSISTANT_TEMPLATE)
    for restated in ("font-family", "font-size", "color:"):
        assert restated not in (own.group(0) if own else ""), restated


def test_every_styled_inspector_class_has_a_rule():
    """A class in the markup with no rule behind it fails silently — the page
    still renders, just wrong. Editing the stylesheet by slicing a range is
    exactly how a neighbouring rule disappears unnoticed.
    """
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    styled = {"ev-crumb-label", "ev-crumb-desc",
              "ev-kpis", "ev-kpi", "ev-pre", "ev-block", "ev-pane",
              "ev-detail", "ev-links", "ev-note", "log-detail", "wf-tick"}
    for name in styled:
        assert re.search(rf"\.{re.escape(name)}\b[^{{]*\{{", ASSISTANT_TEMPLATE), (
            f".{name} is used but has no CSS rule")


def test_a_collapsible_summary_is_not_selectable_text():
    """Clicking a toggle repeatedly selects its label, which is never what the
    click meant. The step's prompt summaries already opt out; the inspector's
    take the same rule rather than a near-copy of it — 70% beside 0.64rem,
    0.05em beside 0.04em, and no user-select at all.
    """
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    rule = re.search(
        r"\.as-main \.step \.prompt > summary,[^{]*\{[^}]*\}", ASSISTANT_TEMPLATE)
    assert rule, "the shared summary rule is gone"
    assert "details.ev-block > summary" in rule.group(0), (
        "the inspector's summaries have their own rule again")
    assert "user-select:none" in rule.group(0).replace(" ", "")


def test_a_long_block_clamps_instead_of_scrolling_inside_itself():
    """A scroll area inside a scrolling page traps the wheel: the reader aims
    at the page and moves the block, or the reverse. A long block is cut to a
    few lines with a toggle instead, so there is only ever one scroller.
    """
    import re

    from webapp.assistant_views import ASSISTANT_TEMPLATE

    rule = re.search(r"\.as-main \.ev-pre \{[^}]*\}", ASSISTANT_TEMPLATE)
    assert rule, "the .ev-pre rule is gone"
    flat = rule.group(0).replace(" ", "").replace("\n", "")
    assert "max-height" not in flat, "the inner scroller is back"
    assert "overflow:auto" not in flat

    # Clamped state and its control exist, and the clamp is measured in lines.
    assert ".ev-pre.clamped" in ASSISTANT_TEMPLATE
    assert ".ev-more" in ASSISTANT_TEMPLATE
    assert "EV_CLAMP_LINES" in ASSISTANT_TEMPLATE
    assert "function clampBlocks" in ASSISTANT_TEMPLATE
