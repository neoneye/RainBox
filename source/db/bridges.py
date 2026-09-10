"""Chat-bridge configuration: connectors, folders, bindings.

Backs the /bridges page, the per-connector config snapshot a bridge fetches,
and the launcher's dynamic desired entries. Design:
docs/superpowers/specs/2026-09-09-bridge-settings-design.md.

Shape rules that matter here:

- Ownership is enforced by the database: `bridge_folder.connector_uuid`,
  `bridge_binding.connector_uuid`, and `bridge_binding.room_uuid` are
  RESTRICT foreign keys, and `(connector_uuid, address_key)` is unique. An
  IntegrityError is the guard; the API turns it into a 409 with blockers.
- Placement (`parent_uuid`, `folder_uuid`) is plain uuid columns validated
  here, under a version token (notes/ui-tree-persistence.md) AND a row lock
  on every affected connector, taken before the token is compared.
- The tree save never creates or deletes; create and delete are their own
  functions. Enabled flags and policies are per-item content, not tree
  structure, and are excluded from the version hash.
- Every write NOTIFYs `bridge_config` on the chat channel (the SSE stream
  forwards it; bridges refetch, browsers ignore it), and a write that
  changes a connector's launch gate or nonce also pushes the launcher's
  desired snapshot over the control socket.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from db.models import BridgeBinding, BridgeConnector, BridgeFolder, Chatroom, db
from services.definitions import DYNAMIC_SERVICES
from services.bridge_adapters import (
    ADAPTERS,
    LAUNCH_MODES,
    Adapter,
    AdapterError,
    adapter_for,
    canonical_json,
    sanitize_label,
    validate_base_url,
    validate_token_env,
)

BRIDGE_SCHEMA_VERSION = 1


class BridgeTreeError(ValueError):
    """A payload failed structural validation (400)."""


class BridgeTreeConflict(Exception):
    """Stale version token (409)."""


class BridgeBlocked(Exception):
    """A delete refused by ownership: carries the blockers for a 409."""

    def __init__(self, message: str, blockers: dict[str, Any]) -> None:
        super().__init__(message)
        self.blockers = blockers


def _to_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


# --- notify + launcher push ----------------------------------------------------


def _notify_config(connector_uuid: UUID) -> None:
    """Emit the `bridge_config` event inside the current transaction; the chat
    SSE stream forwards it to every listener. No room_uuid, so browsers drop
    it; a bridge refetches its config on it."""
    from db.chat import CHAT_NOTIFY_CHANNEL
    db.session.execute(
        sa.text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHAT_NOTIFY_CHANNEL,
         "payload": json.dumps({"event": "bridge_config", "connector_uuid": str(connector_uuid)})},
    )


def _push_launcher() -> None:
    """After a commit that changed a connector's launch gate, nonce, or
    label: push the launcher's desired snapshot down the control socket."""
    from services import registry
    registry.CHANNEL.push_desired()


# --- rows -> dicts ---------------------------------------------------------------


