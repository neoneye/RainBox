"""Behavior tests for the kanban backend (db_kanban + webapp/kanban_api).

Hits the live local Postgres (conftest pins every pytest run to
rainbox_claude). Each test creates its own board via the fixture, which
deletes the board — and with it its columns, tasks, and task events — in
teardown, so the suite is non-destructive.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db


def _expire_lease(task_uuid):
    """Backdate a lease so takeover/cleanup paths can be tested."""
    db.db.session.execute(
        sa.update(db.KanbanTask).where(db.KanbanTask.uuid == task_uuid)
        .values(claim_expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.db.session.commit()


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        # Close any read transaction a test's final query left open: its
        # ACCESS SHARE locks would block the NEXT test's init_db ALTERs on
        # the same tables forever (the lock self-deadlock class — single
        # process, two engines).
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def board(app_ctx):
    """A fresh board (default columns); deleted with all its rows after."""
    b = db.kanban_create_board("Test board", "test description")
    try:
        yield b
    finally:
        db.kanban_delete_board(_u(b["uuid"]))


def _u(s):
    from uuid import UUID
    return UUID(s)


def _client():
    from webapp.core import app as flask_app
    return flask_app.test_client()


def _task(uuid=None, column=None, title="T", description="", agent=None):
    return {"uuid": uuid or str(uuid4()), "columnUuid": column,
            "title": title, "description": description, "agentUuid": agent}


# ---- boards: create / list / load ----

def test_create_board_has_default_columns_and_version(board):
    assert [c["name"] for c in board["columns"]] == ["To do", "In progress", "Done"]
    assert board["tasks"] == []
    assert isinstance(board["version"], str) and board["version"]
    assert any(b["uuid"] == board["uuid"] for b in db.kanban_list_boards())


def test_create_board_requires_name(app_ctx):
    with pytest.raises(db.KanbanError):
        db.kanban_create_board("   ")


def test_delete_board_removes_tasks_and_events(app_ctx):
    b = db.kanban_create_board("Doomed")
    todo = b["columns"][0]["uuid"]
    payload = {**b, "tasks": [_task(column=todo)]}
    db.kanban_save_board(_u(b["uuid"]), payload)
    task_uuid = _u(payload["tasks"][0]["uuid"])
    db.kanban_append_event(task_uuid, "note", detail="x")
    assert db.kanban_delete_board(_u(b["uuid"])) is True
    assert db.kanban_load_board(_u(b["uuid"])) is None
    assert db.kanban_task_events(task_uuid) is None  # task gone with the board
    assert db.kanban_delete_board(_u(b["uuid"])) is False  # already gone


# ---- bulk save: round trip, ordering, validation, guards ----

def test_save_round_trips_tasks_and_order(board):
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    t1, t2 = _task(column=todo, title="first"), _task(column=todo, title="second")
    db.kanban_save_board(bu, {**board, "tasks": [t1, t2]})
    out = db.kanban_load_board(bu)
    assert [t["title"] for t in out["tasks"]] == ["first", "second"]
    # Reorder: list order is the persisted order.
    db.kanban_save_board(bu, {**out, "tasks": [out["tasks"][1], out["tasks"][0]]})
    out2 = db.kanban_load_board(bu)
    assert [t["title"] for t in out2["tasks"]] == ["second", "first"]
    # Version rotates on change, stays stable when nothing changed.
    assert out2["version"] != out["version"]
    db.kanban_save_board(bu, out2)
    assert db.kanban_load_board(bu)["version"] == out2["version"]


def test_save_records_created_and_moved_events(board):
    bu = _u(board["uuid"])
    todo, doing = board["columns"][0]["uuid"], board["columns"][1]["uuid"]
    t = _task(column=todo, title="tracked")
    db.kanban_save_board(bu, {**board, "tasks": [t]})
    out = db.kanban_load_board(bu)
    out["tasks"][0]["columnUuid"] = doing
    db.kanban_save_board(bu, out)
    kinds = [e["kind"] for e in db.kanban_task_events(_u(t["uuid"]))]
    assert "created" in kinds and "moved" in kinds
    moved = next(e for e in db.kanban_task_events(_u(t["uuid"])) if e["kind"] == "moved")
    assert "To do" in moved["detail"] and "In progress" in moved["detail"]


@pytest.mark.parametrize("mutate, msg", [
    (lambda p: p["tasks"].append(_task(column=str(uuid4()))), "missing column"),
    (lambda p: p["tasks"].extend([_task(uuid="x", column=p["columns"][0]["uuid"])]), "not a uuid"),
    (lambda p: p.update(columns=[]), "non-empty"),
    (lambda p: p["tasks"].append(
        _task(column=p["columns"][0]["uuid"], agent="not-a-uuid")), "agentUuid"),
])
def test_save_validation_rejects_bad_payloads(board, mutate, msg):
    bu = _u(board["uuid"])
    payload = {**board, "tasks": list(board["tasks"]),
               "columns": [dict(c) for c in board["columns"]]}
    mutate(payload)
    with pytest.raises(db.KanbanError) as exc:
        db.kanban_save_board(bu, payload)
    assert msg in str(exc.value)
    # Nothing persisted.
    assert db.kanban_load_board(bu)["version"] == board["version"]


def test_save_stale_version_conflicts(board):
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    # Another writer changes the board after our hydrate.
    db.kanban_save_board(bu, {**board, "tasks": [_task(column=todo, title="theirs")]})
    # Our save based on the stale hydrate is refused; their write survives.
    with pytest.raises(db.KanbanConflict):
        db.kanban_save_board(bu, {**board, "tasks": [_task(column=todo, title="mine")]},
                             base_version=board["version"])
    titles = [t["title"] for t in db.kanban_load_board(bu)["tasks"]]
    assert titles == ["theirs"]


def test_save_undeclared_delete_refused(board):
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    db.kanban_save_board(bu, {**board, "tasks": [_task(column=todo)]})
    out = db.kanban_load_board(bu)
    with pytest.raises(db.KanbanError) as exc:
        db.kanban_save_board(bu, {**out, "tasks": []},
                             base_version=out["version"], expected_deletes=0)
    assert "delete" in str(exc.value)
    assert len(db.kanban_load_board(bu)["tasks"]) == 1  # untouched
    db.kanban_save_board(bu, {**out, "tasks": []},
                         base_version=out["version"], expected_deletes=1)
    assert db.kanban_load_board(bu)["tasks"] == []


def test_put_endpoint_guards(board):
    client = _client()
    url = f"/kanban/api/board/{board['uuid']}"
    body = {**board, "deletes": 0}
    # Missing version → 400; stale version → 409 with the current token.
    assert client.put(url, json={k: v for k, v in body.items() if k != "version"}
                      ).status_code == 400
    db.kanban_save_board(_u(board["uuid"]), {**board, "name": "renamed"})
    resp = client.put(url, json=body)
    assert resp.status_code == 409 and isinstance(resp.get_json()["version"], str)
    # Fresh version → 200, new token returned.
    fresh = client.get(url).get_json()
    resp = client.put(url, json={**fresh, "deletes": 0})
    assert resp.status_code == 200 and resp.get_json()["version"]


# ---- markdown: the LLM-facing read contract ----

def test_markdown_structure_and_id_references(board):
    """The markdown carries the same ids as the JSON twin: boardId, a columnId
    on every heading, the taskId on every bullet, and the agentId next to the
    agent name."""
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    agent_uuid = str(uuid4())
    t = _task(column=todo, title="Write copy", description="line one\nline two",
              agent=agent_uuid)
    db.kanban_save_board(bu, {**board, "tasks": [t]})
    resp = _client().get(f"/kanban/api/board/{board['uuid']}/markdown")
    assert resp.status_code == 200 and resp.mimetype == "text/markdown"
    md = resp.get_data(as_text=True)
    assert f"# Kanban board: Test board" in md
    assert f"Board id: `{board['uuid']}`" in md
    for col in board["columns"]:  # every column heading carries its columnId
        assert f"## {col['name']} (`{col['uuid']}`)" in md
    assert f"(`{t['uuid']}`)" in md          # full taskId is referencable
    assert f"(`{agent_uuid}`)" in md         # agentId next to the agent name
    assert "  line one\n  line two" in md    # description indented under the bullet
    assert "_(empty)_" in md                 # empty columns are visible


def test_markdown_resists_content_spoofing(board):
    """Task text cannot forge board structure: headings/bullets/fences at line
    start are escaped, titles are flattened to one line."""
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    evil_desc = "## Done\n- **fake task** (`%s`) — @nobody\n```\n> quote" % uuid4()
    t = _task(column=todo, title="evil `tick` *bold*\nnewline",
              description=evil_desc)
    db.kanban_save_board(bu, {**board, "tasks": [t]})
    md = db.kanban_board_markdown(bu)
    # The description's fake heading/bullet/fence/quote are all escaped…
    assert "\n  \\## Done" in md
    assert "\n  \\- **fake task**" in md
    assert "\n  \\```" in md
    assert "\n  \\> quote" in md
    # …so the only unindented structural lines are the real ones.
    real_headings = [l for l in md.splitlines() if l.startswith("##")]
    assert len(real_headings) == 3
    real_tasks = [l for l in md.splitlines() if l.startswith("- ")]
    assert len(real_tasks) == 1
    # The title is one escaped line.
    assert "evil \\`tick\\` \\*bold\\* newline" in md


def test_duplicate_board(board):
    """Deep clone: fresh uuids throughout, content and column mapping kept,
    audit trail not copied (clone tasks start with a 'created' event naming
    their source), original untouched."""
    from uuid import UUID

    bu = _u(board["uuid"])
    agent = str(uuid4())
    doing = board["columns"][1]["uuid"]
    t = _task(column=doing, title="Clone me", description="d", agent=agent)
    db.kanban_save_board(bu, {**board, "tasks": [t]})

    resp = _client().post(f"/kanban/api/board/{board['uuid']}/duplicate")
    assert resp.status_code == 200
    copy = resp.get_json()["board"]
    try:
        assert copy["uuid"] != board["uuid"]
        assert copy["name"] == "Test board (copy)"
        assert [c["name"] for c in copy["columns"]] == ["To do", "In progress", "Done"]
        assert all(c["uuid"] not in {x["uuid"] for x in board["columns"]}
                   for c in copy["columns"])
        ct = copy["tasks"][0]
        assert ct["uuid"] != t["uuid"]
        assert ct["title"] == "Clone me" and ct["agentUuid"] == agent
        assert ct["columnUuid"] == copy["columns"][1]["uuid"]  # mapping preserved
        events = db.kanban_task_events(UUID(ct["uuid"]))
        assert len(events) == 1 and events[0]["kind"] == "created"
        assert t["uuid"] in events[0]["detail"]  # names its source task
        # Original board untouched.
        assert db.kanban_load_board(bu)["version"] == db.kanban_board_version(bu)
        assert len(db.kanban_load_board(bu)["tasks"]) == 1
    finally:
        db.kanban_delete_board(_u(copy["uuid"]))
    assert _client().post(f"/kanban/api/board/{uuid4()}/duplicate").status_code == 404


def test_llm_json_serialization(board):
    """The JSON twin of the markdown view: columns→tasks nested, agent names
    resolved, no version token."""
    from agents.config import agent_config

    role, entry = next(iter(agent_config.items()))
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    t = _task(column=todo, title="JSON me", description="d",
              agent=str(entry["uuid"]))
    db.kanban_save_board(bu, {**board, "tasks": [t]})

    resp = _client().get(f"/kanban/api/board/{board['uuid']}/json")
    assert resp.status_code == 200 and resp.mimetype == "application/json"
    data = resp.get_json()
    assert data["boardId"] == board["uuid"]
    assert "version" not in data  # a read snapshot, not a save payload
    assert [c["name"] for c in data["columns"]] == ["To do", "In progress", "Done"]
    assert data["columns"][0]["columnId"] == board["columns"][0]["uuid"]
    task = data["columns"][0]["tasks"][0]
    assert task["taskId"] == t["uuid"] and task["title"] == "JSON me"
    assert task["agentId"] == str(entry["uuid"])
    assert task["agentName"] == role  # resolved for LLM readability
    assert data["columns"][1]["tasks"] == []
    assert _client().get(f"/kanban/api/board/{uuid4()}/json").status_code == 404


# ---- agent operations: the uuid-addressed write primitives ----

def _one_task(board):
    bu = _u(board["uuid"])
    t = _task(column=board["columns"][0]["uuid"], title="op target")
    db.kanban_save_board(bu, {**board, "tasks": [t]})
    return _u(t["uuid"])


def test_claim_task(board):
    tu = _one_task(board)
    a1, a2 = uuid4(), uuid4()
    out = db.kanban_claim_task(tu, a1)
    assert out["claimedBy"] == str(a1) and out["claimExpiresAt"] is not None
    assert out["agentUuid"] is None  # the lease, not the assignee: humans assign, agents claim
    assert db.kanban_claim_task(tu, a1)["claimedBy"] == str(a1)  # renewal, idempotent
    with pytest.raises(db.KanbanConflict):
        db.kanban_claim_task(tu, a2)  # live lease held by a1
    kinds = [e["kind"] for e in db.kanban_task_events(tu)]
    assert kinds.count("claimed") == 1  # the renewal added no event
    assert db.kanban_claim_task(uuid4(), a1) is None  # unknown task


def test_expired_lease_takeover(board):
    """A crashed agent can't own a task forever: once the lease expires,
    another agent's claim succeeds and the takeover is recorded."""
    tu = _one_task(board)
    a1, a2 = uuid4(), uuid4()
    db.kanban_claim_task(tu, a1)
    _expire_lease(tu)
    out = db.kanban_claim_task(tu, a2)
    assert out["claimedBy"] == str(a2)
    claimed = [e for e in db.kanban_task_events(tu) if e["kind"] == "claimed"]
    assert any("takeover" in e["detail"] for e in claimed)


