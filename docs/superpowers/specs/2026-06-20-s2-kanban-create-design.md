# S2 (batch 2) — kanban create task write family — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Completes card **S2** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
the assistant can **create a kanban task**, log-and-undo, undo = delete it.

This is the increment deferred from S2 batch 1 because it needs new DB primitives
and a dispatch guard so the delete-inverse can't be invoked by the model.

## Decisions (made, with rationale)

- **`kanban_create` — log-and-undo.** Creating a task is reversible: undo deletes
  it. The **inverse is a new `kanban_delete_task` capability** dispatched by
  `undo_write_intent`.
- **`kanban_delete_task` is NOT prompt-exposed (`prompt_exposed=False`).** Hard
  delete is high blast radius; the model must not be able to call it. It exists in
  the registry only so `undo_write_intent` can dispatch it as the create-inverse.
- **Enforce the documented "model can request only prompt-exposed capabilities"
  contract.** The registry docstring already claims this, but `_validate_decision`
  doesn't enforce it. Add a guard: a model decision for a non-`prompt_exposed`
  capability is rejected (indistinguishable from unknown). `undo_write_intent`
  dispatches the capability's `action` directly and does NOT go through
  `_validate_decision`, so undo can still run the delete-inverse. All current
  capabilities are prompt-exposed, so this guard changes no existing behavior.
