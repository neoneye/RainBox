# S2 (batch 1) — kanban complete + comment write families — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Implements the first batch of card **S2** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
two new assistant kanban write families — **mark a task done** and **comment on a
task** — both **log-and-undo**, both reusing the existing write-intent ledger and
generic `undo_write_intent`.

`kanban_create` (the third S2 sub-family) is **deferred to its own complete
spec**: it needs new `kanban_create_task`/`kanban_delete_task` DB primitives and a
dispatch guard so the model can't invoke the delete-inverse directly — genuinely
bigger, so bundling it here would leave it half-specified.

## Decisions (made, with rationale — no open questions)

- **`kanban_complete` — log-and-undo.** Marking done is reversible (re-open by
  moving back to the prior column), so it fits the proven log-and-undo pattern.
  The **inverse is a `kanban_move`** back to the captured prior column — reuses
  the existing exposed capability, no new "uncomplete" code.
- **`kanban_complete` routes straight to Done (`review=False`).** The assistant
  marking a task done is an explicit operator-proxy action → the board's Done
  (last) column. The worker `review=True` verified-routing is a worker-lifecycle
  concept and does not apply to an operator-proxy write.
- **`kanban_comment` — log-and-undo.** Keeps every kanban write traced + undoable
  for consistency. The event log is append-only, so **undo posts a retraction
  comment** (`↩ retracted: <text>`) rather than erasing — honest, needs no
  delete primitive. The inverse is a `kanban_comment` with the retraction text;
  `undo_write_intent` dispatches it directly (never re-ledgered), so it is
  one-shot (no redo, no undo-of-undo recursion).
- **`_record_log_and_undo` None-undo hardening** (deferred follow-up from the
  kanban-move review, now that there is a second+third log-and-undo family): if a
  `log_and_undo` write's observation carries no `undo` data, log a warning. The
  row is still recorded (the trace must exist); `undo_write_intent` already
  refuses a `None` undo gracefully. This catches a future capability that forgets
  to return its inverse.

## Substrate reused (all on `main`)

- `Capability` / `CAPABILITIES` registry and the loop write branch
  (`tier=="log_and_undo"` → dispatch + `_record_log_and_undo`).
