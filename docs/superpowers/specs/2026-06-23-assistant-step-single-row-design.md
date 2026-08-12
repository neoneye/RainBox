# Assistant step trace: single mutable row + `step_uuid` write-intent FK — design (2026-06-23)

**Status:** ✅ implemented (branch `refactor/assistant-step-single-row`; suite
green, 1220 passed). A follow-up refactor (filed under v3's **S12** grab-bag —
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md)).
Not a new capability: it reshapes the assistant **trace** data model and gives
`assistant_write_intent` a real foreign key to the step that produced it.

## Problem

`assistant_step` is append-only: one logical loop step emits **several rows**
(`planned → running → observed|failed`, plus `final`/`control`), all sharing
`(run_id, step_index)` with **no uniqueness constraint** (`db/models.py:980`).
Consequences:

- `assistant_write_intent` can't reference *the step* by a single uuid — every
  step row has its own `uuid`, but none of them **is** "the step." So the intent
  carries the composite **soft pointer** `(run_id, step_index)` (`run_id` a real
  FK, `step_index` a bare int) instead of a clean FK. This is the confusion the
  admin "Step index" column creates.
- `reason`/`action`/`args` are immutable for a step yet get **rewritten into
  every transition row** (the loop passes `decision` to each `_record_step`
  call) — pure duplication.

