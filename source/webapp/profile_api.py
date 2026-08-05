"""JSON API backing the /profile page's persistence.

Save shape per docs/ui-tree-persistence.md: the tree PUT only updates rows that
already exist (a payload that omits or invents one is a 400), and creation and
deletion are their own endpoints. Every tree-structure endpoint (the tree PUT,
folder/profile create, duplicate, folder/profile delete) carries the new tree
`version` in its response, so the client never holds a stale token. The JSON
uses the frontend's field names (folder `id`/`parentId`, profile
`uuid`/`folderId`), so the page sends/receives its in-browser arrays almost
verbatim; a payload carrying a built-in uuid or the derived `summary` is
rejected with 400, not 500. The tree never carries `data`: the form's autosave
reads/writes it per-profile (GET/PUT profiles/<uuid>, validated against the
field registry with the connector-owned `dynamic` subtree preserved), and
`duplicate` copies a whole profile — the built-in templates included, which is
the only write that can touch them.
"""
from uuid import UUID

from flask import Response, jsonify, request

import db
import user_profile
from profile_fields import FIELDS_BY_KEY

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


@app.route("/profile/api/tree", methods=["GET", "PUT"])
def profile_tree() -> tuple[Response, int] | Response:
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
            db.profile_save_tree(data.get("folders", []), data.get("profiles", []),
                                 base_version=version)
        except db.ProfileTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.profile_tree_version()}), 409
        except db.ProfileTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.profile_tree_version()})
    return jsonify(db.profile_load_tree())


def _create_target_folder(data: dict) -> tuple[UUID | None, str | None]:
    """The `folderId`/`parentId` a create should land in: None (root), a real
    folder uuid, or an error string. A built-in uuid is rejected outright — the
    virtual Templates folder holds shipped templates and can never hold a user
    row (the client already redirects such creates to the root)."""
    raw = data.get("folderId", data.get("parentId"))
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str):
        return None, "bad folderId"
    parsed = _parse_uuid(raw)
    if parsed is None:
        return None, "bad folderId"
    if parsed in db.profile_builtin_uuids():
        return None, "read-only built-in folder"
    return parsed, None


def _create_name(data: dict) -> tuple[str, str | None]:
    raw = data.get("name")
    if raw is not None and not isinstance(raw, str):
        return "", "name required"
    name = (raw or "").strip()
    return name, None if name else "name required"


@app.route("/profile/api/folders", methods=["POST"])
def profile_create_folder_route() -> tuple[Response, int]:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "request body must be a JSON object"}), 400
    name, err = _create_name(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    parent_uuid, err = _create_target_folder(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    folder = db.profile_create_folder(name, parent_uuid)
    return jsonify({"ok": True, "folder": folder,
                    "version": db.profile_tree_version()}), 201


@app.route("/profile/api/profiles", methods=["POST"])
def profile_create_route() -> tuple[Response, int]:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False,
                        "error": "request body must be a JSON object"}), 400
    name, err = _create_name(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    folder_uuid, err = _create_target_folder(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    made = db.profile_create(name, folder_uuid)
    return jsonify({"ok": True, "profile": made,
                    "version": db.profile_tree_version()}), 201


@app.route("/profile/api/folders/<folder_uuid>", methods=["DELETE"])
def profile_delete_folder_route(folder_uuid: str) -> tuple[Response, int] | Response:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if fu in db.profile_builtin_uuids():
        return jsonify({"ok": False, "error": "read-only built-in"}), 400
    if not db.profile_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True, "version": db.profile_tree_version()})


@app.route("/profile/api/profiles/<profile_uuid>", methods=["DELETE"])
def profile_delete_route(profile_uuid: str) -> tuple[Response, int] | Response:
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if pu in db.profile_builtin_uuids():
        return jsonify({"ok": False, "error": "read-only built-in"}), 400
    if not db.profile_delete(pu):
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True, "version": db.profile_tree_version()})


@app.route("/profile/api/profiles/<profile_uuid>", methods=["GET", "PUT"])
def profile_detail(profile_uuid: str) -> tuple[Response, int] | Response:
    """GET: one profile's editable registry fields plus the read-only
    `dynamic` projection the form pane renders (built-ins come from the
    shipped file). The `languages` and `calibration` subtrees are projected
    out — each has its own GET/PUT below. PUT {data}: the flat form's complete
    autosave snapshot, canonicalized and validated against the registry, with
    independently written subtrees preserved; answers the fresh summary."""
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
    detail["data"] = {
        k: v for k, v in (detail.get("data") or {}).items()
        if k in FIELDS_BY_KEY or k == "dynamic"
    }
    return jsonify({"ok": True, **detail})


@app.route("/profile/api/profiles/<profile_uuid>/languages",
           methods=["GET", "PUT"])
