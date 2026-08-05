"""JSON API backing the /git page's persistence + live repo inspection.

Save shape per docs/ui-tree-persistence.md: the tree PUT only updates rows that
already exist (a payload that omits or invents one is a 400), and creation and
deletion are their own endpoints. Every tree-structure endpoint (the tree PUT,
folder/repo create, folder/repo delete) carries the new tree `version` in its
response, so the client never holds a stale token. The JSON uses the frontend's
field names (folder `id`/`parentId`, repo `uuid`/`folderId`/`path`), so the
page sends/receives its in-browser arrays almost verbatim. Repo creation
validates the typed path is a real git repository before it stores it. Plus a
per-repo `detail` read (path / current branch / root listing).
"""
from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


def _parse_uuid(raw: object) -> UUID | None:
    # Called on both URL path segments (always str) and untrusted JSON-body
    # values, so a non-string (dict, int, list, ...) must fail cleanly rather
    # than raising from inside the uuid module.
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


@app.route("/git/api/tree", methods=["GET", "PUT"])
def git_tree() -> tuple[Response, int] | Response:
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        # The PUT must carry the version token from the last GET; a stale token
        # is a 409 and the page re-hydrates instead of clobbering.
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.git_save_tree(data.get("folders", []), data.get("repos", []),
                             base_version=version)
        except db.GitTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.git_tree_version()}), 409
        except db.GitTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.git_tree_version()})
    return jsonify(db.git_load_tree())


@app.route("/git/api/folders", methods=["POST"])
def git_create_folder_route() -> tuple[Response, int]:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "request body must be a JSON object"}), 400
    name_raw = data.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        return jsonify({"ok": False, "error": "folder name required"}), 400
    name = (name_raw or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "folder name required"}), 400
    parent_raw = data.get("parentId")
    if parent_raw is not None and not isinstance(parent_raw, str):
        return jsonify({"ok": False, "error": "bad parentId"}), 400
    parent_uuid = None
    if parent_raw:
        parent_uuid = _parse_uuid(parent_raw)
        if parent_uuid is None:
            return jsonify({"ok": False, "error": "bad parentId"}), 400
    folder = db.git_create_folder(name, parent_uuid)
    return jsonify({"ok": True, "folder": folder,
                    "version": db.git_tree_version()}), 201


@app.route("/git/api/repos", methods=["POST"])
def git_create_repo_route() -> tuple[Response, int]:
    """Create one repo node. The path is re-validated here (not just in the
    browser's earlier check-path call) so the stored path is always one the
    server itself resolved to a real repository."""
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "request body must be a JSON object"}), 400
    checked = db.git_check_path(data.get("path"))
    if not checked.get("ok"):
        return jsonify({"ok": False, "error": checked.get("error")}), 400
    path = checked["path"]
    name_raw = data.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        return jsonify({"ok": False, "error": "bad repo name"}), 400
    # An empty name falls back to the path's last component, so a repo node
    # always has something readable in the tree.
    name = (name_raw or "").strip() or path.rstrip("/").split("/")[-1] or "repo"
    folder_raw = data.get("folderId")
    if folder_raw is not None and not isinstance(folder_raw, str):
        return jsonify({"ok": False, "error": "bad folderId"}), 400
    folder_uuid = None
    if folder_raw:
        folder_uuid = _parse_uuid(folder_raw)
        if folder_uuid is None:
            return jsonify({"ok": False, "error": "bad folderId"}), 400
    repo = db.git_create_repo(name, path, folder_uuid)
    return jsonify({"ok": True, "repo": repo,
                    "version": db.git_tree_version()}), 201


@app.route("/git/api/folders/<folder_uuid>", methods=["DELETE"])
def git_delete_folder_route(folder_uuid: str) -> tuple[Response, int] | Response:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.git_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True, "version": db.git_tree_version()})


@app.route("/git/api/repos/<repo_uuid>", methods=["DELETE"])
def git_delete_repo_route(repo_uuid: str) -> tuple[Response, int] | Response:
    ru = _parse_uuid(repo_uuid)
    if ru is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.git_delete_repo(ru):
        return jsonify({"ok": False, "error": "repo not found"}), 404
    return jsonify({"ok": True, "version": db.git_tree_version()})


@app.route("/git/api/repos/<repo_uuid>/detail")
def git_repo_detail_route(repo_uuid: str) -> tuple[Response, int] | Response:
    """Live detail for the repo pane: path, existence, isRepo, current branch,
    and the root directory listing."""
    ru = _parse_uuid(repo_uuid)
    if ru is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    detail = db.git_repo_detail(ru)
    if detail is None:
        return jsonify({"ok": False, "error": "repo not found"}), 404
    return jsonify({"ok": True, **detail})
