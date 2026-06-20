# Kanban-move Write Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the assistant a `kanban_move` log-and-undo write that moves a task between columns, with a durable reversible ledger and a generic undo.

**Architecture:** A new code-owned `kanban_move` capability whose action performs the move and returns its own inverse op. The ReAct loop records the executed write as a `completed` `AssistantWriteIntent` carrying that inverse; a generic `undo_write_intent` (and HTTP endpoint) replays the inverse and flips the intent to `undone`. The capability bypasses the worker `observe/work/shape` authority model by design — safety is reversibility + trace.

**Tech Stack:** Python, Flask, SQLAlchemy, pytest. Postgres via `db.make_app()`/`db.init_db()`. Determinism via the `scripted_decisions` fake-model seam.

## Global Constraints

- Run tests with the project venv: `./venv/bin/python -m pytest …` (system pytest lacks deps). Run from `source/`.
- Ad-hoc DB work targets `rainbox_claude`; tests are forced onto it by `conftest.py` — no action needed in the test path.
- All new write behavior is model-free: no test may require a live LLM or Ollama.
- `db.kanban_*` functions are auto-exported from `db/kanban.py` (`from db.kanban import *`, no `__all__`); `db.KanbanError` is already reachable.
- Design source of truth: `docs/superpowers/specs/2026-06-20-kanban-move-write-family-design.md`.

---

### Task 1: `db.kanban_get_task` read helper

**Files:**
- Modify: `db/kanban.py` (add public `kanban_get_task` near `_task`/`_task_brief`, ~line 826)
- Test: `db/test_kanban_get_task.py` (create)

**Interfaces:**
- Consumes: existing private `_task(task_uuid) -> KanbanTask | None`, `_task_brief(t) -> dict`, and `db.kanban_create_board`/`kanban_load_board`/`kanban_save_board`/`kanban_delete_board`.
- Produces: `db.kanban_get_task(task_uuid: UUID) -> dict | None` returning the `_task_brief` dict (keys: `uuid`, `boardUuid`, `columnUuid`, `title`, `description`, `agentUuid`, `claimedBy`, `claimExpiresAt`) or `None` when the task does not exist.

- [ ] **Step 1: Write the failing test**

Create `db/test_kanban_get_task.py`:

```python
"""db.kanban_get_task: a public single-task reader returning the task brief
(including the current columnUuid), used by the assistant's kanban-move undo."""

from uuid import UUID, uuid4

import pytest

import db


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
    b = db.kanban_create_board("get-task board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n} for n in ("To do", "Done")]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Ship it", "description": "d"}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    try:
        yield data
    finally:
        db.kanban_delete_board(bu)


def test_get_task_returns_brief_with_current_column(board):
    task = board["tasks"][0]
    out = db.kanban_get_task(UUID(task["uuid"]))
    assert out is not None
    assert out["uuid"] == task["uuid"]
    assert out["columnUuid"] == board["columns"][0]["uuid"]
    assert out["title"] == "Ship it"


def test_get_task_returns_none_for_unknown(app_ctx):
    assert db.kanban_get_task(uuid4()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest db/test_kanban_get_task.py -q`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'kanban_get_task'`.

- [ ] **Step 3: Write minimal implementation**

In `db/kanban.py`, immediately after `_task_brief` (after line 826), add:

```python
def kanban_get_task(task_uuid: UUID) -> dict[str, Any] | None:
    """Public single-task read: the task brief (incl. current columnUuid), or
    None if the task is gone."""
    t = _task(task_uuid)
    return _task_brief(t) if t is not None else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest db/test_kanban_get_task.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add db/kanban.py db/test_kanban_get_task.py
