"""S5: assistant edit_file — confirm-tier write with a dry-run unified-diff
preview, confined to the workspace by resolve_workspace_path. Model-free; the
workspace root is monkeypatched to a tmp dir for isolation."""

from pathlib import Path
from uuid import uuid4

import pytest

import db
import tools.workspace_policy as wp
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_edit_file,
)
from agents.assistant_fakes import scripted_decisions
from agents.assistant_writes import execute_write_intent
from agents.config import ASSISTANT_UUID
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
def ws(tmp_path, monkeypatch):
    """Confine the workspace to tmp_path for the duration of a test."""
    monkeypatch.setattr(wp, "SHELL_CWD", str(tmp_path))
    monkeypatch.setattr(wp, "SHELL_ROOT", Path(tmp_path).resolve())
    return tmp_path


def _ctx(dry_run=False):
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID,
        step_index=0, dry_run=dry_run,
    )


def test_capability_is_confirm_tier_dry_run_write():
    cap = CAPABILITIES[AssistantActionName.EDIT_FILE]
    assert cap.write is True and cap.tier == "confirm" and cap.dry_run is True
    assert cap.output_cap_chars == 12000


def test_dry_run_shows_diff_writes_nothing(ws):
    f = ws / "doc.txt"
    f.write_text("hello\nworld\n")
    obs = _action_edit_file(_ctx(dry_run=True), {"path": "doc.txt", "content": "hello\nthere\n"})
    assert obs.ok is True
    assert "-world" in obs.text and "+there" in obs.text
    assert f.read_text() == "hello\nworld\n"  # unchanged


def test_real_execution_writes_file(ws):
    f = ws / "doc.txt"
    f.write_text("old\n")
    obs = _action_edit_file(_ctx(), {"path": "doc.txt", "content": "new content\n"})
    assert obs.ok is True
    assert f.read_text() == "new content\n"
    assert obs.data["old_chars"] == 4 and obs.data["new_chars"] == 12


def test_create_new_file(ws):
    obs = _action_edit_file(_ctx(), {"path": "sub/new.txt", "content": "fresh\n"})
    assert obs.ok is True
    assert (ws / "sub" / "new.txt").read_text() == "fresh\n"


def test_no_op_rejected(ws):
    f = ws / "doc.txt"
    f.write_text("same\n")
    assert _action_edit_file(_ctx(), {"path": "doc.txt", "content": "same\n"}).ok is False


def test_path_traversal_rejected(ws):
    obs = _action_edit_file(_ctx(), {"path": "../escape.txt", "content": "x"})
    assert obs.ok is False and "blocked" in obs.text
    assert not (ws.parent / "escape.txt").exists()


def test_absolute_outside_workspace_rejected(ws):
    assert _action_edit_file(_ctx(), {"path": "/etc/hosts", "content": "x"}).ok is False


def test_sensitive_name_rejected(ws):
    assert _action_edit_file(_ctx(), {"path": ".env", "content": "SECRET=1"}).ok is False


def test_size_cap(ws):
    big = "x" * 100_001
    assert _action_edit_file(_ctx(), {"path": "big.txt", "content": big}).ok is False
    f = ws / "huge.txt"
    f.write_text("y" * 100_001)
    assert _action_edit_file(_ctx(), {"path": "huge.txt", "content": "small"}).ok is False


def test_propose_uses_diff_preview_then_confirm_writes(app_ctx, ws):
    f = ws / "doc.txt"
    f.write_text("before\n")
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"edit-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "edit the doc")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="edit", action=AssistantActionName.EDIT_FILE,
                              args={"path": "doc.txt", "content": "after\n"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"1_specification": "en, metric", "2_message": "proposed", "3_audit": "OK"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        assert intent.state == "proposed"
        assert "+after" in intent.preview_text and "-before" in intent.preview_text
        assert f.read_text() == "before\n"  # confirm-tier: not written inline
        obs = execute_write_intent(intent.uuid)
        assert obs.ok is True
        assert f.read_text() == "after\n"
        assert db.get_write_intent(intent.uuid).state == "completed"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_confirm_refuses_if_file_changed_since_preview(app_ctx, ws):
    """The diff was previewed against the file at propose time; if the file
    changes before confirm, applying would silently clobber the unpreviewed
    version. Confirm must refuse and not write."""
    f = ws / "doc.txt"
    f.write_text("v1\n")
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"edit-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "edit the doc")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="edit", action=AssistantActionName.EDIT_FILE,
                              args={"path": "doc.txt", "content": "v2\n"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"1_specification": "en, metric", "2_message": "proposed", "3_audit": "OK"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intent = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).one()
        # File changes after the preview, before confirm.
        f.write_text("v1-edited-elsewhere\n")
        obs = execute_write_intent(intent.uuid)
        assert obs.ok is False and "changed" in obs.text
        assert f.read_text() == "v1-edited-elsewhere\n"  # NOT clobbered
        assert db.get_write_intent(intent.uuid).state == "failed"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
