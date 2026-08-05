"""Git repository tree: folder/repo persistence + live repo inspection.

Backs the /git page. Saves follow docs/ui-tree-persistence.md — the tree save
only ever updates rows that already exist, so a payload that omits or invents a
row is an error rather than a silent create or delete; creation and deletion
are their own functions. Also holds read-only filesystem helpers
(git_check_path, git_repo_detail) that shell out to `git` with bounded
timeouts. Re-exported from db for import compatibility.
"""
import hashlib
import json
import os
import subprocess
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from db.models import GitFolder, GitRepo, db

# Bound on every git/filesystem subprocess so a hung/slow repo can't wedge a
# web request (consistent with the lms subprocess hardening).
_GIT_TIMEOUT = 5


class GitTreeError(ValueError):
    """A git tree payload failed structural validation (bad uuid, dangling
    parent, cycle, a row that is missing or unknown). The PUT endpoint maps
    this to 400, not 500."""


class GitTreeConflict(Exception):
    """The tree changed since the caller hydrated (stale base_version on save);
    mapped to HTTP 409 so the client re-hydrates instead of clobbering."""


def _to_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def git_tree_version() -> str:
    """Opaque version token for the persisted tree (optimistic concurrency).
    The /git page hydrates with this and echoes it on PUT; git_save_tree refuses
    a save whose token is stale."""
    folders = db.session.execute(
        sa.select(GitFolder).order_by(GitFolder.uuid)
    ).scalars().all()
    repos = db.session.execute(
        sa.select(GitRepo).order_by(GitRepo.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name, f.description,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(r.uuid), r.name,
          str(r.folder_uuid) if r.folder_uuid else None,
          r.path, r.description, r.position]
         for r in repos],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _folder_row(folder_uuid: UUID) -> GitFolder | None:
    return db.session.execute(
        sa.select(GitFolder).where(GitFolder.uuid == folder_uuid)
    ).scalar_one_or_none()


def _repo_row(repo_uuid: UUID) -> GitRepo | None:
    return db.session.execute(
        sa.select(GitRepo).where(GitRepo.uuid == repo_uuid)
    ).scalar_one_or_none()


def _folder_dict(f: GitFolder) -> dict[str, Any]:
    return {"id": str(f.uuid), "name": f.name, "description": f.description,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "updated_at": f.updated_at.isoformat() if f.updated_at else None}


