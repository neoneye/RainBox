"""Personality tree: folder/personality persistence + append-only revisions.

Backs the /personality page. Saves follow docs/ui-tree-persistence.md — the
tree save only ever updates rows that already exist, so a payload that omits
or invents a row is an error rather than a silent create or delete; creation
and deletion are their own functions. This module holds both tree persistence
and complete revision history. Re-exported from db for import compatibility.
"""
import difflib
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


# ---- create / delete (the tree save does neither) ----

def _next_position(folder_uuid: UUID | None) -> int:
    highest = db.session.execute(
        sa.select(sa.func.max(Personality.position))
        .where(Personality.folder_uuid == folder_uuid)
    ).scalar_one()
    return 0 if highest is None else highest + 1


def personality_create(name: str, folder_uuid: UUID | None) -> dict[str, Any]:
    """Create one empty personality at the end of its folder."""
    row = Personality(uuid=uuid4(), name=name, content="",
                      folder_uuid=folder_uuid,
                      position=_next_position(folder_uuid))
    db.session.add(row)
    db.session.commit()
    return _personality_dict(row, 0)


def personality_create_folder(name: str, parent_uuid: UUID | None) -> dict[str, Any]:
    """Create one folder at the end of its parent."""
    highest = db.session.execute(
        sa.select(sa.func.max(PersonalityFolder.position))
        .where(PersonalityFolder.parent_uuid == parent_uuid)
    ).scalar_one()
    row = PersonalityFolder(uuid=uuid4(), name=name, description="",
                            parent_uuid=parent_uuid,
                            position=0 if highest is None else highest + 1)
    db.session.add(row)
    db.session.commit()
    return _folder_dict(row)


def personality_delete(personality_uuid: UUID) -> bool:
    """Delete one personality and its whole revision history. False if the
    uuid is unknown."""
    row = _personality_row(personality_uuid)
    if row is None:
        return False
    db.session.execute(sa.delete(PersonalityRevision).where(
        PersonalityRevision.personality_uuid == personality_uuid))
    db.session.delete(row)
    db.session.commit()
    return True


def _descendant_folder_uuids(folder_uuid: UUID) -> list[UUID]:
    """`folder_uuid` plus every folder nested under it, any depth. Cycle- and
    depth-guarded: a corrupt parent loop must not spin."""
    out = [folder_uuid]
    seen = {folder_uuid}
    frontier = [folder_uuid]
    while frontier and len(out) < _PERSONALITY_FOLDER_CAP:
        children = db.session.execute(
            sa.select(PersonalityFolder.uuid)
            .where(PersonalityFolder.parent_uuid.in_(frontier))
        ).scalars().all()
        frontier = [c for c in children if c not in seen]
        seen.update(frontier)
        out.extend(frontier)
    return out


def personality_delete_folder(folder_uuid: UUID) -> bool:
    """Delete a folder, every folder nested under it, and every personality
    inside any of them (revisions included). False if the uuid is unknown."""
    if _folder_row(folder_uuid) is None:
        return False
    folder_uuids = _descendant_folder_uuids(folder_uuid)
    doomed = db.session.execute(
        sa.select(Personality.uuid).where(Personality.folder_uuid.in_(folder_uuids))
    ).scalars().all()
    if doomed:
        db.session.execute(sa.delete(PersonalityRevision).where(
            PersonalityRevision.personality_uuid.in_(doomed)))
        db.session.execute(sa.delete(Personality).where(
            Personality.uuid.in_(doomed)))
    db.session.execute(sa.delete(PersonalityFolder).where(
        PersonalityFolder.uuid.in_(folder_uuids)))
    db.session.commit()
    return True


# ---- content + revision history ----

_PREVIEW_CHARS = 80


def _revision_dict(rev: PersonalityRevision, *, current: bool) -> dict[str, Any]:
    """One history row: enough to scan the list without fetching any text."""
    first_line = next((ln for ln in rev.content.splitlines() if ln.strip()), "")
    return {
        "uuid": str(rev.uuid),
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
        "bytes": len(rev.content.encode("utf-8")),
        "lines": len(rev.content.splitlines()),
        "preview": first_line[:_PREVIEW_CHARS],
        "current": current,
    }


