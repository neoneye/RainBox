"""JSON API backing the /prompt page's persistence + version lineage.

Bulk load/save of the whole prompt tree (folders + prompts) using the
frontend's field names (folder `id`/`parentId`, prompt `uuid`/`folderId`/
`parentUuid`), so the page sends/receives its in-browser arrays almost
verbatim. The save is an upsert by uuid (db.prompt_save_tree), validated
server-side (db.validate_prompt_tree) so a malformed tree is rejected with
400, not 500 — and it never carries `content`. Content is read/written
per-prompt (GET/PUT prompts/<uuid>), new versions are made by `clone`, and
`diff` returns a unified diff against an ancestor. Mirrors webapp/git_api.py.
"""
from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


@app.route("/prompt/api/tree", methods=["GET", "PUT"])
def prompt_tree() -> tuple[Response, int] | Response:
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
            db.prompt_save_tree(data.get("folders", []), data.get("prompts", []),
                                base_version=version, expected_deletes=deletes)
        except db.PromptTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.prompt_tree_version()}), 409
        except db.PromptTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.prompt_tree_version()})
    return jsonify(db.prompt_load_tree())


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


@app.route("/prompt/api/prompts/<prompt_uuid>", methods=["GET", "PUT"])
def prompt_detail(prompt_uuid: str) -> tuple[Response, int] | Response:
    """GET: one prompt incl. content + parent info, for the editor pane.
    PUT {content}: textarea autosave (last write wins)."""
    pu = _parse_uuid(prompt_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            return jsonify({"ok": False, "error":
                            "request body must be a JSON object with string 'content'"}), 400
        if not db.prompt_update_content(pu, data["content"]):
            return jsonify({"ok": False, "error": "prompt not found"}), 404
        return jsonify({"ok": True})
    detail = db.prompt_get(pu)
    if detail is None:
        return jsonify({"ok": False, "error": "prompt not found"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/prompt/api/prompts/<prompt_uuid>/clone", methods=["POST"])
def prompt_clone_route(prompt_uuid: str) -> tuple[Response, int] | Response:
    """Make a new version: copy the prompt into a new row whose parentUuid is
    the source, placed right after it. Returns the new row (no content)."""
    pu = _parse_uuid(prompt_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    clone = db.prompt_clone(pu)
    if clone is None:
        return jsonify({"ok": False, "error": "prompt not found"}), 404
    return jsonify({"ok": True, "prompt": clone})


@app.route("/prompt/api/prompts/<prompt_uuid>/diff")
def prompt_diff_route(prompt_uuid: str) -> tuple[Response, int] | Response:
    """Unified diff of an ancestor's content → this prompt's content.
    ?against=<uuid> picks the ancestor (default: the immediate parent)."""
    pu = _parse_uuid(prompt_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    against_raw = request.args.get("against")
    against = None
    if against_raw is not None:
        against = _parse_uuid(against_raw)
        if against is None:
            return jsonify({"ok": False, "error": "bad 'against' uuid"}), 400
    result = db.prompt_diff(pu, against)
    if not result.get("ok"):
        status = 404 if result.get("error") == "prompt not found" else 400
        return jsonify(result), status
    return jsonify(result)