def test_release_and_renew(board):
    tu = _one_task(board)
    a1, a2 = uuid4(), uuid4()
    db.kanban_claim_task(tu, a1)
    # A live lease: only the holder may release or renew.
    with pytest.raises(db.KanbanConflict):
        db.kanban_release_task(tu, a2)
    with pytest.raises(db.KanbanConflict):
        db.kanban_renew_claim(tu, a2)
    assert db.kanban_renew_claim(tu, a1)["claimExpiresAt"] is not None
    out = db.kanban_release_task(tu, a1)
    assert out["claimedBy"] is None
    assert any(e["kind"] == "released" for e in db.kanban_task_events(tu))
    # Releasing an unclaimed task is a no-op; renewing one is an error.
    assert db.kanban_release_task(tu, a1)["claimedBy"] is None
    with pytest.raises(db.KanbanError):
        db.kanban_renew_claim(tu, a1)
    # Anyone may clear an EXPIRED lease.
    db.kanban_claim_task(tu, a1)
    _expire_lease(tu)
    assert db.kanban_release_task(tu, a2)["claimedBy"] is None


def test_claim_endpoint_conflict_is_409(board):
    tu = _one_task(board)
    client = _client()
    assert client.post(f"/kanban/api/tasks/{tu}/claim",
                       json={"agentUuid": str(uuid4())}).status_code == 200
    resp = client.post(f"/kanban/api/tasks/{tu}/claim",
                       json={"agentUuid": str(uuid4())})
    assert resp.status_code == 409 and "claimed" in resp.get_json()["error"]


