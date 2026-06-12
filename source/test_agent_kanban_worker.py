"""Tests for the LLM kanban worker (roadmap item 2): registry wiring,
and (later tasks) the end-to-end handler with a faked structured call."""

from uuid import UUID, uuid4

import pytest

import db


def test_kanban_worker_in_agent_config():
    """kanban_worker is a runnable, structured-output, work-authority agent;
    workspace_shell is work-authority AND verified (exit codes are ground
    truth); authority/verified default to observe/False for other agents."""
    from agent_config import KANBAN_WORKER_UUID, agent_config

    entry = agent_config["kanban_worker"]
    assert entry["uuid"] == KANBAN_WORKER_UUID
    assert entry.get("requires_structured_output") is True
    assert entry.get("kanban_authority") == "work"
    assert "kanban_verified" not in entry  # absent = unverified LLM worker

    ws = agent_config["workspace_shell"]
    assert ws.get("kanban_authority") == "work"
    assert ws.get("kanban_verified") is True

    # Default: an agent with no kanban fields is observe / unverified.
    assert agent_config["dreamer"].get("kanban_authority", "observe") == "observe"


@pytest.fixture()
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


@pytest.fixture()
def worker_board(app_ctx):
    """A 4-column board with one task in 'In progress' assigned to the
    kanban_worker. Yields (board_uuid, task_uuid, {name: column_uuid})."""
    from agent_config import KANBAN_WORKER_UUID

    b = db.kanban_create_board("worker board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n}
                        for n in ("To do", "In progress", "Review", "Done")]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][1]["uuid"],
                       "title": "Summarize the release notes",
                       "description": "Acceptance: a 3-bullet summary.",
                       "agentUuid": str(KANBAN_WORKER_UUID)}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    try:
        yield bu, UUID(data["tasks"][0]["uuid"]), \
            {c["name"]: c["uuid"] for c in data["columns"]}
    finally:
        db.kanban_delete_board(bu)


def _agent(reply=None, exc=None, capture=None):
    """A KanbanWorkerAgent whose _structured_call is faked: returns `reply`,
    or raises `exc`. `capture` (a dict) records the prompt it was called with."""
    from agent_config import KANBAN_WORKER_UUID
    from agent_kanban_worker import KanbanWorkerAgent

    agent = KanbanWorkerAgent(agent_uuid=KANBAN_WORKER_UUID,
                              name="kanban_worker", send=lambda msg: None)

    def fake_structured_call(user_prompt, validator=None):
        if capture is not None:
            capture["prompt"] = user_prompt
        if exc is not None:
            raise exc
        return reply

    agent._structured_call = fake_structured_call
    return agent


def _payload(bu, tu):
    return {"task_uuid": str(tu), "board_uuid": str(bu), "source": "kanban"}


def _task_now(bu, tu):
    return next(t for t in db.kanban_load_board(bu)["tasks"]
                if t["uuid"] == str(tu))


def test_worker_done_routes_to_review_with_deliverable(worker_board):
    from agent_kanban_worker import KanbanWorkerReply

    bu, tu, cols = worker_board
    capture = {}
    agent = _agent(KanbanWorkerReply(status="done",
                                     deliverable="- a\n- b\n- c",
                                     comment="three bullets"), capture=capture)
    out = agent.handle(1, _payload(bu, tu))
    assert out["ok"] is True and out["task_ok"] is True
    assert out["detail"] == "three bullets"
    t = _task_now(bu, tu)
    assert t["columnUuid"] == cols["Review"]   # unverified → Review, not Done
    assert t["claimedBy"] is None              # lease released
    events = db.kanban_task_events(tu)
    progress = next(e for e in events if e["kind"] == "progress")
    assert progress["detail"] == "- a\n- b\n- c"
    assert any(e["kind"] == "review" for e in events)
    # the prompt carried the task AND the focus context
    assert "Summarize the release notes" in capture["prompt"]
    assert "claimed by" in capture["prompt"]


def test_worker_unclear_fails_with_readiness_reason(worker_board):
    from agent_kanban_worker import KanbanWorkerReply

    bu, tu, cols = worker_board
    agent = _agent(KanbanWorkerReply(status="unclear", deliverable="",
                                     comment="no acceptance criteria on the card"))
    out = agent.handle(1, _payload(bu, tu))
    assert out["task_ok"] is False
    t = _task_now(bu, tu)
    assert t["columnUuid"] == cols["In progress"]  # failed stays put
    failed = next(e for e in db.kanban_task_events(tu) if e["kind"] == "failed")
    assert failed["detail"].startswith("unclear acceptance criteria:")


def test_worker_failed_status(worker_board):
    from agent_kanban_worker import KanbanWorkerReply

    bu, tu, _cols = worker_board
    agent = _agent(KanbanWorkerReply(status="failed", deliverable="",
                                     comment="the linked doc 404s"))
    out = agent.handle(1, _payload(bu, tu))
    assert out["task_ok"] is False
    failed = next(e for e in db.kanban_task_events(tu) if e["kind"] == "failed")
    assert failed["detail"] == "the linked doc 404s"


def test_worker_empty_deliverable_is_failure_not_done(worker_board):
    from agent_kanban_worker import KanbanWorkerReply

    bu, tu, cols = worker_board
    agent = _agent(KanbanWorkerReply(status="done", deliverable="  ",
                                     comment=""))
    out = agent.handle(1, _payload(bu, tu))
    assert out["task_ok"] is False
    assert _task_now(bu, tu)["columnUuid"] == cols["In progress"]
    assert not any(e["kind"] == "progress" for e in db.kanban_task_events(tu))


def test_worker_llm_crash_releases_lease(worker_board):
    bu, tu, _cols = worker_board
    agent = _agent(exc=RuntimeError("all models in the group failed"))
    out = agent.handle(1, _payload(bu, tu))
    assert out["ok"] is False
    t = _task_now(bu, tu)
    assert t["claimedBy"] is None
    failed = next(e for e in db.kanban_task_events(tu) if e["kind"] == "failed")
    assert failed["detail"].startswith("crashed:")


def test_worker_rejects_non_kanban_payload():
    agent = _agent()
    out = agent.handle(1, {"room_uuid": str(uuid4())})
    assert out["ok"] is False and "kanban" in out["error"]


def test_worker_registered_in_agent_classes():
    """agent.py's dispatch map must know the kanban_worker kind (the map is
    built inside main(); import the module and check the source wiring by
    constructing the class the same way main() does)."""
    from agent_kanban_worker import KanbanWorkerAgent
    from agent import Agent

    assert issubclass(KanbanWorkerAgent, Agent)
    import inspect

    import agent as agent_module

    src = inspect.getsource(agent_module.main)
    assert '"kanban_worker": KanbanWorkerAgent' in src
