"""JSON API for chat-bridge configuration (design:
docs/superpowers/specs/2026-09-09-bridge-settings-design.md).

The /bridges page's tree follows notes/ui-tree-persistence.md: the tree PUT
only moves/renames/reorders rows that exist (400 otherwise), and creation and
deletion are their own endpoints, each returning the new tree `version`.
Enabled flags and policies are per-item content with their own PUT.

- `GET/PUT /bridges/api/tree`
- `POST /bridges/api/connectors`, `GET/PUT/DELETE /bridges/api/connectors/<uuid>`
- `PUT/DELETE /bridges/api/connectors/<uuid>/credential` — write-only: the
  value is sealed into `bridge_credential` and never returned by anything;
  GET of the connector reports only `credential: {set, updated_at,
  key_configured}`.
- `POST /bridges/api/folders`, `PUT/DELETE /bridges/api/folders/<uuid>`
- `POST /bridges/api/bindings`, `PUT/DELETE /bridges/api/bindings/<uuid>`
- `GET /bridge/api/connectors/<uuid>/config` — what a bridge process fetches
  at SSE connect and on a `bridge_config` event.

Errors: 400 for a shape the adapter or validator refuses, 409 for a stale
tree token or an ownership refusal (with `blockers`), 404 for unknown rows.
"""
from uuid import UUID

from flask import Response, jsonify, request
from sqlalchemy.exc import IntegrityError

import db
from services import registry as services_registry
from services.bridge_adapters import AdapterError
from services.credential_box import CredentialKeyMissing

from .core import app


def _parse_uuid(raw: object) -> UUID | None:
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _body() -> dict | None:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _err(status: int, message: str, **extra: object) -> tuple[Response, int]:
    return jsonify({"ok": False, "error": message, **extra}), status


# --- tree ----------------------------------------------------------------------


@app.route("/bridges/api/tree", methods=["GET", "PUT"])
def bridges_tree() -> Response | tuple[Response, int]:
    if request.method == "GET":
        # `core_url` is what this core actually listens on (RAINBOX_CORE_PORT
        # honoured), for the copyable manual launch command.
        return jsonify({**db.bridge_load_tree(), "core_url": services_registry.rainbox_url()})
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        return _err(400, "missing tree 'version' (hydrate via GET first)")
    try:
        db.bridge_save_tree(data.get("connectors", []), data.get("folders", []),
                            data.get("bindings", []), base_version=version)
    except db.BridgeTreeConflict as exc:
        return _err(409, str(exc), version=db.bridge_tree_version())
    except db.BridgeTreeError as exc:
        return _err(400, str(exc))
    return jsonify({"ok": True, "version": db.bridge_tree_version()})


# --- connectors ------------------------------------------------------------------


@app.route("/bridges/api/connectors", methods=["POST"])
def bridges_create_connector() -> tuple[Response, int]:
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    try:
        row = db.bridge_create_connector(
            data.get("name"), data.get("platform"), data.get("token_env"),
            base_url=data.get("base_url"), identity=data.get("identity"), policy=data.get("policy"))
    except AdapterError as exc:
        return _err(400, str(exc))
    except db.BridgeBlocked as exc:
        return _err(409, str(exc), blockers=exc.blockers)
    return jsonify({"ok": True, "connector": row, "version": db.bridge_tree_version()}), 201


@app.route("/bridges/api/connectors/<connector_uuid>", methods=["GET", "PUT", "DELETE"])
def bridges_connector(connector_uuid: str) -> Response | tuple[Response, int]:
    cu = _parse_uuid(connector_uuid)
    if cu is None:
        return _err(400, "bad uuid")
    if request.method == "GET":
        row = db.bridge_get_connector(cu)
        if row is None:
            return _err(404, "connector not found")
        status = services_registry.CHANNEL.view()["services"].get(f"bridge:{cu}", {"state": "unknown"})
        return jsonify({"ok": True, "connector": row, "launcher": status,
                        "autostart": services_registry.bridges_autostart(),
                        "credential": db.bridge_credential_status(cu)})
    if request.method == "DELETE":
        try:
            found = db.bridge_delete_connector(cu)
        except db.BridgeBlocked as exc:
            return _err(409, str(exc), blockers=exc.blockers)
        if not found:
            return _err(404, "connector not found")
        return jsonify({"ok": True, "version": db.bridge_tree_version()})
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    try:
        row = db.bridge_update_connector(cu, data, autostart=services_registry.bridges_autostart())
    except AdapterError as exc:
        return _err(400, str(exc))
    except db.BridgeBlocked as exc:
        return _err(409, str(exc), blockers=exc.blockers)
    if row is None:
        return _err(404, "connector not found")
    return jsonify({"ok": True, "connector": row, "version": db.bridge_tree_version()})


