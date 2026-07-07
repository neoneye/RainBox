# Cron — design (frontend + backend)

**Status:** **Built and running.** The `/cron` page persists a folder tree of jobs to Postgres, the supervisor loop fires due jobs on their schedule, five action types exist (`message`, `command`, `backup`, `memory_sync`, `script`), run outcomes/health/retries/global-pause are all live, and the assistant creates **one-shot reminder jobs** through the same tables. Requires running via `python main.py` (the scheduler and web app share one process). Page at `GET /cron`.
**Date:** 2026-07-07
**Source brief:** `plan.md` → "## Cronjob"
**UI scope:** **Desktop-first** (tablet acceptable). Small-phone layouts are a **non-goal** — the fixed-width tree + split view is tuned for wide viewports.

## The idea

A cron job is a **schedule** attached to **an action that already exists in this system** — post a chat message, run a workspace-confined command, dump the database, reconcile memory embeddings. The scheduler reuses the machinery we already have (the supervisor loop, the agent inbox, the chat page) rather than inventing a parallel one. Everything the brief asked for is built: folders that toggle whole groups on/off, "Run now" with a debug dry-run, a health panel (last success/error, next-3, recent runs), a cron-events chatroom, and a global pause.

## Where things live

| Piece | File |
|-------|------|
| Tables (`CronFolder`, `CronJob`, `CronRun`) | `db/models.py` |
| Tree load/validate/save, scheduler tick, firing, health, one-shot creation, seeds | `db/cron.py` (re-exported from the `db` facade) |
| HTTP endpoints | `webapp/cron_api.py` |
| Page shell + CSS | `webapp/cron_views.py` |
| Page logic (~1.6k lines of JS) | `static/cron.js`, served with an mtime `?v=` cache-buster |
| Scheduler host | `main.py` `supervisor_loop` |
| Tests | `webapp/test_cron_api.py`, `webapp/test_cron_views.py`, `webapp/test_cron_admin.py`, `db/test_cron_firing.py`, `db/test_cron_events.py`, `db/test_cron_backup.py` |

## Data model (as built)

Three tables, following the repo's SQLAlchemy-2.0 conventions (`docs/data-model.md`): `Mapped[...]` columns, a unique `uuid`, timezone-aware `created_at`/`updated_at`. Reference columns (`parent_uuid`, `folder_uuid`, `cron_uuid`) are **plain UUID columns — no DB foreign keys**; integrity is enforced in `validate_cron_tree` before any write, which keeps the bulk save free of delete-ordering/cascade issues.

```
cron_folder
  id, uuid, name, description,
  parent_uuid (nullable)          -- null = root-level folder (nesting)
  enabled (bool), position (int), created_at, updated_at
  Index cron_folder_children (parent_uuid, position)

cron_job
  id, uuid, name, enabled (default true),
  folder_uuid (nullable)          -- null = unfiled / root-level job
  cron_expr (text; '' = one-shot, see below),
  timezone ('localtime' | 'UTC', default 'localtime'),
  action_type ('message'|'command'|'backup'|'memory_sync'|'script', CHECK-constrained),
  target, message, command        -- only the relevant ones are used
  description,
  max_retries (int, 0 = off, capped at 10),
  origin_run_uuid, origin_step_uuid (nullable)  -- assistant provenance (reminders)
  next_run_at, last_fired_at (timestamptz, nullable),
  position (int), created_at, updated_at
  Index cron_job_in_folder (folder_uuid, position)

cron_run                          -- one row per firing → the "logs"
  id, uuid, cron_uuid,
  trigger ('scheduled' | 'manual' | 'retry'),
  debug (bool), fired_at,
  status ('pending'|'ok'|'error', CHECK-constrained),
  finished_at (nullable), error ('' when ok),
  journal_id (nullable uuid → journal)  -- full command output, written by the ws-shell agent
  created_at
```

There is no `cron_setting` table: global state lives in the settings registry (`db/settings.py`) — `cron.paused` — so it also shows up on `/settings`.

### Tree model

