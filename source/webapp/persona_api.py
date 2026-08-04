"""JSON API backing the /persona page.

Save shape per docs/ui-tree-persistence.md: the tree PUT only updates rows
that already exist (a payload that omits or invents one is a 400), and
creation/deletion are their own endpoints. Every tree-structure endpoint
(the tree PUT, folder/persona create, folder/persona delete) carries
the new tree `version` in its response, so the client never holds a stale
token. Persona text is read and written per-persona via its own PUT,
and restored via the /revisions routes; those content endpoints don't touch
placement, so they deliberately carry no version token.
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


@app.route("/persona/api/tree", methods=["GET", "PUT"])
def persona_tree() -> tuple[Response, int] | Response:
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False,
                            "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.persona_save_tree(data.get("folders", []),
                                     data.get("personas", []),
                                     base_version=version)
        except db.PersonaTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.persona_tree_version()}), 409
        except db.PersonaTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.persona_tree_version()})
    return jsonify(db.persona_load_tree())


@app.route("/persona/api/folders", methods=["POST"])
def persona_create_folder_route() -> tuple[Response, int]:
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
    folder = db.persona_create_folder(name, parent_uuid)
    return jsonify({"ok": True, "folder": folder,
                    "version": db.persona_tree_version()}), 201


@app.route("/persona/api/personas", methods=["POST"])
def persona_create_route() -> tuple[Response, int]:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "request body must be a JSON object"}), 400
    name_raw = data.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        return jsonify({"ok": False, "error": "persona name required"}), 400
    name = (name_raw or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "persona name required"}), 400
    folder_raw = data.get("folderId")
    if folder_raw is not None and not isinstance(folder_raw, str):
        return jsonify({"ok": False, "error": "bad folderId"}), 400
    folder_uuid = None
    if folder_raw:
        folder_uuid = _parse_uuid(folder_raw)
        if folder_uuid is None:
            return jsonify({"ok": False, "error": "bad folderId"}), 400
    made = db.persona_create(name, folder_uuid)
    return jsonify({"ok": True, "persona": made,
                    "version": db.persona_tree_version()}), 201


@app.route("/persona/api/folders/<folder_uuid>", methods=["DELETE"])
def persona_delete_folder_route(folder_uuid: str) -> tuple[Response, int] | Response:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.persona_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True, "version": db.persona_tree_version()})


@app.route("/persona/api/personas/<persona_uuid>", methods=["DELETE"])
def persona_delete_route(persona_uuid: str) -> tuple[Response, int] | Response:
    pu = _parse_uuid(persona_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.persona_delete(pu):
        return jsonify({"ok": False, "error": "persona not found"}), 404
    return jsonify({"ok": True, "version": db.persona_tree_version()})


@app.route("/persona/api/personas/<persona_uuid>",
           methods=["GET", "PUT"])
def persona_detail(persona_uuid: str) -> tuple[Response, int] | Response:
    """GET: one persona incl. its current text, for the editor pane.
    PUT {content}: the editor's explicit Save — appends a revision when the
    text actually changed, and reports `changed: false` when it didn't."""
    pu = _parse_uuid(persona_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            return jsonify({"ok": False, "error":
                            "request body must be a JSON object with string "
                            "'content'"}), 400
        out = db.persona_update_content(pu, data["content"])
        if out is None:
            return jsonify({"ok": False, "error": "persona not found"}), 404
        return jsonify({"ok": True, **out})
    detail = db.persona_get(pu)
    if detail is None:
        return jsonify({"ok": False, "error": "persona not found"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/persona/api/personas/<persona_uuid>/revisions")
def persona_revisions_route(persona_uuid: str) -> tuple[Response, int] | Response:
    """The history, newest first — one row per saved state of the text."""
    pu = _parse_uuid(persona_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    revisions = db.persona_revisions(pu)
    if revisions is None:
        return jsonify({"ok": False, "error": "persona not found"}), 404
    return jsonify({"ok": True, "revisions": revisions})


@app.route("/persona/api/personas/<persona_uuid>"
           "/revisions/<revision_uuid>/diff")
def persona_revision_diff_route(
        persona_uuid: str, revision_uuid: str) -> tuple[Response, int] | Response:
    """Unified diff of that revision's text → the current text."""
    pu, ru = _parse_uuid(persona_uuid), _parse_uuid(revision_uuid)
    if pu is None or ru is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    result = db.persona_revision_diff(pu, ru)
    if not result.get("ok"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/persona/api/personas/<persona_uuid>"
           "/revisions/<revision_uuid>/restore", methods=["POST"])
def persona_restore_route(
        persona_uuid: str, revision_uuid: str) -> tuple[Response, int] | Response:
    """Bring an old revision back by appending a new one holding its text."""
    pu, ru = _parse_uuid(persona_uuid), _parse_uuid(revision_uuid)
    if pu is None or ru is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    result = db.persona_restore_revision(pu, ru)
    if not result.get("ok"):
        return jsonify(result), 404
    return jsonify(result)