@app.route("/bridges/api/connectors/<connector_uuid>/credential", methods=["PUT", "DELETE"])
def bridges_connector_credential(connector_uuid: str) -> Response | tuple[Response, int]:
    """Write-only. PUT {value} seals and stores it (rotating a running
    connector); DELETE forgets it. Neither response, nor any other, carries
    the value."""
    cu = _parse_uuid(connector_uuid)
    if cu is None:
        return _err(400, "bad uuid")
    if request.method == "DELETE":
        if not db.bridge_clear_credential(cu):
            return _err(404, "no stored credential for that connector")
        return jsonify({"ok": True, "credential": db.bridge_credential_status(cu)})
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    try:
        status = db.bridge_set_credential(cu, data.get("value"), autostart=services_registry.bridges_autostart())
    except CredentialKeyMissing as exc:
        return _err(409, str(exc), key_configured=False)
    except AdapterError as exc:
        return _err(400, str(exc))
    except db.BridgeTreeError as exc:
        return _err(404, str(exc))
    return jsonify({"ok": True, "credential": status})


# --- folders ---------------------------------------------------------------------


@app.route("/bridges/api/folders", methods=["POST"])
def bridges_create_folder() -> tuple[Response, int]:
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    cu = _parse_uuid(data.get("connectorId"))
    if cu is None:
        return _err(400, "connectorId required")
    parent = None
    if data.get("parentId") is not None:
        parent = _parse_uuid(data.get("parentId"))
        if parent is None:
            return _err(400, "bad parentId")
    try:
        row = db.bridge_create_folder(cu, data.get("name"), parent)
    except (AdapterError, db.BridgeTreeError) as exc:
        return _err(400, str(exc))
    return jsonify({"ok": True, "folder": row, "version": db.bridge_tree_version()}), 201


@app.route("/bridges/api/folders/<folder_uuid>", methods=["PUT", "DELETE"])
def bridges_folder(folder_uuid: str) -> Response | tuple[Response, int]:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return _err(400, "bad uuid")
    if request.method == "DELETE":
        try:
            found = db.bridge_delete_folder(fu)
        except db.BridgeBlocked as exc:
            return _err(409, str(exc), blockers=exc.blockers)
        if not found:
            return _err(404, "folder not found")
        return jsonify({"ok": True, "version": db.bridge_tree_version()})
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    try:
        row = db.bridge_update_folder(fu, data)
    except AdapterError as exc:
        return _err(400, str(exc))
    if row is None:
        return _err(404, "folder not found")
    return jsonify({"ok": True, "folder": row, "version": db.bridge_tree_version()})


# --- bindings --------------------------------------------------------------------


@app.route("/bridges/api/bindings", methods=["POST"])
def bridges_create_binding() -> tuple[Response, int]:
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    cu = _parse_uuid(data.get("connectorId"))
    ru = _parse_uuid(data.get("roomUuid"))
    if cu is None or ru is None:
        return _err(400, "connectorId and roomUuid required")
    folder = None
    if data.get("folderId") is not None:
        folder = _parse_uuid(data.get("folderId"))
        if folder is None:
            return _err(400, "bad folderId")
    try:
        row = db.bridge_create_binding(cu, ru, data.get("address"), folder_uuid=folder, policy=data.get("policy"))
    except (AdapterError, db.BridgeTreeError) as exc:
        return _err(400, str(exc))
    except db.BridgeBlocked as exc:
        return _err(409, str(exc), blockers=exc.blockers)
    except IntegrityError:
        db.session.rollback()
        return _err(409, "room or connector vanished while creating the binding")
    return jsonify({"ok": True, "binding": row, "version": db.bridge_tree_version()}), 201


@app.route("/bridges/api/bindings/<binding_uuid>", methods=["PUT", "DELETE"])
def bridges_binding(binding_uuid: str) -> Response | tuple[Response, int]:
    bu = _parse_uuid(binding_uuid)
    if bu is None:
        return _err(400, "bad uuid")
    if request.method == "DELETE":
        if not db.bridge_delete_binding(bu):
            return _err(404, "binding not found")
        return jsonify({"ok": True, "version": db.bridge_tree_version()})
    data = _body()
    if data is None:
        return _err(400, "request body must be a JSON object")
    try:
        row = db.bridge_update_binding(bu, data)
    except AdapterError as exc:
        return _err(400, str(exc))
    if row is None:
        return _err(404, "binding not found")
    return jsonify({"ok": True, "binding": row, "version": db.bridge_tree_version()})


# --- what a bridge process fetches --------------------------------------------------


@app.route("/bridge/api/connectors/<connector_uuid>/config")
def bridge_connector_config(connector_uuid: str) -> Response | tuple[Response, int]:
    cu = _parse_uuid(connector_uuid)
    if cu is None:
        return _err(400, "bad uuid")
    snapshot = db.bridge_connector_config(cu)
    if snapshot is None:
        return _err(404, "connector not found")
    return jsonify(snapshot)
