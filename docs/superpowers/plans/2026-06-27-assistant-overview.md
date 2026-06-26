# `/assistant-overview` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/assistant-overview` page — a searchable, filterable, sortable, paginated table of all Assistant ReAct loops — that replaces the cramped `/assistant` left panel as the way to find a run, and links each row to `/assistant?id=<uuid>`.

**Architecture:** Thin Jinja shell page + page-scoped JSON API (`/assistant-overview/api/runs`) + vanilla-JS static file, mirroring the `/cron`, `/kanban`, `/git` convention. The DB layer gains one paginated query + a step-count aggregate; the API serializes runs (status chip derived like `_dash_status`); the JS owns all interactivity.

**Tech Stack:** Python 3 / Flask (`render_template_string`, `jsonify`), SQLAlchemy (Postgres `rainbox_claude` for tests), vanilla JS (`fetch`, `createElement`), pytest (`app.test_client()`).

## Global Constraints

- Ad-hoc DB work targets `rainbox_claude`, never `rainbox_production` (`source/CLAUDE.md`). Tests are auto-forced to `rainbox_claude` by `conftest.py`.
- Docs/comments describe current state, not change history (no "renamed from", "PR N").
- Static JS is served at a bare `/static/<file>?v=<mtime>` cache-buster (NOT `url_for`).
- API responses use the house style: `{"ok": true, ...}` / `{"ok": false, "error": "..."}` + HTTP status.
- Render user-supplied text via `textContent` / `createElement`, never `innerHTML`.
- All new files live under `source/`. Run commands from `source/`.
- Status chip semantics mirror `_dash_status` in `webapp/assistant_views.py` so the overview and the inspector agree.
- Commit message trailer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- Create: `source/db/assistant.py` additions — `list_assistant_runs_page(...)`, `assistant_step_counts(...)` (query-only).
- Create: `source/webapp/assistant_overview_api.py` — `/assistant-overview/api/runs` + `_serialize_run` + `_overview_status`.
- Create: `source/webapp/assistant_overview_views.py` — `/assistant-overview` shell + JS cache-buster.
- Create: `source/static/assistant-overview.js` — frontend logic.
- Create: `source/webapp/test_assistant_overview_api.py`, `source/webapp/test_assistant_overview_views.py`.
- Modify: `source/webapp/__init__.py` — import the two new view/api modules.
- Modify: `source/webapp/core.py` — extend the "Assistant" nav active check to the new endpoint.

---

### Task 1: DB query layer — paginated runs + step counts

**Files:**
- Modify: `source/db/assistant.py` (add two functions near `list_assistant_runs`, line ~236)
- Test: `source/db/test_assistant_overview_query.py` (create)

**Interfaces:**
- Consumes: `AssistantRun`, `AssistantStep` models (`db/models.py`); existing `db.session`.
- Produces:
  - `assistant_step_counts(run_uuids: list[UUID]) -> dict[UUID, int]` — step count per run uuid (missing uuid ⇒ absent from dict, treat as 0).
  - `list_assistant_runs_page(*, q: str = "", status: str = "all", sort: str = "started", direction: str = "desc", offset: int = 0, limit: int = 25) -> tuple[list[AssistantRun], int, dict[str, int]]` — returns `(page_runs, total_matching, counts)` where `counts` has keys `all/running/stopped/resolved/unresolved`. Running runs are pinned before all others, then the chosen sort applies; `total`/`counts` are computed over the `q`-filtered set (NOT the status-filtered set, so the facet tabs can show their counts).

**Notes for implementer:**
- `summary` is a JSONB column; the human summary text is `summary->>'trigger'`. Search `q` matches `summary->>'trigger'` OR `final_summary` OR `CAST(uuid AS text)` (case-insensitive `ILIKE %q%`).
- Status facet predicates (SQLAlchemy):
  - `running`: `status IN ('running','stopping')`
  - `stopped`: `status == 'stopped'`
  - `resolved`: `summary['outcome'].astext == 'resolved'`
  - `unresolved`: `summary['outcome'].astext IN ('partial','failed') OR status IN ('failed','killed')`
- Sort keys → column:
  - `started` → `started_at`
  - `summary` → `summary['trigger'].astext`
  - `duration` → `(finished_at - started_at)` (nulls last)
  - `steps` → correlated `COUNT(assistant_step)` (left join + group, or scalar subquery)
- Running-pin: order by `case(status in running → 0 else 1)` first, then the sort key + direction.

- [ ] **Step 1: Write the failing test**