def test_claim_next(board):
    """The DB picks one eligible task: own tasks before unassigned, earlier
    columns first, never the board's last (done) column; idempotent until the
    task is completed/moved out of eligibility."""
    bu = _u(board["uuid"])
    todo, doing, done = (c["uuid"] for c in board["columns"])
    me, other = uuid4(), uuid4()
    tasks = [
        _task(column=done, title="done-mine", agent=str(me)),   # ineligible: last column
        _task(column=todo, title="unassigned-1"),
        _task(column=doing, title="mine-doing", agent=str(me)),
        _task(column=todo, title="theirs", agent=str(other)),
        _task(column=todo, title="mine-todo", agent=str(me)),
    ]
    db.kanban_save_board(bu, {**board, "tasks": tasks})

    first = db.kanban_claim_next(me, bu)
    assert first["title"] == "mine-todo"  # own task, earliest column
    assert first["claimedBy"] == str(me)  # the claim is a LEASE
    # Idempotent: my live lease is the top pick; renewed, no duplicate event.
    assert db.kanban_claim_next(me, bu)["title"] == "mine-todo"
    assert sum(1 for e in db.kanban_task_events(_u(first["uuid"]))
               if e["kind"] == "claimed") == 1
    # Complete it (releases the lease) → next pick is my other task, then the
    # unassigned one.
    done = db.kanban_complete_task(_u(first["uuid"]), True, actor=str(me))
    assert done["claimedBy"] is None  # completion releases the lease
    assert db.kanban_claim_next(me, bu)["title"] == "mine-doing"
    db.kanban_complete_task(_u(_id_of(board, bu, "mine-doing")), True, actor=str(me))
    third = db.kanban_claim_next(me, bu)
    assert third["title"] == "unassigned-1"
    assert third["claimedBy"] == str(me) and third["agentUuid"] is None  # leased, NOT assigned
    claimed = [e for e in db.kanban_task_events(_u(third["uuid"]))
               if e["kind"] == "claimed"]
    assert len(claimed) == 1 and claimed[0]["detail"] == "claim-next"
    # "theirs" is never eligible for me → after completing mine, nothing left.
    db.kanban_complete_task(_u(third["uuid"]), True, actor=str(me))
    assert db.kanban_claim_next(me, bu) is None
    # include_unassigned=False restricts to my own assigned tasks only.
    assert db.kanban_claim_next(other, bu, include_unassigned=False)["title"] == "theirs"
    # A task under another agent's LIVE lease is not eligible; expired is.
    fourth_agent = uuid4()
    assert db.kanban_claim_next(fourth_agent, bu) is None  # "theirs" leased by `other`
    _expire_lease(_u(_id_of(board, bu, "theirs")))
    assert db.kanban_claim_next(fourth_agent, bu) is None  # still assigned to `other`, not me
    stolen = db.kanban_claim_next(other, bu)  # `other` resumes after its own lease expired
    assert stolen["title"] == "theirs"


