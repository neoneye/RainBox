"""The kanban_task_set_title / kanban_task_set_description /
kanban_board_set_name / kanban_board_set_description actions: log-and-undo
edits of one text field, with a no-op guard and an expect_<field> guarded
undo that restores the previous value via the same capability."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    _action_set_kanban_board_description,
    _action_set_kanban_board_name,
    _action_set_kanban_task_description,
    _action_set_kanban_task_title,
)
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
def board(app_ctx):
    b = db.kanban_create_board("edit board", description="the old blurb")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Old title", "description": "old text"}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    try:
        yield data
    finally:
        db.kanban_delete_board(bu)


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(),
        agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def test_capabilities_are_log_and_undo_writes():
    for name, required in (
        (AssistantActionName.KANBAN_TASK_SET_TITLE, ("task_uuid", "title")),
        (AssistantActionName.KANBAN_TASK_SET_DESCRIPTION, ("task_uuid", "description")),
        (AssistantActionName.KANBAN_BOARD_SET_NAME, ("board_uuid", "name")),
        (AssistantActionName.KANBAN_BOARD_SET_DESCRIPTION, ("board_uuid", "description")),
    ):
        cap = CAPABILITIES[name]
        assert cap.write is True and cap.read is False
        assert cap.tier == "log_and_undo"
        assert cap.prompt_exposed is True
        assert cap.required_args == required


def test_set_task_title_updates_and_records_undo(board):
    task = board["tasks"][0]
    obs = _action_set_kanban_task_title(
        _ctx(), {"task_uuid": task["uuid"], "title": "New title"})
    assert obs.ok is True
    after = db.kanban_get_task(UUID(task["uuid"]))
    assert after["title"] == "New title"
    assert obs.data["undo"] == {
        "capability": "kanban_task_set_title",
        "payload": {"task_uuid": task["uuid"], "title": "Old title",
                    "expect_title": "New title"}}
    # The edit is on the task's audit trail.
    edited = [e for e in db.kanban_task_events(UUID(task["uuid"]))
              if e["kind"] == "edited"]
    assert edited and "Old title" in edited[0]["detail"]


def test_set_task_description_and_undo_restores_old_text(board):
    from agents.assistant_writes import undo_write_intent

    task = board["tasks"][0]
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6)
    obs = _action_set_kanban_task_description(
        _ctx(), {"task_uuid": task["uuid"], "description": "new text"})
    assert obs.ok is True
    assert db.kanban_get_task(UUID(task["uuid"]))["description"] == "new text"
    intent = db.create_write_intent(
        run_uuid=run.uuid, capability_name="kanban_task_set_description",
        payload={"task_uuid": task["uuid"], "description": "new text"},
        preview_text="kanban_task_set_description: …",
        room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
        state="completed", result={"undo": obs.data["undo"]},
    )
    try:
        undone = undo_write_intent(intent.uuid)
        assert undone.ok is True
        assert db.kanban_get_task(UUID(task["uuid"]))["description"] == "old text"
        assert db.get_write_intent(intent.uuid).state == "undone"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_uuid == run.uuid).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == run.uuid).delete()
        db.db.session.commit()


def test_undo_can_restore_an_empty_description(board):
    task = board["tasks"][0]
    db.kanban_update_task(UUID(task["uuid"]), description="")
    obs = _action_set_kanban_task_description(
        _ctx(), {"task_uuid": task["uuid"], "description": "filled in"})
    assert obs.ok is True
    # The undo payload carries the empty previous value; replaying it (undo
    # bypasses required-arg validation) clears the description again.
    undo = _action_set_kanban_task_description(_ctx(), obs.data["undo"]["payload"])
    assert undo.ok is True
    assert db.kanban_get_task(UUID(task["uuid"]))["description"] == ""


def test_undo_refused_when_field_changed_since(board):
    task = board["tasks"][0]
    obs = _action_set_kanban_task_title(
        _ctx(), {"task_uuid": task["uuid"], "title": "New title"})
    assert obs.ok is True
    db.kanban_update_task(UUID(task["uuid"]), title="Even newer")
    undo = _action_set_kanban_task_title(_ctx(), obs.data["undo"]["payload"])
    assert undo.ok is False
    assert "changed since" in undo.text
    assert db.kanban_get_task(UUID(task["uuid"]))["title"] == "Even newer"


def test_same_value_is_flagged_not_silent_noop(board):
    task = board["tasks"][0]
    obs = _action_set_kanban_task_title(
        _ctx(), {"task_uuid": task["uuid"], "title": "Old title"})
    assert obs.ok is False
    assert "changes nothing" in obs.text


def test_empty_task_title_is_refused(board):
    task = board["tasks"][0]
    obs = _action_set_kanban_task_title(
        _ctx(), {"task_uuid": task["uuid"], "title": "   "})
    assert obs.ok is False
    assert db.kanban_get_task(UUID(task["uuid"]))["title"] == "Old title"


def test_set_board_name_and_undo(board):
    bu = board["uuid"]
    obs = _action_set_kanban_board_name(
        _ctx(), {"board_uuid": bu, "name": "renamed board"})
    assert obs.ok is True
    assert db.kanban_load_board(UUID(bu))["name"] == "renamed board"
    assert obs.data["undo"]["payload"] == {
        "board_uuid": bu, "name": "edit board", "expect_name": "renamed board"}
    undo = _action_set_kanban_board_name(_ctx(), obs.data["undo"]["payload"])
    assert undo.ok is True
    assert db.kanban_load_board(UUID(bu))["name"] == "edit board"


def test_set_board_description(board):
    bu = board["uuid"]
    obs = _action_set_kanban_board_description(
        _ctx(), {"board_uuid": bu, "description": "the new blurb"})
    assert obs.ok is True
    assert db.kanban_load_board(UUID(bu))["description"] == "the new blurb"
    assert obs.data["undo"]["payload"]["description"] == "the old blurb"


def test_invalid_and_missing_targets(board):
    task = board["tasks"][0]
    assert _action_set_kanban_task_title(
        _ctx(), {"task_uuid": "nope", "title": "x"}).ok is False
    assert _action_set_kanban_task_title(
        _ctx(), {"task_uuid": str(uuid4()), "title": "x"}).ok is False
    assert _action_set_kanban_board_name(
        _ctx(), {"board_uuid": "nope", "name": "x"}).ok is False
    assert _action_set_kanban_board_name(
        _ctx(), {"board_uuid": str(uuid4()), "name": "x"}).ok is False
    assert db.kanban_get_task(UUID(task["uuid"]))["title"] == "Old title"