```python
# source/db/test_assistant_overview_query.py
"""Tests for db.list_assistant_runs_page + db.assistant_step_counts.

Live local Postgres (rainbox_claude via conftest). Seeds runs/steps in a
db.make_app() context, then queries through the same session."""
from datetime import datetime, timedelta, UTC
from uuid import uuid4

import db
from db import AssistantRun, AssistantStep
from db.core import db as _db


def _mk_run(*, summary_trigger=None, outcome=None, status="finished",
            started=None, n_steps=0):
    app = db.make_app()
    with app.app_context():
        run = AssistantRun(
            uuid=uuid4(), journal_id=uuid4(), room_uuid=uuid4(),
            agent_uuid=uuid4(), status=status, step_limit=6,
            started_at=started or datetime.now(UTC),
            finished_at=None if status in ("running", "stopping")
            else (started or datetime.now(UTC)) + timedelta(seconds=10),
        )
        if summary_trigger is not None or outcome is not None:
            run.summary = {"trigger": summary_trigger, "outcome": outcome}
        _db.session.add(run)
        for i in range(n_steps):
            _db.session.add(AssistantStep(
                uuid=uuid4(), run_uuid=run.uuid, step_index=i, phase="observed"))
        _db.session.commit()
        return run.uuid


def test_page_filters_by_summary_substring():
    app = db.make_app()
    tag = uuid4().hex[:8]
    _mk_run(summary_trigger=f"buy candy {tag}", outcome="resolved")
    _mk_run(summary_trigger=f"solve riemann {tag}", outcome="failed")
    with app.app_context():
        runs, total, counts = db.list_assistant_runs_page(q=f"candy {tag}")
        assert total == 1
        assert runs[0].summary["trigger"] == f"buy candy {tag}"


def test_step_counts_aggregate():
    app = db.make_app()
    rid = _mk_run(summary_trigger="x", outcome="resolved", n_steps=4)
    with app.app_context():
        counts = db.assistant_step_counts([rid])
        assert counts.get(rid) == 4


def test_running_runs_pinned_first():
    app = db.make_app()
    tag = uuid4().hex[:8]
    old = datetime(2020, 1, 1, tzinfo=UTC)
    _mk_run(summary_trigger=f"done {tag}", outcome="resolved",
            status="finished", started=old + timedelta(days=2))
    _mk_run(summary_trigger=f"live {tag}", status="running", started=old)
    with app.app_context():
        runs, _t, _c = db.list_assistant_runs_page(q=tag, sort="started",
                                                   direction="desc")
        assert runs[0].status == "running"  # pinned despite older started_at


def test_status_facet_counts():
    app = db.make_app()
    tag = uuid4().hex[:8]
    _mk_run(summary_trigger=f"a {tag}", status="running")
    _mk_run(summary_trigger=f"b {tag}", outcome="resolved", status="finished")
    _mk_run(summary_trigger=f"c {tag}", status="stopped")
    with app.app_context():
        runs, total, counts = db.list_assistant_runs_page(q=tag, status="running")
        assert counts["all"] == 3
        assert counts["running"] == 1
        assert counts["stopped"] == 1
        assert counts["resolved"] == 1
        assert total == 1  # status filter applied to the returned page
        assert all(r.status in ("running", "stopping") for r in runs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && python -m pytest db/test_assistant_overview_query.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'list_assistant_runs_page'`.

- [ ] **Step 3: Write the implementation**

Add to `source/db/assistant.py` (after `list_assistant_runs`, ~line 244):

```python
def assistant_step_counts(run_uuids: list[UUID]) -> dict[UUID, int]:
    """Number of step rows per run, for a batch of runs (one GROUP BY — no
    N+1). Runs with no steps are absent from the result (caller treats as 0)."""
    if not run_uuids:
        return {}
    rows = (
        db.session.query(AssistantStep.run_uuid, func.count())
        .filter(AssistantStep.run_uuid.in_(run_uuids))
        .group_by(AssistantStep.run_uuid)
        .all()
    )
    return {run_uuid: n for run_uuid, n in rows}


def _overview_q_filter(query, q: str):
    """Case-insensitive substring over the human-facing run text: the summary
    digest's trigger line, the truncated final_summary, and the uuid."""
    needle = f"%{q.strip()}%"
    return query.filter(
        sa.or_(
            AssistantRun.summary["trigger"].astext.ilike(needle),
            AssistantRun.final_summary.ilike(needle),
            sa.cast(AssistantRun.uuid, sa.Text).ilike(needle),
        )
    )


_OVERVIEW_STATUS_PREDICATES = {
    "running": lambda: AssistantRun.status.in_(("running", "stopping")),
    "stopped": lambda: AssistantRun.status == "stopped",
    "resolved": lambda: AssistantRun.summary["outcome"].astext == "resolved",
    "unresolved": lambda: sa.or_(
        AssistantRun.summary["outcome"].astext.in_(("partial", "failed")),
        AssistantRun.status.in_(("failed", "killed")),
    ),
}


def list_assistant_runs_page(
    *, q: str = "", status: str = "all", sort: str = "started",
    direction: str = "desc", offset: int = 0, limit: int = 25,
) -> tuple[list[AssistantRun], int, dict[str, int]]:
    """A filtered/sorted/paginated page of runs for /assistant-overview, plus
    the total matching the page filter and the per-facet counts (over the
    q-filtered set, so the status tabs can show their numbers).

    Running runs are pinned ahead of the rest; the chosen sort orders within.
    """
    base = _overview_q_filter(db.session.query(AssistantRun), q) if q.strip() \
        else db.session.query(AssistantRun)

    # Facet counts over the q-filtered set (status-independent).
    counts = {"all": base.count()}
    for key, pred in _OVERVIEW_STATUS_PREDICATES.items():
        counts[key] = base.filter(pred()).count()

    page_q = base
    if status in _OVERVIEW_STATUS_PREDICATES:
        page_q = page_q.filter(_OVERVIEW_STATUS_PREDICATES[status]())
    total = page_q.count()

    # Step count as a scalar correlated subquery (also the 'steps' sort key).
    step_count = (
        sa.select(func.count(AssistantStep.id))
        .where(AssistantStep.run_uuid == AssistantRun.uuid)
        .correlate(AssistantRun)
        .scalar_subquery()
    )
    sort_col = {
        "started": AssistantRun.started_at,
        "summary": AssistantRun.summary["trigger"].astext,
        "duration": (AssistantRun.finished_at - AssistantRun.started_at),
        "steps": step_count,
    }.get(sort, AssistantRun.started_at)
    ordering = sort_col.asc() if direction == "asc" else sort_col.desc()

    running_first = sa.case(
        (AssistantRun.status.in_(("running", "stopping")), 0), else_=1
    )
    page_runs = (
        page_q.order_by(running_first, ordering,
                        AssistantRun.uuid.desc())
        .offset(max(0, offset)).limit(max(1, limit)).all()
    )
    return page_runs, total, counts
```

