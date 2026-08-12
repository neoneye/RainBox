# S1 — Embedding-sync cron trigger — design (2026-06-20)

**Status:** approved-direction, small. Implements card **S1** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md).
Closes the one caveat left by the embedding-freshness work: nothing runs
`sync_memory_embeddings` on a schedule, so claims written/activated between manual
runs fall back to lexical-only retrieval.

## Goal

Run `memory.embeddings.sync_memory_embeddings()` (backfill active claims + prune
stale embeddings) automatically on a schedule, with no manual call, and make the
`(embedded, pruned)` outcome visible.

## Decision (resolved)

A **first-class in-process cron action type `memory_sync`**, resolved in
`fire_cron_job` exactly like the existing `backup` action (`db/cron.py`) — it is
an in-process maintenance task the workspace-shell allowlist cannot do, a sibling
of `backup`, **not** routed through the `command`/workspace-shell action. A fixed
seeded job in the **System** cron folder runs it daily.

Rejected: a `command`-type job (can't run in-process maintenance; wrong layer),
and an admin-button-only trigger (doesn't meet "no manual call" — could be added
later as polish).

## Changes

1. **`db/cron.py`**
   - Add `"memory_sync"` to `CRON_ACTION_TYPES` (used by the tree validator and
     upsert at lines 289/382).
   - Add `MEMORY_SYNC_CRON_JOB_UUID` constant (fixed uuid, next to
     `BACKUP_CRON_JOB_UUID`).
   - `fire_cron_job`: add a `memory_sync` branch (in the in-process group, before
     the `else: # message`):
     - `debug` (dry-run): post a `▶ dry-run … would backfill … and prune …`
       event; `outcome = "ok"`; mutate nothing.
     - else: `embedded, pruned = sync_memory_embeddings()`; post
       `▶ memory_sync "<name>" (<trigger>): <embedded> embedded, <pruned> pruned`;
       `outcome = "ok"`.
   - `seed_cron_defaults`: idempotently add the `memory_sync` job — **enabled**,
     `cron_expr="15 3 * * *"`, `timezone="localtime"`, in the System folder. It
     needs no destination/secret (unlike backup), so it is safe to seed enabled
     and is never a draft (`cron_job_is_draft` already returns `False` for any
     non-command/message action — no change needed).

2. **`db/models.py`** — widen the `cron_job_action_type_check` CHECK to
   `('message','command','backup','memory_sync')`.

3. **`db/__init__.py`** — update the existing in-place CHECK-widen for
   `cron_job_action_type_check` (the block that already admitted `backup`) to
   include `memory_sync`, so existing databases migrate on `init_db`.

No new columns: `(embedded, pruned)` is recorded in the cron event log via
`post_cron_event` (the same visible surface `backup` uses for its byte count).
`CronRun` keeps its `status`/`error` only.

## Tests (TDD, model-free)

In `db/test_cron_firing.py` (mirror its fixture/teardown style; inject a fake
embedder by monkeypatching `memory.embeddings._default_embed`):

1. **Fires end to end:** a `memory_sync` job + one active `MemoryClaim` → after
   `fire_cron_job`, the claim has an embedding and the `CronRun.status == "ok"`.
2. **Dry-run mutates nothing:** `fire_cron_job(job, debug=True)` → no embedding
   created, `status == "ok"`.
3. **Action type accepted:** a `CronJob(action_type="memory_sync")` inserts
   without violating the CHECK (guards the model + init_db widen).
4. **Seeded job:** after `seed_cron_defaults()` (runs in `init_db`), the
   `MEMORY_SYNC_CRON_JOB_UUID` job exists, is enabled, and has
   `action_type="memory_sync"`; calling it twice doesn't duplicate.

## Done when

- A scheduled `cron_tick` firing of the seeded job embeds newly-active claims and
  prunes stale embeddings with no manual call.
- The `(embedded, pruned)` counts are visible in the cron event log.
- Tests 1-4 pass without a live LLM/Ollama.

## Out of scope

- An admin button / dashboard control for an on-demand sync (later polish).
- Per-run structured `(embedded, pruned)` storage on `CronRun` (event log suffices).
- Tuning cadence adaptively or chunking very large backfills.