def test_claim_next_orders_across_boards(app_ctx):
    """Without a board filter, earlier boards (lower position) win. Scoped via
    include_unassigned=False + a fresh agent uuid, so the global query can
    only ever see this test's rows (the shared DB may hold other boards)."""
    me = uuid4()
    b1 = db.kanban_create_board("Order A")
    b2 = db.kanban_create_board("Order B")
    try:
        # Fill the LATER board first, so insertion order can't mask
        # board-position ordering.
        for b, title in ((b2, "in-b2"), (b1, "in-b1")):
            db.kanban_save_board(_u(b["uuid"]), {**b, "tasks": [
                _task(column=b["columns"][0]["uuid"], title=title, agent=str(me))]})
        first = db.kanban_claim_next(me, include_unassigned=False)
        assert first["title"] == "in-b1"  # earlier board wins
        db.kanban_complete_task(_u(first["uuid"]), True, actor=str(me))
        second = db.kanban_claim_next(me, include_unassigned=False)
        assert second["title"] == "in-b2"
        db.kanban_complete_task(_u(second["uuid"]), True, actor=str(me))
        assert db.kanban_claim_next(me, include_unassigned=False) is None
    finally:
        db.kanban_delete_board(_u(b1["uuid"]))
        db.kanban_delete_board(_u(b2["uuid"]))


