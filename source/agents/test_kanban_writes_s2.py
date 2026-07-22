"""S2: assistant kanban write families — mark-done (kanban_task_complete) and comment
(kanban_task_comment), both log-and-undo. Mirrors test_kanban_move_action.py."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_comment_kanban_task,
    _action_complete_kanban_task,
)
from agents.assistant_fakes import scripted_decisions
from agents.assistant_writes import undo_write_intent
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
    b = db.kanban_create_board("s2 board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n}
                        for n in ("To do", "Doing", "Done")]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Ship it", "description": "d"}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    try:
        yield data
    finally:
        db.kanban_delete_board(bu)


def _ctx():
    return AssistantActionContext(
        journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def _comment_details(task_uuid):
    return [e["detail"] for e in db.kanban_task_events(task_uuid) if e["kind"] == "comment"]


# --- capabilities -------------------------------------------------------------


def test_capabilities_are_log_and_undo_writes():
    for name in (AssistantActionName.KANBAN_TASK_COMPLETE, AssistantActionName.KANBAN_TASK_COMMENT):
        cap = CAPABILITIES[name]
        assert cap.write is True and cap.tier == "log_and_undo"


# --- complete -----------------------------------------------------------------


def test_complete_marks_done_and_returns_inverse(board):
    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][2]["uuid"]
    obs = _action_complete_kanban_task(_ctx(), {"task_uuid": task["uuid"]})
    assert obs.ok is True
    assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done  # moved to last col
    assert obs.data["undo"] == {
        "capability": "kanban_task_column",
        "payload": {"task_uuid": task["uuid"], "column_uuid": todo,
                    "expect_column": done},
    }
    # A 'done' event was recorded.
    assert any(e["kind"] == "done" for e in db.kanban_task_events(UUID(task["uuid"])))


def test_complete_rejects_missing_task(app_ctx):
    obs = _action_complete_kanban_task(_ctx(), {"task_uuid": str(uuid4())})
    assert obs.ok is False


def test_complete_via_loop_then_undo_reopens(board):
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"s2-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "mark it done")
    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][2]["uuid"]
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="done", action=AssistantActionName.KANBAN_TASK_COMPLETE,
                              args={"task_uuid": task["uuid"]}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"1_specification": "en, metric", "2_message": "done", "3_audit": "OK"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done
        intents = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).all()
        assert len(intents) == 1 and intents[0].state == "completed"
        # Undo re-opens the task to its prior column and marks the intent undone.
        obs = undo_write_intent(intents[0].uuid)
        assert obs.ok is True
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == todo
        assert db.get_write_intent(intents[0].uuid).state == "undone"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


# --- comment ------------------------------------------------------------------


def test_comment_appends_event_and_returns_retraction_inverse(board):
    task = board["tasks"][0]
    obs = _action_comment_kanban_task(_ctx(), {"task_uuid": task["uuid"], "text": "looks good"})
    assert obs.ok is True
    assert "looks good" in _comment_details(UUID(task["uuid"]))
    assert obs.data["undo"]["capability"] == "kanban_task_comment"
    assert obs.data["undo"]["payload"]["text"] == "↩ retracted: looks good"


def test_comment_undo_posts_retraction_keeps_original(board):
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"s2c-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "comment please")
    task = board["tasks"][0]
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="comment", action=AssistantActionName.KANBAN_TASK_COMMENT,
                              args={"task_uuid": task["uuid"], "text": "looks good"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"1_specification": "en, metric", "2_message": "commented", "3_audit": "OK"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intents = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).all()
        assert len(intents) == 1 and intents[0].state == "completed"
        undo_write_intent(intents[0].uuid)
        details = _comment_details(UUID(task["uuid"]))
        assert "looks good" in details                      # original kept (append-only)
        assert "↩ retracted: looks good" in details         # retraction posted
        assert db.get_write_intent(intents[0].uuid).state == "undone"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_comment_rejects_missing_task(app_ctx):
    obs = _action_comment_kanban_task(_ctx(), {"task_uuid": str(uuid4()), "text": "hi"})
    assert obs.ok is False
