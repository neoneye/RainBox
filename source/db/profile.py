"""Person-profile tree: folder/profile persistence + data validation.

Backs the /profile page. Holds the profile folder tree (load/validate/save —
the whole-tree bulk pattern shared with /prompt, /git and /cron) plus the
per-profile data operations: the registry-driven validator, data read/write
that preserves the connector-owned `dynamic` subtree, and duplication. The
built-in locale templates are not DB rows — they ship in
data/profile_templates.json and merge virtually into the tree load.
Re-exported from db for import compatibility.
"""
import hashlib
import json
import re
from copy import deepcopy
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from db.models import Profile, ProfileFolder, db
from profile_fields import FIELDS_BY_KEY, SUMMARY_KEYS


class ProfileTreeError(ValueError):
    """A profile tree payload failed structural validation (bad uuid, dangling
    parent folder, cycle, built-in uuid, …). The PUT endpoint maps this to
    400, not 500."""


class ProfileTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version on save);
    mapped to HTTP 409 so the client re-hydrates instead of clobbering."""


class ProfileDataError(ValueError):
    """A profile `data` snapshot failed registry validation (unknown key,
    out-of-enum value, bad date, submitted `dynamic`). Mapped to HTTP 400
    with the offending field named."""


def validate_profile_data(data: Any) -> dict[str, Any]:
    """Validate a complete editable snapshot against the registry and return
    the canonical sparse object: known editable keys only, "" values removed
    before validation, string kinds checked strictly (enum membership, ISO
    calendar date). Deliberately soft on IANA/BCP-47/ISO-4217 membership —
    an uncommon-yet-valid value is never blocked. `dynamic` is
    connector-owned and rejected as read-only. Raises ProfileDataError
    naming the offending field."""
    if not isinstance(data, dict):
        raise ProfileDataError(f"'data' must be an object, got {type(data).__name__}")
    canonical: dict[str, Any] = {}
    for key, value in data.items():
        if key == "dynamic":
            raise ProfileDataError("field 'dynamic' is read-only (connector-owned)")
        if key == "calibration":
            raise ProfileDataError(
                "field 'calibration' is read-only here (server-owned; use the "
                "calibration endpoint)")
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            raise ProfileDataError(f"unknown field: '{key}'")
        if value == "":
            continue  # canonicalize: blank means absent, the JSONB stays sparse
        if not isinstance(value, str):
            raise ProfileDataError(
                f"field '{key}' must be a string, got {type(value).__name__}")
        if field.kind == "enum" and value not in field.choices:
            raise ProfileDataError(
                f"field '{key}' must be one of {list(field.choices)}, got {value!r}")
        if field.kind == "date":
            # The regex pins the extended YYYY-MM-DD shape (fromisoformat alone
            # would also accept the basic 20260230 form); fromisoformat then
            # rejects impossible calendar dates like 2026-02-30.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ProfileDataError(
                    f"field '{key}' must be an ISO date (YYYY-MM-DD), got {value!r}")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ProfileDataError(
                    f"field '{key}' is not a valid calendar date: {value!r}") from None
        canonical[key] = value
    return canonical


def _to_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


# ---- built-in templates (shipped file, virtual rows — never in the DB) ----

_TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "data" / "profile_templates.json"


@lru_cache(maxsize=1)
def _templates() -> dict[str, Any]:
    """The shipped built-in templates file, parsed once per process. The file
    is part of the release, so a new rainbox serves new content on the next
    page load — no re-seed logic, no drift between installs."""
    return json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))


def profile_templates_folder_uuid() -> UUID:
    """Fixed uuid of the virtual Templates folder (deep links survive releases)."""
    return UUID(_templates()["folder"]["uuid"])


def profile_templates_entries() -> list[dict[str, Any]]:
    """The shipped template profiles, file order: {"uuid", "name", "data"}."""
    return _templates()["profiles"]


@lru_cache(maxsize=1)
def profile_builtin_uuids() -> frozenset[UUID]:
    """Every fixed built-in uuid (the virtual folder + the 21 templates).
    The tree validator keeps user rows off these."""
    return frozenset({profile_templates_folder_uuid()} |
                     {UUID(e["uuid"]) for e in profile_templates_entries()})


def profile_builtin_get(profile_uuid: UUID) -> dict[str, Any] | None:
    """One built-in template entry by uuid, or None (the folder uuid is not
    a profile and also returns None)."""
    for e in profile_templates_entries():
        if UUID(e["uuid"]) == profile_uuid:
            return e
    return None


def profile_data_summary(data: dict[str, Any] | None) -> dict[str, Any]:
    """The read-only projection riding on tree rows: just enough of `data`
    for the folder detail table (Name / Person / Language / Units / Time /
    Country) without an N-request detail-fetch fan-out."""
    data = data or {}
    return {k: data.get(k, "") for k in SUMMARY_KEYS}


def profile_tree_version() -> str:
    """Opaque version token for the persisted tree (optimistic concurrency).
    Covers only structural fields of user-owned rows — `data` (and the
    summary derived from it) is excluded so autosaving a form field never
    invalidates an open page's tree version, and the virtual built-ins are
    excluded by construction (they are never DB rows)."""
    folders = db.session.execute(
        sa.select(ProfileFolder).order_by(ProfileFolder.uuid)
    ).scalars().all()
    profiles = db.session.execute(
        sa.select(Profile).order_by(Profile.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name, f.description,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(p.uuid), p.name,
          str(p.folder_uuid) if p.folder_uuid else None, p.position]
         for p in profiles],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def profile_load_tree() -> dict[str, Any]:
    """The whole profile tree in the frontend's field names, each list ordered
    by position then id. Profile `data` is deliberately omitted (loaded
    per-profile via profile_get); each row instead carries the derived
    read-only `summary`. The shipped built-ins merge in after the user's own
    content, under the virtual Templates folder, tagged `builtin: true`."""
    folders = db.session.execute(
        sa.select(ProfileFolder).order_by(ProfileFolder.position, ProfileFolder.id)
    ).scalars().all()
    profiles = db.session.execute(
        sa.select(Profile).order_by(Profile.position, Profile.id)
    ).scalars().all()
    tpl = _templates()
    return {
        "folders": [
            {"id": str(f.uuid), "name": f.name, "description": f.description,
             "parentId": str(f.parent_uuid) if f.parent_uuid else None,
             "created_at": f.created_at.isoformat() if f.created_at else None,
             "updated_at": f.updated_at.isoformat() if f.updated_at else None}
            for f in folders
        ] + [
            {"id": tpl["folder"]["uuid"], "name": tpl["folder"]["name"],
             "description": tpl["folder"]["description"], "parentId": None,
             "builtin": True}
        ],
        "profiles": [
            {"uuid": str(p.uuid), "name": p.name,
             "folderId": str(p.folder_uuid) if p.folder_uuid else None,
             "summary": profile_data_summary(p.data),
             "created_at": p.created_at.isoformat() if p.created_at else None,
             "updated_at": p.updated_at.isoformat() if p.updated_at else None}
            for p in profiles
        ] + [
            {"uuid": e["uuid"], "name": e["name"],
             "folderId": tpl["folder"]["uuid"], "builtin": True,
             "summary": profile_data_summary(e["data"])}
            for e in profile_templates_entries()
        ],
        # Optimistic-concurrency token; the page echoes it on PUT (409 if stale).
        # Covers user rows only — the merged built-ins are excluded.
        "version": profile_tree_version(),
    }


def validate_profile_tree(folders: list, profiles: list) -> None:
    """Structural integrity check run before any DB write: well-formed uuids,
    no duplicate/dangling/cyclic folder references, profile folderIds resolve,
    a profile uuid never collides with a folder id (/profile?id=<uuid> must be
    unambiguous), and no entry carries the derived `summary` or a built-in
    uuid — built-ins are virtual and read-only, they never ride a save.
    Raises ProfileTreeError on the first problem; does not touch the DB."""
    if not isinstance(folders, list):
        raise ProfileTreeError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(profiles, list):
        raise ProfileTreeError(f"'profiles' must be a list, got {type(profiles).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise ProfileTreeError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise ProfileTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in profile_builtin_uuids():
            raise ProfileTreeError(f"folder {fid} is a read-only built-in")
        if fid in parent_of:
            raise ProfileTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise ProfileTreeError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise ProfileTreeError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise ProfileTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise ProfileTreeError(f"folder {fid} references missing parent {pid}")
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise ProfileTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    profile_uuids: set[UUID] = set()
    for p in profiles:
        if not isinstance(p, dict):
            raise ProfileTreeError(f"profile entry must be an object, got {type(p).__name__}")
        pu = _to_uuid(p.get("uuid"))
        if pu is None:
            raise ProfileTreeError(f"profile uuid is not a uuid: {p.get('uuid')!r}")
        if pu in profile_builtin_uuids():
            raise ProfileTreeError(f"profile {pu} is a read-only built-in")
        if pu in profile_uuids:
            raise ProfileTreeError(f"duplicate profile uuid: {pu}")
        if pu in parent_of:
            raise ProfileTreeError(f"profile uuid {pu} collides with a folder id")
        profile_uuids.add(pu)
        if not isinstance(p.get("name", ""), str):
            raise ProfileTreeError(f"profile {pu} name must be a string")
        if "summary" in p:
            raise ProfileTreeError(
                f"profile {pu} carries the derived 'summary' — it must not be submitted")
        fld_raw = p.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise ProfileTreeError(f"profile {pu} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise ProfileTreeError(f"profile {pu} references missing folder {fld}")


def profile_save_tree(folders: list, profiles: list, *,
                      base_version: str | None = None,
                      expected_deletes: int | None = None) -> None:
    """Upsert the whole user-owned profile tree by uuid. List order becomes
    `position`. Rows whose uuid is absent from the incoming lists are deleted.
    Never touches `data`: new rows start empty, existing rows keep theirs (the
    form saves through profile_update_data). Validates first (raises
    ProfileTreeError before any mutation). Two opt-in guards (skipped when
    None): `base_version` (stale → ProfileTreeConflict) and `expected_deletes`
    (a save deleting more than declared → ProfileTreeError, the
    truncated-payload tripwire)."""
    validate_profile_tree(folders, profiles)
    if base_version is not None and base_version != profile_tree_version():
        raise ProfileTreeConflict("profile tree changed since it was loaded")
    existing_f = {f.uuid: f for f in
                  db.session.execute(sa.select(ProfileFolder)).scalars().all()}
    existing_p = {p.uuid: p for p in
                  db.session.execute(sa.select(Profile)).scalars().all()}
    if expected_deletes is not None:
        incoming = {UUID(f["id"]) for f in folders} | {UUID(p["uuid"]) for p in profiles}
        would_delete = len((set(existing_f) | set(existing_p)) - incoming)
        if would_delete > expected_deletes:
            raise ProfileTreeError(
                f"save would delete {would_delete} node(s) but only "
                f"{expected_deletes} deletion(s) were declared — refusing")
    seen_f: set[UUID] = set()
    for i, f in enumerate(folders):
        fu = UUID(f["id"])
        seen_f.add(fu)
        row = existing_f.get(fu)
        if row is None:
            row = ProfileFolder(uuid=fu)
            db.session.add(row)
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for fu, row in existing_f.items():
        if fu not in seen_f:
            db.session.delete(row)
    seen_p: set[UUID] = set()
    for i, p in enumerate(profiles):
        pu = UUID(p["uuid"])
        seen_p.add(pu)
        row = existing_p.get(pu)
        if row is None:
            row = Profile(uuid=pu)
            db.session.add(row)
        row.name = p.get("name", "")
        row.folder_uuid = UUID(p["folderId"]) if p.get("folderId") else None
        row.position = i
    deleted_p = {pu for pu in existing_p if pu not in seen_p}
    for pu in deleted_p:
        db.session.delete(existing_p[pu])
    # Deleting the profile that `profile.current` points at must clear the
    # pointer and stamp the change IN THE SAME TRANSACTION — otherwise the
    # setting dangles: every declared-profile block silently disappears on
    # the next turn and no context marker ever announces it. Staged through
    # the settings module's no-commit helper so tree rows and settings rows
    # commit (or roll back) together.
    if deleted_p:
        from db.settings import _registry, _upsert_setting_row, get_setting

        current_raw = str(get_setting("profile.current") or "").strip()
        current_uuid = _to_uuid(current_raw) if current_raw else None
        if current_uuid is not None and current_uuid in deleted_p:
            from datetime import UTC, datetime

            stamp = datetime.now(UTC).isoformat()
            _upsert_setting_row(_registry("profile.current"), None)
            _upsert_setting_row(
                _registry("profile.current_changed_at"), stamp)
    db.session.commit()


# ---- per-profile data + duplication ----

def _profile_row(profile_uuid: UUID) -> Profile | None:
    return db.session.execute(
        sa.select(Profile).where(Profile.uuid == profile_uuid)
    ).scalar_one_or_none()


def _profile_tree_row(row: Profile) -> dict[str, Any]:
    """One user-owned profile in tree-list field names (no data blob)."""
    return {
        "uuid": str(row.uuid), "name": row.name,
        "folderId": str(row.folder_uuid) if row.folder_uuid else None,
        "summary": profile_data_summary(row.data),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def profile_get(profile_uuid: UUID) -> dict[str, Any] | None:
    """One profile with its full data blob, for the form pane. Built-ins are
    served from the shipped file (builtin: True), user rows from the DB.
    Returns None if the uuid is unknown."""
    builtin = profile_builtin_get(profile_uuid)
    if builtin is not None:
        return {"uuid": builtin["uuid"], "name": builtin["name"],
                "data": builtin["data"], "builtin": True}
    row = _profile_row(profile_uuid)
    if row is None:
        return None
    return {
        "uuid": str(row.uuid), "name": row.name, "data": row.data or {},
        "builtin": False,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# The server-owned subtrees riding on the same JSONB column as the editable
# registry fields. Every writer must go through profile_mutate_data so one
# subtree's save can never read-modify-write-race another subtree's writer.
SERVER_OWNED_SUBTREES = ("dynamic", "calibration")


def profile_mutate_data(profile_uuid: UUID,
                        mutator: Any) -> Profile | None:
    """Apply one subtree mutation to a profile's `data` under the row lock.

    Calibration, flat fields, and `dynamic` share one JSONB column, so a
    subtree write must never be a read-modify-write race against a different
    subtree's writer: this selects the row FOR UPDATE, hands `mutator` a copy
    of the current dict, assigns the returned dict, and commits. Every future
    `dynamic` writer must use it too. Built-in virtual profiles never enter
    here (they have no row). Returns the row, or None if the uuid is unknown;
    a mutator exception rolls back (releasing the lock) and re-raises."""
    row = db.session.execute(
        sa.select(Profile).where(Profile.uuid == profile_uuid).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        db.session.rollback()  # release the transaction the SELECT opened
        return None
    try:
        row.data = mutator(deepcopy(row.data or {}))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return row


def profile_update_data(profile_uuid: UUID, data: Any) -> dict[str, Any] | None:
    """Replace a profile's editable fields with the validated canonical
    snapshot (raises ProfileDataError), preserving the server-owned `dynamic`
    and `calibration` subtrees in the same transaction — a stale form autosave
    can never overwrite a newer connector observation or calibration save.
    Editable keys omitted from the complete snapshot are deleted, not
    retained. Returns the row's new summary projection, or None if the uuid
    is unknown. Rejecting built-in uuids is the API layer's job (there is no
    row here to update anyway)."""
    canonical = validate_profile_data(data)

    def _mutate(current: dict[str, Any]) -> dict[str, Any]:
        for key in SERVER_OWNED_SUBTREES:
            if key in current:
                canonical[key] = current[key]
        return canonical

    row = profile_mutate_data(profile_uuid, _mutate)
    if row is None:
        return None
    return profile_data_summary(row.data)


def profile_duplicate(profile_uuid: UUID) -> dict[str, Any] | None:
    """Copy a profile's whole data blob (dynamic and calibration included)
    into a new row — the one-action way to mint a friend's profile from an
    archetype. A user-owned source yields "<name> copy" in the same folder
    right after the source; a built-in source yields a real editable row named
    after the template at the end of the user-owned top level (the virtual
    Templates folder can't hold user rows). No version lineage — duplication
    is a convenience, not ancestry: calibration rows copy their semantic
    fields and order but receive fresh ids and the duplication timestamp,
    never the source's server-owned identity. Returns the new row in
    tree-list field names, or None if the source uuid is unknown."""
    from db.profile_calibration import refresh_calibration_identity

    builtin = profile_builtin_get(profile_uuid)
    if builtin is not None:
        max_pos = db.session.execute(
            sa.select(sa.func.max(Profile.position)).where(Profile.folder_uuid.is_(None))
        ).scalar()
        row = Profile(uuid=uuid4(), name=builtin["name"],
                      data=refresh_calibration_identity(deepcopy(builtin["data"])),
                      folder_uuid=None,
                      position=(max_pos + 1) if max_pos is not None else 0)
        db.session.add(row)
        db.session.commit()
        return _profile_tree_row(row)
    # Lock the source row so the copy is a coherent snapshot relative to
    # flat-field and calibration autosaves in other tabs/processes (the
    # browser flushes its own pending edits before duplicating).
    src = db.session.execute(
        sa.select(Profile).where(Profile.uuid == profile_uuid).with_for_update()
    ).scalar_one_or_none()
    if src is None:
        db.session.rollback()
        return None
    row = Profile(uuid=uuid4(), name=f"{src.name} copy",
                  data=refresh_calibration_identity(deepcopy(src.data or {})),
                  folder_uuid=src.folder_uuid,
                  position=src.position + 1)
    # Shift later siblings so the copy's slot is unambiguous even before the
    # next whole-tree save rewrites all positions.
    siblings = db.session.execute(
        sa.select(Profile).where(Profile.folder_uuid == src.folder_uuid)
    ).scalars().all()
    for sib in siblings:
        if sib.position > src.position:
            sib.position += 1
    db.session.add(row)
    db.session.commit()
    return _profile_tree_row(row)