The left panel of `/cron` is a forest: folders nest to any depth, each job lives at the root or inside a folder. Two node kinds, each identified globally by uuid (display names are not unique). Nesting is one parent reference per node (`parent_uuid` / `folder_uuid`, `null` = root).

- **Ordering.** Sibling order is user-controlled and persisted via `position`. As built, `position` is a flat index into the whole per-kind list (`cron_save_tree` writes `position = i` over all folders, and separately over all jobs; loads are `ORDER BY position`). Sibling order is *derived* — the relative order of a parent's children within that global sequence. This round-trips correctly because the frontend preserves array order; true per-`(parent, kind)` scoping is only needed if a per-node reorder API arrives.
- **Effective-enabled inherits down.** A job is live only if it is enabled **and** every ancestor folder is enabled. Disabling a folder silently suppresses its whole subtree without touching descendants' own flags, so re-enabling restores the prior state (the launchd "unload a whole folder" behavior). Enforced identically in JS (`cronJobLive`/`cronFolderEnabled`) and in the tick (`_cron_job_effective_enabled`, cycle-guarded).
- **Acyclic.** The UI blocks a folder from being dropped into its own subtree, and the server re-checks on every save (walks the parent chain; self-parent and multi-node cycles are rejected).
- **Folder delete cascades** — the folder, every descendant folder, and every job in the subtree. Confirmation is a custom overlay: non-empty folders show the descendant count and require typing the folder name; a job or an empty folder is a one-click confirm.

## Action types

Five, dispatched by `fire_cron_job`. The New-job builder and Edit-action overlay offer **Message**, **Command**, and **Script**; `backup` and `memory_sync` are system types (seeded jobs) that the validator and firing path treat as first-class.

1. **`message`** — post to a chatroom. `target` is a **chatroom uuid** (rename-proof; the UI's Target control is a `<select>` of rooms fed by the tree payload's `chatrooms` list). A blank or unknown target falls back to the **cron room**. The message is authored by the fixed `cron` system user (`CRON_SYSTEM_UUID`) so the chat page + SSE render it like any other message.
2. **`command`** — enqueue to the **workspace-shell agent** (non-shell argv, no bash, workspace-confined — never a raw shell). The payload carries `room_uuid` (the cron room, where output/blocks post), `command_text`, `cron_run_uuid`, and `debug`. The fire returns with the run `pending`; the agent writes the real outcome back (see *Run outcomes*).
3. **`backup`** — in-process database dump (`backup.dump`), because the workspace-shell allowlist can't run `pg_dump`/`zstd` or write files. Destination resolution: the job's `command` field → the `backup.repo` setting → the `RAINBOX_BACKUP_REPO` env var. Age recipients come from the `backup.age_recipient` setting (falling back to env, fail-closed). With `backup.git_push` set, the encrypted file is committed+pushed to the backup repo — an upload failure is reported (`✖ upload failed`) but does **not** fail the fire, since the local backup already succeeded. Runs synchronously on the supervisor thread — fine for a local single-user DB.
4. **`memory_sync`** — in-process memory maintenance (`memory.embeddings.sync_memory_embeddings`): backfill embeddings for active claims and prune stale ones, keeping hybrid retrieval fresh between writes. Best-effort by construction — a missing embedder degrades to lexical-only and pruning still runs.
5. **`script`** — run an **operator's external program** (e.g. a personal repo's cron entry point). `command` is shlex-split argv with **no shell**; `argv[0]` must be an **absolute path to an existing executable file** (shebang + `chmod +x` — interpreter lookup via PATH is deliberately unsupported, so what runs is always explicit). Runs on a **daemon thread** with `cwd` = the script's directory and a `CRON_SCRIPT_TIMEOUT` (10 min) wall-clock cap, so a slow fetch/render never blocks the supervisor loop; env/venv concerns live inside the script. Captured stdout+stderr is posted to the cron room (clipped to the last `CRON_SCRIPT_OUTPUT_CLIP` chars) and the outcome lands via the same pending→`cron_record_run_outcome` contract as `command` fires, so the pending sweep still backstops a killed process. This is operator tooling created on the /cron page: the workspace-shell policy that chat-issued commands go through is untouched, and per the repo's local-security stance it is a guardrail, not a sandbox.

   Script jobs also get a **"Check health"** button on Job details (`POST /cron/api/jobs/<uuid>/check_health` → `cron_script_health_check`): it runs the job's argv plus `--health` **synchronously** (`CRON_HEALTH_CHECK_TIMEOUT`, 60 s) and renders the output + ✔/✖ verdict inline on the page. The script owns the flag's semantics (print one line per check, exit 0 = healthy). It is a diagnostic probe, not a fire — **no `cron_run` row is recorded** and nothing posts to the cron room.

