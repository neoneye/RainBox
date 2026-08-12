# Kanban-move write family — design (2026-06-20)

**Status:** approved design, pre-implementation. First write family of the
Phase 5 rollout from
[`../../proposals/2026-06-19-improvements-v2.md`](../../proposals/2026-06-19-improvements-v2.md).
It adds the assistant's first **log-and-undo** write and, in doing so, builds the
reusable log-and-undo *ledger + undo* machinery the roadmap flagged as missing
("Add `undone` handling for log-and-undo reverts").

## Goal

Let the assistant move a kanban task between columns on the operator's behalf —
"move the auth task to Done" — as a **log-and-undo, operator-in-loop** write: it
executes immediately during the run, records a durable reversible trace, and
exposes an undo. Move is chosen because it is cleanly reversible (undo = move
back to the original column), making it the right demonstrator for the tier.

## Decisions (locked with the operator)

1. **First write:** move a task between columns.
2. **Tier:** log-and-undo, operator-in-loop. The assistant executes the move
   without up-front confirmation; the safety is *reversibility + trace*, not a
   pre-approval gate.
3. **Authority:** the assistant gets a narrow, code-owned `kanban_move`
   capability rather than blanket `shape` authority (see *Authority stance*).

## Contracts respected

- **Authority is code-owned.** The capability registry entry (enabled,
  `prompt_exposed`) is the permission. The model cannot widen it.
- **Trace before action.** The per-step `assistant_step` trace already commits
  `planned`/`running` before dispatch; this family adds a write-specific ledger
  row (the undo record) right after the move.
- **Writes are family-scoped and risk-tiered.** `kanban_move` owns its validator,
  trace shape, tests, and the `log_and_undo` tier — no blanket mutation switch.

## Components

### 1. Capability

A new registry entry in `agents/assistant.py`:

```
KANBAN_MOVE = "kanban_move"

Capability(
    name=KANBAN_MOVE, family="kanban",
    description="Move a kanban task to another column of its board (reversible).",
    required_args=("task_uuid", "column_uuid"),
    read=False, write=True, tier="log_and_undo",
    action=_action_move_kanban_task,
    prompt_exposed=True,
)
```

The model already has the ids it needs: `kanban_read`'s board markdown exposes
each task's `taskId` and each column's `columnId` (verified in
`db.kanban_board_markdown`), so a read → move sequence is self-sufficient.

### 2. Action — `_action_move_kanban_task(ctx, args)`

1. Parse + validate `task_uuid` and `column_uuid` (UUIDs); malformed → `ok=False`.
2. `before = db.kanban_get_task(task_uuid)` — a small new public read helper
   returning `_task_brief` (carries `columnUuid`). `None` → `ok=False,
   "no such kanban task"`.
3. `db.kanban_move_task(task_uuid, column_uuid, actor=str(ctx.agent_uuid),
   note="assistant move (undoable)")`. This already validates that the column is
   on the task's board (raises `KanbanError`) and appends a `moved` audit event.
   Catch `KanbanError` → `ok=False` with the message.
4. Return:

```
AssistantObservation(
  ok=True,
  text="Moved task <title> to <to-column> (undoable).",
  data={
    "task_uuid": <task_uuid>,
    "from_column_uuid": before["columnUuid"],
    "to_column_uuid": <column_uuid>,
    "undo": {"capability": "kanban_move",
             "payload": {"task_uuid": <task_uuid>,
                         "column_uuid": before["columnUuid"]}},
  },
)
```

The inverse of a move is itself a forward move, so the undo payload is just
another `kanban_move` — no per-capability undo code is needed.

A move to the column the task is already in is allowed (idempotent); the undo
payload then points back at the same column (a harmless no-op move).

### 3. Log-and-undo ledger

Today `remember` is log-and-undo but writes no `assistant_write_intent` row; its
"undo" is an ad-hoc `reject_memory`. This family builds the proper, reusable
flow on the existing `AssistantWriteIntent` table (whose state CHECK already
allows `completed` and `undone`).

The loop's write branch in `handle()` becomes:

```
cap = self._caps[decision.action]
if cap.write and cap.tier == "confirm":
    observation = self._propose_write(...)        # unchanged
else:
    observation = self._dispatch_action(...)      # execute now
    if cap.write and cap.tier == "log_and_undo" and observation.ok:
        self._record_log_and_undo(ctx, cap, decision, observation)
```

