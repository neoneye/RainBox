"""S2 batch 2: assistant kanban_create (log-and-undo; undo deletes the task) and
the internal, non-model-invocable kanban_delete_task inverse."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantStepDecision,
    _action_create_kanban_task,
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
    b = db.kanban_create_board("create board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": "To do"}]
    fresh["tasks"] = []
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


def test_capabilities_create_exposed_delete_internal():
    create = CAPABILITIES[AssistantActionName.KANBAN_CREATE]
    assert create.write is True and create.tier == "log_and_undo" and create.prompt_exposed is True
    delete = CAPABILITIES[AssistantActionName.KANBAN_DELETE_TASK]
    assert delete.prompt_exposed is False


def test_create_makes_task_and_returns_delete_inverse(board):
    bu = board["uuid"]
    col = board["columns"][0]["uuid"]
    obs = _action_create_kanban_task(
        _ctx(), {"board_uuid": bu, "column_uuid": col, "title": "Follow up"})
    assert obs.ok is True
    tu = obs.data["task_uuid"]
    assert db.kanban_get_task(UUID(tu))["title"] == "Follow up"
    assert obs.data["undo"] == {
        "capability": "kanban_delete_task", "payload": {"task_uuid": tu}}
    assert any(e["kind"] == "created" for e in db.kanban_task_events(UUID(tu)))


def test_create_rejects_unknown_column(board):
    obs = _action_create_kanban_task(
        _ctx(), {"board_uuid": board["uuid"], "column_uuid": str(uuid4()), "title": "x"})
    assert obs.ok is False


def test_create_defaults_to_first_column_when_omitted(board):
    """The operator gives a board, not a column ('add a task to board ax'). With
    no column_uuid the task lands in the board's first column."""
    bu = board["uuid"]
    first_col = board["columns"][0]["uuid"]
    obs = _action_create_kanban_task(
        _ctx(), {"board_uuid": bu, "title": "Dentist checkup",
                 "description": "the 6 month check up"})
    assert obs.ok is True
    tu = obs.data["task_uuid"]
    task = db.kanban_get_task(UUID(tu))
    assert task["title"] == "Dentist checkup"
    assert task["columnUuid"] == first_col
    assert obs.data["column_uuid"] == first_col


def test_create_defaults_when_column_is_placeholder(board):
    """A small model that can't resolve a column passes a placeholder like
    '<COLUMN_UUID>'. Treat an unparseable column as 'unspecified' → first column,
    instead of looping on 'invalid column_uuid' until the step limit (run 12)."""
    bu = board["uuid"]
    first_col = board["columns"][0]["uuid"]
    obs = _action_create_kanban_task(
        _ctx(), {"board_uuid": bu, "column_uuid": "<COLUMN_UUID>", "title": "Dentist checkup"})
    assert obs.ok is True
    assert db.kanban_get_task(UUID(obs.data["task_uuid"]))["columnUuid"] == first_col


def test_create_uses_explicit_valid_column(board):
    bu = board["uuid"]
    col = board["columns"][0]["uuid"]
    obs = _action_create_kanban_task(
        _ctx(), {"board_uuid": bu, "column_uuid": col, "title": "explicit"})
    assert obs.ok is True
    assert db.kanban_get_task(UUID(obs.data["task_uuid"]))["columnUuid"] == col


def test_create_rejects_invalid_board(board):
    obs = _action_create_kanban_task(_ctx(), {"board_uuid": "not-a-uuid", "title": "x"})
    assert obs.ok is False and "board_uuid" in obs.text


def test_create_via_loop_then_undo_deletes(board):
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"crt-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "make a task")
    bu, col = board["uuid"], board["columns"][0]["uuid"]
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="create", action=AssistantActionName.KANBAN_CREATE,
                              args={"board_uuid": bu, "column_uuid": col, "title": "Follow up"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "created"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        intents = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).all()
        assert len(intents) == 1 and intents[0].state == "completed"
        tu = UUID(intents[0].result["undo"]["payload"]["task_uuid"])
        assert db.kanban_get_task(tu) is not None
        obs = undo_write_intent(intents[0].uuid)
        assert obs.ok is True
        assert db.kanban_get_task(tu) is None          # undo deleted it
        assert db.get_write_intent(intents[0].uuid).state == "undone"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_duplicate_create_in_same_run_is_blocked(board):
    """Run 13: after a successful create the model didn't reply — it wandered and
    created the task a SECOND time. An identical write already completed this run
    must be blocked (no duplicate task, no second write-intent)."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"dup-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "add dentist task")
    bu = board["uuid"]
    args = {"board_uuid": bu, "title": "Dentist checkup", "description": "the 6 month check up"}
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="create", action=AssistantActionName.KANBAN_CREATE, args=dict(args)),
        AssistantStepDecision(reason="create again", action=AssistantActionName.KANBAN_CREATE, args=dict(args)),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY, args={"message": "created"}),
    )
    try:
        result = agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert result["status"] == "finished"
        tasks = [t for t in db.kanban_load_board(UUID(bu))["tasks"]
                 if t["title"] == "Dentist checkup"]
        assert len(tasks) == 1                       # duplicate blocked
        intents = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).all()
        assert len(intents) == 1                     # only one write recorded
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_reply_includes_clickable_task_link_after_create(board):
    """After creating a task the reply carries a clickable relative link to the
    TASK (not just the board): /kanban?id=<task> opens that task's overlay."""
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"link-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "add bike task")
    bu = board["uuid"]
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="create", action=AssistantActionName.KANBAN_CREATE,
                              args={"board_uuid": bu, "title": "Bike checkup"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "The task has been added."}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        task_uuid = next(t["uuid"] for t in db.kanban_load_board(UUID(bu))["tasks"]
                         if t["title"] == "Bike checkup")
        reply = db.db.session.query(db.ChatMessage).filter_by(
            room_uuid=chatroom.uuid, sender_uuid=ASSISTANT_UUID, kind="message").one()
        link = f"/kanban?id={task_uuid}"
        assert f"[{link}]({link})" in reply.text        # clickable link to the task
        assert reply.text.startswith("The task has been added.")  # model's prose kept
    finally:
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()


def test_model_cannot_invoke_delete_task(board):
    """A scripted kanban_delete_task decision is rejected by the validator guard
    (not prompt-exposed) — the task is NOT deleted."""
    bu, col = board["uuid"], board["columns"][0]["uuid"]
    created = db.kanban_create_task(UUID(bu), UUID(col), title="keep me", actor="test")
    tu = UUID(created["uuid"])
    human = db.get_human_user()
    chatroom = db.create_chatroom(f"del-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "try delete")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="delete", action=AssistantActionName.KANBAN_DELETE_TASK,
                              args={"task_uuid": str(tu)}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "done"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        assert db.kanban_get_task(tu) is not None       # guard blocked the delete
        # No write-intent ledger row was created for the rejected internal action.
        assert db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).count() == 0
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