Ensure the imports at the top of `db/assistant.py` include `sqlalchemy as sa` and `from sqlalchemy import func` (add if missing — check the existing import block first and reuse its style).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd source && python -m pytest db/test_assistant_overview_query.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add source/db/assistant.py source/db/test_assistant_overview_query.py
git commit -m "feat(assistant): paginated run-list query for overview page

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: JSON API — `/assistant-overview/api/runs`

**Files:**
- Create: `source/webapp/assistant_overview_api.py`
- Modify: `source/webapp/__init__.py` (add import)
- Test: `source/webapp/test_assistant_overview_api.py`

**Interfaces:**
- Consumes: `db.list_assistant_runs_page`, `db.assistant_step_counts` (Task 1); `db.get_agent`-style lookup for agent name (check what exists — see note); the shared `app` from `webapp.core`.
- Produces: `GET /assistant-overview/api/runs` → `jsonify({"ok": True, "runs": [...], "total", "page", "pages", "per_page", "counts"})`. Each run row: `{uuid, summary, status_label, status_kind, started_date, started_time, steps, step_limit, duration, agent_name}`. Module-level helpers `_overview_status(run) -> tuple[str, str]` (label, kind) and `_serialize_run(run, step_count) -> dict`.

**Notes for implementer:**
- Read `webapp/assistant_views.py` `_dash_status` (line ~588) and `_format_duration` (~581) and reuse the *logic* (import them, or replicate — prefer import: `from .assistant_views import _format_duration`). For status, the overview needs a `stopped` kind distinct from unresolved, so write `_overview_status` locally:

```python
def _overview_status(run) -> tuple[str, str]:
    """(label, kind) for the overview chip. kind ∈
    running|stopped|resolved|unresolved|pending. Mirrors _dash_status but
    surfaces 'stopped' as its own kind (the overview has a Stopped facet)."""
    if run.status in ("running", "stopping"):
        return ("Running", "running")
    if run.status == "stopped":
        return ("Stopped", "stopped")
    outcome = (run.summary or {}).get("outcome")
    if outcome == "resolved":
        return ("Resolved", "resolved")
    if outcome in ("partial", "failed") or run.status in ("failed", "killed"):
        return ("Unresolved", "unresolved")
    if not run.summary:
        return ("—", "pending")
    return ("Unresolved", "unresolved")
```

- Agent name: check `db` for a helper (`grep -n "def get_agent\b\|def get_agent_config\|agent_name" source/db/*.py`). If one exists returning a config with `.name`/`.display_name`, use it (best-effort, wrapped so a missing agent ⇒ short uuid). If none is trivially available, fall back to the agent uuid's first 8 chars. Do NOT N+1 per row across the page if a batch helper exists; a per-row best-effort lookup over ≤100 rows is acceptable for v1 — note it.
- `per_page` clamped to [5, 100] (default 25); `page` ≥ 1; `pages = max(1, ceil(total/per_page))`; `offset = (page-1)*per_page`.
- Summary serialization: `run.summary["trigger"]` if present and truthy, else `None` (JS shows "summarizing…").

- [ ] **Step 1: Write the failing test**

