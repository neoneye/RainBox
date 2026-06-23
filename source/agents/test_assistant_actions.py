"""Tests for the assistant's read-only actions (PR 4) and dispatch.

Each action reuses an existing rainbox surface (memory retrieval, the QueryAgent
Q&A pipeline, the workspace command policy, kanban reads) and returns an
AssistantObservation; the dispatcher owns validation, the output cap, and the
running->observed/failed trace boundary. No writes, no MCP, no generated code.
"""

from uuid import UUID, uuid4

import pytest

import db
from db import AssistantStep, MemoryClaim
from agents.assistant import (
    AssistantActionContext,
    AssistantActionName,
    AssistantAgent,
    AssistantObservation,
    AssistantStepDecision,
    _action_kanban_read,
    _action_query_memory,
    _action_query_qa,
    _action_workspace_read_command,
)
from agents.assistant import ASSISTANT_SYSTEM_PROMPT, CAPABILITIES
from agents.assistant_fakes import scripted_decisions
from agents.config import ASSISTANT_UUID


def test_read_action_descriptions_disambiguate_query_qa_from_kanban():
    """Run 12: the model used query_qa to 'query the kanban boards'. The catalog
    must steer inspecting a board to kanban_read, and mark query_qa as not-for-kanban."""
    qa = CAPABILITIES[AssistantActionName.QUERY_QA].description.lower()
    kb = CAPABILITIES[AssistantActionName.KANBAN_READ].description.lower()
    assert "kanban" in qa and "not for" in qa          # query_qa says: not for kanban
    assert "column" in kb                              # kanban_read: look up a board's columns
    assert "kanban_read" in ASSISTANT_SYSTEM_PROMPT.lower()


def test_system_prompt_forbids_claiming_unperformed_writes():
    """Run 19: the model read a task then replied 'successfully moved' with no
    kanban_move_task step. The prompt must forbid claiming a write it didn't perform."""
    p = ASSISTANT_SYSTEM_PROMPT.lower()
    assert "never tell the operator you did something" in p
    assert "reading a task is not moving it" in p


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
def fresh_subject() -> str:
    return f"test-{uuid4()}"


def _cleanup_subject(subject: str) -> None:
    db.db.session.query(MemoryClaim).filter(MemoryClaim.subject == subject).delete()
    db.db.session.commit()


def _ctx() -> AssistantActionContext:
    return AssistantActionContext(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_index=0
    )


# --- query_memory -------------------------------------------------------------


def test_query_memory_returns_relevant_fact_and_never_secret(app_ctx, fresh_subject):
    try:
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the deploy host is prod-web-01",
            confidence=0.9, status="active", sensitivity="public",
            subject=fresh_subject,
        )
        db.create_memory_claim(
            scope="global", kind="fact",
            text="the deploy ssh key passphrase is swordfish",
            confidence=1.0, status="active", sensitivity="secret",
            subject=fresh_subject,
        )
        obs = _action_query_memory(_ctx(), {"query": "deploy host"})
        assert obs.ok
        assert "prod-web-01" in obs.text
        assert "swordfish" not in obs.text  # secrets are filtered before ranking
    finally:
        _cleanup_subject(fresh_subject)


def test_query_memory_with_no_matches_is_ok_and_empty(app_ctx):
    obs = _action_query_memory(_ctx(), {"query": "no such topic zzzqqq"})
    assert obs.ok
    assert obs.text  # a human-readable "nothing found" message, not a crash


def test_query_memory_includes_seed_memories_tiered(app_ctx):
    from agents.assistant import _action_query_memory
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="up-1", path="p.up", source="upstream", answer="upstream fact", score=0.7),
                SeedMemory(uuid="ov-1", path="p.ov", source="user-overlay", answer="overlay fact", score=0.65)]
    ctx = AssistantActionContext(journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0)
    obs = _action_query_memory(ctx, {"query": "anything unrelated zzz"}, _seed_retriever=fake_seed)
    assert obs.ok is True
    # user-overlay seed appears before upstream seed
    assert obs.text.index("overlay fact") < obs.text.index("upstream fact")
    # the seed uuids are present (greppable)
    assert "ov-1" in obs.text and "up-1" in obs.text
    # source tag is shown
    assert "user-overlay" in obs.text