def _connector_dict(c: BridgeConnector) -> dict[str, Any]:
    return {
        "uuid": str(c.uuid), "name": c.name, "platform": c.platform,
        "base_url": c.base_url, "identity": c.identity, "token_env": c.token_env,
        "launch_mode": c.launch_mode, "enabled": c.enabled, "policy": c.policy or {},
        "restart_nonce": str(c.restart_nonce) if c.restart_nonce else None,
        "position": c.position,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _folder_dict(f: BridgeFolder) -> dict[str, Any]:
    return {
        "id": str(f.uuid), "connectorId": str(f.connector_uuid),
        "parentId": str(f.parent_uuid) if f.parent_uuid else None,
        "name": f.name, "enabled": f.enabled, "policy": f.policy or {}, "position": f.position,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


def _binding_dict(b: BridgeBinding) -> dict[str, Any]:
    return {
        "uuid": str(b.uuid), "connectorId": str(b.connector_uuid),
        "folderId": str(b.folder_uuid) if b.folder_uuid else None,
        "roomUuid": str(b.room_uuid), "address": b.address or {}, "addressKey": b.address_key,
        "enabled": b.enabled, "policy": b.policy or {}, "position": b.position,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


def _connector_row(uuid: UUID, *, lock: bool = False) -> BridgeConnector | None:
    stmt = sa.select(BridgeConnector).where(BridgeConnector.uuid == uuid)
    if lock:
        stmt = stmt.with_for_update()
    return db.session.execute(stmt).scalar_one_or_none()


def _lock_connectors(uuids: set[UUID]) -> dict[UUID, BridgeConnector]:
    """SELECT … FOR UPDATE on every affected connector, in uuid order, so two
    saves touching the same rows serialize instead of both passing a token
    check they read before either committed."""
    out: dict[UUID, BridgeConnector] = {}
    for u in sorted(uuids, key=str):
        row = _connector_row(u, lock=True)
        if row is not None:
            out[u] = row
    return out


# --- tree: version / load / validate / save ------------------------------------------


def bridge_tree_version() -> str:
    """Structural fields only: uuids, names, placement, order. Enabled flags,
    policies, nonces, and timestamps are content or bookkeeping and are
    excluded so a background change never 409s an open page."""
    connectors = db.session.execute(sa.select(BridgeConnector).order_by(BridgeConnector.uuid)).scalars().all()
    folders = db.session.execute(sa.select(BridgeFolder).order_by(BridgeFolder.uuid)).scalars().all()
    bindings = db.session.execute(sa.select(BridgeBinding).order_by(BridgeBinding.uuid)).scalars().all()
    payload = [
        [[str(c.uuid), c.name, c.position] for c in connectors],
        [[str(f.uuid), str(f.connector_uuid), str(f.parent_uuid) if f.parent_uuid else None, f.name, f.position]
         for f in folders],
        [[str(b.uuid), str(b.connector_uuid), str(b.folder_uuid) if b.folder_uuid else None, b.position]
         for b in bindings],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:16]


def bridge_load_tree() -> dict[str, Any]:
    connectors = db.session.execute(
        sa.select(BridgeConnector).order_by(BridgeConnector.position, BridgeConnector.id)).scalars().all()
    folders = db.session.execute(
        sa.select(BridgeFolder).order_by(BridgeFolder.position, BridgeFolder.id)).scalars().all()
    bindings = db.session.execute(
        sa.select(BridgeBinding).order_by(BridgeBinding.position, BridgeBinding.id)).scalars().all()
    room_names = {u: n for u, n in db.session.execute(sa.select(Chatroom.uuid, Chatroom.name)).all()}
    out_bindings = []
    for b in bindings:
        d = _binding_dict(b)
        d["roomName"] = room_names.get(b.room_uuid)
        out_bindings.append(d)
    return {
        "connectors": [_connector_dict(c) for c in connectors],
        "folders": [_folder_dict(f) for f in folders],
        "bindings": out_bindings,
        "platforms": {p: {"label": a.label, "available": a.available,
                          "requires_base_url": a.requires_base_url,
                          "requires_identity": a.requires_identity,
                          "address_fields": [{"name": f.name, "kind": f.kind, "required": f.required}
                                             for f in a.address_fields],
                          "policy_keys": [{"name": k.name, "type": k.type, "default": k.default,
                                           "supported": k.supported, "choices": list(k.choices),
                                           "minimum": k.minimum, "maximum": k.maximum,
                                           "description": k.description}
                                          for k in a.policy_keys.values()],
                          "state_file_env": a.state_file_env,
                          # Where a manual run of this platform's bridge lives
                          # (relative to source/), for the copyable launch command.
                          "directory": DYNAMIC_SERVICES[a.kind].directory if a.kind in DYNAMIC_SERVICES else None,
                          "argv": list(DYNAMIC_SERVICES[a.kind].argv) if a.kind in DYNAMIC_SERVICES else None}
                      for p, a in ADAPTERS.items()},
        "version": bridge_tree_version(),
    }


def validate_bridge_tree(connectors: list, folders: list, bindings: list) -> None:
    """Structural check before any write: uuids well formed and globally
    unique across kinds (one `?id=` addresses any node); every folder and
    binding names an existing connector; a folder's parent and a binding's
    folder belong to the SAME connector; no cycles. Raises BridgeTreeError."""
    for label, rows in (("connectors", connectors), ("folders", folders), ("bindings", bindings)):
        if not isinstance(rows, list):
            raise BridgeTreeError(f"'{label}' must be a list")
    seen: set[UUID] = set()
    connector_ids: set[UUID] = set()
    for c in connectors:
        if not isinstance(c, dict):
            raise BridgeTreeError("connector entry must be an object")
        u = _to_uuid(c.get("uuid"))
        if u is None:
            raise BridgeTreeError(f"connector uuid is not a uuid: {c.get('uuid')!r}")
        if u in seen:
            raise BridgeTreeError(f"duplicate uuid {u}")
        if not isinstance(c.get("name", ""), str) or not c.get("name", "").strip():
            raise BridgeTreeError(f"connector {u} name must be a non-empty string")
        seen.add(u)
        connector_ids.add(u)
    folder_parent: dict[UUID, UUID | None] = {}
    folder_conn: dict[UUID, UUID] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise BridgeTreeError("folder entry must be an object")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise BridgeTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in seen:
            raise BridgeTreeError(f"duplicate uuid {fid}")
        cid = _to_uuid(f.get("connectorId"))
        if cid is None or cid not in connector_ids:
            raise BridgeTreeError(f"folder {fid} references missing connector {f.get('connectorId')!r}")
        if not isinstance(f.get("name", ""), str):
            raise BridgeTreeError(f"folder {fid} name must be a string")
        pid_raw = f.get("parentId")
        pid = None
        if pid_raw is not None:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise BridgeTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        seen.add(fid)
        folder_parent[fid] = pid
        folder_conn[fid] = cid
    for fid, pid in folder_parent.items():
        if pid is not None:
            if pid not in folder_parent:
                raise BridgeTreeError(f"folder {fid} references missing parent {pid}")
            if folder_conn[pid] != folder_conn[fid]:
                raise BridgeTreeError(f"folder {fid} parent belongs to another connector")
    for start in folder_parent:
        walked: set[UUID] = set()
        cur = folder_parent[start]
        while cur is not None:
            if cur == start or cur in walked:
                raise BridgeTreeError(f"folder cycle involving {start}")
            walked.add(cur)
            cur = folder_parent.get(cur)
    for b in bindings:
        if not isinstance(b, dict):
            raise BridgeTreeError("binding entry must be an object")
        bu = _to_uuid(b.get("uuid"))
        if bu is None:
            raise BridgeTreeError(f"binding uuid is not a uuid: {b.get('uuid')!r}")
        if bu in seen:
            raise BridgeTreeError(f"duplicate uuid {bu}")
        cid = _to_uuid(b.get("connectorId"))
        if cid is None or cid not in connector_ids:
            raise BridgeTreeError(f"binding {bu} references missing connector {b.get('connectorId')!r}")
        fld_raw = b.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None or fld not in folder_parent:
                raise BridgeTreeError(f"binding {bu} references missing folder {fld_raw!r}")
            if folder_conn[fld] != cid:
                raise BridgeTreeError(f"binding {bu} folder belongs to another connector")
        seen.add(bu)


def bridge_save_tree(connectors: list, folders: list, bindings: list, *,
                     base_version: str | None = None) -> None:
    """Update names, placement, and order of rows that already exist. Locks
    every connector row named in the payload (uuid order) BEFORE comparing
    the version token, so two simultaneous saves serialize. Never creates or
    deletes; a payload that omits or invents a row is a BridgeTreeError."""
    validate_bridge_tree(connectors, folders, bindings)
    conn_uuids = {UUID(c["uuid"]) for c in connectors}
    existing_c = {c.uuid: c for c in db.session.execute(sa.select(BridgeConnector)).scalars().all()}
    _lock_connectors(set(existing_c) | conn_uuids)
    try:
        if base_version is not None and base_version != bridge_tree_version():
            raise BridgeTreeConflict("bridge tree changed since it was loaded")
        existing_f = {f.uuid: f for f in db.session.execute(sa.select(BridgeFolder)).scalars().all()}
        existing_b = {b.uuid: b for b in db.session.execute(sa.select(BridgeBinding)).scalars().all()}
        for label, incoming, existing in (
            ("connector", conn_uuids, existing_c),
            ("folder", {UUID(f["id"]) for f in folders}, existing_f),
            ("binding", {UUID(b["uuid"]) for b in bindings}, existing_b),
        ):
            missing = set(existing) - incoming
            if missing:
                raise BridgeTreeError(
                    f"tree save omitted {len(missing)} existing {label}(s) — refusing (the tree save never deletes)")
            unknown = incoming - set(existing)
            if unknown:
                raise BridgeTreeError(
                    f"tree save references {len(unknown)} unknown {label}(s) — refusing (the tree save never creates)")
        touched: set[UUID] = set()
        for i, c in enumerate(connectors):
            row = existing_c[UUID(c["uuid"])]
            name = c.get("name", row.name).strip()
            if row.name != name or row.position != i:
                touched.add(row.uuid)
            row.name = name
            row.position = i
        for i, f in enumerate(folders):
            row = existing_f[UUID(f["id"])]
            if row.connector_uuid != UUID(f["connectorId"]):
                raise BridgeTreeError(f"folder {row.uuid} cannot move between connectors")
            row.name = f.get("name", row.name)
            row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
            row.position = i
            touched.add(row.connector_uuid)
        for i, b in enumerate(bindings):
            row = existing_b[UUID(b["uuid"])]
            if row.connector_uuid != UUID(b["connectorId"]):
                raise BridgeTreeError(f"binding {row.uuid} cannot move between connectors")
            row.folder_uuid = UUID(b["folderId"]) if b.get("folderId") else None
            row.position = i
            touched.add(row.connector_uuid)
        db.session.flush()
        for cu in touched:
            _notify_config(cu)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if touched:
        _push_launcher()   # a rename changes the launcher's label


# --- connectors -----------------------------------------------------------------


def _validate_connector_fields(platform: str, token_env: Any, base_url: Any, identity: Any) -> tuple[Adapter, str, str | None, str | None]:
    adapter = adapter_for(platform)
    if not adapter.available:
        raise AdapterError(f"{adapter.label} has no bridge implementation yet")
    token = validate_token_env(token_env)
    url = None
    if adapter.requires_base_url:
        url = validate_base_url(base_url)
    elif base_url not in (None, ""):
        raise AdapterError(f"{adapter.label} connectors take no base_url")
    ident = None
    if adapter.requires_identity:
        if not isinstance(identity, str) or not identity.strip():
            raise AdapterError("identity is required for this platform")
        ident = identity.strip()
    elif identity not in (None, ""):
        raise AdapterError(f"{adapter.label} connectors take no identity")
    return adapter, token, url, ident


def bridge_create_connector(name: Any, platform: Any, token_env: Any, *,
                            base_url: Any = None, identity: Any = None,
                            policy: Any = None) -> dict[str, Any]:
    """A new connector, disabled, at the end of the list. Raises AdapterError
    (400) for a bad shape and BridgeBlocked (409) for a duplicate name."""
    if not isinstance(name, str) or not name.strip():
        raise AdapterError("name is required")
    adapter, token, url, ident = _validate_connector_fields(platform, token_env, base_url, identity)
    pol = adapter.validate_policy(policy)
    highest = db.session.execute(sa.select(sa.func.max(BridgeConnector.position))).scalar_one()
    row = BridgeConnector(uuid=uuid4(), name=name.strip(), platform=adapter.platform, token_env=token,
                          base_url=url, identity=ident, policy=pol, enabled=False,
                          position=0 if highest is None else highest + 1)
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise BridgeBlocked("a connector with that name already exists", {"name": name.strip()}) from None
    _notify_config(row.uuid)
    db.session.commit()
    _push_launcher()
    return _connector_dict(row)


def bridge_get_connector(connector_uuid: UUID) -> dict[str, Any] | None:
    row = _connector_row(connector_uuid)
    return _connector_dict(row) if row is not None else None


def launch_gate(row: BridgeConnector, autostart: bool) -> bool:
    return bool(autostart and row.launch_mode == "launcher" and row.enabled)


def bridge_update_connector(connector_uuid: UUID, changes: dict[str, Any], *, autostart: bool) -> dict[str, Any] | None:
    """Editable fields only: name, enabled, launch_mode, policy. Platform,
    base_url, identity, and token_env are fixed after creation (400). An
    off→on transition of the launch gate rewrites the nonce in the same
    transaction. Returns None for an unknown connector."""
    row = _connector_row(connector_uuid, lock=True)
    if row is None:
        db.session.rollback()
        return None
    adapter = adapter_for(row.platform)
    fixed = {"platform", "base_url", "identity", "token_env", "uuid", "restart_nonce"}
    for key in changes:
        if key in fixed:
            db.session.rollback()
            raise AdapterError(f"{key} is fixed after creation; create a new connector instead")
        if key not in {"name", "enabled", "launch_mode", "policy"}:
            db.session.rollback()
            raise AdapterError(f"unknown connector field {key!r}")
    gate_before = launch_gate(row, autostart)
    try:
        if "name" in changes:
            if not isinstance(changes["name"], str) or not changes["name"].strip():
                raise AdapterError("name must be a non-empty string")
            row.name = changes["name"].strip()
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise AdapterError("enabled must be true or false")
            row.enabled = changes["enabled"]
        if "launch_mode" in changes:
            if changes["launch_mode"] not in LAUNCH_MODES:
                raise AdapterError(f"launch_mode must be one of {', '.join(LAUNCH_MODES)}")
            row.launch_mode = changes["launch_mode"]
        if "policy" in changes:
            row.policy = adapter.validate_policy(changes["policy"])
        if not gate_before and launch_gate(row, autostart):
            row.restart_nonce = uuid4()
        db.session.flush()
        _notify_config(row.uuid)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise BridgeBlocked("a connector with that name already exists", {"name": changes.get("name")}) from None
    except Exception:
        db.session.rollback()
        raise
    _push_launcher()
    return _connector_dict(row)


def bridge_bump_connector_nonce(connector_uuid: UUID) -> str | None:
    """Restart: rewrite the nonce; the launcher restarts the process. Does
    not enable a disabled connector."""
    row = _connector_row(connector_uuid, lock=True)
    if row is None:
        db.session.rollback()
        return None
    row.restart_nonce = uuid4()
    db.session.commit()
    _push_launcher()
    return str(row.restart_nonce)


def bridge_gate_transitions(autostart_before: bool, autostart_after: bool) -> None:
    """The global autostart flip: every connector whose gate goes false→true
    gets a fresh nonce, in one transaction, rows locked in uuid order. Call
    BEFORE committing the setting itself so both land together."""
    if autostart_before == autostart_after:
        return
    rows = db.session.execute(
        sa.select(BridgeConnector).order_by(BridgeConnector.uuid).with_for_update()).scalars().all()
    for row in rows:
        if not launch_gate(row, autostart_before) and launch_gate(row, autostart_after):
            row.restart_nonce = uuid4()
    db.session.flush()


def bridge_delete_connector(connector_uuid: UUID) -> bool:
    """Refused (BridgeBlocked) while folders or bindings reference it: the
    RESTRICT keys make that refusal race-free; this reports the counts."""
    row = _connector_row(connector_uuid, lock=True)
    if row is None:
        db.session.rollback()
        return False
    try:
        db.session.delete(row)
        db.session.flush()
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        folders = db.session.execute(sa.select(sa.func.count()).select_from(BridgeFolder)
                                     .where(BridgeFolder.connector_uuid == connector_uuid)).scalar_one()
        bindings = db.session.execute(sa.select(sa.func.count()).select_from(BridgeBinding)
                                      .where(BridgeBinding.connector_uuid == connector_uuid)).scalar_one()
        raise BridgeBlocked("connector still has folders or bindings",
                            {"folder_count": int(folders), "binding_count": int(bindings)}) from None
    _push_launcher()
    return True


# --- folders --------------------------------------------------------------------


def _folder_row(uuid: UUID) -> BridgeFolder | None:
    return db.session.execute(sa.select(BridgeFolder).where(BridgeFolder.uuid == uuid)).scalar_one_or_none()


def _next_position(model: Any, *conditions: Any) -> int:
    highest = db.session.execute(sa.select(sa.func.max(model.position)).where(*conditions)).scalar_one()
    return 0 if highest is None else highest + 1


def bridge_create_folder(connector_uuid: UUID, name: Any, parent_uuid: UUID | None) -> dict[str, Any]:
    conn = _connector_row(connector_uuid, lock=True)
    if conn is None:
        db.session.rollback()
        raise BridgeTreeError("connector not found")
    if not isinstance(name, str) or not name.strip():
        db.session.rollback()
        raise AdapterError("folder name is required")
    if parent_uuid is not None:
        parent = _folder_row(parent_uuid)
        if parent is None or parent.connector_uuid != connector_uuid:
            db.session.rollback()
            raise BridgeTreeError("parent folder must belong to the same connector")
    row = BridgeFolder(uuid=uuid4(), connector_uuid=connector_uuid, parent_uuid=parent_uuid, name=name.strip(),
                       position=_next_position(BridgeFolder, BridgeFolder.connector_uuid == connector_uuid,
                                               BridgeFolder.parent_uuid == parent_uuid))
    db.session.add(row)
    db.session.flush()
    _notify_config(connector_uuid)
    db.session.commit()
    return _folder_dict(row)


def bridge_update_folder(folder_uuid: UUID, changes: dict[str, Any]) -> dict[str, Any] | None:
    row = _folder_row(folder_uuid)
    if row is None:
        return None
    _connector_row(row.connector_uuid, lock=True)
    adapter = adapter_for(_connector_row(row.connector_uuid).platform)  # type: ignore[union-attr]
    try:
        for key in changes:
            if key not in {"name", "enabled", "policy"}:
                raise AdapterError(f"unknown folder field {key!r}")
        if "name" in changes:
            if not isinstance(changes["name"], str) or not changes["name"].strip():
                raise AdapterError("name must be a non-empty string")
            row.name = changes["name"].strip()
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise AdapterError("enabled must be true or false")
            row.enabled = changes["enabled"]
        if "policy" in changes:
            row.policy = adapter.validate_policy(changes["policy"])
        db.session.flush()
        _notify_config(row.connector_uuid)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return _folder_dict(row)


def bridge_delete_folder(folder_uuid: UUID) -> bool:
    """Only an empty folder (no subfolders, no bindings) can be deleted."""
    row = _folder_row(folder_uuid)
    if row is None:
        return False
    _connector_row(row.connector_uuid, lock=True)
    subfolders = db.session.execute(sa.select(sa.func.count()).select_from(BridgeFolder)
                                    .where(BridgeFolder.parent_uuid == folder_uuid)).scalar_one()
    bindings = db.session.execute(sa.select(sa.func.count()).select_from(BridgeBinding)
                                  .where(BridgeBinding.folder_uuid == folder_uuid)).scalar_one()
    if subfolders or bindings:
        db.session.rollback()
        raise BridgeBlocked("folder is not empty",
                            {"folder_count": int(subfolders), "binding_count": int(bindings)})
    connector_uuid = row.connector_uuid
    db.session.delete(row)
    db.session.flush()
    _notify_config(connector_uuid)
    db.session.commit()
    return True


# --- bindings -------------------------------------------------------------------


def _binding_row(uuid: UUID) -> BridgeBinding | None:
    return db.session.execute(sa.select(BridgeBinding).where(BridgeBinding.uuid == uuid)).scalar_one_or_none()


def bridge_create_binding(connector_uuid: UUID, room_uuid: UUID, address: Any, *,
                          folder_uuid: UUID | None = None, policy: Any = None) -> dict[str, Any]:
    """A new, disabled binding. Address is validated by the connector's
    adapter and its canonical key must be unique per connector (409); the
    room must exist (the RESTRICT key makes a vanished room a 409 too)."""
    conn = _connector_row(connector_uuid, lock=True)
    if conn is None:
        db.session.rollback()
        raise BridgeTreeError("connector not found")
    adapter = adapter_for(conn.platform)
    try:
        addr = adapter.validate_address(address)
        pol = adapter.validate_policy(policy)
    except AdapterError:
        db.session.rollback()
        raise
    if folder_uuid is not None:
        folder = _folder_row(folder_uuid)
        if folder is None or folder.connector_uuid != connector_uuid:
            db.session.rollback()
            raise BridgeTreeError("folder must belong to the same connector")
    room = db.session.execute(sa.select(Chatroom).where(Chatroom.uuid == room_uuid)).scalar_one_or_none()
    if room is None:
        db.session.rollback()
        raise BridgeTreeError("room not found")
    row = BridgeBinding(uuid=uuid4(), connector_uuid=connector_uuid, folder_uuid=folder_uuid,
                        room_uuid=room_uuid, address=addr, address_key=adapter.address_key(addr),
                        policy=pol, enabled=False,
                        position=_next_position(BridgeBinding, BridgeBinding.connector_uuid == connector_uuid,
                                                BridgeBinding.folder_uuid == folder_uuid))
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise BridgeBlocked("that remote conversation is already bound on this connector",
                            {"address_key": adapter.address_key(addr)}) from None
    _notify_config(connector_uuid)
    db.session.commit()
    out = _binding_dict(row)
    out["roomName"] = room.name
    return out


def bridge_update_binding(binding_uuid: UUID, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Editable: enabled, policy. Room and address are fixed (400): delete and
    recreate to re-point a conversation."""
    row = _binding_row(binding_uuid)
    if row is None:
        return None
    conn = _connector_row(row.connector_uuid, lock=True)
    adapter = adapter_for(conn.platform)  # type: ignore[union-attr]
    try:
        for key in changes:
            if key in {"room_uuid", "roomUuid", "address", "connectorId", "uuid"}:
                raise AdapterError(f"{key} is fixed after creation; delete the binding and create a new one")
            if key not in {"enabled", "policy"}:
                raise AdapterError(f"unknown binding field {key!r}")
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise AdapterError("enabled must be true or false")
            row.enabled = changes["enabled"]
        if "policy" in changes:
            row.policy = adapter.validate_policy(changes["policy"])
        db.session.flush()
        _notify_config(row.connector_uuid)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return _binding_dict(row)


def bridge_delete_binding(binding_uuid: UUID) -> bool:
    row = _binding_row(binding_uuid)
    if row is None:
        return False
    _connector_row(row.connector_uuid, lock=True)
    connector_uuid = row.connector_uuid
    db.session.delete(row)
    db.session.flush()
    _notify_config(connector_uuid)
    db.session.commit()
    return True


# --- room deletion blockers ---------------------------------------------------------


def bridge_room_blockers(room_uuids: list[UUID]) -> list[dict[str, Any]]:
    """Bindings (enabled or not) that reference any of these rooms — what a
    room or chat-folder delete preview lists, and what a refused delete
    reports."""
    if not room_uuids:
        return []
    rows = db.session.execute(
        sa.select(BridgeBinding, BridgeConnector.name)
        .join(BridgeConnector, BridgeConnector.uuid == BridgeBinding.connector_uuid)
        .where(BridgeBinding.room_uuid.in_(room_uuids))
        .order_by(BridgeBinding.id)
    ).all()
    return [{"binding_uuid": str(b.uuid), "connector_uuid": str(b.connector_uuid),
             "connector_name": name, "room_uuid": str(b.room_uuid),
             "address_key": b.address_key, "enabled": b.enabled}
            for b, name in rows]


# --- the config snapshot a bridge fetches ------------------------------------------------


def _folder_chain(folders_by_uuid: dict[UUID, BridgeFolder], start: UUID | None) -> list[BridgeFolder] | None:
    """Root -> leaf chain of folders ending at `start`; None when a link is
    missing or cyclic (fail closed)."""
    chain: list[BridgeFolder] = []
    cur = start
    walked: set[UUID] = set()
    while cur is not None:
        if cur in walked:
            return None
        walked.add(cur)
        f = folders_by_uuid.get(cur)
        if f is None:
            return None
        chain.append(f)
        cur = f.parent_uuid
    chain.reverse()
    return chain


def bridge_connector_config(connector_uuid: UUID) -> dict[str, Any] | None:
    """The resolved snapshot for one connector, assembled in one REPEATABLE
    READ read-only transaction so a policy edit committing mid-read cannot
    mix into it. `revision` hashes the resolved content. Never carries a
    credential value. None for an unknown connector."""
    db.session.rollback()  # ensure the isolation level applies to a fresh transaction
    conn = db.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    conn.execute(sa.text("SET TRANSACTION READ ONLY"))
    try:
        row = _connector_row(connector_uuid)
        if row is None:
            return None
        adapter = adapter_for(row.platform)
        folders = db.session.execute(
            sa.select(BridgeFolder).where(BridgeFolder.connector_uuid == connector_uuid)).scalars().all()
        bindings = db.session.execute(
            sa.select(BridgeBinding).where(BridgeBinding.connector_uuid == connector_uuid)
            .order_by(BridgeBinding.position, BridgeBinding.id)).scalars().all()
        by_uuid = {f.uuid: f for f in folders}
        out_bindings = []
        for b in bindings:
            chain = _folder_chain(by_uuid, b.folder_uuid)
            layers: list[tuple[str, dict[str, Any] | None]] = [("connector", row.policy)]
            if chain is None:
                effective_enabled = False
                note = "folder chain broken"
            else:
                effective_enabled = bool(row.enabled and all(f.enabled for f in chain) and b.enabled)
                layers.extend((f"folder:{f.uuid}", f.policy) for f in chain)
                note = None
            layers.append(("binding", b.policy))
            policy, sources = adapter.resolve_policy(layers)
            entry = {"uuid": str(b.uuid), "room_uuid": str(b.room_uuid), "address": b.address or {},
                     "enabled": b.enabled, "effective_enabled": effective_enabled,
                     "policy": policy, "policy_sources": sources}
            if note:
                entry["note"] = note
            out_bindings.append(entry)
        connector = {"uuid": str(row.uuid), "name": row.name, "platform": row.platform,
                     "base_url": row.base_url, "identity": row.identity, "token_env": row.token_env,
                     "enabled": row.enabled, "launch_mode": row.launch_mode}
        body = {"connector": connector, "bindings": out_bindings}
        revision = hashlib.sha256(canonical_json(body).encode()).hexdigest()[:16]
        return {"schema_version": BRIDGE_SCHEMA_VERSION, "revision": revision, **body}
    finally:
        db.session.rollback()


# --- launcher desired entries ---------------------------------------------------------


def bridge_launcher_entries(*, autostart: bool, rainbox_url: str) -> list[dict[str, Any]]:
    """One dynamic desired entry per connector of an available platform,
    disabled or not; the launcher sees a key vanish only on deletion."""
    rows = db.session.execute(
        sa.select(BridgeConnector).order_by(BridgeConnector.position, BridgeConnector.id)).scalars().all()
    out = []
    for row in rows:
        adapter = ADAPTERS.get(row.platform)
        if adapter is None or not adapter.available:
            continue
        out.append({
            "key": f"bridge:{row.uuid}",
            "kind": adapter.kind,
            "label": sanitize_label(row.name),
            "enabled": launch_gate(row, autostart),
            "restart_nonce": str(row.restart_nonce) if row.restart_nonce else None,
            "env": {"RAINBOX_URL": rainbox_url, "BRIDGE_CONNECTOR": str(row.uuid)},
            "token_env": row.token_env,
            "state_file": {"env": adapter.state_file_env, "name": f"bridge-{row.uuid}.json"},
        })
    return out
