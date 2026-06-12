"""JSON API backing the /cron page's persistence.

Bulk load/save of the whole cron tree. The JSON uses the frontend's field names
(folder `id`/`parentId`, job `uuid`/`folderId`/`cron`/`type`) so the page
sends/receives its in-browser arrays almost verbatim. The save is an upsert by
uuid (see `db.cron_save_tree`) and is validated server-side
(`db.validate_cron_tree`) so a malformed tree is rejected with 400, not 500.
Per-node CRUD/reorder endpoints are a later refinement (see docs/cron-design.md).
Also exposes a manual "Run now" (`POST /cron/api/jobs/<uuid>/run`) that fires a
job immediately; scheduled firing is driven by the supervisor loop's cron tick.
"""

from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


@app.route("/cron/api/tree", methods=["GET", "PUT"])
def cron_tree() -> tuple[Response, int] | Response:
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            # Non-JSON or a non-object body (list/string/number) → 400, not 500.
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        # The PUT replaces the whole tree, so it must carry the version token
        # it hydrated with (GET returns it); a stale token is a 409 and the
        # page re-hydrates instead of clobbering the other writer's changes.
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        # Deletions must be declared: rows absent from the payload are deleted,
        # and an undeclared deletion is more likely a truncated payload (a
        # frontend bug) than an intentional edit.
        deletes = data.get("deletes", 0)
        if not isinstance(deletes, int) or isinstance(deletes, bool) or deletes < 0:
            return jsonify({"ok": False, "error":
                            "'deletes' must be a non-negative integer"}), 400
        try:
            db.cron_save_tree(data.get("folders", []), data.get("jobs", []),
                              base_version=version, expected_deletes=deletes)
        except db.CronTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.cron_tree_version()}), 409
        except db.CronTreeError as exc:
            # Invalid payload (bad shape, bad uuid, dangling/cyclic folder, bad cron, …).
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.cron_tree_version()})
    return jsonify(db.cron_load_tree())


@app.route("/cron/api/jobs/<job_uuid>/run", methods=["POST"])
def cron_run_now(job_uuid: str) -> tuple[Response, int] | Response:
    """Fire a job immediately ("Run now"), independent of its schedule. The
    action runs through the same path as a scheduled fire (events post to the
    cron room); does not advance next_run_at. Works even while globally paused
    (an explicit click is not a scheduled fire). `?debug=1` makes it a dry-run:
    the fire reports what it WOULD do (message text + destination, backup
    destination, validated command argv) without doing it."""
    try:
        ju = UUID(job_uuid)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    debug = request.args.get("debug") in ("1", "true", "yes")
    run = db.fire_cron_job_by_uuid(ju, trigger="manual", debug=debug)
    if run is None:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "debug": debug})


@app.route("/cron/api/jobs/<job_uuid>/health")
def cron_job_health(job_uuid: str) -> tuple[Response, int] | Response:
    """Health snapshot for the Job-details panel: run counts by outcome, last
    success/error, the recent runs (status + error), and the next 3 upcoming
    fire times."""
    try:
        ju = UUID(job_uuid)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    health = db.cron_job_health(ju)
    if health is None:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, **health})


@app.route("/cron/api/pause", methods=["POST"])
@app.route("/cron/api/resume", methods=["POST"])
def cron_pause_resume() -> Response:
    """Global pause/resume: one flag the scheduler checks before firing. It
    never touches per-job/folder enabled flags, so resuming restores the exact
    prior state."""
    from db.settings import set_setting

    paused = request.path.endswith("/pause")
    set_setting("cron.paused", paused)
    return jsonify({"ok": True, "paused": paused})
