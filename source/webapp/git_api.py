"""JSON API backing the /git page's persistence + live repo inspection.

Bulk load/save of the whole git tree (folders + repos) using the frontend's
field names (folder `id`/`parentId`, repo `uuid`/`folderId`/`path`), so the
page sends/receives its in-browser arrays almost verbatim. The save is an
upsert by uuid (db.git_save_tree), validated server-side (db.validate_git_tree)
so a malformed tree is rejected with 400, not 500. Plus `check-path` (validate
a filesystem path is a git repo before adding it) and a per-repo `detail` read
(path / current branch / root listing). Mirrors webapp/cron_api.py.
"""
from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


@app.route("/git/api/tree", methods=["GET", "PUT"])
def git_tree() -> tuple[Response, int] | Response:
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        # Whole-tree replace: must carry the version token from the last GET; a
        # stale token is a 409 and the page re-hydrates instead of clobbering.
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        # Deletions must be declared (an undeclared one is likely a truncated payload).
        deletes = data.get("deletes", 0)
        if not isinstance(deletes, int) or isinstance(deletes, bool) or deletes < 0:
            return jsonify({"ok": False, "error":
                            "'deletes' must be a non-negative integer"}), 400
        try:
            db.git_save_tree(data.get("folders", []), data.get("repos", []),
                             base_version=version, expected_deletes=deletes)
        except db.GitTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.git_tree_version()}), 409
        except db.GitTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.git_tree_version()})
    return jsonify(db.git_load_tree())


@app.route("/git/api/check-path", methods=["POST"])
def git_check_path_route() -> tuple[Response, int] | Response:
    """Validate that a typed filesystem path is an existing git repository,
    before the Add-repo flow creates a node. Returns {ok, path, branch} or
    {ok: False, error}; always 200 (the ok flag carries validity) unless the
    body itself is malformed."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
    return jsonify(db.git_check_path(data.get("path")))


@app.route("/git/api/repos/<repo_uuid>/detail")
def git_repo_detail_route(repo_uuid: str) -> tuple[Response, int] | Response:
    """Live detail for the repo pane: path, existence, isRepo, current branch,
    and the root directory listing."""
    try:
        ru = UUID(repo_uuid)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    detail = db.git_repo_detail(ru)
    if detail is None:
        return jsonify({"ok": False, "error": "repo not found"}), 404
    return jsonify({"ok": True, **detail})