def _id_of(board, bu, title):
    return next(t["uuid"] for t in db.kanban_load_board(bu)["tasks"]
                if t["title"] == title)


def test_claim_next_endpoint(board):
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    db.kanban_save_board(bu, {**board, "tasks": [_task(column=todo, title="pickme")]})
    client = _client()
    me = str(uuid4())
    resp = client.post("/kanban/api/claim-next",
                       json={"agentUuid": me, "boardId": board["uuid"]})
    assert resp.status_code == 200
    task = resp.get_json()["task"]
    assert task["title"] == "pickme"
    assert task["claimedBy"] == me and task["agentUuid"] is None  # leased, not assigned
    # No eligible task is a normal outcome (task: null), not an error.
    other_me = str(uuid4())
    resp = client.post("/kanban/api/claim-next",
                       json={"agentUuid": other_me, "boardId": board["uuid"],
                             "includeUnassigned": False})
    assert resp.status_code == 200 and resp.get_json()["task"] is None
    # Bad inputs are loud.
    assert client.post("/kanban/api/claim-next", json={}).status_code == 400
    assert client.post("/kanban/api/claim-next",
                       json={"agentUuid": me, "boardId": "nope"}).status_code == 400


def test_move_task(board):
    tu = _one_task(board)
    doing = _u(board["columns"][1]["uuid"])
    out = db.kanban_move_task(tu, doing, actor="human", note="starting")
    assert out["columnUuid"] == str(doing)
    moved = next(e for e in db.kanban_task_events(tu) if e["kind"] == "moved")
    assert "To do → In progress" in moved["detail"] and "starting" in moved["detail"]
    # A column from another board is rejected.
    other = db.kanban_create_board("Other")
    try:
        with pytest.raises(db.KanbanError):
            db.kanban_move_task(tu, _u(other["columns"][0]["uuid"]))
    finally:
        db.kanban_delete_board(_u(other["uuid"]))


def test_move_task_to_board(board):
    tu = _one_task(board)
    other = db.kanban_create_board("Other board")
    try:
        out = db.kanban_move_task_to_board(
            tu, _u(other["uuid"]), _u(other["columns"][1]["uuid"]),
            actor="human", note="handoff")
        assert out["boardUuid"] == other["uuid"]
        assert out["columnUuid"] == other["columns"][1]["uuid"]
        # The task keeps its uuid + audit trail; the move names both boards.
        events = db.kanban_task_events(tu)
        assert any(e["kind"] == "created" for e in events)
        moved = next(e for e in events if e["kind"] == "moved")
        assert "board Test board → Other board (In progress)" in moved["detail"]
        assert "handoff" in moved["detail"]
        assert db.kanban_load_board(_u(board["uuid"]))["tasks"] == []
        # Omitted column → the task's column is PRESERVED by name (moving
        # back here: "In progress" stays in progress, not reset to "To do").
        out = db.kanban_move_task_to_board(tu, _u(board["uuid"]))
        assert out["boardUuid"] == board["uuid"]
        assert out["columnUuid"] == board["columns"][1]["uuid"]
        # Loud failures: unknown board; a column not on the target board.
        with pytest.raises(db.KanbanError):
            db.kanban_move_task_to_board(tu, _u(str(uuid4())))
        with pytest.raises(db.KanbanError):
            db.kanban_move_task_to_board(tu, _u(other["uuid"]),
                                         _u(board["columns"][0]["uuid"]))
        # Same board + explicit column = a plain column move.
        out = db.kanban_move_task_to_board(tu, _u(board["uuid"]),
                                           _u(board["columns"][1]["uuid"]))
        assert out["boardUuid"] == board["uuid"]
        assert out["columnUuid"] == board["columns"][1]["uuid"]
        # No name match on the target → the same POSITION carries over.
        weird = db.kanban_create_board("Weird columns")
        try:
            fresh = db.kanban_load_board(_u(weird["uuid"]))
            for c, name in zip(fresh["columns"], ("Backlog", "Doing", "Shipped")):
                c["name"] = name
            db.kanban_save_board(_u(weird["uuid"]), fresh)
            out = db.kanban_move_task_to_board(tu, _u(weird["uuid"]))
            assert out["columnUuid"] == fresh["columns"][1]["uuid"]
        finally:
            db.kanban_delete_board(_u(weird["uuid"]))
    finally:
        db.kanban_delete_board(_u(other["uuid"]))


def test_move_task_to_board_endpoint(board):
    tu = _one_task(board)
    other = db.kanban_create_board("Other board")
    client = _client()
    try:
        resp = client.post(f"/kanban/api/tasks/{tu}/move-to-board",
                           json={"boardId": other["uuid"],
                                 "columnId": other["columns"][0]["uuid"],
                                 "actor": "human"})
        assert resp.status_code == 200
        task = resp.get_json()["task"]
        assert task["boardUuid"] == other["uuid"]
        assert task["columnUuid"] == other["columns"][0]["uuid"]
        # Bad inputs are loud.
        assert client.post(f"/kanban/api/tasks/{tu}/move-to-board",
                           json={}).status_code == 400
        assert client.post(f"/kanban/api/tasks/{tu}/move-to-board",
                           json={"boardId": str(uuid4())}).status_code == 400
        assert client.post(f"/kanban/api/tasks/{uuid4()}/move-to-board",
                           json={"boardId": other["uuid"]}).status_code == 404
    finally:
        db.kanban_delete_board(_u(other["uuid"]))