- **Undo of create is one-shot, no redo.** `kanban_delete_task` returns no `undo`
  data and is only ever dispatched by undo (never via the loop's record path), so
  it is never ledgered. Deleting also removes the task's events — acceptable: the
  operator chose to undo within the window.

## New DB primitives — `db/kanban.py`

```python
def kanban_create_task(
    board_uuid: UUID, column_uuid: UUID, *, title: str,
    description: str = "", actor: str = "",
) -> dict[str, Any] | None:
    """Create a task at the end of a column. None if the column isn't on the
    board. Records a 'created' event."""
    col = db.session.execute(
        sa.select(KanbanColumn).where(KanbanColumn.uuid == column_uuid,
                                      KanbanColumn.board_uuid == board_uuid)
    ).scalar_one_or_none()
    if col is None:
        return None
    pos = (db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanTask.position), -1))
        .where(KanbanTask.column_uuid == column_uuid)
    ).scalar_one()) + 1
    t = KanbanTask(board_uuid=board_uuid, column_uuid=column_uuid,
                   title=title, description=description, position=pos)
    db.session.add(t)
    db.session.flush()
    db.session.add(KanbanTaskEvent(task_uuid=t.uuid, kind="created",
                                   actor=str(actor or ""), detail=title))
    db.session.commit()
    return _task_brief(t)


def kanban_delete_task(task_uuid: UUID) -> bool:
    """Hard-delete a task and its events. True if removed, False if absent."""
    t = _task(task_uuid)
    if t is None:
        return False
    db.session.execute(sa.delete(KanbanTaskEvent).where(KanbanTaskEvent.task_uuid == task_uuid))
    db.session.execute(sa.delete(KanbanTask).where(KanbanTask.uuid == task_uuid))
    db.session.commit()
    return True
```

(`KanbanTaskEvent.kind` is free Text — `'created'` needs no CHECK widen.
`KanbanTask` has no board/column FKs, so the column-on-board check is explicit.)

## `agents/assistant.py`

**Enum** (after `KANBAN_COMMENT`):

```python
    KANBAN_CREATE = "kanban_create"            # log-and-undo: create a task
    KANBAN_DELETE_TASK = "kanban_delete_task"  # internal: create's undo inverse (not prompt-exposed)
```

**Actions** (near the other kanban actions):

```python
def _action_create_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: create a task in a column. Undo deletes it."""
    raw_board, raw_col = args.get("board_uuid"), args.get("column_uuid")
    title = str(args.get("title", "")).strip()
    description = str(args.get("description", "")).strip()
    try:
        board_uuid = UUID(str(raw_board))
        column_uuid = UUID(str(raw_col))
    except (ValueError, TypeError):
        return AssistantObservation(
            ok=False, text=f"invalid board_uuid/column_uuid: {raw_board!r}, {raw_col!r}"
        )
    created = db.kanban_create_task(
        board_uuid, column_uuid, title=title, description=description,
        actor=str(ctx.agent_uuid),
    )
    if created is None:
        return AssistantObservation(ok=False, text="no such board or column")
    return AssistantObservation(
        ok=True,
        text=f"Created task '{title}' (undoable — undo deletes it).",
        data={
            "task_uuid": created["uuid"],
            "board_uuid": str(board_uuid),
            "column_uuid": str(column_uuid),
            "undo": {"capability": "kanban_delete_task",
                     "payload": {"task_uuid": created["uuid"]}},
        },
    )


def _action_delete_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Internal: hard-delete a task. Not prompt-exposed — reached only as the
    undo-inverse of kanban_create (via undo_write_intent)."""
    raw = args.get("task_uuid")
    try:
        task_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw!r}")
    if not db.kanban_delete_task(task_uuid):
        return AssistantObservation(ok=False, text="no such kanban task")
    return AssistantObservation(
        ok=True, text=f"Deleted task {task_uuid}", data={"task_uuid": str(task_uuid)},
    )
```

**Registry entries** (after `KANBAN_COMMENT`):

```python
    AssistantActionName.KANBAN_CREATE: Capability(
        name=AssistantActionName.KANBAN_CREATE, family="kanban",
        description=('create a kanban task in a column; reversible (undo deletes '
                     'it). args: {"board_uuid": "...", "column_uuid": "...", '
                     '"title": "...", optional "description": "..."}'),
        required_args=("board_uuid", "column_uuid", "title"),
        optional_args=frozenset({"description"}),
        action=_action_create_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_DELETE_TASK: Capability(
        name=AssistantActionName.KANBAN_DELETE_TASK, family="kanban",
        description="(internal) delete a kanban task — the undo-inverse of kanban_create.",
        required_args=("task_uuid",), action=_action_delete_kanban_task,
        read=False, write=True, tier="log_and_undo", prompt_exposed=False,
    ),
```

**Validator guard** — in `_validate_decision`, right after the `cap is None` check:

```python
        if not cap.prompt_exposed:
            # The model may request only prompt-exposed capabilities; internal
            # ones (e.g. undo-inverses) are dispatched only by undo_write_intent.
            return f"action '{action.value}' is not available"
```

## `agents/test_assistant_fakes.py`

Add `"kanban_create"` and `"kanban_delete_task"` to the locked action surface.

## Tests (TDD, model-free) — `agents/test_kanban_create.py` (new)

Reuse the `board` fixture (≥1 column) from the S2 tests.

1. **create makes a task + returns delete-inverse:** action creates a task in the
   column (queryable via `kanban_get_task`), returns `undo` =
   `{capability: kanban_delete_task, payload: {task_uuid}}`; a `created` event exists.
2. **create rejects bad board/column:** unknown column → `ok=False`, no task created.
3. **create via loop + undo deletes:** scripted `kanban_create` step lands a
   `completed` intent; `undo_write_intent` deletes the task (`kanban_get_task` →
   None) and flips the intent to `undone`.
4. **model cannot invoke kanban_delete_task:** the loop rejects a scripted
   `kanban_delete_task` decision (validator guard) — the task is NOT deleted and
   the step is recorded rejected/failed (no delete happens).
5. **capabilities:** `kanban_create` is prompt-exposed log-and-undo write;
   `kanban_delete_task` is `prompt_exposed=False`.
6. **surface lock** updated.

## Done when

- The assistant can create a task as a log-and-undo write; undo deletes it.
- The model cannot delete a task directly (validator guard; `kanban_delete_task`
  is not in the prompt catalog and is rejected if requested).
- Model-free tests cover create, undo-deletes, the guard, and bad inputs; full
  affected suite green; surface-lock + None-undo invariants hold.
