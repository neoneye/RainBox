# `/assistant` left panel → virtual status-folder tree — design (2026-06-24)

**Status:** approved-direction, complete spec (decisions made; implement
directly). Reshapes the `/assistant` inspector's left pane from a flat run list
into a **virtual facet tree** (status folders), wider, with a kebab on the
selected run — mirroring `/memory`'s facet-tree chrome (see
[`../../ui-left-panel-tree.md`](../../ui-left-panel-tree.md)).

## Decisions (made, with rationale)

- **Mirror `/memory`, not `/cron`.** `/memory`'s left panel is the *virtual facet
  tree* variant (groups by status facets, not editable folders — no
  drag-drop, no whole-tree save). That is exactly this feature; `/cron`'s editable
  folder machinery (tree PUT, version guard, validator, DnD) would buy nothing for
  computed buckets. Copy `/memory`'s split layout + CSS values; use `as-`-prefixed
  class names.
- **Full-height split layout.** `body{height:100vh;display:flex;flex-direction:
  column;overflow:hidden}`, nav at top (`.pp-nav{margin-bottom:0}`), then
  `.as-split{display:grid;grid-template-columns:340px minmax(0,1fr);flex:1 1 auto;
  min-height:0}` — the left tree (`.as-tree`, `overflow:auto`) scrolls
  independently of the right detail (`.as-main`, `overflow:auto`). **340px** is
  wider than `/memory`'s 260px (the explicit ask). This replaces the current
  centered scrolling `.pp-as` page.
- **Five virtual folders, computed server-side** from `db.list_assistant_runs`,
  each a `/memory`-style node (Lucide folder icon + name + **count badge**,
  expand/collapse, no edit). A run appears under **every** folder it matches
  (facets overlap, like `/memory`):
  - **Recent** — the latest runs, any status (default expanded).
  - **Running** — `status in (running, stopping)`.
  - **Stopped** — `status == stopped`.
  - **Resolved** — `summary.outcome == "resolved"`.
  - **Unresolved** — `summary.outcome in (partial, failed)` **or** `status ==
    failed`. (Not-yet-summarized runs appear only under Recent/Running.)
- **Server-rendered, minimal JS.** The page is server-rendered today and the data
  is small (latest ≤50 runs); render the tree in Jinja. Folders are native
  `<details open>`/`<summary>` (native collapse, no tree JS); the default
  disclosure marker is hidden and the **folder icon is the expand indicator**
  (open vs closed SVG via a `details[open]` CSS swap — no separate caret, per the
  convention). Inline JS only for: the **kebab menu** (toggle + position + Copy
  id / Stop) and **persisting** each folder's open/closed state to `localStorage`
  (the convention's recommended UX), keyed by folder name.
- **Run leaf rows** keep today's content (start time, status badge, obstacle
  badge, trigger snippet). Selecting a run is unchanged: a link to `?id=<uuid>`
  (full server re-render). The selected run gets the tint+bold highlight
  (`.as-run-node.sel{background:#dbeafe;font-weight:600}`) in **every** folder it
  appears in.
- **Kebab on the selected run only** (`visibility:hidden` unless `.sel`, like
  `.mem-kebab`). Items:
  - **Copy id** — the run uuid (the established `/chat`/`/cron`/`/kanban` pattern).
  - **Open in chat** — `/chat?id=<room_uuid>` (room-level; the right pane keeps the
    precise `&msg=` deep-link to the trigger message).
  - **Stop** — only when `status in (running, stopping)`; `POST /chat/api/assistant/
    runs/<uuid>/stop` then reload.

## Data flow

`assistant_page()` already loads `runs = list_assistant_runs(50)` and the
step-count map. Add the bucketing (pure Python over `runs`): build an ordered list
of `{name, runs, count}` folders by the rules above. Pass it to the template
alongside the existing `selected`/`timeline`/etc. No new DB query, no new endpoint
(Stop reuses the existing one).

## Components

- **`webapp/assistant_views.py`** — template rewrite of the left pane (the
  `.as-split` / `.as-tree` / folders / run leaves / kebab markup + the `/memory`
  CSS values under `as-` names + the two Lucide folder SVGs), the view's bucketing
  helper, and the inline kebab/expand JS. The right pane (summary → hr → run
  details → timeline) is unchanged content, moved into `.as-main`.

## Testing (`webapp/test_assistant_views.py`, model-free)

- The five folder headers render, each with the correct **count**.
- A `running` run appears under **Running** *and* **Recent**; a `stopped` run under
  **Stopped**; a run with `summary.outcome=="resolved"` under **Resolved**; a
  `failed` run (or `outcome` partial/failed) under **Unresolved**.
- The selected run renders a kebab with **Copy id** and an **Open in chat** link;
  **Stop** appears only when the run is running.
- The runs-list links still address by uuid (`?id=<uuid>`); the right pane still
  shows the summary above the run details.

## Out of scope

- Editable/user-created folders, drag-drop, persistence of the tree (these are
  facets, computed each load).
- A folder *detail* pane (clicking a folder only expands/collapses; only runs are
  selectable).
- Pagination / a "load more" beyond the existing `limit`.
- Live auto-refresh (still a manual reload; S7 follow-up).

## Acceptance

`/assistant` shows a wider (340px) left tree of five status folders with counts,
each run filed under every matching folder; selecting a run highlights it and
exposes a kebab (Copy id / Open in chat / Stop-when-running); the right pane is
unchanged. Suite green, model-free.
