"""System prompt tree: folder/prompt persistence + version lineage helpers.

Backs the /prompt page. Saves follow docs/ui-tree-persistence.md — the tree
save only ever updates rows that already exist, so a payload that omits or
invents a row is an error rather than a silent create or delete; creation and
deletion are their own functions. Also holds the version-lineage operations:
per-prompt content read/write, clone (the way to make a new version; the
clone's parent_uuid records what it was based on), ancestor-chain walk, and a
2-way unified diff of an ancestor against the current content. Re-exported
from db for import compatibility.
"""
import difflib
import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from db.models import Prompt, PromptFolder, db

# Bound on the ancestor-chain walk so a corrupt parent loop (which the
# validator can't see — parent_uuid may legitimately dangle) can't spin.
_PROMPT_ANCESTOR_CAP = 100


class PromptTreeError(ValueError):
    """A prompt tree payload failed structural validation (bad uuid, dangling
    parent folder, cycle, a row that is missing or unknown). The PUT endpoint
    maps this to 400, not 500."""


class PromptTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version on save);
    mapped to HTTP 409 so the client re-hydrates instead of clobbering."""


def _to_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def prompt_tree_version() -> str:
    """Opaque version token for the persisted tree (optimistic concurrency).
    Covers only structural fields — `content` is excluded so saving a
    prompt's text never invalidates an open page's tree version."""
    folders = db.session.execute(
        sa.select(PromptFolder).order_by(PromptFolder.uuid)
    ).scalars().all()
    prompts = db.session.execute(
        sa.select(Prompt).order_by(Prompt.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name, f.description,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(p.uuid), p.name,
          str(p.folder_uuid) if p.folder_uuid else None,
          str(p.parent_uuid) if p.parent_uuid else None, p.position]
         for p in prompts],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _folder_row(folder_uuid: UUID) -> PromptFolder | None:
    return db.session.execute(
        sa.select(PromptFolder).where(PromptFolder.uuid == folder_uuid)
    ).scalar_one_or_none()


def _folder_dict(f: PromptFolder) -> dict[str, Any]:
    return {"id": str(f.uuid), "name": f.name, "description": f.description,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "updated_at": f.updated_at.isoformat() if f.updated_at else None}


def _prompt_tree_dict(p: Prompt) -> dict[str, Any]:
    """One prompt in tree-list field names — no `content` (the tree never
    carries it; the editor fetches it per-prompt via prompt_get)."""
    return {"uuid": str(p.uuid), "name": p.name,
            "folderId": str(p.folder_uuid) if p.folder_uuid else None,
            "parentUuid": str(p.parent_uuid) if p.parent_uuid else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None}


def prompt_load_tree() -> dict[str, Any]:
    """The whole prompt tree in the frontend's field names, each list ordered
    by position then id so the page renders in saved order. Prompt `content`
    is deliberately omitted (loaded per-prompt via prompt_get)."""
    folders = db.session.execute(
        sa.select(PromptFolder).order_by(PromptFolder.position, PromptFolder.id)
    ).scalars().all()
    prompts = db.session.execute(
        sa.select(Prompt).order_by(Prompt.position, Prompt.id)
    ).scalars().all()
    return {
        "folders": [_folder_dict(f) for f in folders],
        "prompts": [_prompt_tree_dict(p) for p in prompts],
        # Optimistic-concurrency token; the page echoes it on PUT (409 if stale).
        "version": prompt_tree_version(),
    }


def validate_prompt_tree(folders: list, prompts: list) -> None:
    """Structural integrity check run before any DB write: well-formed uuids,
    no duplicate/dangling/cyclic folder references, prompt folderIds resolve,
    and a prompt uuid never collides with a folder id (a node is identified
    globally by uuid, so /prompt?id=<uuid> must be unambiguous). A prompt's
    parentUuid (its cloned-from version) is NOT checked — it may legitimately
    dangle after the parent version is deleted. Raises PromptTreeError on the
    first problem; does not touch the DB."""
    if not isinstance(folders, list):
        raise PromptTreeError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(prompts, list):
        raise PromptTreeError(f"'prompts' must be a list, got {type(prompts).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise PromptTreeError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise PromptTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in parent_of:
            raise PromptTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise PromptTreeError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise PromptTreeError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise PromptTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise PromptTreeError(f"folder {fid} references missing parent {pid}")
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise PromptTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    prompt_uuids: set[UUID] = set()
    for p in prompts:
        if not isinstance(p, dict):
            raise PromptTreeError(f"prompt entry must be an object, got {type(p).__name__}")
        pu = _to_uuid(p.get("uuid"))
        if pu is None:
            raise PromptTreeError(f"prompt uuid is not a uuid: {p.get('uuid')!r}")
        if pu in prompt_uuids:
            raise PromptTreeError(f"duplicate prompt uuid: {pu}")
        if pu in parent_of:
            raise PromptTreeError(f"prompt uuid {pu} collides with a folder id")
        prompt_uuids.add(pu)
        if not isinstance(p.get("name", ""), str):
            raise PromptTreeError(f"prompt {pu} name must be a string")
        par_raw = p.get("parentUuid")
        if par_raw is not None and _to_uuid(par_raw) is None:
            raise PromptTreeError(f"prompt {pu} parentUuid is not a uuid: {par_raw!r}")
        fld_raw = p.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise PromptTreeError(f"prompt {pu} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise PromptTreeError(f"prompt {pu} references missing folder {fld}")


def prompt_save_tree(folders: list, prompts: list, *,
                     base_version: str | None = None) -> None:
    """Update name, description, placement and order of rows that already
    exist. List order becomes `position`.

    Per docs/ui-tree-persistence.md this save NEVER creates and NEVER deletes:
    a payload that omits an existing row, or names one the DB doesn't have, is
    a PromptTreeError — absence means a bug, not an instruction. Creation is
    prompt_create / prompt_clone / prompt_create_folder; deletion is
    prompt_delete / prompt_delete_folder.

    A stale `base_version` raises PromptTreeConflict, checked before structural
    validation so a concurrent edit surfaces as 409, not 400. Never touches
    `content` (the editor saves that through prompt_update_content) nor
    `parent_uuid` — the version lineage is written once, by whichever clone
    made the row, and nothing in the UI re-parents it afterwards."""
    if base_version is not None and base_version != prompt_tree_version():
        raise PromptTreeConflict("prompt tree changed since it was loaded")
    validate_prompt_tree(folders, prompts)
    existing_f = {f.uuid: f for f in
                  db.session.execute(sa.select(PromptFolder)).scalars().all()}
    existing_p = {p.uuid: p for p in
                  db.session.execute(sa.select(Prompt)).scalars().all()}
    incoming_f = {UUID(f["id"]) for f in folders}
    incoming_p = {UUID(p["uuid"]) for p in prompts}
    for label, incoming, existing in (("folder", incoming_f, existing_f),
                                      ("prompt", incoming_p, existing_p)):
        missing = set(existing) - incoming
        if missing:
            raise PromptTreeError(
                f"tree save omitted {len(missing)} existing {label}(s) — refusing "
                f"(the tree save never deletes)")
        unknown = incoming - set(existing)
        if unknown:
            raise PromptTreeError(
                f"tree save references {len(unknown)} unknown {label}(s) — refusing "
                f"(the tree save never creates)")
    for i, f in enumerate(folders):
        row = existing_f[UUID(f["id"])]
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for i, p in enumerate(prompts):
        row = existing_p[UUID(p["uuid"])]
        row.name = p.get("name", "")
        row.folder_uuid = UUID(p["folderId"]) if p.get("folderId") else None
        row.position = i
    db.session.commit()


# ---- create / delete (the tree save does neither) ----

def _prompt_row(prompt_uuid: UUID) -> Prompt | None:
    return db.session.execute(
        sa.select(Prompt).where(Prompt.uuid == prompt_uuid)
    ).scalar_one_or_none()


def _next_position(model: Any, column: Any, parent: UUID | None) -> int:
    """One past the last sibling under `parent`, so a new row lands at the end
    of the folder it was created in."""
    highest = db.session.execute(
        sa.select(sa.func.max(model.position)).where(column == parent)
    ).scalar_one()
    return 0 if highest is None else highest + 1


def prompt_create(name: str, folder_uuid: UUID | None) -> dict[str, Any]:
    """Create one empty prompt at the end of its folder. It starts a fresh
    lineage (no parent_uuid) — a new *version* of an existing prompt is
    prompt_clone."""
    row = Prompt(uuid=uuid4(), name=name, content="", folder_uuid=folder_uuid,
                 position=_next_position(Prompt, Prompt.folder_uuid, folder_uuid))
    db.session.add(row)
    db.session.commit()
    return _prompt_tree_dict(row)


def prompt_create_folder(name: str, parent_uuid: UUID | None) -> dict[str, Any]:
    """Create one folder at the end of its parent."""
    row = PromptFolder(uuid=uuid4(), name=name, description="",
                       parent_uuid=parent_uuid,
                       position=_next_position(PromptFolder,
                                               PromptFolder.parent_uuid,
                                               parent_uuid))
    db.session.add(row)
    db.session.commit()
    return _folder_dict(row)


def prompt_delete(prompt_uuid: UUID) -> bool:
    """Delete one prompt. Descendant versions are NOT deleted — their
    parent_uuid is simply left dangling, which prompt_ancestors already treats
    as the end of the lineage. Losing an intermediate version must not take
    the versions derived from it. False if the uuid is unknown."""
    row = _prompt_row(prompt_uuid)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def _descendant_folder_uuids(folder_uuid: UUID) -> list[UUID]:
    """`folder_uuid` plus every folder nested under it, any depth. Cycle-guarded
    via `seen`: a corrupt parent loop stops expanding a folder once it has
    already been collected, rather than spinning. No size cap — a large but
    legitimate subtree must be walked in full, or `prompt_delete_folder` would
    delete only the collected prefix and orphan the rest."""
    out = [folder_uuid]
    seen = {folder_uuid}
    frontier = [folder_uuid]
    while frontier:
        children = db.session.execute(
            sa.select(PromptFolder.uuid)
            .where(PromptFolder.parent_uuid.in_(frontier))
        ).scalars().all()
        frontier = [c for c in children if c not in seen]
        seen.update(frontier)
        out.extend(frontier)
    return out


def prompt_delete_folder(folder_uuid: UUID) -> bool:
    """Delete a folder, every folder nested under it, and every prompt inside
    any of them. False if the uuid is unknown."""
    if _folder_row(folder_uuid) is None:
        return False
    folder_uuids = _descendant_folder_uuids(folder_uuid)
    db.session.execute(sa.delete(Prompt).where(
        Prompt.folder_uuid.in_(folder_uuids)))
    db.session.execute(sa.delete(PromptFolder).where(
        PromptFolder.uuid.in_(folder_uuids)))
    db.session.commit()
    return True


# ---- per-prompt content + version lineage ----


def prompt_get(prompt_uuid: UUID) -> dict[str, Any] | None:
    """One prompt with its content, for the editor pane. Includes the parent's
    name + existence so the "Based on" link can render without a second fetch.
    Returns None if the uuid is unknown."""
    p = _prompt_row(prompt_uuid)
    if p is None:
        return None
    parent = _prompt_row(p.parent_uuid) if p.parent_uuid else None
    return {
        "uuid": str(p.uuid), "name": p.name, "content": p.content,
        "folderId": str(p.folder_uuid) if p.folder_uuid else None,
        "parentUuid": str(p.parent_uuid) if p.parent_uuid else None,
        "parentName": parent.name if parent else None,
        "parentExists": parent is not None if p.parent_uuid else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def prompt_update_content(prompt_uuid: UUID, content: str) -> bool:
    """Replace a prompt's content (the editor's explicit Save; last write
    wins). Returns False if the uuid is unknown."""
    p = _prompt_row(prompt_uuid)
    if p is None:
        return False
    p.content = content
    db.session.commit()
    return True


def _clone_name(src_name: str) -> str:
    """The name for a clone of `src_name`: a trailing integer is incremented
    ("Daily quiz 73" → "Daily quiz 74", zero-padding kept: "take 09" →
    "take 10"); without one, " 2" is appended. Counts past any name already
    taken by another prompt, so repeated clones of the same source don't
    collide."""
    m = re.search(r"^(.*?)(\d+)\s*$", src_name)
    if m:
        prefix, digits = m.group(1), m.group(2)
        n, width = int(digits) + 1, len(digits)
    else:
        prefix = src_name + " " if src_name else ""
        n, width = 2, 1
    taken = {name for (name,) in db.session.query(Prompt.name).all()}
    while f"{prefix}{str(n).zfill(width)}" in taken:
        n += 1
    return f"{prefix}{str(n).zfill(width)}"


def prompt_clone(prompt_uuid: UUID) -> dict[str, Any] | None:
    """Make a new version of a prompt: copy the content into a new row whose
    parent_uuid records the source, placed in the same folder right after it,
    named by incrementing the source name's trailing number (see _clone_name).
    This is one of the tree's create paths — the tree save can never make a row
    (docs/ui-tree-persistence.md). Returns the new row in tree-list field names
    (no content), or None if the source uuid is unknown."""
    src = _prompt_row(prompt_uuid)
    if src is None:
        return None
    clone = Prompt(uuid=uuid4(), name=_clone_name(src.name), content=src.content,
                   parent_uuid=src.uuid, folder_uuid=src.folder_uuid,
                   position=src.position + 1)
    # Shift later siblings so the clone's slot is unambiguous even before the
    # next whole-tree save rewrites all positions.
    siblings = db.session.execute(
        sa.select(Prompt).where(Prompt.folder_uuid == src.folder_uuid)
    ).scalars().all()
    for sib in siblings:
        if sib.position > src.position:
            sib.position += 1
    db.session.add(clone)
    db.session.commit()
    return _prompt_tree_dict(clone)


def prompt_ancestors(prompt_uuid: UUID) -> list[Prompt]:
    """The version lineage: parent, grandparent, … oldest last. Stops at a
    lineage root, a dangling parent_uuid (deleted ancestor), a cycle, or the
    hop cap — never raises."""
    out: list[Prompt] = []
    seen: set[UUID] = {prompt_uuid}
    cur = _prompt_row(prompt_uuid)
    while cur is not None and cur.parent_uuid and len(out) < _PROMPT_ANCESTOR_CAP:
        if cur.parent_uuid in seen:
            break
        seen.add(cur.parent_uuid)
        cur = _prompt_row(cur.parent_uuid)
        if cur is None:
            break
        out.append(cur)
    return out


def prompt_diff(prompt_uuid: UUID, against_uuid: UUID | None = None) -> dict[str, Any]:
    """2-way line diff of an ancestor's content → this prompt's content.
    `against_uuid` defaults to the immediate parent and must be an ancestor.
    Returns {ok: True, against, ancestors, lines} where `lines` is unified-diff
    text lines (3 context lines), or {ok: False, error} on any lookup/lineage
    problem (the API layer maps that to 400/404 by the `error` text)."""
    p = _prompt_row(prompt_uuid)
    if p is None:
        return {"ok": False, "error": "prompt not found"}
    ancestors = prompt_ancestors(prompt_uuid)
    if not ancestors:
        return {"ok": False, "error": "prompt has no available ancestor to diff against"}
    if against_uuid is None:
        target = ancestors[0]
    else:
        target = next((a for a in ancestors if a.uuid == against_uuid), None)
        if target is None:
            return {"ok": False, "error": "'against' is not an ancestor of this prompt"}
    lines = list(difflib.unified_diff(
        target.content.splitlines(), p.content.splitlines(),
        fromfile=target.name or str(target.uuid),
        tofile=p.name or str(p.uuid), lineterm="", n=3))
    return {
        "ok": True,
        "against": {"uuid": str(target.uuid), "name": target.name},
        "ancestors": [
            {"uuid": str(a.uuid), "name": a.name,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in ancestors
        ],
        "lines": lines,
    }