def test_query_memory_merges_seed_and_dynamic_without_duplicate_legend(app_ctx, fresh_subject):
    """Seed + dynamic together: seed lines first, then dynamic facts, and the
    '{memory_uuid}, ...' legend appears exactly once (the dynamic block's own
    header/legend must not be re-appended)."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="ov-1", path="p", source="user-overlay",
                           answer="overlay fact", score=0.7)]
    try:
        db.create_memory_claim(
            scope="global", kind="fact", text="the deploy host is prod-web-01",
            confidence=0.9, status="active", sensitivity="public", subject=fresh_subject)
        obs = _action_query_memory(_ctx(), {"query": "deploy host prod"}, _seed_retriever=fake_seed)
        assert obs.ok
        assert "overlay fact" in obs.text and "prod-web-01" in obs.text   # both present
        assert obs.text.index("overlay fact") < obs.text.index("prod-web-01")  # seed before dynamic
        assert obs.text.count("{memory_uuid}") == 1   # the legend is not duplicated
    finally:
        _cleanup_subject(fresh_subject)


# --- query_qa (reuses the QueryAgent pipeline) --------------------------------


def test_query_qa_reuses_query_pipeline_and_resolves_match(app_ctx, monkeypatch):
    """query_qa must run the QueryAgent exact/semantic match + resolve path, not
    reimplement Q&A. Stub the embedding-dependent internals (as the existing
    query tests do) and a resolved match, then assert the action returns it."""
    from memory import seed_memory as qkb

    sentinel = qkb.Match(
        qa_id="git_status", method="exact", score=1.0,
        matched_question="git status",
    )
    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_vector_store", lambda: None)
    monkeypatch.setattr(qkb, "_ensure_populated", lambda vs: None)
    monkeypatch.setattr(qkb, "_exact_match", lambda q: sentinel)
    monkeypatch.setattr(qkb, "_semantic_match", lambda q, vs: None)
    monkeypatch.setattr(qkb, "_resolve_match", lambda m, ctx: "Working tree clean.")

    obs = _action_query_qa(_ctx(), {"query": "git status"})
    assert obs.ok
    assert obs.text == "Working tree clean."
    assert obs.data.get("qa_id") == "git_status"


def test_query_qa_reports_no_confident_match(app_ctx, monkeypatch):
    from memory import seed_memory as qkb

    monkeypatch.setattr(qkb, "_load_kb", lambda: None)
    monkeypatch.setattr(qkb, "_vector_store", lambda: None)
    monkeypatch.setattr(qkb, "_ensure_populated", lambda vs: None)
    monkeypatch.setattr(qkb, "_exact_match", lambda q: None)
    monkeypatch.setattr(qkb, "_semantic_match", lambda q, vs: None)

    obs = _action_query_qa(_ctx(), {"query": "something obscure"})
    assert obs.ok
    assert obs.data.get("matched") is False


# --- workspace_read_command ---------------------------------------------------


def test_workspace_read_command_allows_safe_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "pwd"})
    assert obs.ok
    assert obs.data.get("exit_code") == 0


def test_workspace_read_command_blocks_forbidden_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "python -c 'print(1)'"})
    assert obs.ok is False
    assert "blocked" in obs.text.lower()


def test_workspace_read_command_blocks_mutation_command(app_ctx):
    obs = _action_workspace_read_command(_ctx(), {"command": "rm -rf foo"})
    assert obs.ok is False
    assert "blocked" in obs.text.lower()


# --- kanban_read --------------------------------------------------------------


def test_kanban_read_returns_board_state_without_appending_events(app_ctx):
    board = db.kanban_create_board("Assistant read board", "desc")
    bu = UUID(board["uuid"])
    todo = board["columns"][0]["uuid"]
    task = {
        "uuid": str(uuid4()), "columnUuid": todo,
        "title": "ship the thing", "description": "", "agentUuid": None,
    }
    db.kanban_save_board(bu, {**board, "tasks": [task]})
    task_uuid = UUID(task["uuid"])
    events_before = db.kanban_task_events(task_uuid) or []
    try:
        obs = _action_kanban_read(_ctx(), {"board_uuid": str(bu)})
        assert obs.ok
        assert "ship the thing" in obs.text
        events_after = db.kanban_task_events(task_uuid) or []
        assert len(events_after) == len(events_before), "read must not write events"
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_without_args_lists_boards(app_ctx):
    board = db.kanban_create_board("Listable board", "desc")
    bu = UUID(board["uuid"])
    try:
        obs = _action_kanban_read(_ctx(), {})
        assert obs.ok
        assert "Listable board" in obs.text
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_unknown_board_is_blocked(app_ctx):
    obs = _action_kanban_read(_ctx(), {"board_uuid": str(uuid4())})
    assert obs.ok is False


def test_kanban_read_by_task_uuid_returns_task_and_events(app_ctx):
    board = db.kanban_create_board("Task read board", "desc")
    bu = UUID(board["uuid"])
    todo = board["columns"][0]["uuid"]
    task = {"uuid": str(uuid4()), "columnUuid": todo,
            "title": "fix the bug", "description": "acceptance: tests pass"}
    db.kanban_save_board(bu, {**board, "tasks": [task]})
    tu = UUID(task["uuid"])
    db.kanban_append_event(tu, "comment", actor="human", detail="please prioritize")
    try:
        obs = _action_kanban_read(_ctx(), {"task_uuid": str(tu)})
        assert obs.ok is True
        assert "fix the bug" in obs.text and "please prioritize" in obs.text
        assert obs.data["task_uuid"] == str(tu)
    finally:
        db.kanban_delete_board(bu)


def test_kanban_read_unknown_task_is_blocked(app_ctx):
    obs = _action_kanban_read(_ctx(), {"task_uuid": str(uuid4())})
    assert obs.ok is False


# --- argument validation ------------------------------------------------------


def test_validate_rejects_unsupported_kanban_args(app_ctx):
    """kanban_read takes optional board_uuid / task_uuid; an unknown arg must be a
    traceable validation failure, not a silent fall-through to 'list all boards'."""
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    bad = AssistantStepDecision(
        reason="x", action=AssistantActionName.KANBAN_READ,
        args={"column_uuid": str(uuid4())},  # not a kanban_read arg
    )
    err = agent._validate_decision(bad)
    assert err and "column_uuid" in err
    # board_uuid, task_uuid, and empty args are all accepted.
    for ok_args in ({"board_uuid": str(uuid4())}, {"task_uuid": str(uuid4())}, {}):
        ok = AssistantStepDecision(
            reason="x", action=AssistantActionName.KANBAN_READ, args=ok_args)
        assert agent._validate_decision(ok) is None


def test_validate_rejects_unknown_arg_on_query(app_ctx):
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    bad = AssistantStepDecision(
        reason="x", action=AssistantActionName.QUERY_QA,
        args={"query": "hi", "extra": "nope"},
    )
    err = agent._validate_decision(bad)
    assert err and "extra" in err


# --- dispatch through the loop ------------------------------------------------


@pytest.fixture
def room(app_ctx):
    human = db.get_human_user()
    assert human is not None
    chatroom = db.create_chatroom(f"act-test-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    msg = db.post_chat_message(chatroom.uuid, human.uuid, "do a read")
    try:
        yield chatroom.uuid, msg.uuid
    finally:
        from db import AssistantRun
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid
        ).delete()
        db.db.session.query(db.Chatroom).filter(
            db.Chatroom.uuid == chatroom.uuid
        ).delete()
        db.db.session.commit()


def _agent() -> AssistantAgent:
    return AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)


def _decision(action: AssistantActionName, **args) -> AssistantStepDecision:
    return AssistantStepDecision(reason="step", action=action, args=args)


def _steps_for(run_id):
    return (
        db.db.session.query(AssistantStep)
        .filter(AssistantStep.run_id == run_id)
        .order_by(AssistantStep.id)
        .all()
    )


def test_loop_dispatches_read_action_then_replies(room):
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.QUERY_MEMORY, query="anything"),
        _decision(AssistantActionName.REPLY, message="All set."),
    )
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    steps = _steps_for(result["assistant_run_id"])
    # One row per step: the read step settles running->observed in place, the
    # reply is a single terminal row.
    assert [s.phase for s in steps] == ["observed", "final"]
    observed = steps[0]
    assert observed.action == "query_memory"
    assert observed.observation_preview is not None


def test_loop_records_failed_action_and_continues(room):
    """A blocked/forbidden read becomes a failed step (trace-before-action: the
    running row is committed first), and the loop continues to a terminal reply."""
    room_uuid, message_uuid = room
    agent = _agent()
    agent._decide_next_step = scripted_decisions(
        _decision(AssistantActionName.WORKSPACE_READ_COMMAND, command="rm -rf /"),
        _decision(AssistantActionName.REPLY, message="Could not do that."),
    )
    result = agent.handle(uuid4(), {"room_uuid": str(room_uuid), "message_uuid": str(message_uuid)})

    assert result["status"] == "finished"
    steps = _steps_for(result["assistant_run_id"])
    # The blocked read opens a running row (committed before the action) that
    # settles in place to failed; then a terminal reply row.
    assert [s.phase for s in steps] == ["failed", "final"]
    failed = steps[0]
    assert failed.action == "workspace_read_command"
    assert failed.error and "blocked" in failed.error.lower()