def test_complete_task_ok_and_failed(board):
    tu = _one_task(board)
    done_col = board["columns"][-1]["uuid"]
    out = db.kanban_complete_task(tu, True, actor="agent-x", detail="all good")
    assert out["columnUuid"] == done_col
    assert any(e["kind"] == "done" for e in db.kanban_task_events(tu))
    # A failure records the reason and leaves the task where it is.
    tu2 = _u(str(uuid4()))
    fresh = db.kanban_load_board(_u(board["uuid"]))
    fresh["tasks"].append(_task(uuid=str(tu2), column=board["columns"][0]["uuid"]))
    db.kanban_save_board(_u(board["uuid"]), fresh)
    out = db.kanban_complete_task(tu2, False, detail="exit code 2")
    assert out["columnUuid"] == board["columns"][0]["uuid"]  # unmoved
    failed = next(e for e in db.kanban_task_events(tu2) if e["kind"] == "failed")
    assert failed["detail"] == "exit code 2"


def test_enqueue_task(board):
    """Run-now wiring (milestone 3): the endpoint enqueues the assigned agent
    with the kanban payload contract and records an 'enqueued' event; loud
    failures for unassigned / unknown-agent / missing / live-leased tasks."""
    import json

    from agents.config import agent_config

    s = db.db.session
    base_inbox = s.query(sa.func.max(db.Inbox.id)).scalar() or 0
    ws_uuid = str(agent_config["workspace_shell"]["uuid"])
    bu = _u(board["uuid"])
    todo = board["columns"][0]["uuid"]
    t = _task(column=todo, title="Run me", description="pwd", agent=ws_uuid)
    unassigned = _task(column=todo, title="nobody")
    rogue = _task(column=todo, title="rogue", agent=str(uuid4()))  # not in agent_config
    db.kanban_save_board(bu, {**board, "tasks": [t, unassigned, rogue]})
    client = _client()
    try:
        resp = client.post(f"/kanban/api/tasks/{t['uuid']}/enqueue")
        assert resp.status_code == 200
        payloads = [json.loads(r.payload) for r in
                    s.query(db.Inbox).filter(db.Inbox.id > base_inbox).all()]
        assert {"task_uuid": t["uuid"], "board_uuid": board["uuid"],
                "source": "kanban"} in payloads
        assert any(e["kind"] == "enqueued"
                   for e in db.kanban_task_events(_u(t["uuid"])))
        # Loud preconditions.
        assert client.post(f"/kanban/api/tasks/{unassigned['uuid']}/enqueue"
                           ).status_code == 400
        assert client.post(f"/kanban/api/tasks/{rogue['uuid']}/enqueue"
                           ).status_code == 400
        assert client.post(f"/kanban/api/tasks/{uuid4()}/enqueue").status_code == 404
        db.kanban_claim_task(_u(t["uuid"]), uuid4())  # someone is working it
        assert client.post(f"/kanban/api/tasks/{t['uuid']}/enqueue").status_code == 409
    finally:
        s.execute(sa.delete(db.Inbox).where(db.Inbox.id > base_inbox))
        s.commit()


def test_events_endpoint_round_trip(board):
    tu = _one_task(board)
    client = _client()
    resp = client.post(f"/kanban/api/tasks/{tu}/events",
                       json={"kind": "progress", "actor": "agent-y", "detail": "50%"})
    assert resp.status_code == 200
    events = client.get(f"/kanban/api/tasks/{tu}/events").get_json()["events"]
    assert any(e["kind"] == "progress" and e["detail"] == "50%" for e in events)
    # Empty kind is refused.
    assert client.post(f"/kanban/api/tasks/{tu}/events",
                       json={"kind": "  "}).status_code == 400
    # A stale/hallucinated taskId is a loud 404, not a plausible empty list.
    assert client.get(f"/kanban/api/tasks/{uuid4()}/events").status_code == 404


# ---- complete_task review= routing ----

def _make_review_board(columns):
    """A board with the given column names; returns (board_uuid, {name: column_uuid}, task_uuid).
    Caller must kanban_delete_board in teardown."""
    b = db.kanban_create_board("review routing board")
    bu = _u(b["uuid"])
    fresh = db.kanban_load_board(bu)
    col_list = [{"uuid": str(uuid4()), "name": n} for n in columns]
    fresh["columns"] = col_list
    fresh["tasks"] = [{"uuid": str(uuid4()), "columnUuid": col_list[0]["uuid"],
                       "title": "T1", "description": "", "agentUuid": None}]
    db.kanban_save_board(bu, fresh)
    loaded = db.kanban_load_board(bu)
    cols = {c["name"]: c["uuid"] for c in loaded["columns"]}
    task_uuid = _u(loaded["tasks"][0]["uuid"])
    return bu, cols, task_uuid