def _repo_dict(r: GitRepo) -> dict[str, Any]:
    return {"uuid": str(r.uuid), "name": r.name,
            "folderId": str(r.folder_uuid) if r.folder_uuid else None,
            "path": r.path, "description": r.description,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


def git_load_tree() -> dict[str, Any]:
    """The whole git tree in the frontend's field names, each list ordered by
    position then id so the page renders in saved order."""
    folders = db.session.execute(
        sa.select(GitFolder).order_by(GitFolder.position, GitFolder.id)
    ).scalars().all()
    repos = db.session.execute(
        sa.select(GitRepo).order_by(GitRepo.position, GitRepo.id)
    ).scalars().all()
    return {
        "folders": [_folder_dict(f) for f in folders],
        "repos": [_repo_dict(r) for r in repos],
        # Optimistic-concurrency token; the page echoes it on PUT (409 if stale).
        "version": git_tree_version(),
    }


def validate_git_tree(folders: list, repos: list) -> None:
    """Structural integrity check run before any DB write: well-formed uuids,
    no duplicate/dangling/cyclic folder references, repo folderIds resolve, and
    a repo uuid never collides with a folder id (a node is identified globally
    by uuid, so /git?id=<uuid> must be unambiguous). Raises GitTreeError on the
    first problem; does not touch the DB."""
    if not isinstance(folders, list):
        raise GitTreeError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(repos, list):
        raise GitTreeError(f"'repos' must be a list, got {type(repos).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise GitTreeError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise GitTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in parent_of:
            raise GitTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise GitTreeError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise GitTreeError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise GitTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise GitTreeError(f"folder {fid} references missing parent {pid}")
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise GitTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    repo_uuids: set[UUID] = set()
    for r in repos:
        if not isinstance(r, dict):
            raise GitTreeError(f"repo entry must be an object, got {type(r).__name__}")
        ru = _to_uuid(r.get("uuid"))
        if ru is None:
            raise GitTreeError(f"repo uuid is not a uuid: {r.get('uuid')!r}")
        if ru in repo_uuids:
            raise GitTreeError(f"duplicate repo uuid: {ru}")
        if ru in parent_of:
            raise GitTreeError(f"repo uuid {ru} collides with a folder id")
        repo_uuids.add(ru)
        if not isinstance(r.get("name", ""), str):
            raise GitTreeError(f"repo {ru} name must be a string")
        if not isinstance(r.get("path", ""), str):
            raise GitTreeError(f"repo {ru} path must be a string")
        if not isinstance(r.get("description", ""), str):
            raise GitTreeError(f"repo {ru} description must be a string")
        fld_raw = r.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise GitTreeError(f"repo {ru} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise GitTreeError(f"repo {ru} references missing folder {fld}")


def git_save_tree(folders: list, repos: list, *,
                  base_version: str | None = None) -> None:
    """Update name, description, placement and order of rows that already
    exist. List order becomes `position`.

    Per docs/ui-tree-persistence.md this save NEVER creates and NEVER deletes:
    a payload that omits an existing row, or names one the DB doesn't have, is
    a GitTreeError — absence means a bug, not an instruction. Creation is
    git_create_repo / git_create_folder; deletion is git_delete_repo /
    git_delete_folder.

    A stale `base_version` raises GitTreeConflict, checked before structural
    validation so a concurrent edit surfaces as 409, not 400. A repo's `path`
    is never touched: it is the repo's identity on disk, fixed at creation, and
    the page has no editor for it."""
    if base_version is not None and base_version != git_tree_version():
        raise GitTreeConflict("git tree changed since it was loaded")
    validate_git_tree(folders, repos)
    existing_f = {f.uuid: f for f in
                  db.session.execute(sa.select(GitFolder)).scalars().all()}
    existing_r = {r.uuid: r for r in
                  db.session.execute(sa.select(GitRepo)).scalars().all()}
    incoming_f = {UUID(f["id"]) for f in folders}
    incoming_r = {UUID(r["uuid"]) for r in repos}
    for label, incoming, existing in (("folder", incoming_f, existing_f),
                                      ("repo", incoming_r, existing_r)):
        missing = set(existing) - incoming
        if missing:
            raise GitTreeError(
                f"tree save omitted {len(missing)} existing {label}(s) — refusing "
                f"(the tree save never deletes)")
        unknown = incoming - set(existing)
        if unknown:
            raise GitTreeError(
                f"tree save references {len(unknown)} unknown {label}(s) — refusing "
                f"(the tree save never creates)")
    for i, f in enumerate(folders):
        row = existing_f[UUID(f["id"])]
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for i, r in enumerate(repos):
        row = existing_r[UUID(r["uuid"])]
        row.name = r.get("name", "")
        row.folder_uuid = UUID(r["folderId"]) if r.get("folderId") else None
        row.description = r.get("description", "")
        row.position = i
    db.session.commit()


# ---- create / delete (the tree save does neither) ----

def _next_position(model: Any, column: Any, parent: UUID | None) -> int:
    """One past the last sibling under `parent`, so a new row lands at the end
    of the folder it was created in."""
    highest = db.session.execute(
        sa.select(sa.func.max(model.position)).where(column == parent)
    ).scalar_one()
    return 0 if highest is None else highest + 1


def git_create_folder(name: str, parent_uuid: UUID | None) -> dict[str, Any]:
    """Create one folder at the end of its parent."""
    row = GitFolder(uuid=uuid4(), name=name, description="",
                    parent_uuid=parent_uuid,
                    position=_next_position(GitFolder, GitFolder.parent_uuid,
                                            parent_uuid))
    db.session.add(row)
    db.session.commit()
    return _folder_dict(row)


def git_create_repo(name: str, path: str,
                    folder_uuid: UUID | None) -> dict[str, Any]:
    """Create one repo node at the end of its folder. `path` is stored as the
    caller resolved it (git_check_path expands and validates it first)."""
    row = GitRepo(uuid=uuid4(), name=name, path=path, description="",
                  folder_uuid=folder_uuid,
                  position=_next_position(GitRepo, GitRepo.folder_uuid,
                                          folder_uuid))
    db.session.add(row)
    db.session.commit()
    return _repo_dict(row)


def git_delete_repo(repo_uuid: UUID) -> bool:
    """Forget one repo node. The repository on disk is untouched — RainBox only
    ever held a pointer to it. False if the uuid is unknown."""
    row = _repo_row(repo_uuid)
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def _descendant_folder_uuids(folder_uuid: UUID) -> list[UUID]:
    """`folder_uuid` plus every folder nested under it, any depth. Cycle-guarded
    via `seen`: a corrupt parent loop stops expanding a folder once it has
    already been collected, rather than spinning. No size cap — a large but
    legitimate subtree must be walked in full, or `git_delete_folder` would
    delete only the collected prefix and orphan the rest."""
    out = [folder_uuid]
    seen = {folder_uuid}
    frontier = [folder_uuid]
    while frontier:
        children = db.session.execute(
            sa.select(GitFolder.uuid)
            .where(GitFolder.parent_uuid.in_(frontier))
        ).scalars().all()
        frontier = [c for c in children if c not in seen]
        seen.update(frontier)
        out.extend(frontier)
    return out


def git_delete_folder(folder_uuid: UUID) -> bool:
    """Delete a folder, every folder nested under it, and every repo node
    inside any of them. Repositories on disk are untouched. False if the uuid
    is unknown."""
    if _folder_row(folder_uuid) is None:
        return False
    folder_uuids = _descendant_folder_uuids(folder_uuid)
    db.session.execute(sa.delete(GitRepo).where(
        GitRepo.folder_uuid.in_(folder_uuids)))
    db.session.execute(sa.delete(GitFolder).where(
        GitFolder.uuid.in_(folder_uuids)))
    db.session.commit()
    return True


# ---- live filesystem inspection (read-only) ----

def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    """Run `git -C <path> <args…>` with a bounded timeout, capturing output."""
    return subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT)


def _git_branch(path: str) -> str:
    """Current branch name (empty if it can't be resolved, e.g. detached/no commits)."""
    try:
        out = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_check_path(path: Any) -> dict[str, Any]:
    """Validate that `path` is an existing git repository. Returns
    {ok, path, branch} on success or {ok: False, error} otherwise. Used by the
    Add-repo flow before a repo node is created. `path` is expanded
    (~) and resolved to an absolute path that the caller stores."""
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "path is required"}
    abspath = os.path.realpath(os.path.expanduser(path.strip()))
    if not os.path.isdir(abspath):
        return {"ok": False, "error": f"no such directory: {abspath}"}
    try:
        inside = _git(abspath, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"git failed: {exc}"}
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"ok": False, "error": "not a git repository"}
    return {"ok": True, "path": abspath, "branch": _git_branch(abspath)}