```python
# source/webapp/test_assistant_overview_api.py
"""Tests for webapp/assistant_overview_api.py.

HTTP through the real app (webapp.core.app); seeding via db.make_app() — both
hit rainbox_claude (conftest)."""
from datetime import datetime, timedelta, UTC
from uuid import uuid4

import db
from db import AssistantRun, AssistantStep
from db.core import db as _db
from webapp.core import app


def _seed(summary_trigger, *, outcome=None, status="finished", n_steps=3):
    a = db.make_app()
    with a.app_context():
        run = AssistantRun(
            uuid=uuid4(), journal_id=uuid4(), room_uuid=uuid4(),
            agent_uuid=uuid4(), status=status, step_limit=6,
            started_at=datetime.now(UTC),
            finished_at=None if status == "running"
            else datetime.now(UTC) + timedelta(seconds=12),
            summary={"trigger": summary_trigger, "outcome": outcome},
        )
        _db.session.add(run)
        for i in range(n_steps):
            _db.session.add(AssistantStep(uuid=uuid4(), run_uuid=run.uuid,
                                          step_index=i, phase="observed"))
        _db.session.commit()
        return run.uuid


def test_runs_endpoint_shape():
    tag = uuid4().hex[:8]
    rid = _seed(f"candy {tag}", outcome="resolved")
    out = app.test_client().get(f"/assistant-overview/api/runs?q={tag}").get_json()
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["page"] == 1 and out["pages"] == 1
    row = out["runs"][0]
    assert row["uuid"] == str(rid)
    assert row["summary"] == f"candy {tag}"
    assert row["status_kind"] == "resolved"
    assert row["status_label"] == "Resolved"
    assert row["steps"] == 3
    assert row["step_limit"] == 6
    assert row["duration"]  # finished → has a duration string
    assert set(out["counts"]) == {"all", "running", "stopped",
                                  "resolved", "unresolved"}


def test_running_run_has_null_duration_and_pinned():
    tag = uuid4().hex[:8]
    _seed(f"done {tag}", outcome="resolved", status="finished")
    _seed(f"live {tag}", status="running", n_steps=1)
    out = app.test_client().get(f"/assistant-overview/api/runs?q={tag}").get_json()
    assert out["runs"][0]["status_kind"] == "running"
    assert out["runs"][0]["duration"] is None


def test_pagination_clamps_and_paginates():
    tag = uuid4().hex[:8]
    for i in range(7):
        _seed(f"item {i} {tag}", outcome="resolved")
    out = app.test_client().get(
        f"/assistant-overview/api/runs?q={tag}&per_page=5&page=2").get_json()
    assert out["total"] == 7
    assert out["pages"] == 2
    assert out["per_page"] == 5
    assert len(out["runs"]) == 2


def test_bad_page_param_is_400():
    resp = app.test_client().get("/assistant-overview/api/runs?page=abc")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && python -m pytest webapp/test_assistant_overview_api.py -v`
Expected: FAIL — 404 (route not registered) on the GETs.

- [ ] **Step 3: Write the implementation**

```python
# source/webapp/assistant_overview_api.py
"""JSON API backing /assistant-overview (static/assistant-overview.js hydrates
from it). One endpoint: a filtered/sorted/paginated page of Assistant runs,
each serialized to a flat row with a derived status chip. Server-side paging
scales past the inspector's 50-run left panel.

Status chip semantics mirror _dash_status in assistant_views.py (Running /
Resolved / Unresolved / pending) and additionally surface Stopped as its own
kind, matching the overview's Stopped facet."""
import math
from uuid import UUID

from flask import jsonify, request, Response

import db
from .core import app
from .assistant_views import _format_duration


def _overview_status(run) -> tuple[str, str]:
    if run.status in ("running", "stopping"):
        return ("Running", "running")
    if run.status == "stopped":
        return ("Stopped", "stopped")
    outcome = (run.summary or {}).get("outcome")
    if outcome == "resolved":
        return ("Resolved", "resolved")
    if outcome in ("partial", "failed") or run.status in ("failed", "killed"):
        return ("Unresolved", "unresolved")
    if not run.summary:
        return ("—", "pending")
    return ("Unresolved", "unresolved")


def _agent_name(agent_uuid) -> str:
    """Best-effort display name for a run's agent; the short uuid if unknown."""
    try:
        cfg = db.get_agent_config(agent_uuid)  # adjust to the real helper
        if cfg is not None:
            return getattr(cfg, "display_name", None) or getattr(cfg, "name", None) \
                or str(agent_uuid)[:8]
    except Exception:
        pass
    return str(agent_uuid)[:8]


def _serialize_run(run, step_count: int) -> dict:
    label, kind = _overview_status(run)
    trigger = (run.summary or {}).get("trigger")
    started = run.started_at
    return {
        "uuid": str(run.uuid),
        "summary": trigger if trigger else None,
        "status_label": label,
        "status_kind": kind,
        "started_date": started.strftime("%Y-%m-%d") if started else "",
        "started_time": started.strftime("%H:%M") if started else "",
        "steps": step_count,
        "step_limit": run.step_limit,
        "duration": _format_duration(run.started_at, run.finished_at),
        "agent_name": _agent_name(run.agent_uuid),
    }


_SORT_KEYS = {"started", "summary", "steps", "duration"}
_STATUS_KEYS = {"all", "running", "stopped", "resolved", "unresolved"}


@app.route("/assistant-overview/api/runs")
def assistant_overview_runs() -> tuple[Response, int] | Response:
    q = request.args.get("q", "")
    status = request.args.get("status", "all")
    sort = request.args.get("sort", "started")
    direction = request.args.get("dir", "desc")
    if status not in _STATUS_KEYS:
        return jsonify({"ok": False, "error": "bad status"}), 400
    if sort not in _SORT_KEYS:
        return jsonify({"ok": False, "error": "bad sort"}), 400
    if direction not in ("asc", "desc"):
        return jsonify({"ok": False, "error": "bad dir"}), 400
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 25))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "page/per_page must be integers"}), 400
    per_page = max(5, min(100, per_page))
    page = max(1, page)

    runs, total, counts = db.list_assistant_runs_page(
        q=q, status=status, sort=sort, direction=direction,
        offset=(page - 1) * per_page, limit=per_page,
    )
    step_counts = db.assistant_step_counts([r.uuid for r in runs])
    rows = [_serialize_run(r, step_counts.get(r.uuid, 0)) for r in runs]
    pages = max(1, math.ceil(total / per_page)) if total else 1
    return jsonify({
        "ok": True, "runs": rows, "total": total,
        "page": page, "pages": pages, "per_page": per_page, "counts": counts,
    })
```

