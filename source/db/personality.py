"""Personality tree: folder/personality persistence + append-only revisions.

Backs the /personality page. Saves follow docs/ui-tree-persistence.md — the
tree save only ever updates rows that already exist, so a payload that omits
or invents a row is an error rather than a silent create or delete; creation
and deletion are their own functions. The revision half lives in
db/personality_history.py. Re-exported from db for import compatibility.
"""
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from db.models import Personality, PersonalityFolder, PersonalityRevision, db

# Bound on the folder-ancestor walk so a corrupt parent loop can't spin.
_PERSONALITY_FOLDER_CAP = 100


class PersonalityTreeError(ValueError):
    """A personality tree payload failed structural validation (bad uuid,
    dangling parent, cycle, a row that is missing or unknown). The API maps
    this to 400, not 500."""


class PersonalityTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version); mapped
    to HTTP 409 so the client re-hydrates instead of clobbering."""


def _to_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _revision_counts() -> dict[UUID, int]:
    """personality_uuid -> number of revisions, for the tree rows."""
    rows = db.session.execute(
        sa.select(PersonalityRevision.personality_uuid,
                  sa.func.count(PersonalityRevision.id))
        .group_by(PersonalityRevision.personality_uuid)
    ).all()
    return {pu: count for pu, count in rows}


def personality_tree_version() -> str:
    """Opaque version token for the persisted tree (optimistic concurrency).
    Covers only structural fields — `content`, revisions and timestamps are
    excluded, so saving text never invalidates an open page's tree."""
    folders = db.session.execute(
        sa.select(PersonalityFolder).order_by(PersonalityFolder.uuid)
    ).scalars().all()
    personalities = db.session.execute(
        sa.select(Personality).order_by(Personality.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name, f.description,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(p.uuid), p.name,
          str(p.folder_uuid) if p.folder_uuid else None, p.position]
         for p in personalities],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _folder_row(folder_uuid: UUID) -> PersonalityFolder | None:
    return db.session.execute(
        sa.select(PersonalityFolder).where(PersonalityFolder.uuid == folder_uuid)
    ).scalar_one_or_none()


def _personality_row(personality_uuid: UUID) -> Personality | None:
    return db.session.execute(
        sa.select(Personality).where(Personality.uuid == personality_uuid)
    ).scalar_one_or_none()


def _folder_dict(f: PersonalityFolder) -> dict[str, Any]:
    return {"id": str(f.uuid), "name": f.name, "description": f.description,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "updated_at": f.updated_at.isoformat() if f.updated_at else None}


def _personality_dict(p: Personality, revision_count: int = 0) -> dict[str, Any]:
    return {"uuid": str(p.uuid), "name": p.name,
            "folderId": str(p.folder_uuid) if p.folder_uuid else None,
            "revisionCount": revision_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None}


def personality_load_tree() -> dict[str, Any]:
    """The whole tree in the frontend's field names, ordered by position then
    id. `content` is omitted (loaded per-personality via personality_get);
    `revisionCount` rides along for the folder table and the delete modal, and
    is derived, so it stays out of the version hash."""
    folders = db.session.execute(
        sa.select(PersonalityFolder).order_by(
            PersonalityFolder.position, PersonalityFolder.id)
    ).scalars().all()
    personalities = db.session.execute(
        sa.select(Personality).order_by(Personality.position, Personality.id)
    ).scalars().all()
    counts = _revision_counts()
    return {
        "folders": [_folder_dict(f) for f in folders],
        "personalities": [_personality_dict(p, counts.get(p.uuid, 0))
                          for p in personalities],
        "version": personality_tree_version(),
    }


def validate_personality_tree(folders: list, personalities: list) -> None:
    """Structural integrity check run before any DB write: well-formed uuids,
    no duplicate/dangling/cyclic folder references, personality folderIds
    resolve, and a personality uuid never collides with a folder id (a node is
    identified globally by uuid, so /personality?id=<uuid> must be
    unambiguous). Raises PersonalityTreeError on the first problem; does not
    touch the DB."""
    if not isinstance(folders, list):
        raise PersonalityTreeError(
            f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(personalities, list):
        raise PersonalityTreeError(
            f"'personalities' must be a list, got {type(personalities).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise PersonalityTreeError(
                f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise PersonalityTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in parent_of:
            raise PersonalityTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise PersonalityTreeError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise PersonalityTreeError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise PersonalityTreeError(
                    f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise PersonalityTreeError(f"folder {fid} references missing parent {pid}")
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise PersonalityTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    seen_p: set[UUID] = set()
    for p in personalities:
        if not isinstance(p, dict):
            raise PersonalityTreeError(
                f"personality entry must be an object, got {type(p).__name__}")
        pu = _to_uuid(p.get("uuid"))
        if pu is None:
            raise PersonalityTreeError(
                f"personality uuid is not a uuid: {p.get('uuid')!r}")
        if pu in seen_p:
            raise PersonalityTreeError(f"duplicate personality uuid: {pu}")
        if pu in parent_of:
            raise PersonalityTreeError(
                f"personality uuid {pu} collides with a folder id")
        seen_p.add(pu)
        if not isinstance(p.get("name", ""), str):
            raise PersonalityTreeError(f"personality {pu} name must be a string")
        fld_raw = p.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise PersonalityTreeError(
                    f"personality {pu} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise PersonalityTreeError(
                    f"personality {pu} references missing folder {fld}")


def personality_save_tree(folders: list, personalities: list, *,
                          base_version: str | None = None) -> None:
    """Update name, description, placement and order of rows that already
    exist. List order becomes `position`.

    Per docs/ui-tree-persistence.md this save NEVER creates and NEVER deletes:
    a payload that omits an existing row, or names one the DB doesn't have, is
    a PersonalityTreeError — absence means a bug, not an instruction. Creation
    is personality_create / personality_create_folder; deletion is
    personality_delete / personality_delete_folder.

    A stale `base_version` raises PersonalityTreeConflict, checked before
    structural validation so a concurrent edit surfaces as 409, not 400.
    `content` is never touched."""
    if base_version is not None and base_version != personality_tree_version():
        raise PersonalityTreeConflict("personality tree changed since it was loaded")
    validate_personality_tree(folders, personalities)
    existing_f = {f.uuid: f for f in db.session.execute(
        sa.select(PersonalityFolder)).scalars().all()}
    existing_p = {p.uuid: p for p in db.session.execute(
        sa.select(Personality)).scalars().all()}
    incoming_f = {UUID(f["id"]) for f in folders}
    incoming_p = {UUID(p["uuid"]) for p in personalities}
    for label, incoming, existing in (("folder", incoming_f, existing_f),
                                      ("personality", incoming_p, existing_p)):
        missing = set(existing) - incoming
        if missing:
            raise PersonalityTreeError(
                f"tree save omitted {len(missing)} existing {label}(s) — refusing "
                f"(the tree save never deletes)")
        unknown = incoming - set(existing)
        if unknown:
            raise PersonalityTreeError(
                f"tree save references {len(unknown)} unknown {label}(s) — refusing "
                f"(the tree save never creates)")
    for i, f in enumerate(folders):
        row = existing_f[UUID(f["id"])]
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for i, p in enumerate(personalities):
        row = existing_p[UUID(p["uuid"])]
        row.name = p.get("name", "")
        row.folder_uuid = UUID(p["folderId"]) if p.get("folderId") else None
        row.position = i
    db.session.commit()
