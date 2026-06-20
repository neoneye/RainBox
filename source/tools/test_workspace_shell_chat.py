"""End-to-end tests for tools/workspace_shell_chat.py (real subprocess + Postgres).

Uses the `chat_room` and `workspace` fixtures from conftest.py.
"""

from uuid import UUID, uuid4

import pytest

import db
from tools.workspace_command_runner import SHELL_ENV
from tools.workspace_shell_chat import WorkspaceShellChatAgent

from .conftest import WS_AGENT_UUID


def test_workspace_shell_in_agent_config():
    from agents.config import WORKSPACE_SHELL_UUID, agent_config

    entry = agent_config["workspace_shell"]
    assert entry["uuid"] == WORKSPACE_SHELL_UUID
    assert entry["next"] is None
    # No LLM, so it must NOT require a function-calling model group.
    assert "requires_function_calling" not in entry


def test_workspace_shell_is_wired_as_responder():
    from agents.config import WORKSPACE_SHELL_UUID
    from webapp.chat_api import CHAT_RESPONDER_UUIDS

    assert WORKSPACE_SHELL_UUID in CHAT_RESPONDER_UUIDS


def _agent():
    return WorkspaceShellChatAgent(
        agent_uuid=WS_AGENT_UUID, name="ws_shell", send=lambda m: None
    )


def _last(room_uuid):
    return db.list_room_messages(room_uuid)[-1]