- `undo_write_intent` (generic: replays `result["undo"]`'s capability+payload).
- `db.kanban_get_task` (capture prior column), `db.kanban_complete_task(task_uuid,
  ok, *, actor, detail, review)` (ok=True → moves to last column + `done` event;
  releases lease — a no-op for the assistant), `db.kanban_append_event(task_uuid,
  kind, *, actor, detail)`, `db.kanban_move` capability (the complete-inverse).

## Implementation

### `agents/assistant.py`

**Enum** (after `KANBAN_MOVE`):

```python
    KANBAN_COMPLETE = "kanban_complete"  # log-and-undo: mark a task done
    KANBAN_COMMENT = "kanban_comment"    # log-and-undo: comment on a task
```

**Actions** (next to `_action_move_kanban_task`, before the `Capability` dataclass):

```python
def _action_complete_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: mark a task done (move it to the board's Done/last
    column + a 'done' event). Reversible — the undo is a kanban_move back to the
    task's prior column. Operator-proxy intent → Done, not worker review-routing."""
    raw = args.get("task_uuid")
    try:
        task_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw!r}")
    before = db.kanban_get_task(task_uuid)
    if before is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    from_column_uuid = before["columnUuid"]
    after = db.kanban_complete_task(
        task_uuid, True, actor=str(ctx.agent_uuid),
        detail="assistant marked done (undoable)", review=False,
    )
    if after is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    return AssistantObservation(
        ok=True,
        text=f"Marked '{before['title']}' done (undoable).",
        data={
            "task_uuid": str(task_uuid),
            "from_column_uuid": str(from_column_uuid),
            "to_column_uuid": after["columnUuid"],
            "undo": {
                "capability": "kanban_move",
                "payload": {"task_uuid": str(task_uuid),
                            "column_uuid": str(from_column_uuid)},
            },
        },
    )


def _action_comment_kanban_task(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Log-and-undo write: append a comment event to a task. The event log is
    append-only, so the undo posts a retraction comment rather than erasing."""
    raw = args.get("task_uuid")
    text = str(args.get("text", "")).strip()
    try:
        task_uuid = UUID(str(raw))
    except (ValueError, TypeError):
        return AssistantObservation(ok=False, text=f"invalid task_uuid: {raw!r}")
    is_retraction = text.startswith("↩ retracted: ")
    event = db.kanban_append_event(
        task_uuid, "comment", actor=str(ctx.agent_uuid), detail=text,
    )
    if event is None:
        return AssistantObservation(ok=False, text="no such kanban task")
    data: dict[str, Any] = {"task_uuid": str(task_uuid)}
    # A retraction (posted by undo) is itself a comment but needs no further undo.
    if not is_retraction:
        data["undo"] = {
            "capability": "kanban_comment",
            "payload": {"task_uuid": str(task_uuid),
                        "text": f"↩ retracted: {text}"},
        }
    return AssistantObservation(
        ok=True, text=f"Commented on task {task_uuid} (undoable).", data=data,
    )
```

**Registry entries** (after the `KANBAN_MOVE` entry):

```python
    AssistantActionName.KANBAN_COMPLETE: Capability(
        name=AssistantActionName.KANBAN_COMPLETE, family="kanban",
        description=('mark a kanban task done (moves it to the Done column); '
                     'reversible. args: {"task_uuid": "..."}'),
        required_args=("task_uuid",), action=_action_complete_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
    AssistantActionName.KANBAN_COMMENT: Capability(
        name=AssistantActionName.KANBAN_COMMENT, family="kanban",
        description=('add a comment to a kanban task; reversible (posts a '
                     'retraction). args: {"task_uuid": "...", "text": "..."}'),
        required_args=("task_uuid", "text"), action=_action_comment_kanban_task,
        read=False, write=True, tier="log_and_undo",
    ),
```

**`_record_log_and_undo` hardening** — at the top of the method:

```python
        if observation.data.get("undo") is None:
            logger.warning(
                "assistant: log-and-undo write '%s' produced no undo record; "
                "it will not be undoable", cap.name.value,
            )
```

### `agents/test_assistant_fakes.py`

Add `"kanban_complete"` and `"kanban_comment"` to the locked action surface set,
and update the docstring.

## Tests (TDD, model-free) — `agents/test_kanban_writes_s2.py` (new)

Reuse the `board` fixture style from `agents/test_kanban_move_action.py`.

1. **complete marks done + lands undo ledger:** action moves the task to the last
   column, records a `done` event, returns `undo` pointing at the prior column.
2. **complete via loop + undo re-opens:** a scripted `kanban_complete` step lands
   a `completed` intent; `undo_write_intent` moves the task back to the prior
   column and flips the intent to `undone`.
3. **complete rejects missing task:** `ok=False`, no ledger row.
4. **comment appends event + undo posts retraction:** action appends a `comment`
   event; `undo_write_intent` appends a second `comment` event whose detail starts
   `↩ retracted:`, and flips the intent `undone`. The original comment remains.
5. **comment rejects missing task:** `ok=False`.
6. **capabilities declare `tier="log_and_undo"`, `write=True`.**
7. **surface lock:** `test_action_enum_covers_the_known_action_surface` includes
   both new values (updated in `test_assistant_fakes.py`).

## Done when

- The assistant can mark a task done and comment on a task, each as a log-and-undo
  write with a `completed` ledger row and a working undo (re-open / retraction).
- Missing-task paths fail cleanly with no ledger row.
- The surface-lock test and the None-undo hardening are in place.
- Model-free tests cover all of the above; full affected suite stays green.

## Out of scope (next S2 increment, its own complete spec)

- `kanban_create` (task creation) — needs `kanban_create_task`/`kanban_delete_task`
  DB primitives, a non-model-invocable delete-inverse (a dispatch guard so the
  model can request only prompt-exposed capabilities), and create→delete undo.
