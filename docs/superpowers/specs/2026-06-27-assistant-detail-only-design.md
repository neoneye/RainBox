# Simplify `/assistant` to a single-run detail view — design

## Goal

Now that `/assistant-overview` is the place to find and pick a run, the
`/assistant` inspector no longer needs its own left tree (the folder structure
was poor UX). Two changes:

1. **Move the kebab into the main detail panel** — one actions menu for the
   selected run, in a header bar at the top of the pane.
2. **Remove the left panel** — `/assistant` becomes a pure single-run detail
   view, driven entirely by `?id=<uuid>`.

`/assistant-overview` already links each row to `/assistant?id=<uuid>`, so the
detail page is reached by clicking a row there; the nav's "Assistant" link
already points at the overview.

## Current structure (what changes)

`webapp/assistant_views.py` renders `ASSISTANT_TEMPLATE`:

- `.as-split` grid = `.as-tree` (left, 340px) + `.as-main` (right detail).
- `.as-tree`: `as-folder` `<details>` per virtual bucket (Running / Recent /
  Stopped / Resolved / Unresolved, from `_bucket_runs`), each containing
  `run_leaf(r)` items. Every leaf has an `.as-kebab` button calling
  `asKebab(event, uuid, status, journalId)`.
- The kebab opens `#as-menu`, populated in JS with: Copy run id, Copy journal
  id, View as markdown, Refresh summary, Stop (running only).
- `assistant_page()` loads `runs = list_assistant_runs(limit=50)`, builds
  `folders = _bucket_runs(runs)`, and renders both panes.

## Target structure

- Drop the split: `.as-main` fills the page width. Remove `.as-tree`, the
  `folder`/`run_leaf` macros, and the folder expand/collapse persistence JS.
- **Detail header bar** (`.as-main-head`), shown only when a run is selected: a
  flex row at the very top of `.as-main` — the run's short id (monospace
  heading, e.g. `Run a31c2f…`) on the left, a `.as-kebab` button on the right.
  The button calls the existing `asKebab(event, uuid, status, journalId)` with
  the selected run's values. All kebab JS (`asKebab`, `#as-menu`, `asItem`,
  `asCloseMenu`, `asToast`, `ppAct`, `ppConfirmAct`, `ppCopyText`) stays as-is.
- **Empty state** (no `?id=` or unknown id): replace "Select a run on the left…"
  with a pointer to the successor — "No run selected — open the **Assistant
  overview** to pick a run", linking to `/assistant-overview`.

## Backend

`assistant_page()` simplifies to: resolve the selected run (`_selected_run()`),
and if present assemble its detail (`_load_run_detail` + duration); render the
template. It no longer calls `list_assistant_runs` or `_bucket_runs`, and no
longer passes `runs`, `folders`, `icon_open`, `icon_closed`.

- **Remove `_bucket_runs`** (and its unit test) — it existed only for the tree.
- Keep `db.list_assistant_runs(limit=50)` in the `db` layer (still used by
  `db/test_assistant_trace.py`); only the page stops calling it.
- The folder icons (`_ICON_FOLDER`, `_ICON_FOLDER_OPEN`) become unused by this
  template — remove them only if nothing else references them (grep first;
  leave them if shared).

## Markdown export

`/assistant/<run_id>/markdown` and `_run_markdown()` are unaffected — they
serialize a single run's detail, independent of the tree. The kebab's "View as
markdown" still points at `/assistant/<uuid>/markdown`.

## Files touched

- `webapp/assistant_views.py` — template (remove tree + macros, add header bar,
  full-width main, new empty state), `assistant_page()` handler, remove
  `_bucket_runs`.
- `webapp/test_assistant_views.py` — remove the `_bucket_runs` test and its
  import; remove/adjust the "virtual status folders render" assertion; update
  the "Select a run" empty-state assertions to the new copy; update the nav /
  detail tests that still pass.

## Testing

Mirror the existing `test_assistant_views.py` patterns (Flask `test_client`,
`app_ctx` seeding against `rainbox_claude`):

- **Detail still renders**: `/assistant?id=<seeded run>` shows the dashboard /
  timeline and a kebab button in the header (assert a marker, e.g.
  `class="as-kebab"` present in the detail header, and `asKebab(` wired to the
  selected uuid).
- **No tree**: the response no longer contains `as-tree` / `as-folder`.
- **Empty state**: `/assistant` with no id (and with an unknown id) shows the
  overview pointer and links `/assistant-overview`.
- **Kebab JS intact**: served page still defines `asKebab`, `#as-menu`, and the
  menu items (Copy run id, View as markdown, Refresh summary).
- Remove the `_bucket_runs` unit test.

## Out of scope (YAGNI)

- No change to the detail pane's contents (dashboard, summary, trigger,
  timeline, verdict) beyond adding the header bar.
- No change to the kebab's actions or the underlying control/markdown APIs.
- No new "previous/next run" navigation on the detail page — the overview is the
  navigator.