def test_complete_review_routes_to_review_column(app_ctx):
    """ok=True review=True moves to the 'Review' column (case-insensitive,
    first match in board order), records a 'review' event (NOT 'done'), and
    releases the lease."""
    bu, cols, tu = _make_review_board(["To do", "In progress", "review", "Done"])
    try:
        agent = uuid4()
        db.kanban_claim_task(tu, agent)
        out = db.kanban_complete_task(tu, True, actor=str(agent),
                                      detail="deliverable ready", review=True)
        assert out["columnUuid"] == cols["review"]
        assert out["claimedBy"] is None
        kinds = [e["kind"] for e in db.kanban_task_events(tu)]
        assert "review" in kinds and "done" not in kinds
    finally:
        db.kanban_delete_board(bu)


def test_complete_review_falls_back_to_done_without_review_column(app_ctx):
    """No Review-named column → exactly today's behavior: last column + 'done'."""
    bu, cols, tu = _make_review_board(["To do", "Doing", "Done"])
    try:
        db.kanban_claim_task(tu, uuid4())
        out = db.kanban_complete_task(tu, True, review=True)
        assert out["columnUuid"] == cols["Done"]
        kinds = [e["kind"] for e in db.kanban_task_events(tu)]
        assert "done" in kinds and "review" not in kinds
    finally:
        db.kanban_delete_board(bu)


def test_complete_review_false_and_failed_paths_unchanged(app_ctx):
    """review=False keeps the old contract; ok=False ignores review entirely."""
    bu, cols, tu = _make_review_board(["To do", "review", "Done"])
    try:
        out = db.kanban_complete_task(tu, False, detail="nope", review=True)
        assert out["columnUuid"] == cols["To do"]  # failed stays put
        out = db.kanban_complete_task(tu, True, review=False)
        assert out["columnUuid"] == cols["Done"]
    finally:
        db.kanban_delete_board(bu)


# ---- focus=in-progress serialization ----

def _make_focus_board():
    """4 columns; a described task in To do, a claimed task with events in
    In progress, two tasks in Done. Returns (board_uuid, in_progress_task_uuid)."""
    b = db.kanban_create_board("focus board")
    bu = _u(b["uuid"])
    fresh = db.kanban_load_board(bu)
    cols = [{"uuid": str(uuid4()), "name": n}
            for n in ("To do", "In progress", "Review", "Done")]
    fresh["columns"] = cols
    wip = str(uuid4())
    fresh["tasks"] = [
        {"uuid": str(uuid4()), "columnUuid": cols[0]["uuid"], "title": "Todo A",
         "description": "long todo description", "agentUuid": None},
        {"uuid": wip, "columnUuid": cols[1]["uuid"], "title": "Wip B",
         "description": "wip description", "agentUuid": None},
        {"uuid": str(uuid4()), "columnUuid": cols[3]["uuid"], "title": "Done C",
         "description": "done description", "agentUuid": None},
        {"uuid": str(uuid4()), "columnUuid": cols[3]["uuid"], "title": "Done D",
         "description": "", "agentUuid": None},
    ]
    db.kanban_save_board(bu, fresh)
    return bu, _u(wip)


def test_focus_in_progress_markdown(app_ctx):
    """First column: title+id only. Last: count + titles. Middle: full
    descriptions, lease line, recent events (escaped — events cannot forge
    structure)."""
    bu, wip = _make_focus_board()
    try:
        agent = uuid4()
        db.kanban_claim_task(wip, agent)
        db.kanban_append_event(wip, "progress", actor=str(agent),
                               detail="## sneaky heading\nline two")
        md = db.kanban_board_markdown(bu, focus="in-progress")
        assert "long todo description" not in md       # first column: brief
        assert "Todo A" in md and str(wip) in md
        assert "wip description" in md                  # middle: full
        assert f"claimed by `{agent}`" in md            # lease state
        assert "sneaky heading" in md                   # event present...
        assert "\n## sneaky heading" not in md          # ...but cannot be a column
        assert "done description" not in md             # last column: titles only
        assert "2 task(s)" in md and "Done C" in md
        # default (no focus) is unchanged
        assert "long todo description" in db.kanban_board_markdown(bu)
    finally:
        db.kanban_delete_board(bu)


def test_focus_in_progress_json_twin(app_ctx):
    bu, wip = _make_focus_board()
    try:
        db.kanban_claim_task(wip, uuid4())
        db.kanban_append_event(wip, "progress", detail="step done")
        data = db.kanban_board_llm_json(bu, focus="in-progress")
        first, middle, last = data["columns"][0], data["columns"][1], data["columns"][3]
        assert set(first["tasks"][0].keys()) == {"taskId", "title"}
        t = middle["tasks"][0]
        assert set(t.keys()) == {"taskId", "title", "description", "agentId", "agentName", "claimedBy", "claimExpiresAt", "events"}
        assert t["description"] == "wip description"
        assert t["claimedBy"] is not None
        assert any(e["kind"] == "progress" for e in t["events"])
        assert last["taskCount"] == 2
        assert all(set(x.keys()) == {"title"} for x in last["tasks"])
    finally:
        db.kanban_delete_board(bu)