git commit -m "feat(db): kanban_get_task public single-task reader"
```

---

### Task 2: `create_write_intent` accepts `state` and `result`

**Files:**
- Modify: `db/assistant.py:143-167` (`create_write_intent`)
- Test: `db/test_assistant_write_intent.py` (add one test)

**Interfaces:**
- Consumes: `AssistantWriteIntent`, `write_intent_payload_hash` (existing).
- Produces: `db.create_write_intent(*, run_id, step_index, capability_name, payload, preview_text, room_uuid, agent_uuid, state: str = "proposed", result: dict | None = None) -> AssistantWriteIntent`. Default behavior (no `state`/`result`) is unchanged.

- [ ] **Step 1: Write the failing test**

In `db/test_assistant_write_intent.py`, add (it already has an `app_ctx` and a `run` fixture — reuse them):

```python
def test_create_write_intent_accepts_completed_state_and_result(run):
    intent = db.create_write_intent(
        run_id=run.id, step_index=0, capability_name="kanban_move",
        payload={"task_uuid": "t", "column_uuid": "c"},
        preview_text="kanban_move: …",
        room_uuid=run.room_uuid, agent_uuid=run.agent_uuid,
        state="completed", result={"undo": {"capability": "kanban_move"}},
    )
    assert intent.state == "completed"
    assert intent.result == {"undo": {"capability": "kanban_move"}}
```

(If the `run` fixture's attribute names differ, read the top of the file and match them; `run.room_uuid`/`run.agent_uuid` come from the `AssistantRun` row.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest db/test_assistant_write_intent.py::test_create_write_intent_accepts_completed_state_and_result -q`
Expected: FAIL — `TypeError: create_write_intent() got an unexpected keyword argument 'state'`.

- [ ] **Step 3: Write minimal implementation**

Replace `create_write_intent` in `db/assistant.py` (lines 143-167) with:

```python
def create_write_intent(
    *,
    run_id: int,
    step_index: int,
    capability_name: str,
    payload: dict[str, Any],
    preview_text: str,
    room_uuid: UUID,
    agent_uuid: UUID,
    state: str = "proposed",
    result: dict[str, Any] | None = None,
) -> AssistantWriteIntent:
    """Open a write intent. Defaults to a `proposed` confirm-tier proposal; a
    log-and-undo recorder passes `state="completed"` with a `result` so the row
    is never confirmable as `proposed` (no double-execute window)."""
    intent = AssistantWriteIntent(
        run_id=run_id,
        step_index=step_index,
        capability_name=capability_name,
        payload=payload,
        payload_hash=write_intent_payload_hash(capability_name, payload),
        preview_text=preview_text,
        state=state,
        room_uuid=room_uuid,
        agent_uuid=agent_uuid,
        result=result or {},
    )
    db.session.add(intent)
    db.session.commit()
    return intent
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest db/test_assistant_write_intent.py -q`
Expected: PASS (all, including the new test).

- [ ] **Step 5: Commit**

```bash
git add db/assistant.py db/test_assistant_write_intent.py
git commit -m "feat(db): create_write_intent accepts state + result (atomic completed intents)"
```

---

### Task 3: `execute_write_intent` refuses non-confirm-tier capabilities

**Files:**
- Modify: `agents/assistant_writes.py` (`execute_write_intent`, the capability-validation block after `cap` is resolved)
- Test: `agents/test_assistant_writes.py` (add one test)

**Interfaces:**
- Consumes: `CAPABILITIES`, `AssistantActionName`, `db.create_write_intent`, `db.get_write_intent`, `db.set_write_intent_state`.
- Produces: `execute_write_intent` now returns `ok=False` and marks the intent `failed` when the named capability's `tier != "confirm"`. Defense in depth against a log-and-undo intent being confirm-executed into a duplicate write.

- [ ] **Step 1: Write the failing test**

In `agents/test_assistant_writes.py`, add (reuse `app_ctx` and the `run`-like setup; create a run row first):

```python
def test_execute_refuses_non_confirm_tier_capability(app_ctx):
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6,
    )
    # 'remember' is log_and_undo, not confirm — a proposed intent for it must be refused.
    intent = db.create_write_intent(
        run_id=run.id, step_index=0, capability_name="remember",
        payload={"text": "x"}, preview_text="remember: …",
        room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
    )
    try:
        obs = execute_write_intent(intent.uuid)
        assert obs.ok is False
        refreshed = db.get_write_intent(intent.uuid)
        assert refreshed.state == "failed"
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_id == run.id
        ).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.id == run.id).delete()
        db.db.session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_assistant_writes.py::test_execute_refuses_non_confirm_tier_capability -q`