**Key finding that makes this safe:** the intermediate rows are **write-only**.
The only operator-visible trace is the `debug-assistant` chat message, and
`append_assistant_step` posts it **only on the terminal transition**
(`observed`/`failed`/`final` — `db/assistant.py:90`, docstring: *"Anchored at the
terminal phase … so the observation already exists"*). The other renderer
(`webapp/core.py:359-363`) branches only on `observed`/`failed`/`final`. Nothing
reads `planned` or `running` as a distinct state. The system **already treats the
terminal phase as "the step."**

## Decisions (made, with rationale)

- **One mutable row per logical step.** A normal action step is `INSERT(running)`
  then `UPDATE` in place to its terminal state — not two appended rows. Keeps the
  step's identity stable (one `uuid` for its whole life) so a FK can point at it.
  The operator-visible trace is unchanged because it was always terminal-anchored.
- **`(run_id, step_index)` becomes genuinely one-to-one** for new runs, **but no
  DB-level unique constraint is added.** The single-row invariant is enforced in
  code (open-then-settle keyed by the step's `uuid`). A unique index was
  considered and dropped: existing DBs (and the accumulating `rainbox_claude`
  test DB) already hold legacy multi-row steps that would violate it, so it would
  need a data-dependent guarded migration and couldn't be reliably tested. Left
  as a future option once legacy traces age out. (Original wording kept below for
  provenance, superseded by this.)
- **`assistant_write_intent.step_uuid` (UUID, nullable, FK →
  `assistant_step.uuid`, `ON DELETE SET NULL`)** is the **sole** pointer to the
  producing step. `assistant_step.uuid` is already `unique=True`, so the FK is
  valid. `run_id` stays (the real cascade FK). **`step_index` is dropped** from
  the intent: with the step now a single addressable row, `step_index` was pure
  redundancy (derivable via the FK as `step_uuid → step.step_index`), and this
  is a single-operator install with no legacy rows worth preserving — so the
  denormalized ordinal is removed rather than kept. The operator-triggered
  confirm/undo re-dispatch (which has no loop step) passes `step_index=0` into
  the *action context* (still used for skill-candidate provenance) and carries
  `step_uuid` from the intent.
- **Keep the column name `phase` and its vocabulary** (`planned`/`running`/
  `observed`/`failed`/`final`/`control`). No column rename (avoids churning the
  `chat_api` field, the admin, and the CHECK constraint). Its *meaning* shifts
  from "per-transition phase" to "current state," documented in the model
  docstring. `planned` is retired from the write path (see below) but stays in
  the CHECK constraint so legacy rows remain valid.
- **Drop the standalone `planned` insert.** The loop currently records `planned`
  (`agents/assistant.py:1293`) *and then* `running` (`:1328`) before dispatch —
  two pre-action rows, neither consumed. Collapse to a single `INSERT(running)`
  right after the decision is validated. This preserves **crash visibility** (a
  step that hangs/crashes is left as a `running` row with `action`/`reason`/`args`
  on it) while halving pre-action writes.
- **Terminal-only steps are a single insert** (no prior `running`): plan/validation
  failures (`:1298`), the `final` reply step (`:1309`), and `control` events
  (`:1718`). They post their `debug-assistant` chat row on that one insert, exactly
  as today.
- **No backfill of legacy rows.** Existing multi-row steps and existing intents
  (with `step_uuid` NULL) are left as historical residue. The read path already
  tolerates extra rows (orders by `id`/`created_at`); the visible chat trace for
  old runs lives in chat messages and is untouched. Backfilling `step_uuid` from
  the terminal legacy row is possible but low-value and out of scope.

## Schema changes (`db/models.py`)

1. `AssistantWriteIntent`: add `step_uuid` and **drop `step_index`**:
   ```python
   step_uuid: Mapped[UUID | None] = mapped_column(
       ForeignKey("assistant_step.uuid", ondelete="SET NULL"), index=True
   )
   ```
   `step_uuid` is the sole step pointer; `run_id` stays as the cascade FK.
   Migration drops `step_index` (guarded by `_column_exists`, lock-safe) after
   `step_uuid` is added.
2. `AssistantStep`: **keep** the existing `assistant_step_by_run` ordering index
   on `(run_id, step_index, id)`. No unique constraint (see Decisions). Update the
   docstring: a step is now **one mutable row** (`running → observed|failed`),
   code-enforced, no longer append-only per transition; `failed`-at-plan /
   `final` / `control` are single-insert.

## Migration (`db/init_db`, the project's create_all + idempotent-block pattern)

`create_all()` builds the new column/indexes on fresh DBs. For existing DBs, in
`init_db` after `create_all()` (mirroring the `_add_column_if_missing` blocks):

- `_add_column_if_missing("assistant_write_intent", "step_uuid", "step_uuid UUID")`
  — a plain additive nullable column (same pattern/lock-avoidance as the existing
  `model_config` additions). The model-level `ForeignKey` gives fresh DBs the
  DB-level constraint via `create_all`; migrated DBs get the column without the FK
  constraint (consistent with how this project's other ad-hoc additions work) —
  the ORM relationship works regardless. No unique index (see Decisions).

(No data is deleted or rewritten. Old runs keep their multi-row trace; their
intents keep `step_uuid` NULL.)

## Code changes

**`db/assistant.py`** — split the one append helper into an open/settle pair:

- `open_assistant_step(*, run_id, step_index, decision, model_*) -> AssistantStep`
  — `INSERT` at `phase="running"` with `action`/`reason`/`args`. Commits, returns
  the row (so callers get `step.uuid`). **No** chat message.
- `settle_assistant_step(step, *, phase, observation_preview=None, error=None)`
  — `UPDATE` the same row to its terminal `phase` and outcome fields, then post
  the terminal-anchored `debug-assistant` chat row (the existing block at
  `db/assistant.py:90-109`, moved here unchanged in content).
- `record_terminal_step(*, run_id, step_index, phase, …)` — the single-insert path
  for `failed`-at-plan / `final` / `control`: insert at terminal `phase` **and**
  post the chat row in one shot (today's `append_assistant_step` behaviour, kept
  for these cases).

**`agents/assistant.py`** — the loop (`handle`, `_record_step`):

- After a valid decision: `step = open_…(running)`; thread `step.uuid` onto
  `AssistantActionContext` (new `step_uuid` field alongside `step_index`).
- Drop the separate `planned` record (`:1293`).
- After dispatch: `settle_…(step, phase="observed"|"failed", observation_preview=…,
  error=…)` instead of the second append (`:1366`).
- Plan/validation failure (`:1298`), `final` (`:1309`), and `_record_control`
  (`:1718`) call `record_terminal_step`.
- `_propose_write` / `_record_log_and_undo` pass `step_uuid=ctx.step_uuid` into
  `db.create_write_intent`; the in-process `self._steps` mirror records one
  entry per step that mutates in place (used by fast assertions).

**`db/assistant.py:create_write_intent`** — add `step_uuid: UUID | None = None`
param, set it on the row (keep `step_index` as before).

**`webapp/chat_api.py`** — the run-detail endpoint (`:309-322`) gains `step_uuid`
in each step dict if useful; otherwise unchanged (it already returns `phase`).
No frontend change required — old and new runs both render from their terminal
state.

## Tests (model-free, the suite stays LLM-free)

Update existing: `db/test_assistant_trace.py`, `db/test_assistant_write_intent.py`,
`agents/test_assistant_trace.py`, and any `self._steps`/phase-count assertions
that assumed multiple rows per step.

New assertions:
- A normal action step produces **exactly one** `assistant_step` row whose `phase`
  goes `running → observed` (or `→ failed`) via update, not two rows.
- `assistant_write_intent.step_uuid` equals the producing step's `uuid`, and that
  step row exists.
- The `debug-assistant` chat row is posted **exactly once**, on settle (not on
  open).
- Crash visibility: a step opened but never settled remains a single `running`
  row carrying `action`/`reason`/`args`.
- `final`, plan-failure, and `control` each produce a single row + (for the first
  two) one chat row.
- The partial-unique-index guard: with no legacy duplicates the unique index is
  created and a duplicate `(run_id, step_index)` non-control insert raises; with
  seeded legacy duplicates the migration skips the unique index without error.

## Out of scope

- Backfilling `step_uuid` on legacy intents / collapsing legacy multi-row steps.
- Renaming `phase` → `state` (cosmetic; would churn the API/admin field name).
- Any change to the write-intent state machine, confirm/undo endpoints, or the
  capability registry.
- The write-intent **approval UI** (separate S7 work) — this spec only makes the
  intent→step link clean; it does not add a view.

## Acceptance

A new assistant run writes **one row per step** (mutated in place), each
write-intent points at its step via `step_uuid`, the operator-visible trace is
byte-for-byte what it was (terminal-anchored), legacy runs still render, and the
suite is green with the new single-row assertions. The admin "Step index" column
is now backed by a one-to-one row, and "which step produced this write" is a uuid
FK join rather than a composite soft pointer.