def test_focus_small_board_renders_full(app_ctx):
    """<3 columns: focus renders exactly like the default document."""
    b = db.kanban_create_board("small focus board")
    bu = _u(b["uuid"])
    try:
        fresh = db.kanban_load_board(bu)
        fresh["columns"] = [{"uuid": str(uuid4()), "name": n} for n in ("A", "B")]
        fresh["tasks"] = [{"uuid": str(uuid4()),
                           "columnUuid": fresh["columns"][0]["uuid"],
                           "title": "t", "description": "desc here",
                           "agentUuid": None}]
        db.kanban_save_board(bu, fresh)
        assert db.kanban_board_markdown(bu, focus="in-progress") == \
            db.kanban_board_markdown(bu)
    finally:
        db.kanban_delete_board(bu)


def test_focus_unknown_value_raises(app_ctx):
    # _check_focus raises before any DB lookup, so a nonexistent uuid is fine.
    with pytest.raises(ValueError):
        db.kanban_board_markdown(uuid4(), focus="everything")


def test_focus_query_param_on_serialization_endpoints(app_ctx):
    from webapp import app

    bu, _wip = _make_focus_board()
    try:
        client = app.test_client()
        base = f"/kanban/api/board/{bu}"
        ok = client.get(f"{base}/markdown?focus=in-progress")
        assert ok.status_code == 200
        assert "2 task(s)" in ok.get_data(as_text=True)
        assert client.get(f"{base}/json?focus=in-progress").status_code == 200
        bad_md = client.get(f"{base}/markdown?focus=bogus")
        assert bad_md.status_code == 400
        assert "in-progress" in bad_md.get_json()["error"]
        bad_json = client.get(f"{base}/json?focus=bogus")
        assert bad_json.status_code == 400
        assert "in-progress" in bad_json.get_json()["error"]
        assert client.get(f"{base}/markdown").status_code == 200  # default intact
    finally:
        db.kanban_delete_board(bu)


# ---- folder tree API ----

def test_tree_get_and_put_roundtrip(board):
    client = _client()
    folder = db.kanban_create_folder("api folder")
    try:
        tree = client.get("/kanban/api/tree").get_json()
        assert any(f["uuid"] == folder["uuid"] for f in tree["folders"])
        # File the board under the folder via PUT.
        body = {"folders": tree["folders"],
                "boards": [{**b, "folderId": folder["uuid"]} if b["uuid"] == board["uuid"]
                           else b for b in tree["boards"]],
                "version": tree["version"]}
        resp = client.put("/kanban/api/tree", json=body)
        assert resp.status_code == 200 and isinstance(resp.get_json()["version"], str)
        out = client.get("/kanban/api/tree").get_json()
        assert next(b for b in out["boards"] if b["uuid"] == board["uuid"])["folderId"] == folder["uuid"]
    finally:
        db.kanban_delete_folder(_u(folder["uuid"]))


def test_tree_put_missing_version_400_and_stale_409(board):
    client = _client()
    tree = client.get("/kanban/api/tree").get_json()
    assert client.put("/kanban/api/tree",
                      json={"folders": tree["folders"], "boards": tree["boards"]}
                      ).status_code == 400
    # Rotate the version out from under us.
    f = db.kanban_create_folder("rotate")
    try:
        resp = client.put("/kanban/api/tree",
                          json={**tree, "folders": tree["folders"], "boards": tree["boards"]})
        assert resp.status_code == 409 and isinstance(resp.get_json()["version"], str)
    finally:
        db.kanban_delete_folder(_u(f["uuid"]))


def test_folder_create_and_delete_endpoints(app_ctx):
    client = _client()
    r = client.post("/kanban/api/folders", json={"name": "made via api"})
    assert r.status_code == 200 and r.get_json()["folder"]["name"] == "made via api"
    fu = r.get_json()["folder"]["uuid"]
    assert client.delete(f"/kanban/api/folders/{fu}").status_code == 200
    assert client.delete(f"/kanban/api/folders/{fu}").status_code == 404
    assert client.post("/kanban/api/folders", json={"name": "  "}).status_code == 400


def test_board_create_honors_folderId(app_ctx):
    client = _client()
    f = client.post("/kanban/api/folders", json={"name": "dest"}).get_json()["folder"]
    r = client.post("/kanban/api/boards", json={"name": "filed", "folderId": f["uuid"]})
    bu = r.get_json()["board"]["uuid"]
    try:
        placed = next(b for b in db.kanban_load_tree()["boards"] if b["uuid"] == bu)
        assert placed["folderId"] == f["uuid"]
    finally:
        db.kanban_delete_board(_u(bu))
        db.kanban_delete_folder(_u(f["uuid"]))