Expected: FAIL — `execute_write_intent` currently runs `remember` (creates a candidate) and returns `ok=True`, so the `assert obs.ok is False` fails.

- [ ] **Step 3: Write minimal implementation**

In `agents/assistant_writes.py`, in `execute_write_intent`, find the existing block:

```python
    if cap.action is None or not cap.write:
        db.set_write_intent_state(intent, "failed", error="capability is not an executable write")
        return AssistantObservation(ok=False, text="capability is not an executable write")
```

Add immediately after it:

```python
    if cap.tier != "confirm":
        db.set_write_intent_state(intent, "failed", error="capability is not confirm-tier")
        return AssistantObservation(
            ok=False, text="capability is not confirm-tier; refusing to confirm-execute"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest agents/test_assistant_writes.py -q`
Expected: PASS (all, including the new test).

- [ ] **Step 5: Commit**

```bash
git add agents/assistant_writes.py agents/test_assistant_writes.py
git commit -m "fix(assistant): confirm path refuses non-confirm-tier capabilities"
```

---

### Task 4: `kanban_move` capability + action (+ authority doc note)

**Files:**
- Modify: `agents/assistant.py` (add `KANBAN_MOVE` enum value ~line 68; add `_action_move_kanban_task` near the other actions, before the `CAPABILITIES` dict ~line 287; add a `CAPABILITIES` entry ~line 375)
- Modify: `docs/kanban-design.md` (one-line note in the permission section)
- Test: `agents/test_kanban_move_action.py` (create)

**Interfaces:**
- Consumes: `db.kanban_get_task` (Task 1), `db.kanban_move_task`, `db.KanbanError`, `AssistantActionContext`, `AssistantObservation`.
- Produces: `AssistantActionName.KANBAN_MOVE = "kanban_move"`; `_action_move_kanban_task(ctx, args) -> AssistantObservation` with `ok=True` data `{"task_uuid", "from_column_uuid", "to_column_uuid", "undo": {"capability": "kanban_move", "payload": {"task_uuid", "column_uuid"}}}`; a `CAPABILITIES[KANBAN_MOVE]` entry with `write=True, tier="log_and_undo", required_args=("task_uuid","column_uuid")`.

- [ ] **Step 1: Write the failing test**

Create `agents/test_kanban_move_action.py`:

```python
"""The kanban_move action moves a task and returns its inverse (undo) op."""

from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    CAPABILITIES,
    AssistantActionContext,
    AssistantActionName,
    _action_move_kanban_task,
)
from agents.config import ASSISTANT_UUID


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
    b = db.kanban_create_board("move board")
    bu = UUID(b["uuid"])
    fresh = db.kanban_load_board(bu)
    fresh["columns"] = [{"uuid": str(uuid4()), "name": n} for n in ("To do", "Done")]
    fresh["tasks"] = [{"uuid": str(uuid4()),
                       "columnUuid": fresh["columns"][0]["uuid"],
                       "title": "Ship it", "description": "d"}]
    db.kanban_save_board(bu, fresh)
    data = db.kanban_load_board(bu)
    try:
        yield data
    finally:
        db.kanban_delete_board(bu)


def _ctx(room_uuid=None):
    return AssistantActionContext(
        journal_id=None, room_uuid=room_uuid or uuid4(),
        agent_uuid=ASSISTANT_UUID, step_index=0,
    )


def test_capability_is_log_and_undo_write():
    cap = CAPABILITIES[AssistantActionName.KANBAN_MOVE]
    assert cap.write is True
    assert cap.tier == "log_and_undo"
    assert cap.required_args == ("task_uuid", "column_uuid")


def test_move_executes_and_returns_inverse(board):
    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][1]["uuid"]
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": task["uuid"], "column_uuid": done}
    )
    assert obs.ok is True
    # Task actually moved.
    assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done
    # Inverse points back at the original column.
    assert obs.data["undo"] == {
        "capability": "kanban_move",
        "payload": {"task_uuid": task["uuid"], "column_uuid": todo},
    }


def test_move_rejects_missing_task(app_ctx):
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": str(uuid4()), "column_uuid": str(uuid4())}
    )
    assert obs.ok is False


def test_move_rejects_column_not_on_board(board):
    task = board["tasks"][0]
    obs = _action_move_kanban_task(
        _ctx(), {"task_uuid": task["uuid"], "column_uuid": str(uuid4())}
    )
    assert obs.ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_kanban_move_action.py -q`