`_record_log_and_undo(ctx, cap, decision, observation)` creates an
`AssistantWriteIntent` **directly in `completed`** (skipping `proposed`/
`confirmed`, per the roadmap's state-machine note) with `payload=decision.args`
and `result={"undo": observation.data["undo"], ...display fields}`. Needs
`run_id` (`self._run.id`) and `step_index` (`ctx.step_index`), both available on
the agent — which is why this lives in the loop, mirroring `_propose_write`, not
in the pure action function.

**No `proposed` window (correctness).** `create_write_intent` currently always
opens an intent in `proposed`. If a log-and-undo intent existed in `proposed`
even briefly, the existing confirm endpoint would *re-execute the move* (a double
move). Two guards, both required:

- `db.create_write_intent` gains an optional `state="proposed"` (and accepts an
  optional `result`) so the log-and-undo recorder lands the row **atomically in
  `completed`** — there is never a confirmable `proposed` row for a log-and-undo
  write.
- `execute_write_intent` (the confirm path) additionally refuses any capability
  whose `tier != "confirm"`, so even a malformed/legacy intent can never be
  confirm-executed into a duplicate write. Defense in depth.

Durability note: the move is execute-then-record. If the process dies between the
move and the ledger write, the board's own `moved` event and the
`assistant_step` `observed` row still record that it happened; only the
one-click undo ledger is missing (the operator can still move it back manually).
Acceptable for a reversible low-risk write.

### 4. Generic undo — `undo_write_intent(intent_uuid)` in `agents/assistant_writes.py`

```
intent = db.get_write_intent(intent_uuid)
if intent is None: return ok=False "no such write intent"
if intent.state != "completed": return ok=False "intent is not undoable (state=…)"
undo = (intent.result or {}).get("undo")
if not undo: return ok=False "intent has no undo record"
cap = CAPABILITIES[AssistantActionName(undo["capability"])]
ctx = AssistantActionContext(journal_id=None, room_uuid=intent.room_uuid,
                             agent_uuid=intent.agent_uuid, step_index=intent.step_index)
obs = cap.action(ctx, dict(undo["payload"]))      # the inverse forward move
if obs.ok:
    db.set_write_intent_state(intent, "undone", result={..., "undone": True})
return obs
```

Undo is one-shot (`completed → undone`); the undo move is not itself ledgered
(it is dispatched directly, not through the loop's record path), so there is no
recursion and no redo. Redo-after-undo is out of scope.

### 5. Endpoint

In `webapp/chat_api.py`, alongside the existing confirm/reject routes:

```
POST /chat/api/assistant/write-intents/<uuid:intent_uuid>/undo
  → assistant_writes.undo_write_intent(intent_uuid)
  → jsonify({"ok": obs.ok, "text": obs.text, "data": obs.data})
```

The confirm/reject endpoints are unchanged. A polished undo *button* in the chat
UI is out of scope (the endpoint is the surface; chat already renders the trace).

## Authority stance (explicit, to avoid a contradiction)

`tools/kanban_dispatcher.py` enforces an `observe`/`work`/`shape` model where
moves are `shape` (human-only) — that model governs **autonomous worker agents**.
The assistant's `kanban_move` capability deliberately does **not** route through
that dispatcher; the assistant's capability registry is its own code-owned
authority boundary (Phase 4: "authority is code-owned"), and the safety for this
write is reversibility + trace, not the worker ceiling. The board's `moved`
events (forward and undo) keep the kanban audit log complete. A one-line note
will be added to `docs/kanban-design.md`'s permission section so the two models
do not read as contradictory.

## Testing (TDD, model-free)

- **db:** `kanban_get_task` returns the brief (incl. `columnUuid`) for an
  existing task and `None` otherwise.
- **capability:** `kanban_move` declares `write=True, tier="log_and_undo"`.
- **loop / writes:**
  - a `kanban_move` step executes immediately (task actually moved) and lands an
    `AssistantWriteIntent` in `completed` whose `result.undo` round-trips the
    original column;
  - the landed intent is **never** in `proposed` (created atomically `completed`);
  - `undo_write_intent` moves the task back and flips the intent to `undone`;
  - undo refuses a non-`completed` intent;
  - `execute_write_intent` refuses a non-`confirm`-tier capability (no double move);
  - a missing task or bad column yields `ok=False` and **no** ledger row.
- **endpoint:** `/undo` happy path and unknown-intent path.
- Determinism via the existing `scripted_decisions` fake-model seam; reuse the
  `worker_board` / `room` fixture styles from `agents/test_kanban_worker.py` and
  `agents/test_assistant_writes.py`.

## Out of scope (follow-ups)

- Other kanban writes (create / comment / complete / assign).
- Redo-after-undo.
- Retrofitting `remember` onto the new ledger (it keeps its ad-hoc undo for now).
- A polished undo button / richer write-ledger UI.
- **Superseded-move awareness.** Undo replays the stored inverse as a *forward*
  move back to the original column. If the task was moved again after the
  recorded move (A→B ledgered, then B→C by anyone), undoing replays "move to A"
  from wherever it now sits (C→A), not a strict inverse of the recorded
  transition. This is intentional for this tier — every result is reversible,
  column-validated, and leaves a `moved` audit event — but a future optimistic
  "only undo if still in column B" check could make undo a no-op when the task
  has since moved. Not built now.

## Done when

- The assistant can move a task during a run; the move is real and visible in the
  board's `moved` events.
- The move lands a `completed` `AssistantWriteIntent` carrying a working inverse.
- `POST …/undo` moves the task back and marks the intent `undone`.
- A bad move (missing task / wrong column) leaves no ledger row and reports the
  failure.
- Tests cover all of the above without a live model.
