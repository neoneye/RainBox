# `/assistant-overview` — design

## Goal

A clean, fast overview of all Assistant ReAct loops, replacing the cramped,
hard-to-scan left panel of `/assistant`. It supports search, status filtering,
sortable columns, and pagination across the *full* run history (the existing
left panel caps at 50). Clicking a row opens that run in the existing inspector
at `/assistant?id=<uuid>`.

The page is modeled on the provided mock ("ReAct Loops Overview"): a dense,
sortable table — Date · Status · Summary · Steps · Duration — with status chips,
running loops pinned to the top, an empty state, and numbered pagination.

`/assistant-overview` is **added alongside** `/assistant`; the inspector and its
left panel are left unchanged.

## Architecture

Three new pieces, mirroring the `/cron`, `/kanban`, `/git` convention (thin
Jinja shell + page-scoped JSON API + vanilla-JS static file):

1. **`webapp/assistant_overview_views.py`** — `@app.route("/assistant-overview")`
   → `assistant_overview_page()`. Renders an inline `render_template_string`
   shell: `<head>` + `{% include "_nav.html" %}` + a filter bar + empty
   `<table>`/pager containers. No run data is inlined; the JS hydrates from the
   API. Static JS linked with an mtime cache-buster:
   `<script src="/static/assistant-overview.js?v={{ js_v }}"></script>`.

2. **`webapp/assistant_overview_api.py`** — `@app.route("/assistant-overview/api/runs")`
   → returns `jsonify({...})`. Server-side filter + sort + paginate so it scales
   past the 50-cap. Response:
   ```json
   {
     "ok": true,
     "runs": [ {run row…} ],
     "total": 137,
     "page": 1,
     "pages": 6,
     "per_page": 25,
     "counts": { "all": 137, "running": 2, "stopped": 9, "resolved": 88, "unresolved": 38 }
   }
   ```
   Each run row:
   ```json
   {
     "uuid": "…",
     "summary": "Buy candy for the office party",   // run.summary.trigger, or null → "summarizing…"
     "status_label": "Resolved",
     "status_kind": "resolved",                      // running|stopped|resolved|unresolved|pending
     "started_date": "2026-06-26",
     "started_time": "09:12",
     "steps": 4,
     "step_limit": 6,
     "duration": "21.7s",                            // null while running
     "agent_name": "assistant"
   }
   ```

   Query params (all optional):
   - `q` — case-insensitive substring over the summary text (`summary->>'trigger'`),
     also matching `final_summary` and the uuid prefix.
   - `status` — `all` (default) | `running` | `stopped` | `resolved` | `unresolved`.
   - `sort` — `started` (default) | `summary` | `steps` | `duration`.
   - `dir` — `asc` | `desc` (default `desc`, except `summary` defaults `asc`).
   - `page` — 1-based (default 1).
   - `per_page` — default 25, clamped to [5, 100].

   Errors follow the house style: `{"ok": false, "error": "…"}` + 400.

3. **`static/assistant-overview.js`** — vanilla JS (no framework). Owns all
   interactivity: debounced search (~250 ms), status filter, clickable sort
   headers (toggle dir, arrow indicator), numbered pager (« Prev · 1 2 3 · Next »),
   "Showing X–Y of N runs" range text, empty state, and row-click →
   `location.href = '/assistant?id=' + uuid`. Renders rows via
   `document.createElement` + `textContent` (no innerHTML for user data).

## Data model mapping

The mock's invented fields map onto the real `AssistantRun` model:

| Mock field   | Real source                                                            |
|--------------|------------------------------------------------------------------------|
| summary      | `run.summary["trigger"]` (NULL until summarized → "summarizing…")       |
| status chip  | derived (see below), reusing the existing `_dash_status` semantics      |
| date / time  | `run.started_at`                                                        |
| steps        | `COUNT(assistant_step)` for the run                                     |
| step_limit   | `run.step_limit`                                                        |
| duration     | `run.finished_at − run.started_at` (NULL → running)                     |
| agent_name   | resolved from `run.agent_uuid` (best-effort)                           |
| row link     | `/assistant?id=<run.uuid>`                                              |

**Status derivation** (one helper, mirroring `_dash_status` in
`assistant_views.py` so the overview and inspector agree):

- `status in (running, stopping)` → **Running** (blue, pulsing dot)
- `status == stopped` → **Stopped** (gray)
- `summary.outcome == "resolved"` → **Resolved** (green ✓)
- `summary.outcome in (partial, failed)` or `status in (failed, killed)` →
  **Unresolved** (red ✕)
- terminal but not yet summarized → **—** (pending, faint)

Status **facets** = All / Running / Stopped / Resolved / Unresolved, matching the
five buckets users already know from the left panel (`_bucket_runs`). Running
loops are pinned to the top of the result regardless of the active sort.

## DB layer (`db/assistant.py`)

Add:

- `list_assistant_runs_page(*, q, status, sort, dir, offset, limit) -> (runs, total, counts)`
  — one filtered/sorted/paginated query plus the per-facet counts. Sorting by
  `steps` uses a `COUNT(assistant_step)` correlated subquery / left join;
  `duration` sorts on `finished_at - started_at`; `summary` on
  `summary->>'trigger'`. Running-first pinning applied before the page slice.
- `assistant_step_counts(run_uuids) -> {uuid: count}` — one aggregate
  `GROUP BY run_uuid` for the page slice (no N+1).

The status derivation + row serialization live in the API module (a small
`_serialize_run` mirroring `_dash_status`), keeping `db` query-only.

## Colors

The mock's design-system CSS vars are translated to the app's real palette
(already used across the webapp): blue `#2563eb`/`#dbeafe`, green `#16a34a`/
`#dcfce7`, red `#b91c1c`/`#fee2e2`, gray `#6b7280`, borders `#e5e7eb`. Page CSS
lives in the template's inline `<style>` (page-specific, like cron/kanban).

## Nav

"Assistant" already exists in `_nav.html` (`core.py`). Extend its active check
to cover the new endpoint:
`request.endpoint in ('assistant_page', 'assistant_overview_page')`, so the nav
highlights for both. No new nav entry.

## Out of scope (YAGNI)

- The mock's "roomy" card layout direction — dense table only.
- Live auto-refresh / websockets for running loops (the chip shows a static
  pulse; a manual reload reflects progress). Can follow later.
- Agent/room dropdown filters from the mock — search + status + sort + paginate
  cover the stated need; can add later if wanted.
- Token columns (in/out/tps) — not in the dense table.

## Testing

Mirror `test_git_views.py` / `test_git_api.py` (Flask `test_client`, seeding via
`db.make_app()` against `rainbox_claude` per conftest):

- **API** (`test_assistant_overview_api.py`): shape of `/assistant-overview/api/runs`;
  `q` filters by summary substring; `status` facet filtering; pagination
  (`page`/`per_page`, `total`/`pages`); sort by each key + dir; running-first
  pinning; bad params → 400; serialized fields (status_kind, steps, duration,
  summary-null → handled).
- **View** (`test_assistant_overview_views.py`): `/assistant-overview` renders
  with nav + `pp-active`, links `/static/assistant-overview.js?v=`; JS served 200
  and contains the core function markers + the `/assistant?id=` row-link and the
  `/assistant-overview/api/runs` fetch.