Before running, confirm the agent-name helper: `grep -n "def get_agent_config\|def get_agent\b" source/db/*.py`. If the real name differs, fix `_agent_name`; if no helper exists, keep only the `str(agent_uuid)[:8]` fallback (drop the `db.get_agent_config` call).

Add to `source/webapp/__init__.py` (alongside the other view imports, ~line 37):

```python
from . import assistant_overview_api  # noqa: F401  (registers /assistant-overview/api/*)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd source && python -m pytest webapp/test_assistant_overview_api.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add source/webapp/assistant_overview_api.py source/webapp/test_assistant_overview_api.py source/webapp/__init__.py
git commit -m "feat(assistant): JSON API for the run overview page

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Page shell + nav active state

**Files:**
- Create: `source/webapp/assistant_overview_views.py`
- Modify: `source/webapp/__init__.py` (add import)
- Modify: `source/webapp/core.py` (nav active check for "Assistant")
- Test: `source/webapp/test_assistant_overview_views.py`

**Interfaces:**
- Consumes: shared `app` from `webapp.core`; `{% include "_nav.html" %}`.
- Produces: `GET /assistant-overview` → `assistant_overview_page()` rendering the HTML shell; the page links `/static/assistant-overview.js?v=<mtime>`. The endpoint name `assistant_overview_page` is what the nav active check keys on.

**Notes:** Mirror `cron_views.py`: an mtime cache-buster function + `render_template_string`. The shell contains the filter bar (search input, status `<select>`/tabs container, sort handled by table headers), a `<table>` with a `<thead>` (Date · Status · Summary · Steps · Duration) and an empty `<tbody id="ao-body">`, a pager container, a range-text element, and an empty-state element — all populated by JS. Keep page CSS in an inline `<style>` using the app's real palette.

- [ ] **Step 1: Write the failing test**

```python
# source/webapp/test_assistant_overview_views.py
"""Tests for webapp/assistant_overview_views.py + static/assistant-overview.js.

The page is a frontend shell; interactivity lives in the static JS. _body()
concatenates page + served JS so markers cover both."""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/assistant-overview").get_data(as_text=True)
    js = client.get("/static/assistant-overview.js")
    assert js.status_code == 200
    return page + js.get_data(as_text=True)


def test_page_renders_with_nav_and_js():
    body = app.test_client().get("/assistant-overview").get_data(as_text=True)
    assert "pp-nav" in body
    assert "/static/assistant-overview.js?v=" in body
    assert 'id="ao-body"' in body


def test_nav_marks_assistant_active():
    body = app.test_client().get("/assistant-overview").get_data(as_text=True)
    assert "pp-active" in body  # the Assistant link is highlighted here


def test_js_has_core_markers():
    b = _body()
    for marker in ["aoLoad", "aoRender", "/assistant-overview/api/runs",
                   "/assistant?id=", "aoRenderPager"]:
        assert marker in b, f"missing marker: {marker}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && python -m pytest webapp/test_assistant_overview_views.py -v`
Expected: FAIL — 404 on `/assistant-overview` (and the JS 404). (`aoLoad` markers fail until Task 4.)

- [ ] **Step 3: Write the page shell**

