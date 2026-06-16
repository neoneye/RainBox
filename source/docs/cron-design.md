# Cron — design ideas (frontend + backend)

**Status:** **Persistence + firing built & merged.** The `/cron` page loads/saves the folder tree + jobs to Postgres (`GET`/`PUT /cron/api/tree`), deep-links via `?id=<uuid>`, and the cron tables are in Flask-Admin. The **scheduler now fires jobs**: the supervisor loop runs a cron tick (~5s) that fires due jobs on their schedule (timezone- and folder-cascade-aware), and **`POST /cron/api/jobs/<uuid>/run`** ("Run now") fires on demand — both record a `cron_run`, post a line to the **`cron`** chatroom, run commands via the workspace-shell agent (output/blocks post to that room), and post message jobs to their target room. **Built 2026-06-10** (see *Review findings*): run-outcome tracking on `cron_run`, the Job-details **Health** panel (counts, last success/error, next-3, recent runs), **global pause**, draft-job skipping, and the whole-tree-PUT guards. **Also built 2026-06-10 (later the same day):** per-job **retries** (`max_retries`, `trigger='retry'`), the **Run debug dry-run**, and the page JS extracted to **static/cron.js**. **Still not built:** the per-node PATCH API. Requires running via `python main.py` (the supervisor loop and web app share one process). Page at `GET /cron`.
**Date:** 2026-06-05 (last updated 2026-06-10 — added *Review findings*, a full-code-read assessment with prioritized gaps)
**Source brief:** `plan.md` → "## Cronjob"
**UI scope:** **Desktop-first** (tablet acceptable). Small-phone layouts are a **non-goal** — the fixed-width tree + split view is tuned for wide viewports, and narrow-screen responsiveness is intentionally not pursued.

## What the brief asks for

From `plan.md` (the brief has since grown — newer asks marked **NEW**):

- **Create the backend.** Schedule timers and recurring jobs; the scheduler **wakes on a heartbeat** and delegates work to agents by placing a task in their inbox.
- **Group cronjobs in folders**, toggling a whole folder on/off. Examples: *PlanExe* (check PRs, token usage, Railway stats), *My Life* (calendar, email).
- A **"Run now" / Execute** button that fires the command with a *debug* parameter, to verify it works.
- A **"View logs" / health** panel: when a job last ran, success or failure, and when it runs next.
- **NEW — a cron-events chatroom** so you can see what's going on (firings posted as messages).
- **NEW — a global pause** button for all cronjobs.
- **NEW — Job-details health metrics:** the **next 3 upcoming run** timestamps, plus **success count, error count, last success, last error, retries, status, next**.
- The brief also flags *"editing a cronjob is messy"* — addressed by the split-view detail UI: a Job-details page with read-only summaries and small **Edit schedule / Edit action** overlays, rather than one big form.

The throughline: a cron job is just a **schedule** attached to **an action that already exists in this system** — enqueue a message to an agent/chatroom, or run a workspace-confined command. The scheduler should reuse the machinery we already have rather than invent a parallel one.

## Current state — the frontend prototype

`webapp/cron_views.py` renders the page; state lives in browser JS arrays (`cronFolders`/`cronRowsState`) that now **persist to Postgres** — the page hydrates from `GET /cron/api/tree` on load and PUTs the whole tree (debounced) after each change (see *Persistence (built)* below). It implements essentially the whole *organize + edit* surface, with a layout matching `/chat` and `/modelgroups` (full-height split, independent-scrolling panes).