@pytest.fixture()
def kanban_board(workspace):
    """A fresh kanban board with an app context pushed; deleted (with all its
    tasks and events) on teardown."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    b = db.kanban_create_board("WS runner board")
    try:
        yield b
    finally:
        try:
            db.kanban_delete_board(UUID(b["uuid"]))
        finally:
            db.db.session.rollback()  # release read locks before the next init_db ALTERs
            ctx.pop()


def test_handle_kanban_task_executes_description(kanban_board):
    """Milestone-3 loop end to end: an enqueued kanban payload makes the agent
    claim the task, run its description as the command, record progress, and
    complete/fail — releasing the lease either way."""
    b = kanban_board
    bu = UUID(b["uuid"])
    todo = b["columns"][0]["uuid"]
    done_col = b["columns"][-1]["uuid"]

    def add(title, desc):
        fresh = db.kanban_load_board(bu)
        t = {"uuid": str(uuid4()), "columnUuid": todo, "title": title,
             "description": desc, "agentUuid": str(WS_AGENT_UUID)}
        fresh["tasks"].append(t)
        db.kanban_save_board(bu, fresh)
        return t

    def payload(t):
        return {"task_uuid": t["uuid"], "board_uuid": b["uuid"], "source": "kanban"}

    def final(t):
        return next(x for x in db.kanban_load_board(bu)["tasks"]
                    if x["uuid"] == t["uuid"])

    agent = _agent()

    # Success: exit 0 → done column, lease released, full event trail.
    ok_t = add("run pwd", "pwd")
    out = agent.handle(uuid4(), payload(ok_t))
    assert out["ok"] is True and out["task_ok"] is True
    f = final(ok_t)
    assert f["columnUuid"] == done_col and f["claimedBy"] is None
    kinds = [e["kind"] for e in db.kanban_task_events(UUID(ok_t["uuid"]))]
    for k in ("claimed", "started", "progress", "done"):
        assert k in kinds, k
    progress = next(e for e in db.kanban_task_events(UUID(ok_t["uuid"]))
                    if e["kind"] == "progress")
    assert "$ pwd" in progress["detail"] and "[exit code: 0]" in progress["detail"]

    # Failure: non-zero exit → stays put, 'failed' with the code, lease released.
    fail_t = add("bad cd", "cd nope_xyz")
    out = agent.handle(uuid4(), payload(fail_t))
    assert out["ok"] is True and out["task_ok"] is False
    f = final(fail_t)
    assert f["columnUuid"] == todo and f["claimedBy"] is None
    failed = next(e for e in db.kanban_task_events(UUID(fail_t["uuid"]))
                  if e["kind"] == "failed")
    assert failed["detail"] == "exit code 1"

    # Blocked command and empty description fail cleanly (lease released).
    blocked_t = add("secret", "cat .env")
    assert agent.handle(uuid4(), payload(blocked_t))["task_ok"] is False
    assert "blocked" in next(e for e in db.kanban_task_events(UUID(blocked_t["uuid"]))
                             if e["kind"] == "failed")["detail"]
    empty_t = add("no command", "")
    assert agent.handle(uuid4(), payload(empty_t))["task_ok"] is False

    # Another agent's LIVE lease → clean skip; nothing executed.
    skip_t = add("busy", "pwd")
    db.kanban_claim_task(UUID(skip_t["uuid"]), uuid4())
    out = agent.handle(uuid4(), payload(skip_t))
    assert out["ok"] is True and "skipped" in out
    assert not any(e["kind"] == "started"
                   for e in db.kanban_task_events(UUID(skip_t["uuid"])))

    # A vanished task (deleted between enqueue and execution) → clean skip.
    out = agent.handle(uuid4(), {"task_uuid": str(uuid4()),
                           "board_uuid": b["uuid"], "source": "kanban"})
    assert out["ok"] is True and "skipped" in out


def test_handle_records_cron_run_outcome(chat_room, workspace):
    """A cron-fired command (payload carries cron_run_uuid) writes its outcome
    back onto the CronRun row: ok on exit 0, error on non-zero exit or a
    blocked command, always linking the journal id."""
    import sqlalchemy as sa
    room, _human = chat_room
    _, make_file, _ = workspace
    make_file("data.txt", "payload")
    s = db.db.session
    # Recording an outcome posts a ✔/✖ line to the cron room — track the
    # watermark so those event messages are torn down too.
    base_msg = s.query(sa.func.max(db.ChatMessage.id)).filter(
        db.ChatMessage.room_uuid == db.CRON_ROOM_UUID).scalar() or 0
    runs = [db.CronRun(cron_uuid=uuid4(), trigger="manual") for _ in range(3)]
    s.add_all(runs)
    s.commit()
    ok_run, fail_run, blocked_run = runs
    try:
        agent = _agent()
        j_ok, j_fail, j_blocked = uuid4(), uuid4(), uuid4()
        agent.handle(j_ok, {"room_uuid": str(room.uuid), "command_text": "cat data.txt",
                            "cron_run_uuid": str(ok_run.uuid)})
        agent.handle(j_fail, {"room_uuid": str(room.uuid), "command_text": "cd nope_xyz",
                              "cron_run_uuid": str(fail_run.uuid)})
        agent.handle(j_blocked, {"room_uuid": str(room.uuid), "command_text": "cat .env",
                                 "cron_run_uuid": str(blocked_run.uuid)})
        for r in runs:
            s.refresh(r)
        assert ok_run.status == "ok" and ok_run.error == "" and ok_run.journal_id == j_ok
        assert ok_run.finished_at is not None
        assert fail_run.status == "error" and "exit code" in fail_run.error
        assert fail_run.journal_id == j_fail
        assert blocked_run.status == "error" and "blocked" in blocked_run.error
        assert blocked_run.journal_id == j_blocked
    finally:
        s.execute(sa.delete(db.CronRun).where(
            db.CronRun.uuid.in_([r.uuid for r in runs])))
        s.execute(sa.delete(db.ChatMessage).where(
            db.ChatMessage.room_uuid == db.CRON_ROOM_UUID,
            db.ChatMessage.id > base_msg))
        s.commit()


def test_handle_debug_dry_runs_without_executing(chat_room, workspace):
    """payload.debug=True (cron 'Run debug'): the command validates, the agent
    echoes the argv it WOULD run, nothing executes, outcome records ok."""
    import sqlalchemy as sa
    room, _human = chat_room
    _, make_file, _ = workspace
    make_file("dry.txt", "secret-content")
    s = db.db.session
    base_msg = s.query(sa.func.max(db.ChatMessage.id)).filter(
        db.ChatMessage.room_uuid == db.CRON_ROOM_UUID).scalar() or 0
    run = db.CronRun(cron_uuid=uuid4(), trigger="manual", debug=True)
    s.add(run)
    s.commit()
    try:
        j_dry = uuid4()
        out = _agent().handle(j_dry, {"room_uuid": str(room.uuid),
                                      "command_text": "cat dry.txt",
                                      "cron_run_uuid": str(run.uuid), "debug": True})
        assert out.get("debug") is True
        msg = _last(room.uuid)
        assert msg["text"].startswith("[debug] would run")
        assert "cat dry.txt" in msg["text"]
        assert "secret-content" not in msg["text"]  # the command did NOT run
        s.refresh(run)
        assert run.status == "ok" and run.journal_id == j_dry
    finally:
        s.execute(sa.delete(db.CronRun).where(db.CronRun.uuid == run.uuid))
        s.execute(sa.delete(db.ChatMessage).where(
            db.ChatMessage.room_uuid == db.CRON_ROOM_UUID,
            db.ChatMessage.id > base_msg))
        s.commit()


def test_handle_without_cron_run_uuid_touches_no_runs(chat_room, workspace):
    """A chat-triggered run (no cron_run_uuid) records nothing."""
    import sqlalchemy as sa
    room, human = chat_room
    s = db.db.session
    before = s.query(sa.func.max(db.CronRun.id)).scalar() or 0
    db.post_chat_message(room.uuid, human.uuid, "pwd")
    _agent().handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert (s.query(sa.func.max(db.CronRun.id)).scalar() or 0) == before


def test_handle_runs_command_and_posts(chat_room, workspace):
    room, human = chat_room
    _, make_file, _ = workspace
    make_file("README.md", "hello ws")
    db.post_chat_message(room.uuid, human.uuid, "cat README.md")
    _agent().handle(uuid4(), {"room_uuid": str(room.uuid)})
    msg = _last(room.uuid)
    assert msg["sender_uuid"] == str(WS_AGENT_UUID)
    assert "hello ws" in msg["text"]
    assert "[exit code: 0]" in msg["text"]


def test_handle_blocks_sensitive(chat_room):
    room, human = chat_room
    db.post_chat_message(room.uuid, human.uuid, "cat .env")
    _agent().handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert _last(room.uuid)["text"].startswith("[blocked:")


def test_handle_cd_persists_between_messages(chat_room, workspace):
    room, human = chat_room
    _, _, make_dir = workspace
    make_dir("sub")
    agent = _agent()
    db.post_chat_message(room.uuid, human.uuid, "cd sub")
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    db.post_chat_message(room.uuid, human.uuid, "pwd")
    agent.handle(uuid4(), {"room_uuid": str(room.uuid)})
    assert "/sub\n[exit code: 0]" in _last(room.uuid)["text"]


def test_handle_persists_cwd_only_not_env(chat_room, workspace):
    room, human = chat_room
    _, _, make_dir = workspace
    make_dir("sub")
    db.post_chat_message(room.uuid, human.uuid, "cd sub")
    _agent().handle(uuid4(), {"room_uuid": str(room.uuid)})
    state = db.get_workspace_shell_state(room.uuid)
    assert state is not None
    assert state.cwd.endswith("/sub")
    assert state.env == dict(SHELL_ENV)


def test_handle_runs_the_triggering_message_not_latest(chat_room, workspace):
    # Two commands posted before either is processed; each enqueued item must run
    # ITS OWN message (by message_uuid), not whichever is newest.
    room, human = chat_room
    _, make_file, _ = workspace
    make_file("a.txt", "AAA")
    make_file("b.txt", "BBB")
    agent = _agent()
    a = db.post_chat_message(room.uuid, human.uuid, "cat a.txt")
    b = db.post_chat_message(room.uuid, human.uuid, "cat b.txt")
    agent.handle(uuid4(), {"room_uuid": str(room.uuid), "message_uuid": str(a.uuid)})
    assert "AAA" in db.list_room_messages(room.uuid)[-1]["text"]
    agent.handle(uuid4(), {"room_uuid": str(room.uuid), "message_uuid": str(b.uuid)})
    assert "BBB" in db.list_room_messages(room.uuid)[-1]["text"]


def test_handle_wraps_backtick_output_safely(chat_room, workspace):
    room, human = chat_room
    _, make_file, _ = workspace
    make_file("ticks.txt", "```")
    db.post_chat_message(room.uuid, human.uuid, "cat ticks.txt")
    _agent().handle(uuid4(), {"room_uuid": str(room.uuid)})
    text = _last(room.uuid)["text"]
    assert text.startswith("````")
    assert text.rstrip().endswith("````")
    assert "```" in text
    assert "[exit code: 0]" in text