Expected: FAIL — `ImportError: cannot import name '_action_move_kanban_task'` / no `KANBAN_MOVE`.

- [ ] **Step 3: Write minimal implementation**

(a) In `agents/assistant.py`, add to the `AssistantActionName` enum after `ACTIVATE_MEMORY` (line 68):

```python
    KANBAN_MOVE = "kanban_move"        # log-and-undo: move a task between columns
```

Use the enum member name `KANBAN_MOVE` with value `"kanban_move"`.

(b) Add the action just before the `Capability` dataclass (before line 289):

```python
def _action_move_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: move a kanban task to another column of its board.
    Reversible — `data["undo"]` is the inverse move (back to the task's current
    column). Code-owned authority: this does not route through the worker
    observe/work/shape dispatcher; reversibility + trace is the safety."""
    raw_task, raw_col = args.get("task_uuid"), args.get("column_uuid")
    try:
        task_uuid = UUID(str(raw_task))
        column_uuid = UUID(str(raw_col))
    except (ValueError, TypeError):
        return AssistantObservation(
            ok=False, text=f"invalid task_uuid/column_uuid: {raw_task!r}, {raw_col!r}"
        )
    before = db.kanban_get_task(task_uuid)
    if before is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    from_column_uuid = before["columnUuid"]
    try:
        moved = db.kanban_move_task(
            task_uuid, column_uuid,
            actor=str(ctx.agent_uuid), note="assistant move (undoable)",
        )
    except db.KanbanError as e:
        return AssistantObservation(ok=False, text=f"cannot move: {e}")
    if moved is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    return AssistantObservation(
        ok=True,
        text=f"Moved '{before['title']}' to column {column_uuid} (undoable).",
        data={
            "task_uuid": str(task_uuid),
            "from_column_uuid": str(from_column_uuid),
            "to_column_uuid": str(column_uuid),
            "undo": {
                "capability": "kanban_move",
                "payload": {"task_uuid": str(task_uuid),
                            "column_uuid": str(from_column_uuid)},
            },
        },
    )
```

(c) Add the registry entry inside `CAPABILITIES`, after the `ACTIVATE_MEMORY` entry (after line 375):

```python
    AssistantActionName.KANBAN_MOVE: Capability(
        name=AssistantActionName.KANBAN_MOVE, family="kanban",
        description=('move a kanban task to another column; reversible (undoable). '
                     'args: {"task_uuid": "...", "column_uuid": "..."}'),
        required_args=("task_uuid", "column_uuid"),
        action=_action_move_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
```

(d) In `docs/kanban-design.md`, in the agent-permission section, add one line:

```markdown
> Note: the personal **assistant**'s `kanban_move` capability is code-owned and
> does not pass through this observe/work/shape model; it is a log-and-undo write
> whose safety is operator reversibility + trace, not the worker authority ceiling.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest agents/test_kanban_move_action.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add agents/assistant.py agents/test_kanban_move_action.py docs/kanban-design.md
git commit -m "feat(assistant): kanban_move log-and-undo capability + action"
```

---

### Task 5: Loop records the log-and-undo ledger

**Files:**
- Modify: `agents/assistant.py` (the write branch in `handle()` ~lines 562-566; add `_record_log_and_undo` method near `_propose_write` ~line 793)
- Test: `agents/test_kanban_move_action.py` (add an end-to-end loop test) — or a new `agents/test_kanban_move_loop.py`

