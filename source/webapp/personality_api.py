"""JSON API backing the /personality page.

Save shape per docs/ui-tree-persistence.md: the tree PUT only updates rows
that already exist (a payload that omits or invents one is a 400), and
creation/deletion are their own endpoints. Every mutating response carries the
new tree `version`, so the client never holds a stale token. Personality text
is read and written per-personality, and its history lives behind the
/revisions routes.
"""
from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


@app.route("/personality/api/tree", methods=["GET", "PUT"])
def personality_tree() -> tuple[Response, int] | Response:
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
            db.personality_save_tree(data.get("folders", []),
                                     data.get("personalities", []),
                                     base_version=version)
        except db.PersonalityTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.personality_tree_version()}), 409
        except db.PersonalityTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.personality_tree_version()})
    return jsonify(db.personality_load_tree())


@app.route("/personality/api/folders", methods=["POST"])
def personality_create_folder_route() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "folder name required"}), 400
    parent_raw = data.get("parentId")
    parent_uuid = None
    if parent_raw:
        parent_uuid = _parse_uuid(parent_raw)
        if parent_uuid is None:
            return jsonify({"ok": False, "error": "bad parentId"}), 400
    folder = db.personality_create_folder(name, parent_uuid)
    return jsonify({"ok": True, "folder": folder,
                    "version": db.personality_tree_version()}), 201


@app.route("/personality/api/personalities", methods=["POST"])
def personality_create_route() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "personality name required"}), 400
    folder_raw = data.get("folderId")
    folder_uuid = None
    if folder_raw:
        folder_uuid = _parse_uuid(folder_raw)
        if folder_uuid is None:
            return jsonify({"ok": False, "error": "bad folderId"}), 400
    made = db.personality_create(name, folder_uuid)
    return jsonify({"ok": True, "personality": made,
                    "version": db.personality_tree_version()}), 201


@app.route("/personality/api/folders/<folder_uuid>", methods=["DELETE"])
def personality_delete_folder_route(folder_uuid: str) -> tuple[Response, int] | Response:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.personality_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True, "version": db.personality_tree_version()})


@app.route("/personality/api/personalities/<personality_uuid>", methods=["DELETE"])
def personality_delete_route(personality_uuid: str) -> tuple[Response, int] | Response:
    pu = _parse_uuid(personality_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.personality_delete(pu):
        return jsonify({"ok": False, "error": "personality not found"}), 404
    return jsonify({"ok": True, "version": db.personality_tree_version()})