def git_repo_detail(repo_uuid: UUID) -> dict[str, Any] | None:
    """Live snapshot for the repo detail pane: path, existence, whether it is
    still a git repo, current branch, and the root directory listing (all
    entries incl. .git and dotfiles, directories first then files, each
    name-sorted). Returns None if the uuid is unknown. Never raises on
    filesystem problems — it reports them via the exists/isRepo flags so the UI
    can message."""
    repo = _repo_row(repo_uuid)
    if repo is None:
        return None
    path = repo.path
    exists = bool(path) and os.path.isdir(path)
    is_repo = False
    branch = ""
    entries: list[dict[str, Any]] = []
    if exists:
        try:
            inside = _git(path, "rev-parse", "--is-inside-work-tree")
            is_repo = inside.returncode == 0 and inside.stdout.strip() == "true"
        except (OSError, subprocess.SubprocessError):
            is_repo = False
        if is_repo:
            branch = _git_branch(path)
        try:
            names = os.listdir(path)
        except OSError:
            names = []
        dirs = sorted((n for n in names if os.path.isdir(os.path.join(path, n))),
                      key=str.lower)
        files = sorted((n for n in names if not os.path.isdir(os.path.join(path, n))),
                       key=str.lower)
        entries = ([{"name": n, "isDir": True} for n in dirs] +
                   [{"name": n, "isDir": False} for n in files])
    return {"path": path, "exists": exists, "isRepo": is_repo,
            "branch": branch, "entries": entries}