**Left panel — folder tree.** An `All jobs` node, then `+ Folder` / `+ Job` actions, then a nested tree of folders containing job names. Single-node drag-and-drop: reorder siblings (drop *between*), nest a folder (drop on its *middle third*), move jobs between folders, and a "Move to top level" zone for the root — dropping a node **selects** it, self-drops are no-ops, and "All jobs" is not a drop target. Each item has a 3-dot kebab: **Delete** and **Duplicate** (deep-clones a job, or a folder's *whole subtree*, with fresh uuids — the copy starts **inactive**); folders also get **New job**. **Delete is guarded** (see *Operations*): a job or an empty folder needs a one-click confirm; a **non-empty folder cascades** (deletes its whole subtree) and requires typing the folder name to confirm — the descendant count is shown and the overlay's **Delete** button stays disabled until the typed name matches. Creating a folder (**+ Folder**) likewise opens a small name overlay (Create disabled until a name is entered). **All of these are custom in-page overlays — the page uses no native `prompt`/`confirm`/`alert`**, since a browser (e.g. Firefox) can offer to *permanently* suppress native dialogs, which would silently break the flow. Folder icons are inline Lucide; exactly one node is highlighted at a time (never a folder + an unrelated job) — the highlight is a tinted background **and a bold name**, on folders and jobs alike.

**Right panel — titled by selection** (*All jobs* / *Folder details* / *Job details*):
- The **list** (All jobs / Folder details) shows folders *and* jobs as rows, in **depth-first tree order** with the name column **indented per nesting level**; Folder details shows the folder's *whole subtree*, not one level. Columns: **Active** (read-only `Active`/`Inactive`), **uuid** (short prefix), **name** (folder rows use the tree's folder icon; the name cell is one line, **truncated with an ellipsis + full-name tooltip** so long names don't wrap), the **merged schedule** column (cron expression on the first line, the `cronDescribe()` explanation below it), **command**, **description**, and a **Details** link. Folder rows leave the schedule/command columns blank but **do show the folder's description**. Disabled / ancestor-disabled rows are grayed.
- **Folder / Job details** share a top **rename** field (no label), an **Active** toggle with a plain-English effect note ("the command will be executed on its schedule" / "… will not be executed"; folders cascade to the whole subtree, with a "(a parent folder is deactivated)" hint), and the node's **Created / Modified timestamps** (`2026-jun-06 23:49:21` local time, full date + timezone on hover; only present once the node has been persisted and reloaded).
  - **Folder details** additionally shows a read-only **Description** (notes about the child nodes) with an **Edit description** button.
  - **Job details** shows read-only **Schedule** (cron string + `cronDescribe()` explanation + the job's **time zone**), **Action** (`cmd: …` / `msg → target: …`), and **Description** summaries, each with an **Edit** button. Editing is deliberately *not* the New-job form — see the overlays below. (Re-filing a job into another folder is done by **dragging** it in the tree, not from this page.) There is **no "New job" button** on Folder details / All jobs — job creation is the tree's **+ Job** action or a folder's kebab **New job**, keeping these pages uncluttered.
- **Edit overlays.** Each detail facet is edited in a small focused modal over its own backdrop, with **Save / Cancel** — distinct from the New-job builder so editing one facet doesn't surface the whole form. **Edit schedule** (Job details) = the five dropdowns + live cron string + explanation + a **Time zone** picker (preloaded from the job); **Edit action** (Job details) = the Message/Command toggle + the relevant fields, with the same required-field validation as create; **Edit description** (Folder *and* Job details) = a textarea editing the selected node's notes. The Edit-schedule/Edit-action **Save** is **disabled until its data differs** from the job's original (cron + time zone; type/target/message/command) and re-disables on revert. Saving stamps the node's `updated_at` optimistically and persists. The schedule/action overlays use their own `es-`/`ea-` prefixed controls so they never collide with the builder's.
- **New job** opens as a **modal overlay** over a click-blocking backdrop (a stray click can't lose typed data; the underlying view stays visible behind it): Name (prefilled "Unnamed"), Description (textarea), the five schedule dropdowns + live cron string + `cronDescribe()` (24-hour-labelled), a **Time zone** picker (Local time / UTC), a Message/Command toggle, and a Folder picker. The save button reads **Create job**. The builder is **create-only** — it's never reused for editing (that's the overlays above).
- Creating, updating, duplicating, adding a folder, and dropping all **select the resulting node**.
- **Deep-linking.** The page reads a `?id=<uuid>` query param on load and selects that folder/job (so an admin row or a bookmark can jump straight to the node causing trouble), and mirrors the current selection back into the URL via `history.replaceState` (`cronSyncUrl`). An unknown id falls back to *All jobs*.

Each job object is already shaped close to a persistable record:

```js
// job:
{ uuid, name, enabled, folderId, cron, timezone: 'localtime'|'UTC', type: 'message'|'command', target, message, command, description, created_at, updated_at }
// folder:
{ id, name, description, parentId, enabled, created_at, updated_at }
```

(`created_at` / `updated_at` are read-only, server-supplied ISO strings used only for display.)

Persistence and **firing are done** (next subsections): the scheduler tick + **Run now** fire jobs and emit events into the `cron` room. What remains of the brief — **View logs / health** (next run, next-3, success/error counts, last success/error, retries, status), a **global pause**, and **retries** — plus a polished async per-fire completion summary line (build-order steps 3+).

### Persistence (built)

The first backend slice (build-order step 1) is merged:

- **Tables** in `db.py`: `cron_folder`, `cron_job`, `cron_run` (the last created but **unwritten** until firing). Created by `init_db`'s `db.create_all()`. `parent_uuid` / `folder_uuid` / `cron_uuid` are **plain UUID columns — no DB foreign keys** (integrity in app code), which keeps the bulk save free of delete-ordering/cascade issues. Both `cron_folder` and `cron_job` carry timezone-aware `created_at` (default) and `updated_at` (default + `onupdate`); `cron_folder` also has a `description` text column, and `cron_job` a `timezone` text column (`'localtime'` | `'UTC'`, default `'localtime'`).
- **Helpers** in `db.py`: `cron_load_tree()` / `cron_save_tree(folders, jobs)`, using the **frontend's field names** (folder `id`/`parentId`/`description`, job `uuid`/`folderId`/`cron`/`type`), with sibling order carried as a `position` int (list order in ↔ `ORDER BY position` out). `cron_load_tree` also returns each node's `created_at`/`updated_at` (and the folder `description`).
- **Save is an upsert by uuid, *not* delete-all + insert-all.** `cron_save_tree` loads the existing rows keyed by uuid, **updates matched rows in place**, inserts new ones, and deletes those whose uuid is absent from the incoming payload. This is the key reason the timestamps are meaningful: an in-place update **preserves `created_at`**, and SQLAlchemy's dirty-checking means `updated_at`/`onupdate` only fires for rows that actually changed (the debounced full-tree PUT touches every row, but unchanged rows emit no `UPDATE`). A delete-all+insert-all would reset `created_at` to "now" on every save, making the displayed "Created" date useless — that regression is why the approach changed.
- **API** in `webapp/cron_api.py`: `GET`/`PUT /cron/api/tree` (mirrors `chat_api.py`), registered in `webapp/__init__.py`. Since 2026-06-10 the PUT additionally carries a **`version` token** (from GET; stale → 409) and a **`deletes` count** declaring intended deletions (undeclared deletions → 400) — see *Review findings → finding 2* for the full contract.
- **Page**: hydrates from `GET` on load (the inline demo seed is **dropped**, so it starts empty) and PUTs the whole tree, **debounced 250 ms**, after each of its mutation paths (including editing the folder description).
- **Tests**: `test_cron_api.py` (DB + endpoint round-trips, plus a test that `created_at` survives a re-save while `updated_at` advances) with a snapshot/restore fixture so the shared live Postgres isn't left with artifacts; `test_cron_views.py` asserts the rendered page markers.

**First-cut tradeoffs (deviations from *HTTP API* below):** shipped as a **bulk whole-tree `GET`/`PUT`** rather than the per-node PATCH API sketched below — simpler and lower-risk. Consequences: per-node endpoints, the `user_id` column, and explicit `position`/move/reorder endpoints are **not built yet**. The per-node API remains the documented refinement target. (Note the save is now an **upsert** rather than a literal replace, so it already preserves identity/timestamps across saves — a step toward the per-node model.)

### Validation (built)

The bulk PUT is **thin but no longer blindly trusting**. `db.validate_cron_tree(folders, jobs)` runs at the top of `cron_save_tree` (so *every* writer — the endpoint today, future MCP/agent editors — is covered) and raises `db.CronTreeError` **before any DB mutation**; the endpoint maps that to a **400** with an error message (not a 500, and nothing is persisted). It checks:

- **uuid shape + global uniqueness** — every folder `id` / job `uuid` parses as a UUID (normalized, so case/format variants collide), and uuids are unique **across folders *and* jobs**, not just within a kind — a node is identified globally by uuid (e.g. `/cron?id=<uuid>`), so a folder/job collision would make that deep link ambiguous.
- **Reference integrity** — a folder's `parentId` and a job's `folderId` are either `null` or the id of a folder *present in the same payload* (no dangling references).
- **Acyclic folders** — walking `parentId` from any folder terminates at a root; self-parent and multi-node cycles are rejected. This makes the doc's "server validates acyclic" claim actually true.
- **Action type** — `type ∈ {message, command}`.
- **Time zone** — `timezone ∈ {localtime, UTC}` (absent ⇒ `localtime`).
- **Cron shape** — exactly 5 whitespace-separated fields of UI-grammar characters (`[0-9*/,-]`). This is a *shape* check, not full semantic validation; the real parser (`croniter`) arrives with the scheduler.

**Deliberately lenient (for now):** empty `command`/`message`/`target` and empty names are **allowed**, because the UI legitimately autosaves *drafts* (a job created and filled in gradually). Enforcing non-empty action fields here would break the debounced save; the scheduler will instead skip/withhold a job whose action is empty. Tightening this belongs with the firing phase.

### Flask-Admin (built)

The three cron tables are registered in `webapp/core.py` under an **Admin** "Cron" category (`CronFolderView`, `CronJobView`, `CronRunView`), as a low-level inspection surface alongside the curated `/cron` page:

- **`uuid` columns are truncated** to a 6-char prefix in a `<code>` with the full uuid as a hover `title` (`_fmt_short_uuid`), so the columns stay narrow.
- **Folder-reference columns** (`cron_job.folder_uuid`, `cron_folder.parent_uuid`) render the **truncated uuid on one line and the folder's name below it** (`_cron_folder_label`) — these are plain columns (no FK), so the name is looked up by uuid.
- **Datetime columns** render compactly as `2026-06-05 23:57:30 +02:00` — sub-seconds dropped, a space before the timezone so the cell word-wraps (`_fmt_cron_datetime`, applied via `column_type_formatters` so it covers all datetime columns). `created_at`/`updated_at` are listed on both folder and job views.
- A virtual **"Cron page"** column renders an **`inspect ↗`** link to `/cron?id=<uuid>` (`_cron_open_link`), the read side of the page's deep-link feature — jump from an admin row to that node on the cron page.

## Tree model: nested folders containing jobs

The left panel of `/cron` is a **tree (a forest, really)**: folders nest inside folders to any depth, and each job lives either at the root or inside a folder. The prototype builds this in the browser; this section is how it should persist.

### Nodes

Two node kinds, **each identified by its own `uuid`** (the uuid is the identity — display names are *not* unique; two folders or jobs may share a name):

- **Folder** — `{ uuid, name, description, parent_uuid | null, position, enabled }` (a future `project` field is planned). `parent_uuid = null` ⇒ a root-level folder.
- **Job** — the cron record (`{ uuid, name, enabled, cron, type, target, message, command, description }`) plus `{ folder_uuid | null, position }`. `folder_uuid = null` ⇒ an unfiled, root-level job.

"What nests where" is therefore a single parent reference per node: folders point at a parent folder, jobs point at their containing folder. The **root level** is just the set of nodes whose reference is `null`.

### Ordering (user-controllable, must be persisted)

Sibling order is changeable by the user, so it cannot rely on insertion/array order once rows come from a DB. Each node carries a **`position`** integer; a parent's children are rendered sorted by `position`, and reordering rewrites the `position` of the affected siblings.

The prototype keeps folders and jobs in two separate ordered lists and renders **child folders first, then jobs** within each level. **As built, `position` is a flat index into the whole per-kind list** (`cron_save_tree` writes `position = i` over *all* folders, and separately over *all* jobs; `cron_load_tree` returns `ORDER BY position`). Sibling order is therefore *derived* — it's the relative order of a parent's children within that global sequence — rather than a per-`(parent, kind)` counter. This round-trips correctly because the frontend preserves array order, but true per-parent `position` scoping (needed once nodes can be reordered via a per-node API rather than a whole-tree resave) is a **future refinement**.

### Operations (all single-node, via drag-and-drop)

- **Reorder** a folder among its sibling folders, or a job among its sibling jobs — drop *between* nodes; the top/bottom half of the target picks before/after.
- **Move a job** into a different folder (drop *onto* a folder) or out to the root.
- **Nest a folder** inside another (drop onto the middle third of a folder) or move it to the root.
- **Delete a folder** (kebab → Delete) **cascades**: it removes the folder, every descendant folder, and every job inside the subtree. Confirmation is a **custom overlay** (`#cron-delete-modal`), not a native dialog: a non-empty folder shows the descendant count and a name field whose match enables the **Delete** button; an empty folder or a **job** shows a one-click Delete. *(Earlier the prototype reparented children to the grandparent; it now deletes the subtree, matching the "everything under here goes" intuition.)*

### Invariants

- **Acyclic.** A folder may not become its own ancestor; a move is rejected when the target parent lies inside the dragged folder's subtree. The UI enforces this live via `cronFolderInSubtree`, **and the server now re-checks it** on every save (`db.validate_cron_tree` walks the parent chain and rejects self-parent / multi-node cycles) — so a hand-crafted or buggy payload can't persist a cycle. See *Validation (built)*.
- **Effective-enabled inherits down.** A job is *live* only if it is enabled **and** every ancestor folder is enabled: `cron_job.enabled AND all(folder.enabled up the chain)`. Disabling a folder silently suppresses its whole subtree without touching descendants' own flags (see Data model).

### Persistence mapping

The `cron_folder` / `cron_job` tables (below) carry everything this needs: folder nesting → `cron_folder.parent_uuid`, job placement → `cron_job.folder_uuid`, ordering → the `position` column on each. Every tree mutation is a partial update of one node (move = change `parent_uuid`/`folder_uuid` + `position`; reorder = change `position`); see **HTTP API (endpoints)** below. The server validates the acyclic invariant on folder moves.

### Worked example (the seeded tree)

```
PlanExe              (folder, root, pos 0)
└── CI               (folder, parent=PlanExe, pos 0)
    └── Check PRs    (job,  folder=CI)
    Token usage      (job,  folder=PlanExe)
    Railway stats    (job,  folder=PlanExe, disabled)
My Life              (folder, root, pos 1)
├── Calendar check   (job,  folder=My Life)
└── Email check      (job,  folder=My Life)
```

As records:

```json
{
  "folders": [
    { "uuid": "f-planexe", "name": "PlanExe", "parent_uuid": null,        "position": 0, "enabled": true },
    { "uuid": "f-ci",      "name": "CI",      "parent_uuid": "f-planexe", "position": 0, "enabled": true },
    { "uuid": "f-mylife",  "name": "My Life", "parent_uuid": null,        "position": 1, "enabled": true }
  ],
  "jobs": [
    { "uuid": "j-prs",     "name": "Check PRs",      "folder_uuid": "f-ci",      "position": 0, "enabled": true,  "cron": "*/30 * * * *", "type": "message", "target": "#planexe", "message": "review open PRs" },
    { "uuid": "j-tokens",  "name": "Token usage",    "folder_uuid": "f-planexe", "position": 0, "enabled": true,  "cron": "0 9 * * *",    "type": "command", "command": "planexe tokens --today" },
    { "uuid": "j-railway", "name": "Railway stats",  "folder_uuid": "f-planexe", "position": 1, "enabled": false, "cron": "0 * * * *",    "type": "command", "command": "railway status" },
    { "uuid": "j-cal",     "name": "Calendar check", "folder_uuid": "f-mylife",  "position": 0, "enabled": true,  "cron": "0 7 * * *",    "type": "command", "command": "cal today" },
    { "uuid": "j-email",   "name": "Email check",    "folder_uuid": "f-mylife",  "position": 1, "enabled": true,  "cron": "0 8 * * 1",    "type": "message", "target": "#me", "message": "any urgent email?" }
  ]
}
```

## Backend design

### Where the scheduler lives

Do **not** add a new process. The supervisor loop in `main.py` already runs inside `app_context()`, ticks roughly once per second (`sel.select(timeout=TICK_TIMEOUT=1.0)`), and is the thing that "wakes up on a heartbeat." Add one pass to that loop:

```
# pseudocode, inside supervisor_loop, once per tick (or throttled to every ~10s)
now = datetime.now(tz)
for cron in db.fetch_due_crons(now):          # enabled AND folder enabled AND next_run_at <= now
    journal_or_inbox = fire_cron(cron, trigger="scheduled", debug=False)
    db.advance_cron_schedule(cron, now)        # set last_fired_at, recompute next_run_at
```

This reuses the existing "enqueue → supervisor spawns the agent → agent writes a Journal entry" pipeline. A fired cron is just an `enqueue(...)` call, so everything downstream (process isolation, the 60s hang-kill, journaling) comes for free.

**Computing "due":** store `next_run_at` on each job and compare against `now`. Recompute `next_run_at` from the cron expression after each fire, evaluated against the job's **`timezone`** (already stored): `'UTC'` ⇒ compute in UTC; `'localtime'` ⇒ compute in the host's local tz *at that moment*, so a job set for "09:00 local" fires at 09:00 wherever the machine currently is. The UI constrains the grammar (`*`, `*/N`, specific values — no ranges/lists), but the backend should parse general 5-field cron so hand-edited rows still work. Use a small, well-tested parser — **`croniter`** is the obvious pick (add to `requirements.txt`) rather than rolling our own; `next_run_at` itself is stored as a timezone-aware UTC instant regardless of the job's `timezone` choice.

### Firing a cron

Two action types, each mapping to something that already exists:

1. **Message** → delegate to an agent / post to a chatroom.
   - *Direct-inbox variant:* resolve `target` to an agent uuid (via `agent_config`) and `enqueue(agent_uuid, {"cron_uuid", "text": message, "debug"})`. Silent, task-like. Matches the brief's "placing a task in their inbox."
   - *Chatroom variant:* resolve `target` to a `Chatroom` and insert a `ChatMessage`, letting the normal router/chat agents react. Visible and conversational; aligns with plan.md "mention an agent by name and it wakes up."
   - These aren't exclusive — `target` can accept either a `#chatroom` or an `AgentName`, resolved by prefix. (The prototype's free-text target already anticipates this.)

2. **Command** → run via **`WorkspaceShellChatAgent`** (`WORKSPACE_SHELL_UUID`), never via a raw shell. That agent runs commands as **non-shell argv, no bash, workspace-confined** — exactly the safe primitive we want, and it honors the project constraint "don't be a security nightmare." Firing = `enqueue(WORKSPACE_SHELL_UUID, {"cron_uuid", "argv": parse(command), "debug"})`. The per-folder "project" (PlanExe, My Life) can pin which workspace/repo the command is confined to.

In both cases the payload carries `cron_uuid` and a `debug` flag so we can (a) attribute the resulting Journal entry back to the cron for logs, and (b) let the action run in a verbose/dry-run mode when manually executed. Each fire also posts a one-line note to the **cron-events chatroom** (see *Newer brief items*) for a live feed, and the scheduler skips the whole pass when **globally paused**.

### Data model (new tables)

```
cron_folder
  id, uuid, name,
  description (text),                                -- BUILT: notes about the child nodes
  parent_uuid (nullable, plain uuid col),           -- null = root-level folder (nesting; see Tree model)
  enabled (bool),
  project (text, e.g. "PlanExe"),                   -- planned (folder = execution context)
  user_id (fk → chat_user, nullable),               -- planned: multi-user from day one (see Schema notes)
  position (int, sibling order within parent), created_at, updated_at

cron_job
  id, uuid, folder_uuid (nullable fk → cron_folder.uuid),   -- null = unfiled / root-level job
  name (required), enabled (bool, default true),
  user_id (fk → chat_user, nullable),
  cron_expr (text, "*/5 * * * *"),
  timezone (text: 'localtime' | 'UTC', default 'localtime'),   -- BUILT: clock the cron is evaluated against
  action_type (text: 'message' | 'command'),
  target (text), message (text), command (text),   -- only the relevant ones are used
  description (text, optional),
  last_fired_at (timestamptz, null), next_run_at (timestamptz),
  -- optional denormalized health (else derive from cron_run); see Newer brief items
  success_count (int), error_count (int),
  last_success_at (timestamptz, null), last_error_at (timestamptz, null), last_status (text, null),
  retry_count (int), max_retries (int, default 0),
  position (int, sibling order within folder), created_at, updated_at

cron_run                                            -- one row per firing → the "logs"
  id, cron_uuid (fk → cron_job.uuid),
  trigger (text: 'scheduled' | 'manual' | 'retry'),
  debug (bool),
  fired_at (timestamptz),
  status (text: 'pending'|'ok'|'error'),            -- BUILT: outcome on the row itself
  finished_at (timestamptz, null),                  -- BUILT
  error (text),                                     -- BUILT: failure reason ('' when ok)
  journal_id (nullable fk → journal.id),            -- BUILT (written by the ws-shell agent): full command output
  created_at

cron_setting                                        -- single-row global state (or a Setting kv)
  id, paused (bool, default false), events_room_uuid (nullable fk → chatroom.uuid), updated_at
```

**Effective-enabled** = `cron_job.enabled AND all(folder.enabled up the ancestor chain)` (see Tree model — folders nest, so every ancestor must be enabled, not just the immediate one). A disabled folder silently suppresses its whole subtree without touching descendants' own `enabled` flags — so re-enabling it restores the previous per-node state. This is the launchd "unload a whole folder" behavior.

**Run history / "View logs":** `cron_run` records every firing **and its outcome** (`status`/`finished_at`/`error` — built 2026-06-10, see *Review findings → finding 1*): in-process actions resolve synchronously in `fire_cron_job`, command fires are written back by the workspace-shell agent (which also links `journal_id` to the journal row holding the full output), and `cron_tick` sweeps never-completed `pending` runs to `error`. "View logs" for a job = the last N `cron_run` rows: *fired_at · trigger · status badge · error/result snippet* (join the journal only when the full command output is wanted).

### Schema notes (fitting `db.py`)

These follow the existing SQLAlchemy-2.0 / Postgres conventions in `db.py` (see `docs/data-model.md`): `Mapped[...]` columns, `uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)`, timezone-aware `created_at`/`updated_at`, and `__table_args__` for indexes/constraints. The initial migration was **additive** — three new tables created by the app's `init_db`/`db.create_all()` (same as `Inbox`/`Journal`/`Chatroom`). **Columns added to a cron table after that first cut** follow the repo's existing pattern: an idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in `init_db` (because `create_all()` never ALTERs existing tables). `cron_folder.description` and `cron_job.timezone` were added exactly this way.

> **Built-table reality vs. this design.** The design blocks above are the *target* schema. The tables as built keep the reference columns as **plain UUID columns (no FKs)** — see *Persistence (built)* — and omit the not-yet-needed fields (`project`, `user_id`, the denormalized health counters, `cron_setting`). `cron_folder.description`, `cron_job.timezone`, and the `created_at`/`updated_at` pair on both tables **are** built.

- **Indexes**
  - Scheduler hot path (runs every tick): `Index("cron_due", "enabled", "next_run_at")` for `WHERE enabled AND next_run_at <= now`.
  - Tree fetch + ordering: `Index("cron_folder_children", "parent_uuid", "position")`, `Index("cron_job_in_folder", "folder_uuid", "position")`.
  - Logs: `Index("cron_run_by_job", "cron_uuid", "id")` (mirrors `journal_by_agent`).
- **Constraints**
  - `CheckConstraint("action_type IN ('message','command')")` — same pattern as `Journal.state`'s check.
  - FKs: `cron_folder.parent_uuid → cron_folder.uuid` (self-ref), `cron_job.folder_uuid → cron_folder.uuid`, `cron_run.cron_uuid → cron_job.uuid`, `cron_run.journal_id → journal.id`. Folder delete **cascades the subtree** in app code (the UI computes the descendant set and confirms), so use `ON DELETE RESTRICT` (like `ModelConfigOverride`) and delete the subtree explicitly rather than relying on DB `CASCADE`.
  - **Positions are app-managed and dense** (rewritten on reorder). A DB `UniqueConstraint(parent, kind, position)` is tempting but forces position-shuffling on every insert; keep it app-side and just renumber a parent's children on reorder.
- **uuids are the identity** — display names are not unique, so the API takes/returns uuids and never names as identifiers. The client already keys everything by uuid.
- **Effective-enabled** is an ancestor-chain walk up `parent_uuid`. The scheduler already iterates due jobs, so walk ancestors in app code and skip a job if any is disabled (matches the UI's `cronFolderEnabled`); a recursive CTE is the SQL alternative if the due-query should pre-filter. Tree sizes here are small — app-side is fine.
- **Action payload** is kept as three nullable columns (`target`, `message`, `command`) for queryability/clarity, but a single `JSONB action` column is a viable alternative (JSONB is already used by `ModelConfig.arguments`).
- **Multi-user:** add `user_id` to `cron_folder`/`cron_job` from the first migration (even if single-user today) so it doesn't need a later backfill; scope every query and the scheduler by it.

### Manual "Execute" + debug — **BUILT 2026-06-10**

Two Job-details buttons, both firing **now**, independent of the schedule, neither advancing `next_run_at`, both allowed during global pause (an explicit click):

- **Run now** — a real fire; the page then watches the outcome via the health endpoint and shows the verdict inline.
- **Run debug** (`POST …/run?debug=1` → `fire_cron_job(debug=True)`) — a **dry-run** that reports what the fire *would* do without doing it: a **message** posts `[debug] would send "…" → #room` to the cron room (the message itself is not sent); a **backup** resolves and reports the destination + recipient count without dumping; a **command** is enqueued with `debug` so the workspace-shell agent — which owns the policy — validates and echoes `[debug] would run in <cwd>: <argv>` without executing (cwd untouched). The run row records `debug=true` (shown as `· debug` in the health run table) and resolves `ok`/`error` like a real fire, so a dry-run that would be blocked tells you so.

### Catch-up / missed-run policy (server was down)

`launchd` fires at most once on wake for a missed interval. Suggest the same: on startup, for each due job whose `next_run_at` is in the past, fire **once** and roll `next_run_at` forward to the next future slot — don't replay every missed interval. Make this a per-job or per-folder setting later if needed.

### Concurrency

If a job's previous firing is still in flight when the next slot arrives, **skip** (don't pile up) and note the skip. *Built 2026-06-10:* in-flight = the job's latest `cron_run` is `pending` (not the Journal — message/backup never journal); the skip posts a `⏭` line to the cron room rather than writing a run row, and the pending sweep bounds the guard at 15 minutes. Matches the project's core goal of "no runaway processes that burn tokens."

### Newer brief items (events chatroom, global pause, health, next-runs)

These came in after the first draft of this doc:

- **Cron-events chatroom.** *Built and live.* `seed_chat_defaults()` seeds a dedicated, fixed-uuid **`cron`** `Chatroom` (`db.CRON_ROOM_UUID`) whose author is a fixed agent-type `chat_user` (`db.CRON_SYSTEM_UUID`, name `cron`) that is **deliberately not in `agent_config`**, so the supervisor never runs it — it only authors event lines. `db.post_cron_event(text)` posts a one-line `ChatMessage` to that room; the chat page + SSE render it live, *separate* from the action's own message/command so it stays a clean audit trail. **`fire_cron_job` calls it on every fire** — `▶ ran "Backup" (command, scheduled): \`…\`` / `▶ sent "X" (message, manual) → #room` — and on immediate failures (`✖ "X" failed to fire: …`). A command's own output/blocks are posted to the same room by the workspace-shell agent. *Built 2026-06-10:* the consolidated async **✔/✖ completion line** — `cron_record_run_outcome` posts `✔ "X" completed (trigger)` / `✖ "X" failed (trigger): exit code N` when the agent reports back, so the room reads start → output → verdict. The Job-details **Run now** also watches the outcome (bounded health-endpoint polling, ~15 s) and shows the verdict inline instead of just "see the chatroom".
- **Global pause.** A single app-level flag (a one-row `cron_setting` table, or a key in a `Setting` store) that the scheduler checks *before* its due-cron pass — when paused, fire nothing. The page shows a top-level **Pause / Resume** toggle. This is distinct from per-folder enable: global pause halts everything without touching any `enabled` flag, so resuming restores the exact prior state.
- **Health metrics** (Job details: success/error counts, last success, last error, retries, status, next). All derivable from `cron_run` ⋈ `journal` — count by status, `max(fired_at)` per status, the current `next_run_at`. For O(1) reads, optionally **denormalize counters** onto `cron_job` (`success_count`, `error_count`, `last_success_at`, `last_error_at`, `last_status`), updated when a run completes. *Retries* implies an automatic re-fire on failure — a later feature (`retry_count`/`max_retries` per job; each retry is another `cron_run` with `trigger='retry'`).
- **Next N upcoming runs.** `croniter(expr, now)` → `.get_next()` called N times. Show the next 1 as a list/badge and the next 3 in Job details. Pure computation, nothing stored.

## HTTP API (endpoints)

JSON in/out, same-origin, under `/cron/api`. A new `webapp/cron_api.py` hosts these (cf. `webapp/chat_api.py`); `cron_views.py` keeps rendering the page shell. uuids are server-generated; the API takes/returns uuids, never names, as identifiers.

**Design stance — PATCH-centric.** Every right-pane control (rename field, **Active** toggle, drag-drop move/reorder, action-type/schedule edit) changes one facet of one node, so each maps to a partial `PATCH` of a single node. Prefer `PATCH` (partial) over `PUT` (full replace). After a write, the client refetches the affected node (or the tree) so server-normalized `position` / `next_run_at` win.

### Hydrate

- `GET /cron/api/tree` → `{ "folders": [...], "jobs": [...] }` — one call to render the whole sidebar + lists. The page hydrates from this instead of seeding a JS array. Folders carry `name`, `description`, `parentId`, `enabled`; jobs carry `folderId`, `enabled`, the cron/action fields, and `timezone`. Both kinds also carry read-only `created_at`/`updated_at` (and, later, jobs will carry `next_run_at` + last-run status). *(Ordering is implicit in array order — the server returns rows `ORDER BY position`; an explicit `position` field per node is not yet emitted.)*

### Folders

| Method | Path | Body | UI action |
|--------|------|------|-----------|
| `POST` | `/cron/api/folders` | `{ name, parent_uuid|null, position? }` | "+ Folder"; drag-nest creates none (it moves) |
| `PATCH` | `/cron/api/folders/<uuid>` | any of `{ name, enabled, parent_uuid, position }` | rename · **Active** toggle (cascades) · move/reorder (validates acyclic) |
| `DELETE` | `/cron/api/folders/<uuid>?cascade=1` | — | kebab Delete; default **cascades** (deletes the whole subtree — matches the UI), `?reparent=1` would instead lift children to the parent |

### Jobs

| Method | Path | Body | UI action |
|--------|------|------|-----------|
| `POST` | `/cron/api/jobs` | `{ name, folder_uuid|null, cron, timezone, type, target, message, command, description, enabled?, position? }` | tree "+ Job" / folder-kebab "New job"; server computes `next_run_at` |
| `PATCH` | `/cron/api/jobs/<uuid>` | any subset of the above | rename · edit (recompute `next_run_at` if `cron` changes) · **Active** toggle · move/reorder |
| `POST` | `/cron/api/jobs/<uuid>/duplicate` | — | kebab "Duplicate" → clone into the same folder, **inactive** |
| `DELETE` | `/cron/api/jobs/<uuid>` | — | kebab Delete / Details-view delete |

(Folder duplicate, `POST /cron/api/folders/<uuid>/duplicate`, deep-clones the whole subtree server-side with fresh uuids and the top copy inactive — the prototype does this in the browser.)

### Reorder (bulk alternative)

- `POST /cron/api/reorder` `{ parent_uuid|null, kind: "folder"|"job", order: [uuid, …] }` — renumber one parent's children in a single call. Cleaner than N `PATCH`es and keeps `position` dense; the dnd "select-after-drop" then refetches the moved node. Use this *or* the per-node `position` PATCH, not both.

### Firing, logs, health & global (later phases)

- `POST /cron/api/jobs/<uuid>/execute?debug=1` → fire now (manual "Run now"). Returns the new `cron_run` (with `journal_id`); does **not** advance `next_run_at`.
- `GET  /cron/api/jobs/<uuid>/runs?limit=N` → recent `cron_run` ⋈ `journal` rows for "View logs": `fired_at, trigger, debug, status, result snippet`.
- `GET  /cron/api/jobs/<uuid>` (or fold into `/tree`) → also returns **health** (`success_count, error_count, last_success_at, last_error_at, last_status, status`), `next_run_at`, and the **next 3 upcoming** run times (via `croniter`).
- `POST /cron/api/pause` · `POST /cron/api/resume` → toggle the **global pause** flag (scheduler fires nothing while paused). `GET /cron/api/state` returns `{ paused }` for the page's Pause/Resume toggle.

## Frontend evolution (from prototype → product)

Keep everything the prototype already does; add:

1. **Persistence** — replace the in-browser array with the JSON API in **HTTP API (endpoints)** above (a new `webapp/cron_api.py`); the page hydrates from `GET /cron/api/tree` on load instead of seeding fake rows.
2. **Folders** — *already implemented in the prototype* as a nested tree (collapsible, drag-and-drop reorder/nest/move, kebab menu); see **Tree model**. Still needs persistence (the move/reorder API) plus a folder-level enable toggle that grays the whole subtree and a project label.
3. **Next run** column — computed from the cron expression (reuse the explanation logic; show both "every Monday at 09:00 (24h)" and the concrete next datetime).
4. **Execute** controls — "Run now" and "Run now (debug)" per row.
5. **View logs** — expand/modal showing the `cron_run` history with status badges and result snippets (consistent with the chat page's existing collapsible debug rows).
6. **Status at a glance** — last-run badge (ok/failed/never) per row, so the table doubles as a health dashboard.

## How this maps onto existing code

| Need | Reuse |
|------|-------|
| Fire a job | `db.enqueue(agent_uuid, payload)` |
| Run the scheduler | the `supervisor_loop` tick in `main.py` (add a due-cron pass) |
| Execute a command safely | `WorkspaceShellChatAgent` (`WORKSPACE_SHELL_UUID`) — argv, no bash, workspace-confined |
| Send a message to an agent | `enqueue(agent_uuid, …)` via `agent_config` lookup |
| Post to a chatroom | insert a `ChatMessage` (router/chat agents react) |
| Run history / success-failure | the `Journal` table, linked from `cron_run.journal_id` |
| Process isolation + hang-kill | already handled by the supervisor (60s heartbeat → SIGKILL) |
| Cron-events feed | a dedicated `Chatroom` + a `ChatMessage` per fire (the `/chat` page + SSE render it live) |
| Next run / next-3 upcoming | `croniter(expr, now).get_next()` |
| Global pause | a `cron_setting.paused` flag checked at the top of the scheduler pass |

## Open questions

- **Timezone.** *Partly resolved:* each job stores a `timezone` choice — **`localtime`** (the host's local tz at fire time, so it follows the machine when it travels) or **`UTC`** — set via the Time-zone picker in the New-job builder and the Edit-schedule overlay, and shown on Job details. The scheduler must honor it when computing `next_run_at` (see *Computing "due"*). Still open: DST edge cases (a `croniter`/tz concern), and whether to offer named IANA zones (e.g. `Europe/Copenhagen`) beyond the local/UTC pair.
- **Message target resolution.** Chatroom vs agent — prefix convention (`#room` vs `Name`), and what happens if the target doesn't exist (fail the run vs warn).
- **Debug semantics per action type.** *Resolved 2026-06-10* — see *Manual "Execute" + debug*: command = validate + echo argv without executing; message = `[debug] would send …` event without sending; backup = report destination without dumping.
- **Cron parser dependency.** Confirm `croniter` (or equivalent) is acceptable, or restrict the backend to exactly the UI grammar and parse it ourselves.
- **Multi-user ownership.** plan.md wants multi-user; add `user_id` to `cron_job`/`cron_folder` from the start, even if single-user today.
- **Folder = project binding.** Should "project" pin a workspace/repo path for command confinement (PlanExe folder → PlanExe repo), making the folder both an on/off group *and* an execution context?
- **Cron-events chatroom noise.** Post every fire, or only failures/manual runs? One shared room or one per project/folder? Keep it terse (link to the journal) so it doesn't drown real chat.
- **Retries.** *Resolved 2026-06-10* (see *Suggested build order* step 6): per-job `max_retries`, no backoff (next tick), bounded by a 10-minute window; each retry is a `cron_run` with `trigger='retry'` and counts toward `error_count` naturally; retries run *between* slots and never displace a scheduled fire.
- **Global pause vs in-flight.** Pause stops *new* fires; should it also try to stop runs already `processing`, or just let them finish?

## Suggested build order

1. **✅ DONE (merged).** Tables `cron_folder`/`cron_job`/`cron_run` in `db.py`, `cron_load_tree`/`cron_save_tree` helpers, and `webapp/cron_api.py` (`GET`/`PUT /cron/api/tree`); the page hydrates on load and saves the whole tree (debounced) after each mutation. Shipped as a **bulk whole-tree** save (not per-node PATCH), but the save is an **upsert by uuid** so identity and `created_at` survive across saves. `position` is computed from list order; `user_id` and the per-node/move/reorder endpoints are deferred. `cron_run` created but unwritten. Also built on top of this slice: a **folder `description`** field and a per-job **`timezone`** choice (Local time / UTC) — both idempotent `ALTER TABLE` migrations, **`created_at`/`updated_at`** shown on Folder/Job details, the **detail edit overlays** (Edit schedule / Edit action / Edit description, the first two with Save disabled until changed), **cascade folder-delete** behind a typed-name confirmation overlay (all dialogs are in-page overlays — no native `prompt`/`confirm`/`alert`), the **`?id=<uuid>` deep-link**, and the **Flask-Admin** views. (See *Persistence (built)* and *Flask-Admin (built)*.)
2. **✅ DONE (merged).** Scheduler pass in `supervisor_loop` (`db.cron_tick()` every ~5s, self-guarded) + `croniter`; `next_run_at` is populated by `cron_save_tree` and the tick (backfill + advance, no replay of missed slots); due jobs fire via `db.fire_cron_job` honoring timezone and the folder-enabled cascade; each fire writes a `cron_run` and posts to the `cron` room. **Run now** = `POST /cron/api/jobs/<uuid>/run` + a Job-details button (`trigger='manual'`). **Message** jobs post to their target chatroom (or the cron room); **command** jobs enqueue `WorkspaceShellChatAgent` via a programmatic `command_text` payload (its output/blocks post to the cron room). Linking `cron_run.journal_id` and an async completion summary are deferred.
3. **✅ DONE (2026-06-10).** The **cron-events chatroom** feed posts per fire (step 2); the **async ✔/✖ completion/error summary line** posts when an async command's outcome is recorded; "View logs" = the Job-details Health panel's recent-runs table (step 5); the last-run status badge is the lists' health column.
4. **✅ DONE (2026-06-10).** **Command** jobs (via `WorkspaceShellChatAgent`) and manual **Run now** (step 2); the **global pause** (`cron.paused` setting + Pause/Resume, see *Review findings → finding 4*); and the **Run debug** dry-run (see *Manual "Execute" + debug*).
5. **✅ Mostly done (2026-06-10).** **Job-details Health** panel: `GET /cron/api/jobs/<uuid>/health` (`db.cron_job_health`) returns ok/error/pending counts, last success/error, the **next 3 upcoming** runs (via `croniter`), and the last 20 `cron_run` rows (fired_at · trigger · status · error) — rendered as a Health section with a recent-runs table on Job details. The All-jobs / Folder-details lists also carry a **health column** (each job's `last_run` rides along in the tree payload via a `DISTINCT ON` latest-run lookup): ✓ ok / ✖ error / … running at a glance, with timestamp · trigger · error text on hover. The lists also carry a **next-run column** (`next_run_at` rides along read-only in the tree payload): the next fire time, or why it won't fire — `—` for disabled/unscheduled, `paused` during global pause, and a muted timestamp with a "will be skipped" tooltip for drafts.
6. **Mostly done.** Chatroom-target messages (done for rooms; **agent-inbox** targeting still TODO); catch-up is *fire-once* (no replay); the **skip-if-still-running guard is built** (finding 5); **retries are built** (2026-06-10): `cron_job.max_retries` (0 = off, capped at 10, `maxRetries` in the tree payload, "Retry on failure" select on the builder + Edit-action overlay) — a run that resolves to error within `CRON_RETRY_WINDOW` (10 min, so restarts never refire ancient failures) refires as `trigger='retry'` between slots until the trailing retry chain hits the budget; a success or the next scheduled fire resets the chain; drafts excluded. Still TODO: multi-user `user_id`.

(The folder tree, drag-and-drop, detail/edit panes, modal create, Active cascade, and Duplicate are **already built in the prototype** — step 1 just persists them.)

## Review findings (2026-06-10)

A full read of the built system (`db/cron.py`, `webapp/cron_api.py`, `webapp/cron_views.py`, the `main.py` tick) against this design. Summary: **the architecture holds up** — "a cron job is a schedule attached to an action the system already has" kept the backend small, the defensive details are consistently right (validate-before-mutate, upsert preserves `created_at`, a bad cron expression never fires instead of crashing, the tick is self-guarded, fire-at-most-once catch-up, idempotent seeding), and events landing in the `cron` chatroom puts observability where the operator already looks. The gaps below are ordered by how much they block the rest of the brief.

### 1. `cron_run` cannot record an outcome — schema gap blocking steps 3 & 5 — **FIXED 2026-06-10**

The design above assumed run status comes from `cron_run.journal_id ⋈ journal`. Two problems with that as first built: `journal_id` was never written (the workspace-shell payload carried `cron_run_uuid`, but nothing wrote anything back), and two of the three action types (`message`, `backup`) never produce a journal row at all — in the database, a failed fire was indistinguishable from a success.

**As now built**, the outcome lands on the run row itself: `cron_run` gained `status` (`pending`/`ok`/`error`, CHECK-constrained), `finished_at`, and `error` columns (idempotent `ALTER TABLE`, same pattern as `timezone`).

- **In-process actions** (`message`, `backup`): `fire_cron_job` resolves the status synchronously — `ok` on success (a backup whose optional git-push fails is still `ok`, matching the existing "upload failure doesn't fail the fire" behavior), `error` + the exception text on failure.
- **Async commands**: the fire returns with `status='pending'`; the workspace-shell agent calls `cron_record_run_outcome(cron_run_uuid, status=…, error=…, journal_id=…)` on every exit path — `ok` on exit 0, `error` on non-zero exit (`"exit code N"`), blocked command, timeout, or missing command — linking the journal row that holds the full output.
- **Sweep**: a `pending` run older than `CRON_RUN_PENDING_TIMEOUT` (15 min; the supervisor SIGKILLs hung agents after ~60s, so a completion that late never arrives) is swept to `error` by `cron_tick`. This also honestly classifies rows that predate outcome tracking, and it's what a future skip-if-still-running guard needs to not deadlock on dead runs.

This unblocks the health panel, status badges, the async ✔/✖ summary line, and retries (findings/steps 3 & 5).

### 2. The whole-tree PUT is last-writer-wins, including deletions — **FIXED 2026-06-10**

`cron_save_tree` deletes every row whose uuid is absent from the payload. Two open tabs (or one tab that hydrated before an external edit, e.g. a future MCP/agent editor) silently clobber each other's changes — and a frontend bug that PUTs a truncated array mass-deletes jobs with no confirmation. The per-node PATCH API above remains the long-term refinement; both interim guards are now **built**:

- **Tree version (optimistic concurrency).** `cron_tree_version()` derives an opaque token from the *user-managed* fields only (scheduler bookkeeping — `next_run_at`/`last_fired_at`/`updated_at` — is excluded, so background firing never invalidates an open page). `GET /cron/api/tree` returns it; `PUT` must echo it (missing → **400**, stale → **409** + the current token, raised as `CronTreeConflict` before any mutation). On 409 the page re-hydrates, resets a now-dangling selection, and shows a toast ("changed elsewhere — reloaded") instead of clobbering. A successful PUT returns the new token; the page serializes its debounced PUTs (in-flight + queued) so it can't 409 against its own previous save. A failed initial hydrate leaves the token null, so a PUT of the resulting empty state is refused rather than wiping the real tree.
- **Mass-delete tripwire (declared deletions).** Rows absent from the payload are deletions, and an *undeclared* deletion is more likely a truncated payload than an edit. The page counts its two delete paths (single node; folder-cascade subtree) into a `deletes` field on the PUT; `cron_save_tree(expected_deletes=…)` refuses (400) any save that would delete more rows than declared. Both guards are keyword-opt-in on `cron_save_tree` (None skips them) so internal/test callers are unaffected; the HTTP endpoint always enforces them.

### 3. Enabled-but-empty draft jobs fail loudly forever — **FIXED 2026-06-10**

Validation deliberately allows empty `command`/`message` (draft autosave — see *Validation (built)*), but the scheduler didn't compensate: an **enabled** command job with an empty command fired on schedule and posted `✖ … failed to fire: no command to run` to the cron room on every slot. **As built:** `cron_job_is_draft` (empty command for command jobs, empty message for message jobs; backups are never drafts — their destination falls back to settings/env) makes `cron_tick` roll a due draft's `next_run_at` forward silently — no `cron_run`, no event spam, and no stale-slot fire the moment the action is filled in. The UI badges such jobs **Draft** in the list's Active column (with a tooltip) and notes it on the Job-details Action summary. A *manual* Run-now of a draft still reports the error — an explicit click deserves feedback.

### 4. Global pause — designed, trivial, still missing — **FIXED 2026-06-10**

One `cron.paused` bool in the settings registry (so it also appears on /settings), checked by `cron_tick` after the pending-sweep but before any firing — schedules don't advance while paused, so resume behaves like wake-from-sleep: each due job catches up with at most one fire. `POST /cron/api/pause` / `/resume` toggle it; the tree GET carries `paused` so the page hydrates the state; the page shows a **Pause all / Resume all** button in the tree panel and a banner across the right pane while paused. Per-job/folder `enabled` flags are untouched, so resuming restores the exact prior state. Manual "Run now" still works while paused (an explicit click is not a scheduled fire).

### 5. No skip-if-still-running guard — **FIXED 2026-06-10**

The *Concurrency* section above says a slot should be skipped (and the skip noted) while the previous fire is still in flight. **As built** (on top of finding #1's outcome tracking): `cron_job_run_in_flight` checks whether the job's latest run is still `pending`; a due slot then skips — `next_run_at` rolls forward, no run row, a `⏭ "X" skipped: previous run still in flight` note in the cron room. Deadlock-free by construction: the pending sweep runs at the top of every tick, so a dead run flips to `error` after 15 minutes and the guard releases. Manual "Run now" deliberately bypasses the guard (an explicit click).

### 6. The page is a 1,672-line Python string — **FIXED 2026-06-10**

Consistent with the repo's self-contained-views pattern (`tts_kokoro_views.py`), but `/cron` outgrew it. **As built:** the ~1.5k lines of page JS moved to **static/cron.js** (Flask's default static route), referenced with an mtime `?v=` cache-buster; the HTML shell + CSS stay in `webapp/cron_views.py`. The JS is now lintable/syntax-checkable; the views tests assert against page + served JS combined.

### Smaller notes

- **`debug` is recorded but dead.** *Resolved 2026-06-10:* wired as the **Run debug** dry-run — see *Manual "Execute" + debug*.
- **Near-zero sentinel uuids — fixed 2026-06-10, migration retired 2026-06-11.** The seeded System-folder/backup-job uuids and the cron room/sender (`c0000000-…`) were indistinguishable in the UI's short-uuid columns; all four are now random-looking fixed uuids. The one-time legacy-uuid migrations (in `seed_cron_defaults` / `seed_chat_defaults` — the chat one needed insert-new → repoint → delete-old because `chatroom.uuid`/`chat_user.uuid` are FK targets) were applied to both `rainbox_production` and `rainbox_claude`, verified (zero legacy rows), and then **removed from the codebase**.

### Reviewed and deliberately left alone

- **No DB foreign keys** — documented tradeoff; integrity is genuinely enforced in `validate_cron_tree` (uuid shape/uniqueness, dangling refs, cycles) before any write.
- **Two-value timezone model** (`localtime`/`UTC`) — covers a single-operator local app; named IANA zones stay an open question, not a gap.
- **Fire-at-most-once catch-up** — correct default for both messages and backups; per-job replay policy can wait for a real need.
