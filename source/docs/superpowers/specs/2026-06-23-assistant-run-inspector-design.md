# `/assistant` — run timeline inspector + action surface — design (2026-06-23)

**Status:** ✅ implemented (`webapp/assistant_views.py`, route `/assistant`; suite
green, 1228 passed). Delivers the **inspection + approval slice of card S7**
(runtime dashboard) of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
a run-centric timeline over `assistant_run → assistant_step → assistant_write_intent`,
with the lifecycle actions the existing endpoints already expose. It deliberately
leaves S7's kill/retry and live SSE streaming for later.

## Goal

One page where the operator can see an assistant turn end to end — the run, its
ordered step timeline, and each step's write-intent — and act on it: confirm /
reject / undo a write-intent, and stop / redirect a live run. This also gives the
**confirm-tier writes** (`set_reminder`, `edit_file`, `activate_memory`,
`activate_skill`) their first browser approval surface (today they need a raw
`curl` to the write-intent endpoints).

## Decisions (made, with rationale)

- **A new server-rendered page at `GET /assistant`**, in `webapp/assistant_views.py`,
  mirroring `webapp/doctor_views.py` (`render_template_string` + `_nav.html` + a
  little inline JS). Nav link **"Assistant"** in `webapp/core.py`'s `pp-links`
  (next to "Memory"); module imported in `webapp/__init__.py`. Not a Flask-Admin
  view — the four models are *already* in Flask-Admin as flat tables; the value
  here is the run-centric **join** (run → steps → intents) those flat tables
  can't show.
- **Master–detail layout** (matches the "tree" framing). Left: a list of recent
  `AssistantRun`s, newest first, selectable. Right: the selected run's step
  timeline, each step's `AssistantWriteIntent` rendered inline beneath it. The
  selected run is a query param: `GET /assistant?run=<id>`; the whole page
  re-renders server-side on selection (no client-side run cache to drift).
- **Inspect + lifecycle actions only — no free-form field editing.** The trace is
  an audit record; raw row edits stay in Flask-Admin. The page's only writes are
  the lifecycle transitions the endpoints already own.
- **Reuse the existing endpoints** (`webapp/chat_api.py`) for every action — no
  new action endpoints:
  - write-intent: `POST /chat/api/assistant/write-intents/<uuid>/{confirm,reject,undo}`
  - run: `POST /chat/api/assistant/runs/<id>/{stop,redirect}`
  Buttons call them via `fetch`, then reload the page on success.
- **Action visibility is state-driven:**
  - write-intent `proposed` → **Confirm** / **Reject**; `completed` *and* carries
    an `undo` record → **Undo**; any other state → a badge only.
  - **Stop** / **Redirect** show only when `run.status == "running"` (controls are
    meaningless once a run is terminal). Redirect prompts for the instruction.
- **`AssistantControl` is read-only context.** Applied stop/redirect already
  appear as `control`-phase steps in the timeline; additionally, any still-
  `pending` controls on a running run render as a small banner. No control
  editing — they are produced by the stop/redirect endpoints.
- **Live auto-refresh is out of v1.** A manual **Refresh** link only. (A light
  poll of a running run is a noted follow-up, not built now — keeps v1 free of
  SSE/LISTEN-NOTIFY.)
- **Legacy / unlinked intents.** A write-intent with NULL `step_uuid` (pre-FK
  legacy) can't attach to a step; render those in a small "unlinked writes" group
  under the run so they're still actionable.

## Data layer (`db/assistant.py`)

Two new read helpers (the action paths already exist):
- `list_assistant_runs(limit: int = 50) -> list[AssistantRun]` — recent runs,
  `started_at` desc.
- `list_write_intents_for_run(run_id: int) -> list[AssistantWriteIntent]` — all
  intents for a run (the view buckets them by `step_uuid`).

The view assembles: `run`, `steps = list_assistant_steps(run_id)`,
`intents_by_step = {step_uuid: [intent, …]}` plus an `unlinked` bucket.

## Page structure (`webapp/assistant_views.py`)

- **Left — runs list.** Each row: status badge (`running`/`finished`/`failed`/
  `stopped`/`stopping`/`killed`), `#id`, room (short uuid), started-at, step
  count. Active row highlighted. Empty state when no runs.
- **Right — timeline** (only when `?run=` selects one):
  - Run header: status, journal id, started/finished, `final_summary`, plus
    **Stop**/**Redirect** when running, and a pending-controls banner.
  - Step cards in `id` order: step_index, phase badge, action, reason, args
    (`<pre>`), observation_preview, error, model. A `control` step is styled
    distinctly (it's an operator event, not a model action).
  - Under a step, its write-intent(s): capability, state badge, preview_text,
    payload (`<pre>`), and the state-appropriate buttons.
- Inline JS: `ppAssistAction(url)` → `fetch(url, {method:'POST'})`, then
  `location.reload()` on `ok`; a tiny redirect-instruction `prompt()` wrapper.

## Testing (`webapp/test_assistant_views.py`, model-free)

Mirror `test_doctor_views.py` / `test_memory_views.py` with Flask's test client:
- `GET /assistant` renders the runs list (seed a couple of runs).
- `GET /assistant?run=<id>` renders the step timeline in order, and a step's
  write-intent inline (seed a run + open/settle a step + a `proposed` intent
  bound by `step_uuid`).
- A `proposed` intent shows Confirm + Reject; a `completed` intent with an `undo`
  record shows Undo; a `completed` intent without one shows neither.
- Stop/Redirect appear for a `running` run and are absent for a `finished` run.
- The rendered action controls target the correct existing endpoint URLs.
- `list_assistant_runs` / `list_write_intents_for_run` unit coverage in
  `db/test_assistant_trace.py` (ordering; bucketing incl. an unlinked intent).

## Out of scope (noted, not built)

- Live streaming / auto-poll (SSE / LISTEN-NOTIFY) — S7 follow-up.
- Kill (watchdog) and retry (re-enqueue) controls — S7 follow-up.
- Editing step/run/intent fields — Flask-Admin already covers raw edits.
- Pagination / filtering of the runs list beyond a `limit` (add when the list
  grows unwieldy).

## Acceptance

From `/assistant` the operator can pick a run, read its full step timeline with
each write-intent inline, approve/reject a proposed confirm-tier write and undo a
completed log-and-undo write, and stop/redirect a live run — all wired to the
existing endpoints, with the suite green and no raw `curl` needed.
