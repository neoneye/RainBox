"""Person-profile tree: folder/profile persistence + data validation.

Backs the /profile page. Saves follow notes/ui-tree-persistence.md — the tree
save only ever updates rows that already exist, so a payload that omits or
invents a row is an error rather than a silent create or delete; creation and
deletion are their own functions. Also holds the per-profile data operations:
the registry-driven validator, data read/write that preserves the
connector-owned `dynamic` subtree, and duplication. The built-in locale
templates are not DB rows — they ship in data/profile_templates.json and merge
virtually into the tree load. Re-exported from db for import compatibility.
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
    parent folder, cycle, built-in uuid, a row that is missing or unknown).
    The PUT endpoint maps this to 400, not 500."""


class ProfileTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version on save);
    mapped to HTTP 409 so the client re-hydrates instead of clobbering."""


class ProfileDataError(ValueError):
    """A profile `data` snapshot failed registry validation (unknown key,
    out-of-enum value, bad date, submitted `dynamic`). Mapped to HTTP 400
    with the offending field named."""


# Independently-written subtrees riding on the same JSONB column as the flat
# registry fields. Every writer must go through profile_mutate_data so one
# subtree's save can never read-modify-write-race another subtree's writer.
SERVER_OWNED_SUBTREES = ("dynamic", "calibration", "languages")


def validate_profile_data(data: Any) -> dict[str, Any]:
    """Validate a complete editable snapshot against the registry and return
    the canonical sparse object: known editable keys only, "" values removed
    before validation, string kinds checked strictly (enum membership, ISO
    calendar date). Deliberately soft on IANA/BCP-47/ISO-4217 membership —
    an uncommon-yet-valid value is never blocked. Independently-written
    subtrees are rejected with a targeted endpoint hint. Raises
    ProfileDataError naming the offending field."""
    if not isinstance(data, dict):
        raise ProfileDataError(f"'data' must be an object, got {type(data).__name__}")
    canonical: dict[str, Any] = {}
    for key, value in data.items():
        if key in SERVER_OWNED_SUBTREES:
            if key == "dynamic":
                raise ProfileDataError(
                    "field 'dynamic' is read-only (connector-owned)")
            raise ProfileDataError(
                f"field '{key}' is read-only here (server-owned; use the "
                f"{key} endpoint)")
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
    from language_tags import effective_language_rows

    data = data or {}
    summary = {k: data.get(k, "") for k in SUMMARY_KEYS}
    rows = effective_language_rows(data)
    preferred = next(
        (row for row in rows if row.get("stance") == "prefer"), None)
    summary["language"] = str((preferred or (rows[0] if rows else {})).get(
        "tag") or "")
    return summary


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
        "folders": [_folder_tree_row(f) for f in folders] + [
            {"id": tpl["folder"]["uuid"], "name": tpl["folder"]["name"],
             "description": tpl["folder"]["description"], "parentId": None,
             "builtin": True}
        ],
        "profiles": [_profile_tree_row(p) for p in profiles] + [
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
                      base_version: str | None = None) -> None:
    """Update name, description, placement and order of user-owned rows that
    already exist. List order becomes `position`.

    Per notes/ui-tree-persistence.md this save NEVER creates and NEVER deletes:
    a payload that omits an existing row, or names one the DB doesn't have, is
    a ProfileTreeError — absence means a bug, not an instruction. Creation is
    profile_create / profile_create_folder / profile_duplicate; deletion is
    profile_delete / profile_delete_folder.

    A stale `base_version` raises ProfileTreeConflict, checked before
    structural validation so a concurrent edit surfaces as 409, not 400. The
    virtual built-ins never ride a save (the validator rejects their uuids),
    and `data` is never touched — the form saves that through
    profile_update_data."""
    if base_version is not None and base_version != profile_tree_version():
        raise ProfileTreeConflict("profile tree changed since it was loaded")
    validate_profile_tree(folders, profiles)
    existing_f = {f.uuid: f for f in
                  db.session.execute(sa.select(ProfileFolder)).scalars().all()}
    existing_p = {p.uuid: p for p in
                  db.session.execute(sa.select(Profile)).scalars().all()}
    incoming_f = {UUID(f["id"]) for f in folders}
    incoming_p = {UUID(p["uuid"]) for p in profiles}
    for label, incoming, existing in (("folder", incoming_f, existing_f),
                                      ("profile", incoming_p, existing_p)):
        missing = set(existing) - incoming
        if missing:
            raise ProfileTreeError(
                f"tree save omitted {len(missing)} existing {label}(s) — refusing "
                f"(the tree save never deletes)")
        unknown = incoming - set(existing)
        if unknown:
            raise ProfileTreeError(
                f"tree save references {len(unknown)} unknown {label}(s) — refusing "
                f"(the tree save never creates)")
    for i, f in enumerate(folders):
        row = existing_f[UUID(f["id"])]
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for i, p in enumerate(profiles):
        row = existing_p[UUID(p["uuid"])]
        row.name = p.get("name", "")
        row.folder_uuid = UUID(p["folderId"]) if p.get("folderId") else None
        row.position = i
    db.session.commit()


# ---- create / delete (the tree save does neither) ----

def _profile_row(profile_uuid: UUID) -> Profile | None:
    return db.session.execute(
        sa.select(Profile).where(Profile.uuid == profile_uuid)
    ).scalar_one_or_none()


def _folder_row(folder_uuid: UUID) -> ProfileFolder | None:
    return db.session.execute(
        sa.select(ProfileFolder).where(ProfileFolder.uuid == folder_uuid)
    ).scalar_one_or_none()


def _folder_tree_row(f: ProfileFolder) -> dict[str, Any]:
    return {"id": str(f.uuid), "name": f.name, "description": f.description,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "updated_at": f.updated_at.isoformat() if f.updated_at else None}


def _profile_tree_row(row: Profile) -> dict[str, Any]:
    """One user-owned profile in tree-list field names (no data blob)."""
    return {
        "uuid": str(row.uuid), "name": row.name,
        "folderId": str(row.folder_uuid) if row.folder_uuid else None,
        "summary": profile_data_summary(row.data),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _next_position(model: Any, column: Any, parent: UUID | None) -> int:
    """One past the last sibling under `parent`, so a new row lands at the end
    of the folder it was created in."""
    highest = db.session.execute(
        sa.select(sa.func.max(model.position)).where(column == parent)
    ).scalar_one()
    return 0 if highest is None else highest + 1


def profile_create(name: str, folder_uuid: UUID | None) -> dict[str, Any]:
    """Create one empty profile at the end of its folder."""
    row = Profile(uuid=uuid4(), name=name, data={}, folder_uuid=folder_uuid,
                  position=_next_position(Profile, Profile.folder_uuid,
                                          folder_uuid))
    db.session.add(row)
    db.session.commit()
    return _profile_tree_row(row)


def profile_create_folder(name: str, parent_uuid: UUID | None) -> dict[str, Any]:
    """Create one folder at the end of its parent."""
    row = ProfileFolder(uuid=uuid4(), name=name, description="",
                        parent_uuid=parent_uuid,
                        position=_next_position(ProfileFolder,
                                                ProfileFolder.parent_uuid,
                                                parent_uuid))
    db.session.add(row)
    db.session.commit()
    return _folder_tree_row(row)


def _clear_current_profile_pointer(doomed: set[UUID]) -> None:
    """Stage the `profile.current` clear when one of `doomed` is the declared
    profile. Deleting it must clear the pointer and stamp the change IN THE
    SAME TRANSACTION as the row deletion — otherwise the setting dangles:
    every declared-profile block silently disappears on the next turn and no
    context marker ever announces it. The setting row is LOCKED first (the
    same lock set_current_profile takes before validating), so a concurrent
    switch cannot validate a profile this transaction is deleting and
    re-dangle the pointer after the commit. Staged through the settings
    module's no-commit helper so profile rows and settings rows commit (or
    roll back) together — the caller commits."""
    if not doomed:
        return
    from db.settings import (
        _registry,
        _upsert_setting_row,
        get_setting,
        lock_setting_row,
    )

    lock_setting_row("profile.current")
    current_raw = str(get_setting("profile.current") or "").strip()
    current_uuid = _to_uuid(current_raw) if current_raw else None
    if current_uuid is not None and current_uuid in doomed:
        from datetime import UTC, datetime

        stamp = datetime.now(UTC).isoformat()
        _upsert_setting_row(_registry("profile.current"), None)
        _upsert_setting_row(_registry("profile.current_changed_at"), stamp)


def profile_delete(profile_uuid: UUID) -> bool:
    """Delete one profile and its whole data blob. Clears the
    `profile.current` pointer in the same transaction if it named this row.
    False if the uuid is unknown (built-ins included — they are virtual, so
    there is nothing to delete)."""
    row = _profile_row(profile_uuid)
    if row is None:
        return False
    _clear_current_profile_pointer({profile_uuid})
    db.session.delete(row)
    db.session.commit()
    return True


def _descendant_folder_uuids(folder_uuid: UUID) -> list[UUID]:
    """`folder_uuid` plus every folder nested under it, any depth. Cycle-guarded
    via `seen`: a corrupt parent loop stops expanding a folder once it has
    already been collected, rather than spinning. No size cap — a large but
    legitimate subtree must be walked in full, or `profile_delete_folder` would
    delete only the collected prefix and orphan the rest."""
    out = [folder_uuid]
    seen = {folder_uuid}
    frontier = [folder_uuid]
    while frontier:
        children = db.session.execute(
            sa.select(ProfileFolder.uuid)
            .where(ProfileFolder.parent_uuid.in_(frontier))
        ).scalars().all()
        frontier = [c for c in children if c not in seen]
        seen.update(frontier)
        out.extend(frontier)
    return out


def profile_delete_folder(folder_uuid: UUID) -> bool:
    """Delete a folder, every folder nested under it, and every profile inside
    any of them. Clears the `profile.current` pointer in the same transaction
    if it named one of them. False if the uuid is unknown (the virtual
    Templates folder included)."""
    if _folder_row(folder_uuid) is None:
        return False
    folder_uuids = _descendant_folder_uuids(folder_uuid)
    doomed = set(db.session.execute(
        sa.select(Profile.uuid).where(Profile.folder_uuid.in_(folder_uuids))
    ).scalars().all())
    _clear_current_profile_pointer(doomed)
    if doomed:
        db.session.execute(sa.delete(Profile).where(Profile.uuid.in_(doomed)))
    db.session.execute(sa.delete(ProfileFolder).where(
        ProfileFolder.uuid.in_(folder_uuids)))
    db.session.commit()
    return True


# ---- per-profile data + duplication ----

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


def profile_mutate_data(profile_uuid: UUID,
                        mutator: Any) -> Profile | None:
    """Apply one subtree mutation to a profile's `data` under the row lock.

    Languages, calibration, flat fields, and `dynamic` share one JSONB column,
    so a subtree write must never be a read-modify-write race against a
    different subtree's writer: this selects the row FOR UPDATE, hands
    `mutator` a copy of the current dict, assigns the returned dict, and
    commits. Every future `dynamic` writer must use it too. Built-in virtual
    profiles never enter here (they have no row). Returns the row, or None if
    the uuid is unknown; a mutator exception rolls back (releasing the lock)
    and re-raises."""
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
    snapshot (raises ProfileDataError), preserving `dynamic`, `languages`,
    and `calibration` in the same transaction — a stale form autosave can
    never overwrite another editor.
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
    """Copy a profile's whole data blob (dynamic and nested editors included)
    into a new row — the one-action way to mint a friend's profile from an
    archetype. A user-owned source yields "<name> copy" in the same folder
    right after the source; a built-in source yields a real editable row named
    after the template at the end of the user-owned top level (the virtual
    Templates folder can't hold user rows). No version lineage — duplication
    is a convenience, not ancestry: calibration and language rows copy their
    semantic fields and order but receive fresh ids and the duplication
    timestamp, never the source's server-owned identity. Returns the new row in
    tree-list field names, or None if the source uuid is unknown."""
    from db.profile_calibration import refresh_calibration_identity
    from db.profile_languages import refresh_language_identity

    def copied_data(data: dict[str, Any]) -> dict[str, Any]:
        copied = deepcopy(data)
        refresh_calibration_identity(copied)
        refresh_language_identity(copied)
        return copied

    builtin = profile_builtin_get(profile_uuid)
    if builtin is not None:
        max_pos = db.session.execute(
            sa.select(sa.func.max(Profile.position)).where(Profile.folder_uuid.is_(None))
        ).scalar()
        row = Profile(uuid=uuid4(), name=builtin["name"],
                      data=copied_data(builtin["data"]),
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
                  data=copied_data(src.data or {}),
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