**Interfaces:**
- Consumes: `db.create_write_intent(..., state=, result=)` (Task 2), `_action_move_kanban_task` (Task 4), `scripted_decisions`, `AssistantAgent`.
- Produces: `AssistantAgent._record_log_and_undo(self, ctx, cap, decision, observation) -> None` creating a `completed` `AssistantWriteIntent` whose `result["undo"]` is `observation.data["undo"]`. After a `kanban_move` step the task is moved AND exactly one `completed` intent exists for the run; no intent is ever `proposed`.

- [ ] **Step 1: Write the failing test**

Add to `agents/test_kanban_move_action.py` (imports at top: add `AssistantAgent`, `AssistantStepDecision`, `scripted_decisions`, `AssistantWriteIntent`, `AssistantRun`):

```python
def test_move_via_loop_lands_completed_undo_ledger(board):
    from agents.assistant import AssistantAgent, AssistantStepDecision
    from agents.assistant_fakes import scripted_decisions
    from db import AssistantRun, AssistantWriteIntent

    human = db.get_human_user()
    chatroom = db.create_chatroom(f"mv-{uuid4().hex[:8]}", human.uuid, [ASSISTANT_UUID])
    db.post_chat_message(chatroom.uuid, human.uuid, "move it to done")
    task = board["tasks"][0]
    done = board["columns"][1]["uuid"]

    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="move", action=AssistantActionName.KANBAN_MOVE,
                              args={"task_uuid": task["uuid"], "column_uuid": done}),
        AssistantStepDecision(reason="done", action=AssistantActionName.REPLY,
                              args={"message": "moved"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        # Task moved.
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == done
        # Exactly one ledger row, completed, never proposed, with a working inverse.
        intents = db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid
        ).all()
        assert len(intents) == 1
        assert intents[0].state == "completed"
        assert intents[0].result["undo"]["payload"]["column_uuid"] == board["columns"][0]["uuid"]
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(AssistantRun).filter(
            AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
```