### One-shot jobs (reminders)

An **empty `cron_expr` is a one-shot**: no recurrence, fires once at its pre-set `next_run_at`, and the tick then **retires it** (`enabled = False`) so it doesn't linger enabled-but-dead. The tree validator accepts the empty expression (otherwise re-saving the tree — e.g. toggling Active on a reminder — would be rejected as "must have 5 fields"); a re-save keeps the pre-set `next_run_at` because the schedule didn't change.

These are created by the **assistant's `set_reminder` action** (`agents/assistant.py` → `db.cron_create_one_shot_message`), a confirm-tier write with a dry-run preview ("Would remind you at …"). A naive `when` is interpreted as the operator's **local wall-clock time** (DST-correct), not UTC. The job posts `⏰ Reminder: …` to the room the assistant was asked in. Provenance is linked both ways: the job stores `origin_run_uuid`/`origin_step_uuid`, surfaced on Job details as an **Origin** section ("created by assistant — View step ↗" → `/assistant?id=<run>#step-<step>`), and the assistant's chat card links back with "View reminder ↗" → `/cron?id=<job uuid>`.

### Drafts

Validation deliberately allows empty `command`/`message` because the page autosaves *drafts* (a job created and filled in gradually). The scheduler compensates: `cron_job_is_draft` (empty command for command jobs, empty message for message jobs; backups and memory_sync are never drafts) makes the tick roll a due draft's `next_run_at` forward **silently** — no run row, no event spam, and no stale-slot fire the moment the action is filled in. The UI badges such jobs **Draft** in the Active column and notes it on the Job-details Action summary. A *manual* Run-now of a draft still reports the error — an explicit click deserves feedback.

## Scheduler

The supervisor loop in `main.py` is the heartbeat — no extra process. It calls `db.cron_tick()` throttled to `CRON_TICK_INTERVAL` (5 s; cron granularity is 1 min), self-guarded so a cron bug can't take down the supervisor thread (exception → log + rollback). When fully idle the loop's `select()` timeout backs off from 1 s to 5 s (`IDLE_TICK_TIMEOUT`) to stop at-rest Postgres polling; the cron pass counts as "found work" when it fires, keeping the loop responsive while jobs run.

One `cron_tick` pass:

1. **Pending sweep.** A `pending` run older than `CRON_RUN_PENDING_TIMEOUT` (15 min) is dead — the supervisor SIGKILLs hung agents after ~60 s, so a completion that late never arrives — and is swept to `error`. This bounds the in-flight guard so it can't deadlock on dead runs.
2. **Global pause.** If `cron.paused` is set, commit the sweep and fire nothing. Schedules don't advance while paused, so resume behaves like wake-from-sleep: each due job catches up with at most one fire.
3. For each enabled job whose ancestors are all enabled:
   - **No `next_run_at` yet** → schedule it (compute from the expression), don't fire on first sight.
   - **Due** (`next_run_at <= now`): a **draft** rolls forward silently; a job whose **previous run is still in flight** (latest run `pending`) skips the slot — `next_run_at` advances, no run row, a `⏭ "X" skipped: previous run still in flight` note in the cron room (no runaway pile-ups). Otherwise **fire** (`trigger='scheduled'`) and advance `next_run_at` to the next *future* slot — missed slots are **not replayed** (catch-up = fire at most once, the launchd behavior). A fired one-shot (empty expr) is retired instead of rescheduled.
   - **Not due but recently failed** → **retry** (below).