```python
# source/webapp/assistant_overview_views.py
"""The /assistant-overview page — a searchable, sortable, paginated table of
all Assistant ReAct loops (a roomier replacement for the /assistant left
panel). The shell is server-rendered; static/assistant-overview.js hydrates it
from /assistant-overview/api/runs and links each row to /assistant?id=<uuid>."""
from pathlib import Path

from flask import render_template_string

from .core import app

_JS = Path(__file__).resolve().parent.parent / "static" / "assistant-overview.js"


def _js_version() -> int:
    try:
        return int(_JS.stat().st_mtime)
    except OSError:
        return 0


OVERVIEW_TEMPLATE = """
<!doctype html>
<title>Assistant overview &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#fbfbfb;color:#374151}
  .ao-wrap{max-width:1320px;margin:0 auto;padding:24px 28px 56px}
  .ao-filters{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:18px}
  .ao-search{flex:1 1 260px;min-width:200px;padding:8px 12px;border:1px solid #e5e7eb;
    border-radius:6px;font:inherit;font-size:0.9rem;background:#fff;color:#1a1a2e}
  .ao-search:focus{outline:none;border-color:#2563eb}
  .ao-tabs{display:flex;gap:2px}
  .ao-tab{appearance:none;background:none;border:none;border-bottom:2px solid transparent;
    cursor:pointer;padding:9px 15px;font:inherit;font-size:0.9rem;font-weight:500;
    color:#6c757d;display:flex;align-items:center;gap:8px}
  .ao-tab.sel{border-bottom-color:#2563eb;font-weight:700;color:#1a1a2e}
  .ao-tab .ct{font-size:0.72rem;font-weight:600;color:#6b7280;background:#f3f4f6;
    padding:1px 8px;border-radius:999px;min-width:20px;text-align:center}
  .ao-tab.sel .ct{color:#2563eb;background:#dbeafe}
  .ao-table{width:100%;border-collapse:collapse;border:1px solid #e5e7eb;
    border-radius:8px;overflow:hidden;background:#fff}
  .ao-table th{background:#fbfbfb;border-bottom:1px solid #e5e7eb;text-align:left;
    padding:11px 14px;font-size:0.72rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.03em;color:#9ca3af;white-space:nowrap;user-select:none}
  .ao-table th.sortable{cursor:pointer}
  .ao-table td{padding:12px 14px;border-bottom:1px solid #e5e7eb;font-size:0.9rem}
  .ao-table tbody tr{cursor:pointer}
  .ao-table tbody tr:hover{background:#f1f5f9}
  .ao-date{font-size:0.8rem;color:#374151}
  .ao-time{font-size:0.72rem;color:#9ca3af;font-family:ui-monospace,Menlo,monospace;margin-top:2px}
  .ao-sum{font-weight:600;color:#1a1a2e;max-width:0;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .ao-sum.pending{font-weight:400;color:#98a2b3;font-style:italic}
  .ao-mono{font-family:ui-monospace,Menlo,monospace;font-size:0.8rem;color:#374151}
  .ao-chip{display:inline-flex;align-items:center;gap:6px;font-size:0.72rem;
    font-weight:600;padding:4px 10px;border-radius:999px;white-space:nowrap}
  .ao-chip.running{color:#1d4ed8;background:#dbeafe}
  .ao-chip.resolved{color:#16a34a;background:#dcfce7}
  .ao-chip.unresolved{color:#b91c1c;background:#fee2e2}
  .ao-chip.stopped{color:#6b7280;background:#f3f4f6}
  .ao-chip.pending{color:#9ca3af;background:#f3f4f6}
  .ao-dot{width:7px;height:7px;border-radius:999px;background:#2563eb;
    animation:aopulse 1.5s ease-in-out infinite}
  @keyframes aopulse{0%,100%{opacity:1}50%{opacity:0.3}}
  .ao-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;
    margin-top:18px;flex-wrap:wrap}
  .ao-range{font-size:0.8rem;color:#6b7280}
  .ao-pager{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .ao-pg{min-width:34px;padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;
    background:#fff;color:#374151;font:inherit;font-size:0.8rem;cursor:pointer}
  .ao-pg.sel{border-color:#2563eb;background:#2563eb;color:#fff;font-weight:700}
  .ao-pg:disabled{opacity:0.4;cursor:default}
  .ao-empty{border:1px dashed #d1d5db;border-radius:8px;padding:52px 24px;
    text-align:center;background:#fff}
  .ao-empty .t{font-size:0.95rem;color:#1a1a2e;font-weight:600;margin-bottom:6px}
  .ao-empty .s{font-size:0.8rem;color:#6b7280}
  [hidden]{display:none!important}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="ao-wrap">
  <div class="ao-filters">
    <input id="ao-search" class="ao-search" type="search" placeholder="Search summary&hellip;">
    <div id="ao-tabs" class="ao-tabs"></div>
  </div>
  <table class="ao-table" id="ao-table">
    <thead>
      <tr>
        <th class="sortable" data-sort="started">Date</th>
        <th>Status</th>
        <th class="sortable" data-sort="summary">Summary</th>
        <th class="sortable" data-sort="steps">Steps</th>
        <th class="sortable" data-sort="duration">Duration</th>
      </tr>
    </thead>
    <tbody id="ao-body"></tbody>
  </table>
  <div id="ao-empty" class="ao-empty" hidden>
    <div class="t">No runs match these filters</div>
    <div class="s">Try a different status or search.</div>
  </div>
  <div class="ao-foot" id="ao-foot" hidden>
    <div id="ao-range" class="ao-range"></div>
    <div id="ao-pager" class="ao-pager"></div>
  </div>
</div>
<script src="/static/assistant-overview.js?v={{ js_v }}"></script>
"""


@app.route("/assistant-overview")
def assistant_overview_page() -> str:
    return render_template_string(OVERVIEW_TEMPLATE, js_v=_js_version())
```

Add to `source/webapp/__init__.py` (with the other view imports):

```python
from . import assistant_overview_views  # noqa: F401  (registers /assistant-overview)
```

- [ ] **Step 4: Extend the nav active check**

In `source/webapp/core.py`, find the Assistant nav link:

```html
    <a href="{{ url_for('assistant_page') }}" class="{{ 'pp-active' if request.endpoint == 'assistant_page' }}">Assistant</a>
```

Replace its class condition so it also lights up on the overview:

```html
    <a href="{{ url_for('assistant_page') }}" class="{{ 'pp-active' if request.endpoint in ('assistant_page', 'assistant_overview_page') }}">Assistant</a>
```

- [ ] **Step 5: Run the view test (page + nav parts pass; JS markers still fail)**

