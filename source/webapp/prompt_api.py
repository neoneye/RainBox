"""JSON API backing the /prompt page's persistence + version lineage.

Save shape per notes/ui-tree-persistence.md: the tree PUT only updates rows that
already exist (a payload that omits or invents one is a 400), and creation and
deletion are their own endpoints. Every tree-structure endpoint (the tree PUT,
folder/prompt create, clone, folder/prompt delete) carries the new tree
`version` in its response, so the client never holds a stale token. The JSON
uses the frontend's field names (folder `id`/`parentId`, prompt
`uuid`/`folderId`/`parentUuid`), so the page sends/receives its in-browser
arrays almost verbatim. Prompt text is read and written per-prompt (GET/PUT
prompts/<uuid>) and `diff` returns a unified diff against an ancestor; those
content endpoints don't touch placement, so they deliberately carry no version
token.
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


@app.route("/prompt/api/tree", methods=["GET", "PUT"])
def prompt_tree() -> tuple[Response, int] | Response:
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
            db.prompt_save_tree(data.get("folders", []), data.get("prompts", []),
                                base_version=version)
        except db.PromptTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.prompt_tree_version()}), 409
        except db.PromptTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.prompt_tree_version()})
    return jsonify(db.prompt_load_tree())


@app.route("/prompt/api/folders", methods=["POST"])
def prompt_create_folder_route() -> tuple[Response, int]:
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
    folder = db.prompt_create_folder(name, parent_uuid)
    return jsonify({"ok": True, "folder": folder,
                    "version": db.prompt_tree_version()}), 201


@app.route("/prompt/api/prompts", methods=["POST"])
def prompt_create_route() -> tuple[Response, int]:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "request body must be a JSON object"}), 400
    name_raw = data.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        return jsonify({"ok": False, "error": "prompt name required"}), 400
    name = (name_raw or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "prompt name required"}), 400
    folder_raw = data.get("folderId")
    if folder_raw is not None and not isinstance(folder_raw, str):
        return jsonify({"ok": False, "error": "bad folderId"}), 400
    folder_uuid = None
    if folder_raw:
        folder_uuid = _parse_uuid(folder_raw)
        if folder_uuid is None:
            return jsonify({"ok": False, "error": "bad folderId"}), 400
    made = db.prompt_create(name, folder_uuid)
    return jsonify({"ok": True, "prompt": made,
                    "version": db.prompt_tree_version()}), 201


@app.route("/prompt/api/folders/<folder_uuid>", methods=["DELETE"])
def prompt_delete_folder_route(folder_uuid: str) -> tuple[Response, int] | Response:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.prompt_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True, "version": db.prompt_tree_version()})


@app.route("/prompt/api/prompts/<prompt_uuid>", methods=["DELETE"])
def prompt_delete_route(prompt_uuid: str) -> tuple[Response, int] | Response:
    pu = _parse_uuid(prompt_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.prompt_delete(pu):
        return jsonify({"ok": False, "error": "prompt not found"}), 404
    return jsonify({"ok": True, "version": db.prompt_tree_version()})


@app.route("/prompt/api/prompts/<prompt_uuid>", methods=["GET", "PUT"])
def prompt_detail(prompt_uuid: str) -> tuple[Response, int] | Response:
    """GET: one prompt incl. content + parent info, for the editor pane.
    PUT {content}: the editor's explicit Save (last write wins)."""
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
    the source, placed right after it. A create, so it returns the new tree
    version alongside the new row (no content)."""
    pu = _parse_uuid(prompt_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    clone = db.prompt_clone(pu)
    if clone is None:
        return jsonify({"ok": False, "error": "prompt not found"}), 404
    return jsonify({"ok": True, "prompt": clone,
                    "version": db.prompt_tree_version()})


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