def _revision_rows(personality_uuid: UUID) -> list[PersonalityRevision]:
    """Newest first. Ordered by id, not created_at — two saves can land in the
    same clock tick and the order still has to be exact."""
    return list(db.session.execute(
        sa.select(PersonalityRevision)
        .where(PersonalityRevision.personality_uuid == personality_uuid)
        .order_by(PersonalityRevision.id.desc())
    ).scalars().all())


def _revision_row(personality_uuid: UUID,
                  revision_uuid: UUID) -> PersonalityRevision | None:
    """One revision, but only if it belongs to this personality — a revision
    of some other personality is 'not found', never a diff against a stranger."""
    return db.session.execute(
        sa.select(PersonalityRevision).where(
            PersonalityRevision.uuid == revision_uuid,
            PersonalityRevision.personality_uuid == personality_uuid)
    ).scalar_one_or_none()


def personality_get(personality_uuid: UUID) -> dict[str, Any] | None:
    """One personality with its current text, for the editor pane. None if the
    uuid is unknown."""
    p = _personality_row(personality_uuid)
    if p is None:
        return None
    count = db.session.execute(
        sa.select(sa.func.count(PersonalityRevision.id))
        .where(PersonalityRevision.personality_uuid == personality_uuid)
    ).scalar_one()
    return {**_personality_dict(p, count), "content": p.content}


def _append_revision(p: Personality, content: str) -> dict[str, Any]:
    """Set the text and append the revision that mirrors it. The invariant
    'newest revision == content' is maintained here and nowhere else."""
    p.content = content
    rev = PersonalityRevision(uuid=uuid4(), personality_uuid=p.uuid, content=content)
    db.session.add(rev)
    db.session.commit()
    return _revision_dict(rev, current=True)


def personality_update_content(personality_uuid: UUID,
                               content: str) -> dict[str, Any] | None:
    """Save the text. A save that changes nothing appends nothing — no
    revision, no updated_at churn. None if the uuid is unknown."""
    p = _personality_row(personality_uuid)
    if p is None:
        return None
    if p.content == content:
        return {"changed": False, "revision": None}
    return {"changed": True, "revision": _append_revision(p, content)}


def personality_revisions(personality_uuid: UUID) -> list[dict[str, Any]] | None:
    """The history, newest first; the newest is flagged `current` because it
    is by definition the text the personality currently holds. None if the
    uuid is unknown."""
    if _personality_row(personality_uuid) is None:
        return None
    rows = _revision_rows(personality_uuid)
    return [_revision_dict(r, current=(i == 0)) for i, r in enumerate(rows)]


def personality_revision_diff(personality_uuid: UUID,
                              revision_uuid: UUID) -> dict[str, Any]:
    """Unified diff (3 context lines) of a revision's text → the current text.
    {ok: False, error} on any lookup problem; the API maps that to 404."""
    p = _personality_row(personality_uuid)
    if p is None:
        return {"ok": False, "error": "personality not found"}
    rev = _revision_row(personality_uuid, revision_uuid)
    if rev is None:
        return {"ok": False, "error": "revision not found"}
    stamp = rev.created_at.isoformat() if rev.created_at else str(rev.uuid)
    lines = list(difflib.unified_diff(
        rev.content.splitlines(), p.content.splitlines(),
        fromfile=f"{p.name} @ {stamp}", tofile=f"{p.name} (current)",
        lineterm="", n=3))
    rows = _revision_rows(personality_uuid)
    is_current = bool(rows) and rows[0].uuid == rev.uuid
    return {"ok": True, "revision": _revision_dict(rev, current=is_current),
            "lines": lines}


def personality_restore_revision(personality_uuid: UUID,
                                 revision_uuid: UUID) -> dict[str, Any]:
    """Bring an old revision back as the current text by APPENDING a new
    revision holding it. Nothing in the history is rewritten or removed, so a
    mistaken restore is itself undoable. Restoring text that is already
    current changes nothing."""
    p = _personality_row(personality_uuid)
    if p is None:
        return {"ok": False, "error": "personality not found"}
    rev = _revision_row(personality_uuid, revision_uuid)
    if rev is None:
        return {"ok": False, "error": "revision not found"}
    if p.content == rev.content:
        return {"ok": True, "changed": False, "content": p.content, "revision": None}
    return {"ok": True, "changed": True, "content": rev.content,
            "revision": _append_revision(p, rev.content)}
