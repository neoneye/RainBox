"""JSON API backing the /profile page's persistence.

Bulk load/save of the whole profile tree (folders + profiles) using the
frontend's field names (folder `id`/`parentId`, profile `uuid`/`folderId`),
so the page sends/receives its in-browser arrays almost verbatim. The save is
an upsert by uuid (db.profile_save_tree), validated server-side
(db.validate_profile_tree) so a malformed tree — or one carrying a built-in
uuid or the derived `summary` — is rejected with 400, not 500. It never
carries `data`: the form's autosave reads/writes it per-profile
(GET/PUT profiles/<uuid>, validated against the field registry with the
connector-owned `dynamic` subtree preserved), and `duplicate` copies a whole
profile — the built-in templates included, which is the only write that can
touch them. Mirrors webapp/prompt_api.py.
"""
from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


@app.route("/profile/api/tree", methods=["GET", "PUT"])
def profile_tree() -> tuple[Response, int] | Response:
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
            db.profile_save_tree(data.get("folders", []), data.get("profiles", []),
                                 base_version=version, expected_deletes=deletes)
        except db.ProfileTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.profile_tree_version()}), 409
        except db.ProfileTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.profile_tree_version()})
    return jsonify(db.profile_load_tree())


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


@app.route("/profile/api/profiles/<profile_uuid>", methods=["GET", "PUT"])
def profile_detail(profile_uuid: str) -> tuple[Response, int] | Response:
    """GET: one profile incl. its full data blob, for the form pane (built-ins
    come from the shipped file). PUT {data}: the form's autosave — a complete
    editable snapshot, canonicalized + validated against the registry, with
    the server's `dynamic` subtree preserved; answers the fresh summary."""
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "PUT":
        if pu in db.profile_builtin_uuids():
            return jsonify({"ok": False, "error": "read-only built-in"}), 400
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return jsonify({"ok": False, "error":
                            "request body must be a JSON object with object 'data'"}), 400
        try:
            summary = db.profile_update_data(pu, data["data"])
        except db.ProfileDataError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if summary is None:
            return jsonify({"ok": False, "error": "profile not found"}), 404
        return jsonify({"ok": True, "summary": summary})
    detail = db.profile_get(pu)
    if detail is None:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/profile/api/profiles/<profile_uuid>/duplicate", methods=["POST"])
def profile_duplicate_route(profile_uuid: str) -> tuple[Response, int] | Response:
    """Copy a profile's whole data blob into a new row: a user-owned source
    yields "<name> copy" right after it; a built-in template yields a real
    editable top-level row named after the template."""
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    new = db.profile_duplicate(pu)
    if new is None:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True, "profile": new})
