"""JSON API backing /assistant-overview (static/assistant-overview.js hydrates
from it). One endpoint returns a filtered/sorted/paginated page of Assistant
runs, each serialized to a flat row with a derived status chip. Server-side
paging scales past the inspector's 50-run left panel.

The status chip mirrors _dash_status in assistant_views.py (Running / Resolved
/ Unresolved / pending) and additionally surfaces Stopped as its own kind,
matching the overview's Stopped facet."""
import math
from datetime import UTC, datetime, timedelta

from flask import Response, jsonify, request

import db

from .assistant_views import _format_duration
from .core import app

_SORT_KEYS = {"started", "summary", "steps", "duration"}
_STATUS_KEYS = {"all", "running", "stopped", "resolved", "unresolved"}

# Time-range picker → how far back to look. "all" (any time) maps to no cutoff.
_RANGE_DELTAS = {
    "3h": timedelta(hours=3),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "48h": timedelta(hours=48),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_RANGE_KEYS = {"all", *_RANGE_DELTAS}


def _overview_status(run) -> tuple[str, str]:
    """(label, kind) for the overview chip. kind ∈
    running|stopped|resolved|unresolved|pending."""
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
        return ("—", "pending")        # terminal but not yet summarized
    return ("Unresolved", "unresolved")


def _serialize_run(run, step_count: int) -> dict:
    """A run flattened to one overview-table row. `summary` is the digest's
    trigger line, or None while the run is still being summarized."""
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
    }


@app.route("/assistant-overview/api/runs")
def assistant_overview_runs() -> tuple[Response, int] | Response:
    """A page of runs for the overview table: ?q&status&sort&dir&page&per_page."""
    q = request.args.get("q", "")
    status = request.args.get("status", "all")
    range_ = request.args.get("range", "all")
    sort = request.args.get("sort", "started")
    direction = request.args.get("dir", "desc")
    if status not in _STATUS_KEYS:
        return jsonify({"ok": False, "error": "bad status"}), 400
    if range_ not in _RANGE_KEYS:
        return jsonify({"ok": False, "error": "bad range"}), 400
    if sort not in _SORT_KEYS:
        return jsonify({"ok": False, "error": "bad sort"}), 400
    if direction not in ("asc", "desc"):
        return jsonify({"ok": False, "error": "bad dir"}), 400
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 25))
    except (TypeError, ValueError):
        return jsonify({"ok": False,
                        "error": "page/per_page must be integers"}), 400
    per_page = max(5, min(100, per_page))
    page = max(1, page)
    delta = _RANGE_DELTAS.get(range_)
    since = datetime.now(UTC) - delta if delta else None

    runs, total, counts = db.list_assistant_runs_page(
        q=q, status=status, since=since, sort=sort, direction=direction,
        offset=(page - 1) * per_page, limit=per_page,
    )
    step_counts = db.assistant_step_counts([r.uuid for r in runs])
    rows = [_serialize_run(r, step_counts.get(r.uuid, 0)) for r in runs]
    pages = max(1, math.ceil(total / per_page)) if total else 1
    return jsonify({
        "ok": True, "runs": rows, "total": total,
        "page": page, "pages": pages, "per_page": per_page, "counts": counts,
    })