(Verified pattern: `scripted_decisions(*decisions)` takes **varargs** — pass the decisions as positional args, not a list — and is assigned via `agent._decide_next_step = scripted_decisions(d1, d2)`, exactly as `test_assistant_writes.py` does.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_kanban_move_action.py::test_move_via_loop_lands_completed_undo_ledger -q`
Expected: FAIL — the task moves but **no** `AssistantWriteIntent` row is created (`len(intents) == 0`), because the loop doesn't record log-and-undo writes yet.

- [ ] **Step 3: Write minimal implementation**

(a) In `agents/assistant.py`, change the write branch (lines 562-566) to:

```python
                cap = self._caps[decision.action]
                if cap.write and cap.tier == "confirm":
                    observation = self._propose_write(action_ctx, decision, cap)
                else:
                    observation = self._dispatch_action(action_ctx, decision)
                    if cap.write and cap.tier == "log_and_undo" and observation.ok:
                        self._record_log_and_undo(action_ctx, cap, decision, observation)
```

(b) Add the method right after `_propose_write` (after line 793):

```python
    def _record_log_and_undo(
        self,
        ctx: AssistantActionContext,
        cap: "Capability",
        decision: AssistantStepDecision,
        observation: AssistantObservation,
    ) -> None:
        """Record an executed log-and-undo write as a `completed`, reversible
        ledger row. Created atomically in `completed` (never `proposed`) so it
        can't be confirm-executed into a duplicate write; `result["undo"]`
        carries the inverse op consumed by undo_write_intent."""
        preview = f"{cap.name.value}: {json.dumps(decision.args, sort_keys=True)}"
        db.create_write_intent(
            run_id=self._run.id,
            step_index=ctx.step_index,
            capability_name=cap.name.value,
            payload=decision.args,
            preview_text=preview,
            room_uuid=ctx.room_uuid,
            agent_uuid=ctx.agent_uuid,
            state="completed",
            result={"undo": observation.data.get("undo"), "text": observation.text},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest agents/test_kanban_move_action.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add agents/assistant.py agents/test_kanban_move_action.py
git commit -m "feat(assistant): record log-and-undo writes as completed undo ledger rows"
```

---

### Task 6: Generic `undo_write_intent` + HTTP endpoint

**Files:**
- Modify: `agents/assistant_writes.py` (add `undo_write_intent`)
- Modify: `webapp/chat_api.py` (add `/undo` route after the `/reject` route ~line 377)
- Test: `agents/test_kanban_move_action.py` (undo behavior) and `webapp/test_chat_api*.py` or a new `webapp/test_assistant_undo_endpoint.py` (endpoint)

**Interfaces:**
- Consumes: `db.get_write_intent`, `db.set_write_intent_state`, `CAPABILITIES`, `AssistantActionName`, `AssistantActionContext`, `_action_move_kanban_task` (via registry).
- Produces: `agents.assistant_writes.undo_write_intent(intent_uuid: UUID) -> AssistantObservation` — replays `result["undo"]` (`{capability, payload}`) and, on success, sets the original intent to `undone`; refuses a non-`completed` intent or one without an undo record. HTTP `POST /chat/api/assistant/write-intents/<uuid>/undo` returning `{ok, text, data}`.

- [ ] **Step 1: Write the failing tests**

Add to `agents/test_kanban_move_action.py`:

```python
def test_undo_moves_task_back_and_marks_undone(board):
    from agents.assistant_writes import undo_write_intent
    from db import AssistantRun, AssistantWriteIntent

    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_limit=6)
    task = board["tasks"][0]
    todo, done = board["columns"][0]["uuid"], board["columns"][1]["uuid"]
    db.kanban_move_task(UUID(task["uuid"]), UUID(done), actor=str(ASSISTANT_UUID))
    intent = db.create_write_intent(
        run_id=run.id, step_index=0, capability_name="kanban_move",
        payload={"task_uuid": task["uuid"], "column_uuid": done},
        preview_text="kanban_move: …", room_uuid=run.room_uuid, agent_uuid=ASSISTANT_UUID,
        state="completed",
        result={"undo": {"capability": "kanban_move",
                         "payload": {"task_uuid": task["uuid"], "column_uuid": todo}}},
    )
    try:
        obs = undo_write_intent(intent.uuid)
        assert obs.ok is True
        assert db.kanban_get_task(UUID(task["uuid"]))["columnUuid"] == todo
        assert db.get_write_intent(intent.uuid).state == "undone"
        # Second undo is refused (already undone, not completed).
        assert undo_write_intent(intent.uuid).ok is False
    finally:
        db.db.session.query(AssistantWriteIntent).filter(
            AssistantWriteIntent.run_id == run.id).delete()
        db.db.session.query(AssistantRun).filter(AssistantRun.id == run.id).delete()
        db.db.session.commit()


def test_undo_refuses_unknown_intent(app_ctx):
    from agents.assistant_writes import undo_write_intent
    assert undo_write_intent(uuid4()).ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest agents/test_kanban_move_action.py -q`
Expected: FAIL — `ImportError: cannot import name 'undo_write_intent'`.

- [ ] **Step 3: Write minimal implementation**

(a) In `agents/assistant_writes.py`, add (the module already imports `db`, `CAPABILITIES`, `AssistantActionContext`, `AssistantActionName`, `AssistantObservation`, `logger`):

```python
def undo_write_intent(intent_uuid: UUID) -> AssistantObservation:
    """Revert a completed log-and-undo write by replaying its stored inverse op,
    then mark the original intent `undone`. One-shot: only a `completed` intent
    with an `undo` record can be undone (no redo)."""
    intent = db.get_write_intent(intent_uuid)
    if intent is None:
        return AssistantObservation(ok=False, text="no such write intent")
    if intent.state != "completed":
        return AssistantObservation(
            ok=False, text=f"write intent is not undoable (state={intent.state})"
        )
    undo = (intent.result or {}).get("undo")
    if not undo:
        return AssistantObservation(ok=False, text="write intent has no undo record")
    try:
        cap = CAPABILITIES[AssistantActionName(undo["capability"])]
    except (KeyError, ValueError):
        return AssistantObservation(ok=False, text="unknown capability for undo")
    if cap.action is None:
        return AssistantObservation(ok=False, text="undo capability has no dispatcher")
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=intent.room_uuid,
        agent_uuid=intent.agent_uuid, step_index=intent.step_index,
    )
    try:
        obs = cap.action(ctx, dict(undo["payload"]))
    except Exception as e:
        logger.exception("undo of write intent %s failed", intent_uuid)
        return AssistantObservation(ok=False, text=f"{type(e).__name__}: {e}")
    if obs.ok:
        db.set_write_intent_state(intent, "undone", result={**intent.result, "undone": True})
    return obs
