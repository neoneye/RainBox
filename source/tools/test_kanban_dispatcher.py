"""Authority matrix for the kanban dispatcher (docs/kanban-design.md:
'Models propose, code disposes' — authority lives here, not in prompts)."""

from uuid import UUID, uuid4

import pytest

import db
from agent_config import KANBAN_WORKER_UUID, WORKSPACE_SHELL_UUID
from tools.kanban_dispatcher import (
    KanbanAuthorityError,
    KanbanDispatchError,
    kanban_authority_for,
    kanban_dispatch,
    kanban_is_verified,
)


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
def board(app_ctx):
    """A 4-column board (To do / In progress / Review / Done) with one
    unassigned task in To do. Deleted afterwards."""
    b = db.kanban_create_board("dispatcher board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    assert fresh is not None
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n}
                        for n in ("To do", "In progress", "Review", "Done")]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Dispatch me", "description": "",
                       "agentUuid": None}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    assert data is not None
    try:
        yield bu, UUID(data["tasks"][0]["uuid"]), \
            {c["name"]: UUID(c["uuid"]) for c in data["columns"]}
    finally:
        db.kanban_delete_board(bu)


def test_authority_resolution_defaults_to_observe():
    assert kanban_authority_for(KANBAN_WORKER_UUID) == "work"
    assert kanban_authority_for(WORKSPACE_SHELL_UUID) == "work"
    assert kanban_authority_for(uuid4()) == "observe"  # unknown agent
    assert kanban_is_verified(WORKSPACE_SHELL_UUID) is True
    assert kanban_is_verified(KANBAN_WORKER_UUID) is False
    assert kanban_is_verified(uuid4()) is False


def test_work_authority_full_cycle(board):
    """claim → started event → complete all dispatch for a work agent; the
    unverified worker's ok=true lands in Review with a 'review' event."""
    bu, tu, cols = board
    task = kanban_dispatch(KANBAN_WORKER_UUID, {"op": "claim", "taskId": str(tu)})
    assert task["claimedBy"] == str(KANBAN_WORKER_UUID)
    kanban_dispatch(KANBAN_WORKER_UUID,
                    {"op": "append_event", "taskId": str(tu),
                     "kind": "started", "detail": ""})
    out = kanban_dispatch(KANBAN_WORKER_UUID,
                          {"op": "complete", "taskId": str(tu),
                           "ok": True, "detail": "did it"})
    assert out["columnUuid"] == str(cols["Review"])  # unverified → Review
    kinds = [e["kind"] for e in db.kanban_task_events(tu)]
    assert "review" in kinds

    # Re-claim from Review and complete with ok=False — failed stays put
    kanban_dispatch(KANBAN_WORKER_UUID, {"op": "claim", "taskId": str(tu)})
    failed = kanban_dispatch(KANBAN_WORKER_UUID,
                             {"op": "complete", "taskId": str(tu),
                              "ok": False, "detail": "retry needed"})
    assert failed["columnUuid"] == str(cols["Review"])  # column unchanged
    assert failed["claimedBy"] is None                  # lease released
    kinds2 = [e["kind"] for e in db.kanban_task_events(tu)]
    assert "failed" in kinds2


def test_verified_agent_completes_to_done(board):
    bu, tu, cols = board
    kanban_dispatch(WORKSPACE_SHELL_UUID, {"op": "claim", "taskId": str(tu)})
    out = kanban_dispatch(WORKSPACE_SHELL_UUID,
                          {"op": "complete", "taskId": str(tu),
                           "ok": True, "detail": ""})
    assert out["columnUuid"] == str(cols["Done"])  # verified → straight to Done


def test_observe_denied_claim_and_complete_and_progress(board):
    bu, tu, _cols = board
    nobody = uuid4()  # not in the registry → observe
    with pytest.raises(KanbanAuthorityError):
        kanban_dispatch(nobody, {"op": "claim", "taskId": str(tu)})
    with pytest.raises(KanbanAuthorityError):
        kanban_dispatch(nobody, {"op": "complete", "taskId": str(tu), "ok": True})
    with pytest.raises(KanbanAuthorityError):  # progress is a WORK event kind
        kanban_dispatch(nobody, {"op": "append_event", "taskId": str(tu),
                                 "kind": "progress", "detail": "x"})
    with pytest.raises(KanbanAuthorityError):  # note is also a WORK event kind
        kanban_dispatch(nobody, {"op": "append_event", "taskId": str(tu),
                                 "kind": "note", "detail": "x"})


def test_observe_allowed_comment_and_suggestion(board):
    bu, tu, _cols = board
    nobody = uuid4()
    for kind in ("comment", "suggestion"):
        out = kanban_dispatch(nobody, {"op": "append_event", "taskId": str(tu),
                                       "kind": kind, "detail": "an idea"})
        assert out is not None
    kinds = [e["kind"] for e in db.kanban_task_events(tu)]
    assert "comment" in kinds and "suggestion" in kinds


def test_move_requires_shape_even_for_work_agents(board):
    bu, tu, cols = board
    with pytest.raises(KanbanAuthorityError):
        kanban_dispatch(KANBAN_WORKER_UUID,
                        {"op": "move", "taskId": str(tu),
                         "columnId": str(cols["Done"])})


def test_malformed_ops_raise_loudly(board):
    bu, tu, _cols = board
    with pytest.raises(KanbanDispatchError):
        kanban_dispatch(KANBAN_WORKER_UUID, {"op": "frobnicate", "taskId": str(tu)})
    with pytest.raises(KanbanDispatchError):
        kanban_dispatch(KANBAN_WORKER_UUID, {"op": "claim", "taskId": "not-a-uuid"})
    with pytest.raises(KanbanDispatchError):
        kanban_dispatch(KANBAN_WORKER_UUID, {"op": "claim"})  # missing taskId


def test_runner_routes_through_dispatcher(board):
    """run_kanban_task: an observe agent is refused and the refusal lands in the ledger; a work agent gets focus=in-progress context and (unverified) completes into Review."""
    from tools.kanban_runner import run_kanban_task

    bu, tu, cols = board
    fresh = db.kanban_load_board(bu)
    for t in fresh["tasks"]:
        if t["uuid"] == str(tu):
            t["columnUuid"] = str(cols["In progress"])
    db.kanban_save_board(bu, fresh)

    nobody = uuid4()
    out = run_kanban_task(nobody, {"task_uuid": str(tu)},
                          lambda task, ctx: (True, ""))
    assert out["ok"] is False and "authority" in out["error"]
    assert any(e["kind"] == "permission-denied"
               for e in db.kanban_task_events(tu))
    task = next(t for t in db.kanban_load_board(bu)["tasks"]
                if t["uuid"] == str(tu))
    assert task["claimedBy"] is None

    seen = {}
    def work(task, ctx):
        seen["ctx"] = ctx
        return True, "fine"
    out = run_kanban_task(KANBAN_WORKER_UUID, {"task_uuid": str(tu)}, work)
    assert out["ok"] is True and out["task_ok"] is True
    assert "[started]" in seen["ctx"]  # contract: focus inlines events as [kind] bullets
    assert "claimed by" in seen["ctx"]         # and the lease line
    task = next(t for t in db.kanban_load_board(bu)["tasks"]
                if t["uuid"] == str(tu))
    assert task["columnUuid"] == str(cols["Review"])  # unverified → Review