def profile_languages(profile_uuid: str) -> tuple[Response, int] | Response:
    """The multilingual ``languages.rows`` subtree's own endpoint.

    GET returns the complete canonical row list. PUT replaces that list;
    existing rows carry their id, new rows omit it, and ``updated_at`` is
    server-owned.
    """
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "PUT":
        if pu in db.profile_builtin_uuids():
            return jsonify({"ok": False, "error": "read-only built-in"}), 400
        if (request.content_length or 0) > 1_000_000:
            return jsonify({"ok": False, "error":
                            "request body exceeds 1 MB"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            return jsonify({"ok": False, "error":
                            "request body must be a JSON object with list 'rows'"}), 400
        try:
            rows = db.languages_put(pu, data["rows"])
        except db.ProfileLanguagesError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if rows is None:
            return jsonify({"ok": False, "error": "profile not found"}), 404
        return jsonify({"ok": True, "builtin": False, "rows": rows})
    detail = db.languages_get(pu)
    if detail is None:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/profile/api/profiles/<profile_uuid>/calibration",
           methods=["GET", "PUT"])
def profile_calibration(profile_uuid: str) -> tuple[Response, int] | Response:
    """The knowledge-calibration subtree's own endpoint (never part of the
    flat registry-field PUT). GET: the canonical topic rows. PUT {topics}: a
    complete snapshot — existing rows carry their id, new rows omit it,
    `updated_at` is server-owned; last acknowledged write wins, matching the
    flat fields and /prompt content. A successful PUT answers the complete
    canonical snapshot because the client needs server-assigned ids and
    stamps before its next edit."""
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "PUT":
        if pu in db.profile_builtin_uuids():
            return jsonify({"ok": False, "error": "read-only built-in"}), 400
        # Cheap limits BEFORE parsing/iterating: the canonical 64 KiB cap is
        # the semantic layer, but it only runs after the whole body has been
        # parsed and traversed — a huge list of blank rows must be refused
        # up front, not after consuming memory and CPU.
        if (request.content_length or 0) > 1_000_000:
            return jsonify({"ok": False, "error":
                            "request body exceeds 1 MB"}), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
            return jsonify({"ok": False, "error":
                            "request body must be a JSON object with list 'topics'"}), 400
        try:
            topics = db.calibration_put(pu, data["topics"])
        except db.ProfileCalibrationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if topics is None:
            return jsonify({"ok": False, "error": "profile not found"}), 404
        return jsonify({"ok": True, "builtin": False, "topics": topics})
    detail = db.calibration_get(pu)
    if detail is None:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/profile/api/profiles/<profile_uuid>/export")
def profile_export(profile_uuid: str) -> tuple[Response, int] | Response:
    """One profile's prompt blocks, serialized for inspection.

    `format` is one of user_profile.FORMATS; `sections` is a comma-separated
    subset of user_profile.SECTIONS (default: all of them). The body is built
    by the same functions that build the real prompt blocks, so this endpoint
    can be trusted to answer "what does this profile put in the prompt?".
    Read-only, and the profile's own values are the only dynamic content.
    """
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    fmt = (request.args.get("format") or "json").strip().lower()
    if fmt not in user_profile.FORMATS:
        return jsonify({"ok": False, "error":
                        f"format must be one of {', '.join(user_profile.FORMATS)}"}), 400
    raw = request.args.get("sections")
    if raw is None:
        sections = list(user_profile.SECTIONS)
    else:
        sections = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = [s for s in sections if s not in user_profile.SECTIONS]
        if unknown:
            return jsonify({"ok": False, "error":
                            f"unknown section(s): {', '.join(unknown)}"}), 400
    profile = db.profile_get(pu)
    if profile is None:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    # Collect once, render every format: `sizes` is what makes the dialog's
    # byte counts comparable, and re-collecting per format would let the three
    # numbers describe three different documents.
    doc = user_profile.collect_sections(profile, sections)
    return jsonify({
        "ok": True,
        "format": fmt,
        "sections": sections,
        "text": user_profile.render(doc, fmt),
        "sizes": {f: len(user_profile.render(doc, f).encode("utf-8"))
                  for f in user_profile.FORMATS},
    })


@app.route("/profile/api/profiles/<profile_uuid>/duplicate", methods=["POST"])
def profile_duplicate_route(profile_uuid: str) -> tuple[Response, int] | Response:
    """Copy a profile's whole data blob into a new row: a user-owned source
    yields "<name> copy" right after it; a built-in template yields a real
    editable top-level row named after the template. A create, so it returns
    the new tree version alongside the new row."""
    pu = _parse_uuid(profile_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    new = db.profile_duplicate(pu)
    if new is None:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True, "profile": new,
                    "version": db.profile_tree_version()})