Run: `cd source && python -m pytest webapp/test_assistant_overview_views.py::test_page_renders_with_nav_and_js webapp/test_assistant_overview_views.py::test_nav_marks_assistant_active -v`
Expected: PASS (2 tests). (`test_js_has_core_markers` stays red until Task 4 — that's fine.)

- [ ] **Step 6: Commit**

```bash
git add source/webapp/assistant_overview_views.py source/webapp/__init__.py source/webapp/core.py source/webapp/test_assistant_overview_views.py
git commit -m "feat(assistant): overview page shell + nav active state

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Frontend — `static/assistant-overview.js`

**Files:**
- Create: `source/static/assistant-overview.js`
- Test: `source/webapp/test_assistant_overview_views.py` (the `test_js_has_core_markers` from Task 3 now passes)

**Interfaces:**
- Consumes: `GET /assistant-overview/api/runs` (Task 2); DOM ids from the Task 3 shell (`ao-search`, `ao-tabs`, `ao-body`, `ao-empty`, `ao-foot`, `ao-range`, `ao-pager`, `ao-table` headers with `data-sort`).
- Produces: a self-contained page controller. Functions named `aoLoad`, `aoRender`, `aoRenderTabs`, `aoRenderPager` (markers asserted by the view test). Row click → `location.href = '/assistant?id=' + uuid`.

**Behavior:** state `{q, status, sort, dir, page, perPage:25}`; debounced search (250 ms) resets to page 1; clicking a status tab sets `status` + page 1; clicking a sortable header toggles dir (or switches key, default dir desc except `summary`→asc) and shows a ▲/▼ indicator; pager buttons set page; every state change calls `aoLoad` which fetches and re-renders. Render rows with `createElement`/`textContent` only.

- [ ] **Step 1: (test already written in Task 3 — confirm it currently fails on JS markers)**

Run: `cd source && python -m pytest webapp/test_assistant_overview_views.py::test_js_has_core_markers -v`
Expected: FAIL (file not found / markers missing).

- [ ] **Step 2: Write the frontend**

```javascript
// source/static/assistant-overview.js
// /assistant-overview page logic (vanilla JS, no framework). The HTML shell +
// CSS live in webapp/assistant_overview_views.py; this file is served at
// /static/assistant-overview.js with an mtime cache-buster. It hydrates a
// dense, sortable, paginated table from /assistant-overview/api/runs and links
// each row to the inspector at /assistant?id=<uuid>.
'use strict';

const aoState = { q: '', status: 'all', sort: 'started', dir: 'desc', page: 1, perPage: 25 };

const AO_TABS = [
  ['all', 'All'], ['running', 'Running'], ['stopped', 'Stopped'],
  ['resolved', 'Resolved'], ['unresolved', 'Unresolved'],
];

const aoEl = (id) => document.getElementById(id);

function aoChip(label, kind) {
  const span = document.createElement('span');
  span.className = 'ao-chip ' + kind;
  if (kind === 'running') {
    const dot = document.createElement('span');
    dot.className = 'ao-dot';
    span.appendChild(dot);
  }
  span.appendChild(document.createTextNode(label));
  return span;
}

function aoRow(run) {
  const tr = document.createElement('tr');
  tr.onclick = () => { location.href = '/assistant?id=' + encodeURIComponent(run.uuid); };

  const date = document.createElement('td');
  const d1 = document.createElement('div'); d1.className = 'ao-date'; d1.textContent = run.started_date;
  const d2 = document.createElement('div'); d2.className = 'ao-time'; d2.textContent = run.started_time;
  date.append(d1, d2);

  const status = document.createElement('td');
  status.appendChild(aoChip(run.status_label, run.status_kind));

  const sum = document.createElement('td');
  const s = document.createElement('div');
  s.className = 'ao-sum' + (run.summary ? '' : ' pending');
  s.textContent = run.summary || 'summarizing…';
  sum.appendChild(s);

  const steps = document.createElement('td');
  steps.className = 'ao-mono';
  steps.textContent = run.steps + ' / ' + run.step_limit;

  const dur = document.createElement('td');
  dur.className = 'ao-mono';
  dur.textContent = run.duration || '—';

  tr.append(date, status, sum, steps, dur);
  return tr;
}

function aoRenderTabs(counts) {
  const wrap = aoEl('ao-tabs');
  wrap.textContent = '';
  AO_TABS.forEach(([key, label]) => {
    const b = document.createElement('button');
    b.className = 'ao-tab' + (aoState.status === key ? ' sel' : '');
    b.textContent = label;
    const ct = document.createElement('span');
    ct.className = 'ct';
    ct.textContent = (counts && counts[key] != null) ? counts[key] : 0;
    b.appendChild(ct);
    b.onclick = () => { aoState.status = key; aoState.page = 1; aoLoad(); };
    wrap.appendChild(b);
  });
}

function aoRenderPager(total, page, pages) {
  const range = aoEl('ao-range');
  const pager = aoEl('ao-pager');
  pager.textContent = '';
  const from = total === 0 ? 0 : (page - 1) * aoState.perPage + 1;
  const to = Math.min(page * aoState.perPage, total);
  range.textContent = 'Showing ' + from + '–' + to + ' of ' + total + ' runs';

  const btn = (label, disabled, onClick, sel) => {
    const b = document.createElement('button');
    b.className = 'ao-pg' + (sel ? ' sel' : '');
    b.textContent = label;
    b.disabled = disabled;
    if (!disabled) b.onclick = onClick;
    return b;
  };
  pager.appendChild(btn('‹ Prev', page <= 1, () => { aoState.page = page - 1; aoLoad(); }));
  for (let n = 1; n <= pages; n++) {
    pager.appendChild(btn(String(n), false, () => { aoState.page = n; aoLoad(); }, n === page));
  }
  pager.appendChild(btn('Next ›', page >= pages, () => { aoState.page = page + 1; aoLoad(); }));
}

function aoRenderHeaders() {
  document.querySelectorAll('#ao-table th.sortable').forEach((th) => {
    const key = th.dataset.sort;
    const base = th.textContent.replace(/[↑↓]\s*$/, '').trim();
    th.textContent = base + (aoState.sort === key ? (aoState.dir === 'asc' ? ' ↑' : ' ↓') : '');
    th.onclick = () => {
      if (aoState.sort === key) {
        aoState.dir = aoState.dir === 'asc' ? 'desc' : 'asc';
      } else {
        aoState.sort = key;
        aoState.dir = key === 'summary' ? 'asc' : 'desc';
      }
      aoState.page = 1;
      aoLoad();
    };
  });
}

function aoRender(data) {
  aoRenderTabs(data.counts);
  aoRenderHeaders();
  const body = aoEl('ao-body');
  body.textContent = '';
  (data.runs || []).forEach((r) => body.appendChild(aoRow(r)));

  const empty = (data.runs || []).length === 0;
  aoEl('ao-table').hidden = empty;
  aoEl('ao-empty').hidden = !empty;
  aoEl('ao-foot').hidden = empty;
  if (!empty) aoRenderPager(data.total, data.page, data.pages);
}

async function aoLoad() {
  const p = new URLSearchParams({
    q: aoState.q, status: aoState.status, sort: aoState.sort,
    dir: aoState.dir, page: aoState.page, per_page: aoState.perPage,
  });
  try {
    const r = await fetch('/assistant-overview/api/runs?' + p.toString());
    const data = await r.json();
    if (!data || !data.ok) return;
    aoRender(data);
  } catch (e) { /* transient: next interaction retries */ }
}

let aoSearchTimer = null;
function aoInit() {
  aoRenderTabs(null);
  aoRenderHeaders();
  aoEl('ao-search').addEventListener('input', (e) => {
    clearTimeout(aoSearchTimer);
    const v = e.target.value;
    aoSearchTimer = setTimeout(() => { aoState.q = v; aoState.page = 1; aoLoad(); }, 250);
  });
  aoLoad();
}

document.addEventListener('DOMContentLoaded', aoInit);
```

- [ ] **Step 3: Run the full view test suite**

Run: `cd source && python -m pytest webapp/test_assistant_overview_views.py -v`
Expected: PASS (all 3 tests, including `test_js_has_core_markers`).

- [ ] **Step 4: Commit**

```bash
git add source/static/assistant-overview.js
git commit -m "feat(assistant): overview frontend — table, search, sort, paginate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Manual verification + full suite

**Files:** none (verification only).

- [ ] **Step 1: Run all new tests together**

Run: `cd source && python -m pytest db/test_assistant_overview_query.py webapp/test_assistant_overview_api.py webapp/test_assistant_overview_views.py -v`
Expected: all PASS.

- [ ] **Step 2: Confirm no regression in adjacent suites**

Run: `cd source && python -m pytest webapp/test_assistant_views.py -q` (if it exists; otherwise skip)
Expected: PASS (no change to the inspector).

- [ ] **Step 3: Manual smoke (optional, needs the app running)**

Use the project's run skill / normal launch, browse to `/assistant-overview`:
- Search box filters as you type; status tabs filter + show counts; column headers sort with ▲/▼; pager pages; a running loop (if any) sits on top with a pulsing blue chip; clicking a row opens `/assistant?id=<uuid>`; empty search shows the empty state.

- [ ] **Step 4: Final commit (only if any fixups were needed)**

```bash
git add -A
git commit -m "test(assistant): verify overview end-to-end

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** route ✓(T3) · JSON API w/ filter+sort+paginate ✓(T2) · DB paginated query + step counts ✓(T1) · status derivation mirroring `_dash_status` + Stopped facet ✓(T1 facets/T2 chip) · summary=`summary.trigger`/"summarizing…" ✓(T2/T4) · search over summary+final_summary+uuid ✓(T1) · running-first pinning ✓(T1) · row→`/assistant?id=` ✓(T4) · dense table columns ✓(T3/T4) · status chips + colors ✓(T3) · empty state ✓(T3/T4) · numbered pager + range text ✓(T4) · nav active ✓(T3) · default per_page 25 ✓(T2) · facets All/Running/Stopped/Resolved/Unresolved ✓(T1/T4) · tests ✓(T1–T4) · YAGNI (no roomy/live/agent-room/token) ✓.

**Placeholder scan:** the only deliberate "confirm the real helper name" notes are the `db.get_agent_config` agent-name lookup (Task 2) and the `sqlalchemy`/`func` import presence (Task 1) — both come with an explicit grep + a concrete fallback, not a TODO.

**Type consistency:** `list_assistant_runs_page(q, status, sort, direction, offset, limit) -> (runs, total, counts)` and `assistant_step_counts(list[UUID]) -> dict[UUID,int]` are used identically in Task 2. Endpoint `assistant_overview_page` matches the nav check (T3). JS function names `aoLoad/aoRender/aoRenderTabs/aoRenderPager` match the markers asserted in T3's test. Status kinds `running|stopped|resolved|unresolved|pending` match CSS classes in T3 and `_overview_status` in T2.