```

(If `UUID` is not already imported in `assistant_writes.py`, it is — `from uuid import UUID` is at the top.)

(b) In `webapp/chat_api.py`, after the `/reject` route (after line 377), add:

```python
@app.route("/chat/api/assistant/write-intents/<uuid:intent_uuid>/undo", methods=["POST"])
def undo_assistant_write_intent(intent_uuid: UUID) -> Response:
    """Revert a completed log-and-undo write (e.g. a kanban move)."""
    from agents.assistant_writes import undo_write_intent

    obs = undo_write_intent(intent_uuid)
    return jsonify({"ok": obs.ok, "text": obs.text, "data": obs.data})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest agents/test_kanban_move_action.py -q`
Expected: PASS (all).

- [ ] **Step 5: Write the endpoint test**

Create `webapp/test_assistant_undo_endpoint.py` (verified: `webapp/chat_api.py` does `from .core import app`, so importing `app` from `webapp.chat_api` both yields the Flask app and registers the chat routes):

```python
"""POST /chat/api/assistant/write-intents/<uuid>/undo reverts a kanban move."""

from uuid import UUID, uuid4

import pytest

import db
from agents.config import ASSISTANT_UUID
from webapp.chat_api import app as flask_app


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


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def test_undo_endpoint_unknown_intent_returns_ok_false(app_ctx, client):
    resp = client.post(f"/chat/api/assistant/write-intents/{uuid4()}/undo")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
```

(If `webapp/chat_api.py` exposes the Flask app under a different name, read its top and import the real symbol.)

- [ ] **Step 6: Run the endpoint test**

Run: `./venv/bin/python -m pytest webapp/test_assistant_undo_endpoint.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/assistant_writes.py webapp/chat_api.py agents/test_kanban_move_action.py webapp/test_assistant_undo_endpoint.py
git commit -m "feat(assistant): generic undo_write_intent + /undo endpoint (kanban move revert)"
```

---

### Task 7: Full affected-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the affected suites**

Run: `./venv/bin/python -m pytest agents/ memory/ db/ webapp/ user_profile/ -q`
Expected: all pass (no regressions in existing assistant/kanban/write-intent tests).

- [ ] **Step 2: If green, no commit needed**

If a pre-existing unrelated failure appears, note it; do not fix out-of-scope tests. Otherwise the family is complete.

---

## Notes for the implementer

- The enum member is `AssistantActionName.KANBAN_MOVE` with string value `"kanban_move"`. The string is what appears in the prompt catalog, the registry key, and `capability_name`; the Python member name is `KANBAN_MOVE`.
- `db.create_write_intent` is used in three modes now: confirm proposal (default `proposed`), and log-and-undo ledger (`state="completed"`, `result={...}`). Both share one helper — do not fork it.
- `_record_log_and_undo` lives on the agent (not in the action) because it needs `self._run.id`; the pure action only returns the inverse in its observation `data`.
- Undo is generic: it re-dispatches the stored inverse capability+payload. For `kanban_move` the inverse is itself a `kanban_move`, so no per-capability undo code exists or is needed.
