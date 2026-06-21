"""The ChatMessage admin list surfaces the message uuid (full + copyable) so an
operator can grab a precise reference to a problematic message."""

import json
from uuid import UUID, uuid4

import pytest

import db
import webapp  # noqa: F401 — registers admin views on the app
from agents.config import ASSISTANT_UUID
from webapp.core import (
    _fmt_copyable_uuid,
    _format_chatmessage_text,
    _resolve_debug_assistant_text,
    app as flask_app,
)


class _Msg:
    def __init__(self, kind, text):
        self.kind, self.text = kind, text


def test_text_formatter_passthrough_for_normal_message(app_ctx):
    out = _format_chatmessage_text(None, None, _Msg("message", "hello world"), "text")
    assert out == "hello world"


def test_text_formatter_expands_debug_assistant_pointer(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"adm-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID, step_limit=6)
    db.append_assistant_step(
        run_id=run.id, step_index=0, phase="planned", action="kanban_read",
        reason="List all Done tasks", args={"board_uuid": "b-123"})
    db.append_assistant_step(
        run_id=run.id, step_index=0, phase="observed", action="kanban_read",
        observation_preview="# Done\n- task A\n- task B")
    try:
        ptr = json.dumps({"run_id": run.id, "step_index": 0, "summary": "x"})
        out = str(_format_chatmessage_text(None, None, _Msg("debug-assistant", ptr), "text"))
        assert "kanban_read" in out
        assert "List all Done tasks" in out
        assert "b-123" in out                 # the args
        assert "task A" in out                # the observation/result
        assert "run_id" not in out            # not the raw pointer
    finally:
        db.db.session.query(db.ChatMessage).filter_by(room_uuid=room.uuid).delete()
        db.db.session.query(db.Chatroom).filter_by(uuid=room.uuid).delete()
        db.db.session.commit()


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


class _Row:
    def __init__(self, value):
        self.uuid = value


def test_copyable_uuid_formatter_renders_full_value_and_copy_button():
    u = UUID("795ea3ee-9426-4e03-973a-5d6f6c814b46")
    out = str(_fmt_copyable_uuid(None, None, _Row(u), "uuid"))
    assert "795ea3ee-9426-4e03-973a-5d6f6c814b46" in out  # full, not truncated
    assert "<code>" in out
    assert "Copy uuid" in out
    assert "clipboard.writeText" in out


def test_copyable_uuid_formatter_handles_missing_value():
    assert _fmt_copyable_uuid(None, None, _Row(None), "uuid") == ""


def test_chatmessage_admin_list_has_uuid_column(app_ctx):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = flask_app.test_client()
    resp = client.get("/admin/chatmessage/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "UUID" in body  # the column header is registered


def _make_debug_assistant_row(observation):
    """Create a run + a terminal step with `observation`, returning the
    debug-assistant chat message it anchored."""
    from agents.config import ASSISTANT_UUID
    human = db.get_human_user()
    room = db.create_chatroom(f"trunc-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID, step_limit=6)
    db.append_assistant_step(
        run_id=run.id, step_index=0, phase="observed", action="query_memory",
        reason="look it up", args={"query": "x"}, observation_preview=observation)
    msg = next(m for m in db.list_room_messages(room.uuid) if m["kind"] == "debug-assistant")
    return room, msg


def test_formatter_and_resolver_show_full_untruncated_observation(app_ctx):
    long_obs = "FACT-" + ("A" * 3000)  # well beyond the old 600/1200 caps
    room, msg = _make_debug_assistant_row(long_obs)
    try:
        model = db.db.session.query(db.ChatMessage).filter_by(uuid=UUID(msg["uuid"])).one()
        out = str(_format_chatmessage_text(None, None, model, "text"))
        assert long_obs in out                       # full observation, not truncated
        assert long_obs in model.text                # the text column itself holds it
    finally:
        db.db.session.query(db.ChatMessage).filter_by(room_uuid=room.uuid).delete()
        db.db.session.query(db.Chatroom).filter_by(uuid=room.uuid).delete()
        db.db.session.commit()


def test_edit_page_shows_resolved_trace_field(app_ctx):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = flask_app.test_client()
    long_obs = "OBS-" + ("B" * 2000)
    room, msg = _make_debug_assistant_row(long_obs)
    try:
        resp = client.get(f"/admin/chatmessage/edit/?id={msg['id']}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Resolved trace" in body          # the read-only field is present
        assert long_obs in body                  # with the full observation
        assert "query_memory" in body
    finally:
        db.db.session.query(db.ChatMessage).filter_by(room_uuid=room.uuid).delete()
        db.db.session.query(db.Chatroom).filter_by(uuid=room.uuid).delete()
        db.db.session.commit()