**Computing "due":** `next_run_at` is stored as a tz-aware UTC instant, computed by `croniter` against the job's `timezone` choice — `'UTC'`, or `'localtime'` = the host's local tz *at that moment* (so a "09:00 local" job follows the machine when it travels). An unparseable expression yields `None` — a bad row just never fires rather than crashing the scheduler. The UI grammar only produces `*`, `*/N`, and specific values, but the backend parses general 5-field cron so hand-edited rows still work.

**Retries.** Per-job `max_retries` (0 = off, capped at 10; the "Retry on failure" select on the builder and Edit-action overlay). A run that resolved to `error` within `CRON_RETRY_WINDOW` (10 min — so restarts never refire ancient failures) refires as `trigger='retry'` between slots, until the *trailing chain* of retry-trigger runs reaches the budget. A success or the next scheduled fire resets the chain; a pending run blocks it; drafts are excluded.

## Firing and run outcomes

`fire_cron_job(job, trigger, debug)` records a `CronRun`, sets `last_fired_at`, performs the action, and posts a one-line event to the **cron room**. It never advances `next_run_at` (the tick owns that for scheduled fires; manual fires don't touch it).

- **In-process actions** (`message`, `backup`, `memory_sync`) resolve the run's outcome synchronously — `ok`, or `error` + the exception text (any firing failure also posts `✖ "X" failed to fire: …`).
- **Commands** return `pending`; when the workspace-shell agent finishes it calls `cron_record_run_outcome(cron_run_uuid, status, error, journal_id)` on every exit path — `ok` on exit 0, `error` on non-zero exit / blocked command / timeout — linking the journal row (a uuid; journal ids are uuids app-wide) that holds the full output, and posting the consolidated verdict line: `✔ "X" completed (trigger)` / `✖ "X" failed (trigger): exit code N`. The room therefore reads start → output → verdict.

**The cron room** is a dedicated, fixed-uuid `Chatroom` (`CRON_ROOM_UUID`) whose author is a fixed agent-type `chat_user` (`CRON_SYSTEM_UUID`, name `cron`) deliberately **not** in `agent_config`, so the supervisor never runs it — it only authors event lines (`db.post_cron_event`). Event vocabulary: `▶` fire/dry-run, `✔`/`✖` async verdict, `⏭` skip, `↑` backup upload.

### Manual "Run now" + "Run debug"

Two Job-details buttons, both firing immediately, independent of the schedule, neither advancing `next_run_at`, both allowed during global pause and both bypassing the in-flight guard (an explicit click):

- **Run now** — a real fire (`trigger='manual'`). The page then watches the outcome via the health endpoint (bounded polling, ~15 s) and shows the verdict inline; past the window it says "still running — see the cron chatroom".
- **Run debug** (`?debug=1`) — a **dry-run** that reports what the fire *would* do without doing it: a message posts `[debug-style] would send "…" → #room` (nothing sent); a backup resolves and reports the destination + recipient count (nothing dumped); a memory_sync reports what it would reconcile; a command is enqueued with `debug` so the workspace-shell agent — which owns the policy — validates and echoes the argv without executing. The run row records `debug=true` (shown as `· debug` in the health table) and resolves `ok`/`error` like a real fire, so a dry-run that would be blocked tells you so.

## HTTP API

JSON, same-origin, in `webapp/cron_api.py`. uuids are the identifiers — never names.

- **`GET /cron/api/tree`** → `{ folders, jobs, chatrooms, version, paused }`. Folders/jobs use the frontend's field names (folder `id`/`parentId`/`description`, job `uuid`/`folderId`/`cron`/`type`/`timezone`/`maxRetries`), ordered by `position` (ordering is implicit in array order). Jobs also carry read-only fields the page renders but never sends back: `created_at`/`updated_at`, `last_run` (the latest run's outcome, via a single `DISTINCT ON` lookup — the lists' health column), `next_run_at` (the scheduler owns it — the next-run column), and `origin_step_link`. `chatrooms` feeds the message-target picker; `paused` the Pause/Resume toggle.
- **`PUT /cron/api/tree`** — bulk whole-tree save (debounced 250 ms on the page), an **upsert by uuid**: matched rows update in place (preserving `created_at`; SQLAlchemy dirty-checking means unchanged rows emit no `UPDATE`), new rows insert, rows absent from the payload are deleted. Two guards against the whole-tree-replace foot-gun, both enforced by the endpoint (opt-in keyword args on `cron_save_tree`, so internal/test callers can skip them):
  - **`version`** (optimistic concurrency) — the token from GET, derived from the *user-managed* fields only (scheduler bookkeeping — `next_run_at`/`last_fired_at`/`updated_at` — is excluded, so background firing never invalidates an open page). Missing → 400; stale → **409** + the current token, before any mutation. On 409 the page re-hydrates, resets a dangling selection, and shows a toast instead of clobbering the other writer. A successful PUT returns the new token; the page serializes its debounced PUTs so it can't 409 against its own previous save; a failed initial hydrate leaves the token null, so a PUT of the resulting empty state is refused rather than wiping the real tree.
  - **`deletes`** (mass-delete tripwire) — the page declares how many deletions it knowingly performed (single node, or a folder-cascade's subtree count); a save that would delete more rows than declared is refused with 400 (a truncated payload from a frontend bug, not an edit).
- **`POST /cron/api/jobs/<uuid>/run`** (`?debug=1`) — Run now / Run debug.
- **`GET /cron/api/jobs/<uuid>/health`** — ok/error/pending counts, last success/error timestamps, the **next 3 upcoming** fire times (pure `croniter` computation, nothing stored), and the last 20 runs (fired_at · trigger · debug · status · error).
- **`POST /cron/api/pause`** / **`/resume`** — flip `cron.paused`. Never touches per-job/folder `enabled` flags, so resuming restores the exact prior state.

Per-node CRUD/reorder endpoints (`PATCH /cron/api/jobs/<uuid>` etc.) remain the documented long-term refinement — the bulk PUT with its two guards has proven sufficient for a single-operator app, and every mutation path on the page already funnels through one debounced save.

### Validation

`db.validate_cron_tree` runs at the top of `cron_save_tree` — so *every* writer (the endpoint today, future MCP/agent editors) is covered — and raises `CronTreeError` **before any DB mutation**; the endpoint maps it to 400. Checks: payload shape (lists of objects); uuid shape + **global uniqueness across folders and jobs** (a node is identified globally by uuid — `/cron?id=<uuid>` — so a cross-kind collision would make the deep link ambiguous; uuids are normalized so case/format variants collide); reference integrity (parents/folders must exist in the same payload); **acyclic** folder nesting; `type` ∈ the four action types; a message `target` must be empty or a uuid (so a stale room *name* can't be saved); `timezone` ∈ {`localtime`, `UTC`}; `maxRetries` an int 0–10; cron expression **empty (one-shot) or exactly 5 fields** of UI-grammar characters (`[0-9*/,-]`) — a shape check; croniter is the real parser.

## Frontend

The page shell + CSS live in `webapp/cron_views.py`; all logic in `static/cron.js`. State is two browser arrays (`cronFolders`/`cronRowsState`) hydrated from `GET /cron/api/tree` and saved back whole (debounced) after every mutation. Layout matches `/chat` and `/modelgroups`: full-height split, independently scrolling panes. The tree follows the app-wide left-panel conventions (`docs/ui-left-panel-tree.md`): nested `<ul>`s with a left-border guide line, tinted + **bold** selection (exactly one node highlighted at a time, folder and job alike), outlined action buttons.

**Left panel — folder tree.** An `All jobs` node; then **+ Folder** / **+ Job** / **Pause all** actions; then the nested tree. Single-node drag-and-drop: reorder siblings (drop *between* — top/bottom half picks before/after), nest a folder (drop on its *middle third*), move jobs between folders, and a "Move to top level" zone that appears only while dragging — dropping selects the node, self-drops are no-ops, "All jobs" is not a drop target, and a folder can't be dropped into its own subtree. Each item has a 3-dot kebab, visible on the selected node: **Duplicate** (deep-clones a job, or a folder's whole subtree, with fresh uuids — the copy starts **inactive** and lands right after the original), **Copy job id / Copy folder id** (uuid → clipboard, with a toast), **Delete** (guarded, see *Tree model*); folders additionally get **New job**. Disabled / ancestor-disabled nodes are dimmed.

**Right panel — titled by selection** (*All jobs* / *Folder details* / *Job details*):

- The **list** (All jobs / Folder details) shows folders *and* jobs as rows in depth-first tree order, the name column indented per nesting level and ellipsis-truncated with a full-path tooltip; Folder details shows the folder's *whole subtree*. Columns: **Active** (`Active`/`Inactive`, or a **Draft** badge), **uuid** (short prefix, full on hover), **name**, **schedule** (cron string, never wrapped, with the plain-English `cronDescribe()` explanation and the time-zone label beneath — so "06:45" is unambiguous), **next run** (`—` for disabled/unscheduled, `paused` during global pause, a muted timestamp with a "will be skipped" tooltip for drafts), **health** (the job's latest run at a glance: ✓ ok / ✖ error / … running, with timestamp · trigger · error on hover), **command** (the action summary: `msg → #room: …` / `cmd: …` / `backup → …`), **description**, and a **Details** link. Folder rows show the folder icon and description, leave the schedule columns blank. Non-live rows are grayed.
- **Folder / Job details** share a top **rename** field, an **Active** toggle with a plain-English effect note (folders cascade, with a "(a parent folder is deactivated)" hint), and **Created / Modified** timestamps (local time, full date + tz on hover). Folder details adds a read-only **Description** with an Edit button. Job details shows **Run** (Run now / Run debug + inline verdict), read-only **Schedule** / **Action** / **Description** summaries each with an Edit button, an **Origin** section (only for assistant-created jobs — "View step ↗"), and the **Health** panel (counts, last success/error, next-3 upcoming, recent-runs mini table with per-status colors). Re-filing a job into another folder is done by dragging it in the tree; there is deliberately no "New job" button on the list pages (creation lives in the tree).
- **Modals.** All dialogs follow the app-wide modal standard (`docs/ui-modals.md`, `/static/ui-modal.css`): one shared backdrop, each card a *sibling* of it, dismissed on backdrop click only when clean (`cronDismissIfClean` — a dirty form isn't lost to a stray click). **No native `prompt`/`confirm`/`alert` anywhere** — a browser can offer to permanently suppress native dialogs, which would silently break the flow. The cards: the **New-job builder** (create-only, wider than the standard card: name, description, five schedule dropdowns + live cron string + explanation, time-zone picker, Message/Command toggle with a chatroom `<select>` target, retry select, folder picker), **Edit schedule** and **Edit action** (per-facet editing with `es-`/`ea-` prefixed controls so they never collide with the builder; **Save disabled until the data differs** from the job's original, re-disabling on revert), **Edit description**, **New folder** (Create disabled until named), and **Delete confirm** (typed-name gate for non-empty folders).
- **Deep-linking.** `?id=<uuid>` selects that folder/job on load (an admin row or an assistant "View reminder ↗" card jumps straight to the node); the current selection is mirrored back into the URL via `history.replaceState`. Unknown id falls back to *All jobs*.
- **Pause.** The tree's **Pause all / Resume all** button plus a banner across the right pane while paused.
- **Toast.** A transient corner toast for save-status messages (conflict reload, refused save, clipboard results) — the in-page surface that replaces native alerts.

## Seeded defaults

`seed_cron_defaults()` is idempotent (fixed, random-looking uuids so their short forms are distinguishable in the UI; existing rows are never overwritten or re-enabled, so user edits stick):

- **System** folder — "System maintenance jobs."
- **Database backup** — `backup`, daily 03:30 local, seeded **disabled** with no destination; enabling it requires a destination (job command / `backup.repo` / `RAINBOX_BACKUP_REPO`) and an age recipient.
- **Memory embedding sync** — `memory_sync`, daily 03:15 local, seeded **enabled** (unlike backup it needs no destination or secret and is safe, idempotent maintenance).

`seed_chat_defaults()` seeds the `cron` chatroom + system user (see *Firing*).

## Flask-Admin

The three tables are registered under an **Admin → Cron** category (`CronFolderView`, `CronJobView`, `CronRunView` in `webapp/core.py`) as a low-level inspection surface alongside the curated page: uuid columns truncated to a 6-char `<code>` prefix with the full uuid on hover; folder-reference columns render the short uuid with the folder's name beneath (looked up by uuid — no FK); datetimes render compactly (`2026-06-05 23:57:30 +02:00`); and a virtual **"Cron page"** column links `inspect ↗` to `/cron?id=<uuid>` — the read side of the page's deep-link.

## How this maps onto existing code

| Need | Reuse |
|------|-------|
| Run the scheduler | the `supervisor_loop` tick in `main.py` (`db.cron_tick()` every ~5 s) |
| Execute a command safely | the workspace-shell agent — argv, no bash, workspace-confined |
| Post to a chatroom | `post_chat_message` as the `cron` system user (router/chat agents react) |
| Full command output | the `Journal` row linked from `cron_run.journal_id` |
| Process isolation + hang-kill | the supervisor (60 s heartbeat → SIGKILL) |
| Cron-events feed | the dedicated `cron` `Chatroom` (the `/chat` page + SSE render it live) |
| Next run / next-3 upcoming | `croniter(expr, now).get_next()` per the job's timezone |
| Global pause | the `cron.paused` setting, checked at the top of the tick |
| Reminders | the assistant's `set_reminder` → `cron_create_one_shot_message` |

## Deliberate tradeoffs

- **Bulk whole-tree PUT, not per-node PATCH.** Simpler and lower-risk for a single-operator app; the upsert-by-uuid save plus the version token and delete tripwire remove the data-loss failure modes that motivated the per-node design. Per-node endpoints (and per-parent `position` scoping) remain the refinement target if concurrent editors ever become real.
- **No DB foreign keys.** Integrity is genuinely enforced in `validate_cron_tree` (uuid shape/uniqueness, dangling refs, cycles) before any write; the bulk save stays free of delete-ordering concerns.
- **Two-value timezone model** (`localtime`/`UTC`) — covers a single-operator local app.
- **Fire-at-most-once catch-up** — the right default for messages and backups alike; per-job replay policy can wait for a real need.
- **Lenient action validation** (empty command/message allowed) — required by draft autosave; the scheduler's draft skip closes the loop.
- **In-process backup/memory_sync on the supervisor thread** — synchronous is fine at this scale; revisit with a worker if dumps grow long.

## Open questions

- **Agent-inbox message targeting.** Message jobs post to chatrooms; delegating directly into an agent's inbox (`enqueue(agent_uuid, …)` — the brief's "placing a task in their inbox") is unbuilt. In practice, posting to a room an agent watches covers the need.
- **Named IANA timezones.** `Europe/Copenhagen`-style zones beyond the local/UTC pair, and DST edge cases (a `croniter`/tz concern).
- **Multi-user ownership.** plan.md wants multi-user; `user_id` on `cron_folder`/`cron_job` (and scoping every query + the tick by it) is still deferred.
- **Folder = project binding.** Should a folder pin a workspace/repo path for command confinement (PlanExe folder → PlanExe repo), making it both an on/off group *and* an execution context?
- **Cron-room noise.** Every fire posts an event line. Fine at current volume; a high-frequency job would want a failures-only or per-folder-room policy.
- **Global pause vs in-flight.** Pause stops *new* fires; runs already in flight are left to finish.
