# `/personality` Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/personality` page — a folder tree of assistant personalities, one free-text body each, where every save that changes the text appends a revision so any edit can be rolled back.

**Architecture:** Server-rendered Jinja shell + vanilla-JS client holding the whole tree in memory, a near-verbatim port of `/prompt`. Persistence follows `docs/ui-tree-persistence.md`: the tree PUT only updates rows that already exist, while creation and deletion are dedicated endpoints, so no payload can delete anything. Text lives in `personality.content` with an append-only `personality_revision` log behind it; restore appends rather than rewinds.

**Tech Stack:** Python 3 + Flask + Flask-SQLAlchemy (Postgres), vanilla JS (no framework), CodeMirror 5 from jsDelivr, Jinja2 `render_template_string`, `pytest`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-03-personality-ui-design.md`. Read it before Task 1.
- Save shape is fixed by `docs/ui-tree-persistence.md` — the tree PUT never creates and never deletes; no `expected_deletes` parameter anywhere in this feature.
- Tree/CSS conventions: `docs/ui-left-panel-tree.md` (especially §8 gotchas), `docs/ui-modals.md`, `docs/ui-kebab-menu.md`, `docs/ui-modal-rename.md`.
- Reference columns are plain UUID columns — **no FK constraints**; integrity lives in `validate_personality_tree`.
- New tables need **no** migration: `db.init_db` calls `db.create_all()`, which creates new tables (it only skips ALTERs to existing ones).
- Tests run against `rainbox_claude` automatically (`source/conftest.py`). Never point ad-hoc scripts at `rainbox_production`.
- Working directory for every command is `/Users/neoneye/git/rainbox/source`. Run tests with `./venv/bin/python -m pytest <path> -v`.
- Revision ordering is by `id` (monotonic), never `created_at` — two saves in the same clock tick must still order deterministically.
- Docs describe current state, not change history: no "renamed from", no migration notes.

## Reference implementations (keep open)

- Models: `db/models.py` → `PromptFolder` / `Prompt` (lines ~1503-1542).
- Tree DB ops: `db/prompt.py` (whole file, 362 lines).
- Placement-only save with missing/unknown-uuid guards: `db/chat.py` → `chat_save_tree` (line ~460) — the closest existing shape to this standard.
- API: `webapp/prompt_api.py` (109 lines); folder create/delete endpoints: `webapp/chat_api.py` lines 160-193.
- Page shell: `webapp/prompt_views.py` (251 lines).
- Client: `static/prompt.js` (1209 lines) — Tasks 7 and 8 port it.
- Tests: `db/test_prompt_tree.py`, `webapp/test_prompt_api.py`, `webapp/test_prompt_views.py`.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `db/models.py` | `PersonalityFolder`, `Personality`, `PersonalityRevision` ORM tables | Modify (append three classes after `Profile`) |
| `db/personality.py` | Tree load/validate/save/version, create/delete, content + revisions | Create |
| `db/__init__.py` | Re-export `db.personality` public names | Modify (one import line) |
| `webapp/personality_api.py` | JSON API (tree, create/delete, content, revisions) | Create |
| `webapp/personality_views.py` | `/personality` page shell (HTML + inline CSS) | Create |
| `webapp/__init__.py` | Import the two new modules to register routes | Modify (two import lines) |
| `webapp/core.py` | Nav entry in the Assistant dropdown + three Flask-Admin views | Modify |
| `db/find_uuid.py` | Resolve personality / folder / revision uuids | Modify |
| `static/personality.js` | Browser client (tree, editor, history) | Create |
| `db/test_personality_tree.py` | Models, tree ops, revision semantics | Create |
| `webapp/test_personality_api.py` | Endpoint behavior + status codes | Create |
| `webapp/test_personality_views.py` | Page shell + JS markers | Create |
| `docs/personality-design.md` | The page's design doc | Create |
| `docs/README.md` | Index the new design doc | Modify (one line) |

---

## Task 1: The three tables

**Files:**
- Modify: `db/models.py` (append after the `Profile` class, before `psycopg_dsn()`)
- Test: `db/test_personality_tree.py` (create)

**Interfaces:**
- Produces: `PersonalityFolder`, `Personality`, `PersonalityRevision` importable from `db.models`.

- [ ] **Step 1: Write the failing test**

Create `db/test_personality_tree.py`:

```python
"""Tests for the personality tree persistence + revision history (db.personality)."""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db.models import Personality, PersonalityFolder, PersonalityRevision


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def test_personality_models_round_trip(app_ctx):
    fu, pu, ru = uuid4(), uuid4(), uuid4()
    db.db.session.add(PersonalityFolder(uuid=fu, name="T-folder", parent_uuid=None, position=0))
    db.db.session.add(Personality(
        uuid=pu, name="T-personality", content="Curious and blunt.",
        folder_uuid=fu, position=0,
    ))
    db.db.session.add(PersonalityRevision(
        uuid=ru, personality_uuid=pu, content="Curious and blunt."))
    db.db.session.commit()
    try:
        f = db.db.session.execute(
            sa.select(PersonalityFolder).where(PersonalityFolder.uuid == fu)).scalar_one()
        p = db.db.session.execute(
            sa.select(Personality).where(Personality.uuid == pu)).scalar_one()
        r = db.db.session.execute(
            sa.select(PersonalityRevision).where(PersonalityRevision.uuid == ru)).scalar_one()
        assert f.name == "T-folder" and f.parent_uuid is None
        assert p.content == "Curious and blunt." and p.folder_uuid == fu
        assert r.personality_uuid == pu and r.content == p.content
        assert f.created_at and p.updated_at and r.created_at  # timestamp defaults fire
    finally:
        db.db.session.execute(
            sa.delete(PersonalityRevision).where(PersonalityRevision.uuid == ru))
        db.db.session.execute(sa.delete(Personality).where(Personality.uuid == pu))
        db.db.session.execute(
            sa.delete(PersonalityFolder).where(PersonalityFolder.uuid == fu))
        db.db.session.commit()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v`
Expected: FAIL — `ImportError: cannot import name 'Personality' from 'db.models'`.

- [ ] **Step 3: Add the models**

In `db/models.py`, append after the `Profile` class (immediately before `def psycopg_dsn()`):

```python
class PersonalityFolder(db.Model):
    """Folder in the /personality tree. Same structural shape as PromptFolder:
    parent pointer, no FK, position-ordered within a parent."""

    __tablename__ = "personality_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")  # notes about the child nodes
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("personality_folder_children", "parent_uuid", "position"),)


class Personality(db.Model):
    """Who the assistant is: one free-text character description, backing
    /personality. The uuid is stable for the life of the personality — edits
    never mint a new one — so anything that binds to a personality keeps
    pointing at it. `content` is the current text; every saved state of it is
    kept in personality_revision, whose newest row always mirrors `content`."""

    __tablename__ = "personality"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")  # the personality text itself
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = unfiled at root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("personality_in_folder", "folder_uuid", "position"),)


class PersonalityRevision(db.Model):
    """One saved state of a personality's text, appended on every content save
    that actually changed something. Rows are never updated or deleted except
    by cascade when the personality itself is deleted — restoring an old
    revision appends a new one rather than rewinding. Full snapshots, not
    deltas: the texts are small and there is no chain to corrupt. Order by
    `id`, not `created_at` — two saves can share a clock tick."""

    __tablename__ = "personality_revision"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    personality_uuid: Mapped[UUID] = mapped_column()  # owner; plain col, no FK
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (Index("personality_revision_of", "personality_uuid", "id"),)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v`
Expected: PASS (1 test). `db.init_db` creates the three tables on first run.

- [ ] **Step 5: Commit**

```bash
git add source/db/models.py source/db/test_personality_tree.py
git commit -m "feat: personality, folder and revision tables"
```

---

## Task 2: `db/personality.py` — tree load, validate, save, version

**Files:**
- Create: `db/personality.py`
- Modify: `db/__init__.py` (one import line)
- Test: `db/test_personality_tree.py` (append)

**Interfaces:**
- Consumes: `Personality`, `PersonalityFolder`, `PersonalityRevision` from Task 1.
- Produces:
  - `PersonalityTreeError(ValueError)`, `PersonalityTreeConflict(Exception)`
  - `personality_tree_version() -> str`
  - `personality_load_tree() -> dict` — `{"folders": [...], "personalities": [...], "version": str}`; folder dicts are `{id, name, description, parentId, created_at, updated_at}`; personality dicts are `{uuid, name, folderId, revisionCount, created_at, updated_at}`
  - `validate_personality_tree(folders: list, personalities: list) -> None`
  - `personality_save_tree(folders: list, personalities: list, *, base_version: str | None = None) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `db/test_personality_tree.py`:

```python
@pytest.fixture
def clean_tree(app_ctx):
    """Empty the personality tables around a test (the DB is shared)."""
    def wipe():
        db.db.session.execute(sa.delete(PersonalityRevision))
        db.db.session.execute(sa.delete(Personality))
        db.db.session.execute(sa.delete(PersonalityFolder))
        db.db.session.commit()
    wipe()
    try:
        yield
    finally:
        wipe()


def test_load_tree_shape_and_version(clean_tree):
    out = db.personality_load_tree()
    assert out["folders"] == [] and out["personalities"] == []
    assert isinstance(out["version"], str) and out["version"]


def test_validate_rejects_dangling_folder_parent(clean_tree):
    with pytest.raises(db.PersonalityTreeError, match="missing parent"):
        db.validate_personality_tree(
            [{"id": str(uuid4()), "name": "a", "parentId": str(uuid4())}], [])


def test_validate_rejects_folder_cycle(clean_tree):
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.PersonalityTreeError, match="cycle"):
        db.validate_personality_tree(
            [{"id": a, "name": "a", "parentId": b},
             {"id": b, "name": "b", "parentId": a}], [])


def test_validate_rejects_uuid_collision(clean_tree):
    shared = str(uuid4())
    with pytest.raises(db.PersonalityTreeError, match="collides"):
        db.validate_personality_tree(
            [{"id": shared, "name": "f", "parentId": None}],
            [{"uuid": shared, "name": "p", "folderId": None}])


def test_save_tree_updates_placement_and_name(clean_tree):
    f = db.personality_create_folder("Folder", None)
    p = db.personality_create("Original", None)
    db.personality_save_tree(
        [{"id": f["id"], "name": "Renamed folder", "description": "d", "parentId": None}],
        [{"uuid": p["uuid"], "name": "Renamed", "folderId": f["id"]}])
    tree = db.personality_load_tree()
    assert tree["folders"][0]["name"] == "Renamed folder"
    assert tree["personalities"][0]["name"] == "Renamed"
    assert tree["personalities"][0]["folderId"] == f["id"]


def test_save_tree_refuses_to_omit_an_existing_row(clean_tree):
    p = db.personality_create("Keep me", None)
    with pytest.raises(db.PersonalityTreeError, match="omitted"):
        db.personality_save_tree([], [])
    # nothing was touched
    assert [x["uuid"] for x in db.personality_load_tree()["personalities"]] == [p["uuid"]]


def test_save_tree_refuses_an_unknown_row(clean_tree):
    with pytest.raises(db.PersonalityTreeError, match="unknown"):
        db.personality_save_tree(
            [], [{"uuid": str(uuid4()), "name": "ghost", "folderId": None}])


def test_save_tree_stale_version_conflicts(clean_tree):
    p = db.personality_create("A", None)
    stale = db.personality_tree_version()
    db.personality_create("B", None)          # someone else changed the tree
    with pytest.raises(db.PersonalityTreeConflict):
        db.personality_save_tree(
            [], [{"uuid": p["uuid"], "name": "A", "folderId": None}],
            base_version=stale)


def test_version_ignores_content(clean_tree):
    p = db.personality_create("A", None)
    before = db.personality_tree_version()
    db.personality_update_content(p["uuid"], "some text")
    assert db.personality_tree_version() == before
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'personality_load_tree'`.

- [ ] **Step 3: Create `db/personality.py`**

```python
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
```

- [ ] **Step 4: Re-export from the `db` facade**

In `db/__init__.py`, add next to the other tree re-exports (after the `db.prompt` line):

```python
from db.personality import *  # noqa: F401,F403  re-export personality tree + revision ops
```

- [ ] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v`
Expected: the validation, version and save tests PASS; the four tests calling `personality_create`, `personality_create_folder` or `personality_update_content` still FAIL with `AttributeError` — those arrive in Task 3.

- [ ] **Step 6: Commit**

```bash
git add source/db/personality.py source/db/__init__.py source/db/test_personality_tree.py
git commit -m "feat: personality tree load/validate/save (no create or delete in the PUT)"
```

---

## Task 3: `db/personality.py` — create, delete, content, revisions

**Files:**
- Modify: `db/personality.py` (append)
- Test: `db/test_personality_tree.py` (append)

**Interfaces:**
- Consumes: everything from Task 2.
- Produces:
  - `personality_create(name: str, folder_uuid: UUID | None) -> dict` (a `_personality_dict`)
  - `personality_create_folder(name: str, parent_uuid: UUID | None) -> dict` (a `_folder_dict`)
  - `personality_delete(personality_uuid: UUID) -> bool`
  - `personality_delete_folder(folder_uuid: UUID) -> bool`
  - `personality_get(personality_uuid: UUID) -> dict | None` — the `_personality_dict` fields plus `content`
  - `personality_update_content(personality_uuid: UUID, content: str) -> dict | None` — `{"changed": bool, "revision": dict | None}`
  - `personality_revisions(personality_uuid: UUID) -> list[dict] | None` — newest first, each `{uuid, created_at, bytes, lines, preview, current}`
  - `personality_revision_diff(personality_uuid: UUID, revision_uuid: UUID) -> dict` — `{ok, revision, lines}` or `{ok: False, error}`
  - `personality_restore_revision(personality_uuid: UUID, revision_uuid: UUID) -> dict` — `{ok, changed, content, revision}` or `{ok: False, error}`

- [ ] **Step 1: Write the failing tests**

Append to `db/test_personality_tree.py`:

```python
def test_create_places_at_end_of_folder(clean_tree):
    f = db.personality_create_folder("F", None)
    a = db.personality_create("A", f["id"])
    b = db.personality_create("B", f["id"])
    names = [p["name"] for p in db.personality_load_tree()["personalities"]]
    assert names == ["A", "B"]
    assert a["folderId"] == f["id"] and b["folderId"] == f["id"]
    assert a["revisionCount"] == 0


def test_first_save_creates_revision_one(clean_tree):
    p = db.personality_create("A", None)
    out = db.personality_update_content(p["uuid"], "Dry wit, no filler.")
    assert out["changed"] is True
    revs = db.personality_revisions(p["uuid"])
    assert len(revs) == 1
    assert revs[0]["current"] is True
    assert revs[0]["preview"] == "Dry wit, no filler."
    assert db.personality_get(p["uuid"])["content"] == "Dry wit, no filler."


def test_unchanged_save_appends_nothing(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "same")
    out = db.personality_update_content(p["uuid"], "same")
    assert out["changed"] is False and out["revision"] is None
    assert len(db.personality_revisions(p["uuid"])) == 1


def test_newest_revision_always_mirrors_content(clean_tree):
    p = db.personality_create("A", None)
    for text in ("one", "two", "three"):
        db.personality_update_content(p["uuid"], text)
    revs = db.personality_revisions(p["uuid"])
    assert len(revs) == 3 and revs[0]["current"] is True
    assert db.personality_get(p["uuid"])["content"] == "three"
    assert db.personality_revision_diff(p["uuid"], revs[0]["uuid"])["lines"] == []


def test_restore_appends_rather_than_rewinds(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "one")
    db.personality_update_content(p["uuid"], "two")
    oldest = db.personality_revisions(p["uuid"])[-1]
    out = db.personality_restore_revision(p["uuid"], UUID(oldest["uuid"]))
    assert out["ok"] is True and out["changed"] is True and out["content"] == "one"
    revs = db.personality_revisions(p["uuid"])
    assert len(revs) == 3               # nothing was removed
    assert revs[0]["current"] is True
    assert db.personality_get(p["uuid"])["content"] == "one"


def test_restore_of_a_foreign_revision_fails(clean_tree):
    a = db.personality_create("A", None)
    b = db.personality_create("B", None)
    db.personality_update_content(a["uuid"], "a text")
    rev = db.personality_revisions(a["uuid"])[0]
    out = db.personality_restore_revision(b["uuid"], UUID(rev["uuid"]))
    assert out["ok"] is False and out["error"] == "revision not found"


def test_diff_reports_the_change(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "old line")
    db.personality_update_content(p["uuid"], "new line")
    oldest = db.personality_revisions(p["uuid"])[-1]
    out = db.personality_revision_diff(p["uuid"], UUID(oldest["uuid"]))
    assert out["ok"] is True
    assert any(line.startswith("-old line") for line in out["lines"])
    assert any(line.startswith("+new line") for line in out["lines"])


def test_delete_personality_cascades_revisions(clean_tree):
    p = db.personality_create("A", None)
    db.personality_update_content(p["uuid"], "text")
    assert db.personality_delete(UUID(p["uuid"])) is True
    assert db.personality_get(UUID(p["uuid"])) is None
    assert db.db.session.execute(
        sa.select(sa.func.count(PersonalityRevision.id))
        .where(PersonalityRevision.personality_uuid == UUID(p["uuid"]))
    ).scalar_one() == 0


def test_delete_folder_cascades_the_subtree(clean_tree):
    outer = db.personality_create_folder("Outer", None)
    inner = db.personality_create_folder("Inner", outer["id"])
    p = db.personality_create("A", inner["id"])
    db.personality_update_content(p["uuid"], "text")
    assert db.personality_delete_folder(UUID(outer["id"])) is True
    tree = db.personality_load_tree()
    assert tree["folders"] == [] and tree["personalities"] == []
    assert db.db.session.execute(
        sa.select(sa.func.count(PersonalityRevision.id))).scalar_one() == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'personality_create'`.

- [ ] **Step 3: Append the implementation to `db/personality.py`**

Add `import difflib` to the imports at the top of the file, then append:

```python
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
    return {"ok": True, "revision": _revision_dict(
        rev, current=bool(_revision_rows(personality_uuid))
        and _revision_rows(personality_uuid)[0].uuid == rev.uuid),
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
```

- [ ] **Step 4: Run the whole file**

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v`
Expected: PASS — every test in the file.

- [ ] **Step 5: Simplify the `current` flag in `personality_revision_diff`**

The expression above queries the rows twice. Replace the `return` in
`personality_revision_diff` with:

```python
    rows = _revision_rows(personality_uuid)
    is_current = bool(rows) and rows[0].uuid == rev.uuid
    return {"ok": True, "revision": _revision_dict(rev, current=is_current),
            "lines": lines}
```

Run: `./venv/bin/python -m pytest db/test_personality_tree.py -v` — still PASS.

- [ ] **Step 6: Commit**

```bash
git add source/db/personality.py source/db/test_personality_tree.py
git commit -m "feat: personality create/delete + append-only revision history"
```

---

## Task 4: `webapp/personality_api.py` — tree, create, delete

**Files:**
- Create: `webapp/personality_api.py`
- Modify: `webapp/__init__.py`
- Test: `webapp/test_personality_api.py` (create)

**Interfaces:**
- Consumes: the `db.personality_*` functions from Tasks 2-3.
- Produces: `GET/PUT /personality/api/tree`, `POST /personality/api/folders`, `POST /personality/api/personalities`, `DELETE /personality/api/folders/<uuid>`, `DELETE /personality/api/personalities/<uuid>`. Every mutating response carries `{"ok": true, "version": <token>}`.

- [ ] **Step 1: Write the failing tests**

Create `webapp/test_personality_api.py`:

```python
"""Tests for webapp/personality_api.py.

Uses the live local Postgres (rainbox_claude via conftest). HTTP goes through
the real app; each test creates its rows through the public endpoints and
deletes them the same way, so nothing leaks between runs.
"""
from webapp.core import app


def _client():
    return app.test_client()


def _create_personality(client, name="ApiTest"):
    resp = client.post("/personality/api/personalities",
                       json={"name": name, "folderId": None})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


def _delete_personality(client, uuid):
    assert client.delete(f"/personality/api/personalities/{uuid}").status_code == 200


def test_tree_get_returns_shape():
    out = _client().get("/personality/api/tree").get_json()
    assert isinstance(out["folders"], list)
    assert isinstance(out["personalities"], list)
    assert out["version"]


def test_create_returns_201_and_a_usable_version():
    c = _client()
    made = _create_personality(c)
    try:
        assert made["personality"]["name"] == "ApiTest"
        assert made["personality"]["revisionCount"] == 0
        # the version handed back by the POST must satisfy the very next PUT
        tree = c.get("/personality/api/tree").get_json()
        resp = c.put("/personality/api/tree", json={
            "folders": tree["folders"], "personalities": tree["personalities"],
            "version": made["version"]})
        assert resp.status_code == 200, resp.get_data(as_text=True)
    finally:
        _delete_personality(c, made["personality"]["uuid"])


def test_tree_put_requires_version():
    resp = _client().put("/personality/api/tree",
                         json={"folders": [], "personalities": []})
    assert resp.status_code == 400
    assert "version" in resp.get_json()["error"]


def test_tree_put_rejects_an_omitted_row():
    c = _client()
    made = _create_personality(c, "OmitMe")
    try:
        tree = c.get("/personality/api/tree").get_json()
        keep = [p for p in tree["personalities"]
                if p["uuid"] != made["personality"]["uuid"]]
        resp = c.put("/personality/api/tree", json={
            "folders": tree["folders"], "personalities": keep,
            "version": tree["version"]})
        assert resp.status_code == 400
        assert "omitted" in resp.get_json()["error"]
        # and the row is still there
        after = c.get("/personality/api/tree").get_json()["personalities"]
        assert any(p["uuid"] == made["personality"]["uuid"] for p in after)
    finally:
        _delete_personality(c, made["personality"]["uuid"])


def test_tree_put_stale_version_is_409():
    c = _client()
    tree = c.get("/personality/api/tree").get_json()
    made = _create_personality(c, "Stale")       # invalidates the token above
    try:
        resp = c.put("/personality/api/tree", json={
            "folders": tree["folders"], "personalities": tree["personalities"],
            "version": tree["version"]})
        assert resp.status_code == 409
        assert resp.get_json()["version"]        # the fresh token to retry with
    finally:
        _delete_personality(c, made["personality"]["uuid"])


def test_delete_folder_cascades_and_returns_version():
    c = _client()
    folder = c.post("/personality/api/folders",
                    json={"name": "DelFolder", "parentId": None}).get_json()
    inside = c.post("/personality/api/personalities",
                    json={"name": "Inside", "folderId": folder["folder"]["id"]}
                    ).get_json()["personality"]
    resp = c.delete(f"/personality/api/folders/{folder['folder']['id']}")
    assert resp.status_code == 200 and resp.get_json()["version"]
    tree = c.get("/personality/api/tree").get_json()
    assert not any(f["id"] == folder["folder"]["id"] for f in tree["folders"])
    assert not any(p["uuid"] == inside["uuid"] for p in tree["personalities"])


def test_delete_unknown_is_404():
    resp = _client().delete(
        "/personality/api/personalities/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_bad_uuid_is_400():
    assert _client().delete("/personality/api/personalities/not-a-uuid").status_code == 400
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest webapp/test_personality_api.py -v`
Expected: FAIL — every request 404s; the route doesn't exist.

- [ ] **Step 3: Create `webapp/personality_api.py`**

```python
"""JSON API backing the /personality page.

Save shape per docs/ui-tree-persistence.md: the tree PUT only updates rows
that already exist (a payload that omits or invents one is a 400), and
creation/deletion are their own endpoints. Every mutating response carries the
new tree `version`, so the client never holds a stale token. Personality text
is read and written per-personality, and its history lives behind the
/revisions routes.
"""
from uuid import UUID

from flask import Response, jsonify, request

import db

from .core import app


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


@app.route("/personality/api/tree", methods=["GET", "PUT"])
def personality_tree() -> tuple[Response, int] | Response:
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False,
                            "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.personality_save_tree(data.get("folders", []),
                                     data.get("personalities", []),
                                     base_version=version)
        except db.PersonalityTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.personality_tree_version()}), 409
        except db.PersonalityTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.personality_tree_version()})
    return jsonify(db.personality_load_tree())


@app.route("/personality/api/folders", methods=["POST"])
def personality_create_folder_route() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "folder name required"}), 400
    parent_raw = data.get("parentId")
    parent_uuid = None
    if parent_raw:
        parent_uuid = _parse_uuid(parent_raw)
        if parent_uuid is None:
            return jsonify({"ok": False, "error": "bad parentId"}), 400
    folder = db.personality_create_folder(name, parent_uuid)
    return jsonify({"ok": True, "folder": folder,
                    "version": db.personality_tree_version()}), 201


@app.route("/personality/api/personalities", methods=["POST"])
def personality_create_route() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "personality name required"}), 400
    folder_raw = data.get("folderId")
    folder_uuid = None
    if folder_raw:
        folder_uuid = _parse_uuid(folder_raw)
        if folder_uuid is None:
            return jsonify({"ok": False, "error": "bad folderId"}), 400
    made = db.personality_create(name, folder_uuid)
    return jsonify({"ok": True, "personality": made,
                    "version": db.personality_tree_version()}), 201


@app.route("/personality/api/folders/<folder_uuid>", methods=["DELETE"])
def personality_delete_folder_route(folder_uuid: str) -> tuple[Response, int] | Response:
    fu = _parse_uuid(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.personality_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True, "version": db.personality_tree_version()})


@app.route("/personality/api/personalities/<personality_uuid>", methods=["DELETE"])
def personality_delete_route(personality_uuid: str) -> tuple[Response, int] | Response:
    pu = _parse_uuid(personality_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.personality_delete(pu):
        return jsonify({"ok": False, "error": "personality not found"}), 404
    return jsonify({"ok": True, "version": db.personality_tree_version()})
```

- [ ] **Step 4: Register the module**

In `webapp/__init__.py`, add after the `profile_api` import line:

```python
from . import personality_api  # noqa: F401,E402
```

- [ ] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_personality_api.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Commit**

```bash
git add source/webapp/personality_api.py source/webapp/__init__.py source/webapp/test_personality_api.py
git commit -m "feat: personality tree + create/delete endpoints"
```

---

## Task 5: `webapp/personality_api.py` — content and revision endpoints

**Files:**
- Modify: `webapp/personality_api.py` (append)
- Test: `webapp/test_personality_api.py` (append)

**Interfaces:**
- Produces: `GET/PUT /personality/api/personalities/<uuid>`, `GET .../revisions`, `GET .../revisions/<rev>/diff`, `POST .../revisions/<rev>/restore`.

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_personality_api.py`:

```python
def test_content_put_appends_a_revision():
    c = _client()
    made = _create_personality(c, "ContentTest")
    uuid = made["personality"]["uuid"]
    try:
        resp = c.put(f"/personality/api/personalities/{uuid}",
                     json={"content": "Warm, concrete, allergic to filler."})
        assert resp.status_code == 200
        assert resp.get_json()["changed"] is True
        detail = c.get(f"/personality/api/personalities/{uuid}").get_json()
        assert detail["content"] == "Warm, concrete, allergic to filler."
        assert detail["revisionCount"] == 1
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        assert len(revs["revisions"]) == 1 and revs["revisions"][0]["current"] is True
    finally:
        _delete_personality(c, uuid)


def test_unchanged_content_put_reports_no_change():
    c = _client()
    made = _create_personality(c, "NoopTest")
    uuid = made["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "same"})
        resp = c.put(f"/personality/api/personalities/{uuid}", json={"content": "same"})
        assert resp.get_json()["changed"] is False
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        assert len(revs["revisions"]) == 1
    finally:
        _delete_personality(c, uuid)


def test_restore_appends_and_returns_the_old_text():
    c = _client()
    made = _create_personality(c, "RestoreTest")
    uuid = made["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "first"})
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "second"})
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        oldest = revs["revisions"][-1]["uuid"]
        resp = c.post(
            f"/personality/api/personalities/{uuid}/revisions/{oldest}/restore")
        assert resp.status_code == 200
        assert resp.get_json()["content"] == "first"
        after = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        assert len(after["revisions"]) == 3   # appended, not rewound
    finally:
        _delete_personality(c, uuid)


def test_diff_lists_the_change():
    c = _client()
    made = _create_personality(c, "DiffTest")
    uuid = made["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "before"})
        c.put(f"/personality/api/personalities/{uuid}", json={"content": "after"})
        revs = c.get(f"/personality/api/personalities/{uuid}/revisions").get_json()
        oldest = revs["revisions"][-1]["uuid"]
        out = c.get(
            f"/personality/api/personalities/{uuid}/revisions/{oldest}/diff").get_json()
        assert out["ok"] is True
        assert any(ln.startswith("-before") for ln in out["lines"])
        assert any(ln.startswith("+after") for ln in out["lines"])
    finally:
        _delete_personality(c, uuid)


def test_foreign_revision_diff_is_404():
    c = _client()
    a = _create_personality(c, "OwnerA")
    b = _create_personality(c, "OwnerB")
    ua, ub = a["personality"]["uuid"], b["personality"]["uuid"]
    try:
        c.put(f"/personality/api/personalities/{ua}", json={"content": "a text"})
        rev = c.get(f"/personality/api/personalities/{ua}/revisions"
                    ).get_json()["revisions"][0]["uuid"]
        resp = c.get(f"/personality/api/personalities/{ub}/revisions/{rev}/diff")
        assert resp.status_code == 404
    finally:
        _delete_personality(c, ua)
        _delete_personality(c, ub)


def test_content_put_requires_a_string():
    c = _client()
    made = _create_personality(c, "TypeTest")
    uuid = made["personality"]["uuid"]
    try:
        assert c.put(f"/personality/api/personalities/{uuid}",
                     json={"content": 42}).status_code == 400
    finally:
        _delete_personality(c, uuid)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest webapp/test_personality_api.py -v`
Expected: the six new tests FAIL (404 on the content and revision routes).

- [ ] **Step 3: Append the routes to `webapp/personality_api.py`**

```python
@app.route("/personality/api/personalities/<personality_uuid>",
           methods=["GET", "PUT"])
def personality_detail(personality_uuid: str) -> tuple[Response, int] | Response:
    """GET: one personality incl. its current text, for the editor pane.
    PUT {content}: the editor's explicit Save — appends a revision when the
    text actually changed, and reports `changed: false` when it didn't."""
    pu = _parse_uuid(personality_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            return jsonify({"ok": False, "error":
                            "request body must be a JSON object with string "
                            "'content'"}), 400
        out = db.personality_update_content(pu, data["content"])
        if out is None:
            return jsonify({"ok": False, "error": "personality not found"}), 404
        return jsonify({"ok": True, **out})
    detail = db.personality_get(pu)
    if detail is None:
        return jsonify({"ok": False, "error": "personality not found"}), 404
    return jsonify({"ok": True, **detail})


@app.route("/personality/api/personalities/<personality_uuid>/revisions")
def personality_revisions_route(personality_uuid: str) -> tuple[Response, int] | Response:
    """The history, newest first — one row per saved state of the text."""
    pu = _parse_uuid(personality_uuid)
    if pu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    revisions = db.personality_revisions(pu)
    if revisions is None:
        return jsonify({"ok": False, "error": "personality not found"}), 404
    return jsonify({"ok": True, "revisions": revisions})


@app.route("/personality/api/personalities/<personality_uuid>"
           "/revisions/<revision_uuid>/diff")
def personality_revision_diff_route(
        personality_uuid: str, revision_uuid: str) -> tuple[Response, int] | Response:
    """Unified diff of that revision's text → the current text."""
    pu, ru = _parse_uuid(personality_uuid), _parse_uuid(revision_uuid)
    if pu is None or ru is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    result = db.personality_revision_diff(pu, ru)
    if not result.get("ok"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/personality/api/personalities/<personality_uuid>"
           "/revisions/<revision_uuid>/restore", methods=["POST"])
def personality_restore_route(
        personality_uuid: str, revision_uuid: str) -> tuple[Response, int] | Response:
    """Bring an old revision back by appending a new one holding its text."""
    pu, ru = _parse_uuid(personality_uuid), _parse_uuid(revision_uuid)
    if pu is None or ru is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    result = db.personality_restore_revision(pu, ru)
    if not result.get("ok"):
        return jsonify(result), 404
    return jsonify(result)
```

- [ ] **Step 4: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_personality_api.py -v`
Expected: PASS (14 tests).

- [ ] **Step 5: Commit**

```bash
git add source/webapp/personality_api.py source/webapp/test_personality_api.py
git commit -m "feat: personality content + revision endpoints"
```

---

## Task 6: The page shell, CSS and nav entry

**Files:**
- Create: `webapp/personality_views.py`
- Modify: `webapp/__init__.py`, `webapp/core.py`
- Test: `webapp/test_personality_views.py` (create)

**Interfaces:**
- Produces: `GET /personality` (endpoint name `personality_page`) rendering the shell that `static/personality.js` drives. Element ids the JS relies on: `personality-tree`, `personality-tree-root`, `personality-root-drop`, `personality-all`, `personality-main`, `personality-node-rename`, `personality-folder-desc`, `personality-rows`, `personality-editor`, `personality-content`, `personality-history`, `personality-dates`, `personality-revcount`, `personality-edit-btn`, `personality-save-btn`, `personality-cancel-btn`, `personality-history-btn`, and the modals `personality-folder-modal`, `personality-new-modal`, `personality-rename-modal`, `personality-desc-modal`, `personality-delete-modal`, `personality-restore-modal`, plus `personality-toast`.

- [ ] **Step 1: Write the failing test**

Create `webapp/test_personality_views.py`:

```python
"""Tests for webapp/personality_views.py + static/personality.js.

The page is frontend-only: the route renders the HTML shell (+ inline CSS) and
all interactivity lives in static/personality.js. `_body()` returns the page
concatenated with the served JS so marker assertions cover both.
"""
from webapp.core import app


def _body() -> str:
    client = app.test_client()
    page = client.get("/personality").get_data(as_text=True)
    js = client.get("/static/personality.js")
    assert js.status_code == 200  # the shell references it; it must serve
    return page + js.get_data(as_text=True)


def test_page_renders_with_nav():
    body = app.test_client().get("/personality").get_data(as_text=True)
    assert 'class="personality-split"' in body
    assert "pp-nav" in body
    assert "/static/personality.js?v=" in body


def test_nav_has_personality_link():
    body = app.test_client().get("/personality").get_data(as_text=True)
    assert ">Personality<" in body
    assert "pp-active" in body


def test_page_has_editor_and_history_markers():
    body = app.test_client().get("/personality").get_data(as_text=True)
    for marker in ['id="personality-content"', 'id="personality-history"',
                   'id="personality-revcount"', 'id="personality-history-btn"',
                   'id="personality-new-modal"', 'id="personality-delete-modal"',
                   'id="personality-restore-modal"']:
        assert marker in body, f"missing page marker: {marker}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py -v`
Expected: FAIL — `GET /personality` 404s.

- [ ] **Step 3: Create `webapp/personality_views.py`**

Copy `webapp/prompt_views.py` and adapt. Start from the real file so the CSS
is byte-identical where it should be — §8 of `docs/ui-left-panel-tree.md`
exists because `/git` hand-wrote its CSS and a dozen divergences piled up:

```bash
sed -e 's/prompt/personality/g' -e 's/Prompt/Personality/g' \
    -e 's/PROMPT/PERSONALITY/g' -e 's/personalitys/personalities/g' \
    webapp/prompt_views.py > webapp/personality_views.py
```

Then make these edits by hand in `webapp/personality_views.py`:

1. **Module docstring** — replace it wholesale with:

```python
"""The /personality page (HTML shell + CSS; the page logic lives in
static/personality.js).

Manages the assistant's personalities as a folder tree of free-text bodies.
A personality's uuid is stable for its whole life (deep-linkable via
/personality?id=<uuid>) and every save that changes the text appends a
revision, so the History view can diff any earlier state against the current
one and restore it — by appending, never by rewinding. Persistence follows
docs/ui-tree-persistence.md: the tree PUT only updates existing rows, while
creation and deletion are their own endpoints (webapp/personality_api.py →
db/personality.py). Text is read-only until an explicit Edit → Save.
Mirrors the /prompt page; desktop-first.
"""
```

2. **Toolbar** — replace the `personality-toolbar` div's contents (the sed left
   `/prompt`'s Clone/New chat/Diff buttons) with:

```html
      <div class="personality-toolbar">
        <button id="personality-edit-btn" onclick="personalityStartEdit()">Edit</button>
        <button id="personality-save-btn" onclick="personalitySaveEdit()" hidden>Save</button>
        <button id="personality-cancel-btn" onclick="personalityCancelEdit()" hidden>Cancel</button>
        <button id="personality-history-btn" onclick="personalityToggleHistory()">History</button>
      </div>
```

3. **Meta line** — replace the `personality-meta` div (the sed left a
   "based on" span, which has no meaning here) with:

```html
      <div class="personality-meta">
        <span id="personality-dates" class="muted"></span>
        <span id="personality-revcount" class="muted"></span>
      </div>
```

4. **History pane** — replace the `<div id="personality-diff" hidden></div>`
   line with:

```html
      <div id="personality-history" hidden>
        <table class="personality-table">
          <thead><tr><th>Saved</th><th>Size</th><th>First line</th><th></th></tr></thead>
          <tbody id="personality-history-rows"></tbody>
        </table>
        <div id="personality-history-diff" hidden></div>
      </div>
```

5. **Textarea placeholder** — change it to
   `placeholder="Describe who the assistant is&hellip;"`.

6. **Restore modal** — add before the `<div class="personality-toast"…>` line:

```html
<div class="ui-modal" id="personality-restore-modal" hidden>
  <h3>Restore this version?</h3>
  <p id="personality-restore-msg"></p>
  <p class="muted">This appends a new version holding that text. Nothing in
     the history is deleted, so you can undo it the same way.</p>
  <div class="modal-actions">
    <button type="button" class="btn-primary" id="personality-restore-confirm">Restore</button>
    <button type="button" class="btn-cancel" onclick="personalityCloseRestoreModal()">Cancel</button>
  </div>
</div>
```

7. **CSS** — the sed already renamed every selector. Add these two rules next
   to the existing `#personality-history-diff` colors (the sed renamed
   `#prompt-diff` to `#personality-diff`; rename those rules to
   `#personality-history-diff` and keep their values):

```css
  #personality-history[hidden]{display:none}
  #personality-history-diff{margin-top:10px;max-height:50vh}
```

8. **CodeMirror `<script>` tags** — keep all four; drop nothing.

- [ ] **Step 4: Register the route module**

In `webapp/__init__.py`, add after the `personality_api` import:

```python
from . import personality_views  # noqa: F401,E402
```

(Both lines can sit together; import order between view modules is irrelevant.)

- [ ] **Step 5: Add the nav entry**

In `webapp/core.py`, replace the Assistant dropdown block with:

```html
    <details class="pp-dd {{ 'pp-active' if request.endpoint in ('assistant_page', 'assistant_overview_page', 'second_opinion_page', 'personality_page') }}">
      <summary>Assistant &#9662;</summary>
      <div class="pp-dd-menu">
        <a href="{{ url_for('assistant_overview_page') }}" class="{{ 'pp-active' if request.endpoint in ('assistant_page', 'assistant_overview_page') }}">Runs</a>
        <a href="{{ url_for('second_opinion_page') }}" class="{{ 'pp-active' if request.endpoint == 'second_opinion_page' }}">Second opinion</a>
        <a href="{{ url_for('personality_page') }}" class="{{ 'pp-active' if request.endpoint == 'personality_page' }}">Personality</a>
      </div>
    </details>
```

- [ ] **Step 6: Create a placeholder JS file so the shell's `<script>` resolves**

```bash
printf '// /personality page logic — filled in by Tasks 7 and 8.\n' > static/personality.js
```

- [ ] **Step 7: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Commit**

```bash
git add source/webapp/personality_views.py source/webapp/__init__.py source/webapp/core.py source/static/personality.js source/webapp/test_personality_views.py
git commit -m "feat: /personality page shell, CSS and nav entry"
```

---

## Task 7: `static/personality.js` — the tree

**Files:**
- Create: `static/personality.js` (replacing the placeholder)
- Test: `webapp/test_personality_views.py` (append)

**Interfaces:**
- Consumes: the endpoints from Tasks 4-5 and the element ids from Task 6.
- Produces: `personalityLoadTree`, `personalityRenderTree`, `personalityItemNode`, `personalitySave`, `personalitySavePush`, `personalityAddPersonalityConfirm`, `personalityAddFolderConfirm`, `personalityDeleteItem`, `personalityDeleteFolderById`, plus the selection/drag-drop/rename machinery inherited from `prompt.js`.

**Why this task is a port, not a transcription:** `static/prompt.js` is 1209
lines and this page needs ~1100 of them unchanged (render, selection,
deep-link, drag-drop, kebabs, rename and description modals, toast, dirty
guard). Copying the real file and applying the listed edits keeps the source
of truth on disk rather than in this document, where a transcription would
silently drift. Every line that *differs* is written out in full below.

- [ ] **Step 1: Write the failing marker test**

Append to `webapp/test_personality_views.py`:

```python
def test_js_has_core_markers():
    b = _body()
    for marker in ["personalityLoadTree", "personalityRenderTree",
                   "personalityItemNode", "personalitySavePush",
                   "personalityAddPersonalityConfirm", "personalityDeleteItem",
                   "/personality/api/tree"]:
        assert marker in b, f"missing JS marker: {marker}"


def test_tree_save_declares_no_deletes():
    """Per docs/ui-tree-persistence.md the tree PUT cannot delete, so the
    client must not carry a deletes counter — deletion goes to DELETE."""
    b = _body()
    assert "deletes" not in b
    assert "method: 'DELETE'" in b
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py::test_js_has_core_markers -v`
Expected: FAIL — the placeholder JS has none of those markers.

- [ ] **Step 3: Port the file mechanically**

```bash
sed -e 's/prompt/personality/g' -e 's/Prompt/Personality/g' \
    -e 's/PROMPT/PERSONALITY/g' -e 's/personalitys/personalities/g' \
    static/prompt.js > static/personality.js
```

Check the rename left nothing odd:

```bash
grep -n "personalitie\?s\?[A-Z]" static/personality.js | head -30
grep -c "prompt" static/personality.js   # expect 0
```

- [ ] **Step 4: Fix the header comment**

Replace the first comment block of `static/personality.js` with:

```javascript
// /personality page logic (vanilla JS, no framework). The HTML shell + CSS
// live in webapp/personality_views.py; this file is served at
// /static/personality.js with an mtime cache-buster. State hydrates from
// GET /personality/api/tree and structural edits save via debounced PUTs.
// Per docs/ui-tree-persistence.md the PUT can only update rows that already
// exist: creating and deleting go to their own endpoints, so no payload of
// ours can destroy a personality or its history. Ported from static/prompt.js.
```

- [ ] **Step 5: Delete the parts `/prompt` has and this page doesn't**

Remove these functions entirely (the sed renamed them; they have no meaning
without clone lineage): `personalityBasedOnLabel`, `personalityCloneUuid`,
`personalityNewChat`, `personalityToggleDiff`, `personalityDiffAgainstChanged`,
`personalityLoadDiff`, `personalityApplyDiffVisibility`, and the
`personalityDiffOpen` variable.

Then remove their remaining references:

- In `personalityRenderContents`, drop the "Based on" `<td>` from both the
  header and the row template; the folder table's columns become
  Name / Revisions / Updated / Open, with the Revisions cell rendering
  `p.revisionCount`.
- In `personalityRenderEditor`, delete the block that fills
  `#personality-based-on` and the call to `personalityApplyDiffVisibility()`.
- In `personalityMakeKebab`, drop the "Clone" menu entry.

- [ ] **Step 6: Rewrite creation to use the POST endpoints**

Replace `personalityAddFolderConfirm` and `personalityAddPersonalityConfirm`
with:

```javascript
async function personalityAddFolderConfirm(){
  const input = document.getElementById('personality-folder-input');
  const name = (input.value || '').trim();
  if (!name) return;
  const parentId = personalityAddFolderAsSub ? personalitySelectedFolder : null;
  try {
    const resp = await fetch('/personality/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, parentId}),
    });
    const data = await resp.json();
    if (!resp.ok) { personalityToastMsg(data.error || 'could not create folder'); return; }
    personalityFolders.push(data.folder);
    personalityTreeVersion = data.version;
    personalityCloseFolderModal();
    personalityExpanded[data.folder.id] = true;
    personalitySelectFolder(data.folder.id);
  } catch (e) {
    personalityToastMsg('could not create folder');
  }
}

async function personalityAddPersonalityConfirm(){
  const input = document.getElementById('personality-new-input');
  const name = (input.value || '').trim();
  if (!name) return;
  try {
    const resp = await fetch('/personality/api/personalities', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, folderId: personalitySelectedFolder}),
    });
    const data = await resp.json();
    if (!resp.ok) { personalityToastMsg(data.error || 'could not create'); return; }
    personalityItems.push(data.personality);
    personalityTreeVersion = data.version;
    personalityCloseNewModal();
    personalitySelectItem(data.personality.uuid);
  } catch (e) {
    personalityToastMsg('could not create');
  }
}
```

- [ ] **Step 7: Rewrite deletion to use the DELETE endpoints**

Replace `personalityDeleteItem` and `personalityDeleteFolderById` with:

```javascript
async function personalityDeleteItem(uuid){
  try {
    const resp = await fetch('/personality/api/personalities/' + uuid,
                             {method: 'DELETE'});
    const data = await resp.json();
    if (!resp.ok) { personalityToastMsg(data.error || 'could not delete'); return; }
    personalityItems = personalityItems.filter(p => p.uuid !== uuid);
    personalityTreeVersion = data.version;
    if (personalitySelectedItem === uuid) personalitySelectedItem = null;
    personalityRenderTree();
    personalityRender();
    personalitySyncUrl();
    personalityToastMsg('deleted');
  } catch (e) {
    personalityToastMsg('could not delete');
  }
}

async function personalityDeleteFolderById(id){
  try {
    const resp = await fetch('/personality/api/folders/' + id, {method: 'DELETE'});
    const data = await resp.json();
    if (!resp.ok) { personalityToastMsg(data.error || 'could not delete'); return; }
    // The server cascaded the subtree; mirror that locally instead of re-fetching.
    const doomedFolders = new Set([id]);
    let grew = true;
    while (grew) {
      grew = false;
      personalityFolders.forEach(f => {
        if (f.parentId && doomedFolders.has(f.parentId) && !doomedFolders.has(f.id)) {
          doomedFolders.add(f.id); grew = true;
        }
      });
    }
    personalityItems = personalityItems.filter(p => !doomedFolders.has(p.folderId));
    personalityFolders = personalityFolders.filter(f => !doomedFolders.has(f.id));
    personalityTreeVersion = data.version;
    if (doomedFolders.has(personalitySelectedFolder)) personalitySelectedFolder = null;
    if (personalitySelectedItem && !personalityByUuid(personalitySelectedItem)) {
      personalitySelectedItem = null;
    }
    personalityRenderTree();
    personalityRender();
    personalitySyncUrl();
    personalityToastMsg('deleted');
  } catch (e) {
    personalityToastMsg('could not delete');
  }
}
```

- [ ] **Step 8: State the stakes in the delete modals**

Replace `personalityConfirmDeleteItem` with a version that counts revisions
(a personality with history requires typing its name, like a non-empty
folder):

```javascript
function personalityConfirmDeleteItem(uuid){
  const p = personalityByUuid(uuid);
  if (!p) return;
  const revisions = p.revisionCount || 0;
  personalityOpenDeleteModal({
    title: 'Delete personality',
    message: revisions
      ? `Delete "${p.name}" and its ${revisions} saved version` +
        `${revisions === 1 ? '' : 's'}? This cannot be undone.`
      : `Delete "${p.name}"?`,
    requireName: revisions ? p.name : null,
    onConfirm: () => personalityDeleteItem(uuid),
  });
}
```

- [ ] **Step 9: Drop the deletes counter from the save path**

Replace `personalitySavePush` with:

```javascript
async function personalitySavePush(){
  if (personalitySaveInFlight) { personalitySaveQueued = true; return; }
  personalitySaveInFlight = true;
  try {
    const resp = await fetch('/personality/api/tree', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        folders: personalityFolders.map(f => ({
          id: f.id, name: f.name, description: f.description || '',
          parentId: f.parentId || null})),
        personalities: personalityItems.map(p => ({
          uuid: p.uuid, name: p.name, folderId: p.folderId || null})),
        version: personalityTreeVersion,
      }),
    });
    const data = await resp.json();
    if (resp.status === 409) {
      personalityToastMsg('tree changed elsewhere — reloaded');
      await personalityLoadTree();
      return;
    }
    if (!resp.ok) {
      // A 400 here means our payload disagreed with the server about which
      // rows exist — re-hydrate rather than retry the same bad shape.
      personalityToastMsg(data.error || 'save failed — reloaded');
      await personalityLoadTree();
      return;
    }
    personalityTreeVersion = data.version;
  } catch (e) {
    personalityToastMsg('save failed — reloaded');
    await personalityLoadTree();
  } finally {
    personalitySaveInFlight = false;
    if (personalitySaveQueued) { personalitySaveQueued = false; personalitySave(); }
  }
}
```

Also delete the `let personalityPendingDeletes = 0;` declaration and every
remaining reference to it (`grep -n personalityPendingDeletes static/personality.js`
must come back empty).

- [ ] **Step 10: Run the marker tests**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py -v`
Expected: PASS (5 tests).

- [ ] **Step 11: Sanity-check the page loads clean**

```bash
./venv/bin/python -c "
from webapp.core import app
c = app.test_client()
assert c.get('/personality').status_code == 200
assert c.get('/static/personality.js').status_code == 200
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 12: Commit**

```bash
git add source/static/personality.js source/webapp/test_personality_views.py
git commit -m "feat: /personality tree client (create and delete via endpoints)"
```

---

## Task 8: `static/personality.js` — the editor and the History view

**Files:**
- Modify: `static/personality.js`
- Test: `webapp/test_personality_views.py` (append)

**Interfaces:**
- Consumes: `personalitySelectedItem`, `personalityEditorSet`, `personalityEditorValue`, `personalityToastMsg` from Task 7.
- Produces: `personalityToggleHistory`, `personalityLoadHistory`, `personalityShowRevisionDiff`, `personalityConfirmRestore`, `personalityCloseRestoreModal`, and an amended `personalitySaveEdit` / `personalityRenderEditor`.

- [ ] **Step 1: Write the failing marker test**

Append to `webapp/test_personality_views.py`:

```python
def test_history_view_markers():
    b = _body()
    for marker in ["function personalityToggleHistory",
                   "function personalityLoadHistory",
                   "function personalityShowRevisionDiff",
                   "function personalityConfirmRestore",
                   "/revisions", "/restore"]:
        assert marker in b, f"missing history marker: {marker}"


def test_content_editing_is_explicit():
    """Personality text is read-only until Edit is clicked; the edit resolves
    only via Save or Cancel, with the rest of the page behind the modal
    backdrop meanwhile — no autosave."""
    b = _body()
    assert 'id="personality-edit-btn"' in b
    assert 'id="personality-save-btn"' in b
    assert 'id="personality-cancel-btn"' in b
    assert "function personalityStartEdit" in b
    assert "function personalitySaveEdit" in b
    assert "function personalityCancelEdit" in b
    assert "#personality-editor.editing{position:relative;z-index:1600" in b
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py::test_history_view_markers -v`
Expected: FAIL — no history functions yet.

- [ ] **Step 3: Teach the editor about revision counts**

In `personalityRenderEditor`, after the dates are rendered, add:

```javascript
  const rev = document.getElementById('personality-revcount');
  const count = (p && p.revisionCount) || 0;
  rev.textContent = count === 1 ? '1 version' : count + ' versions';
```

And in `personalitySaveEdit`, after a successful PUT, replace the toast block
with (the response tells us whether anything was actually recorded):

```javascript
    const data = await resp.json();
    if (data.changed) {
      const row = personalityByUuid(uuid);
      if (row) row.revisionCount = (row.revisionCount || 0) + 1;
      personalityToastMsg('saved — version ' + ((row && row.revisionCount) || 1));
    } else {
      personalityToastMsg('no changes');
    }
    personalityExitEdit();
    personalityRenderEditor();
```

- [ ] **Step 4: Add the History view**

Append to `static/personality.js`, before the wiring section at the bottom:

```javascript
// ---- history view (append-only revisions; restore appends, never rewinds) ----
let personalityHistoryOpen = false;
let personalityHistoryRows = [];   // [{uuid, created_at, bytes, lines, preview, current}]
let personalityRestoreUuid = null; // revision awaiting confirmation

function personalityHistoryVisible(show){
  document.getElementById('personality-history').hidden = !show;
  document.getElementById('personality-editor')
          .querySelector('.CodeMirror').style.display = show ? 'none' : '';
}

async function personalityToggleHistory(){
  personalityHistoryOpen = !personalityHistoryOpen;
  document.getElementById('personality-history-btn').textContent =
    personalityHistoryOpen ? 'Editor' : 'History';
  personalityHistoryVisible(personalityHistoryOpen);
  if (personalityHistoryOpen) await personalityLoadHistory(personalitySelectedItem);
}

async function personalityLoadHistory(uuid){
  const box = document.getElementById('personality-history-rows');
  const diff = document.getElementById('personality-history-diff');
  diff.hidden = true;
  box.innerHTML = '';
  if (!uuid) return;
  let data;
  try {
    const resp = await fetch('/personality/api/personalities/' + uuid + '/revisions');
    data = await resp.json();
    if (!resp.ok) { personalityToastMsg(data.error || 'could not load history'); return; }
  } catch (e) {
    personalityToastMsg('could not load history');
    return;
  }
  personalityHistoryRows = data.revisions;
  if (!personalityHistoryRows.length) {
    box.innerHTML = '<tr><td colspan="4" class="muted">' +
      'No versions yet — the first save records one.</td></tr>';
    return;
  }
  personalityHistoryRows.forEach(r => {
    const tr = document.createElement('tr');
    const when = personalityShortDate(r.created_at) + (r.current ? ' (current)' : '');
    tr.innerHTML =
      '<td class="personality-name-cell">' + personalityEscapeHtml(when) + '</td>' +
      '<td>' + r.bytes + ' B</td>' +
      '<td>' + personalityEscapeHtml(r.preview) + '</td>' +
      '<td></td>';
    const actions = tr.lastElementChild;
    const diffBtn = document.createElement('button');
    diffBtn.textContent = 'Diff';
    diffBtn.onclick = () => personalityShowRevisionDiff(r.uuid);
    actions.appendChild(diffBtn);
    if (!r.current) {
      const restoreBtn = document.createElement('button');
      restoreBtn.textContent = 'Restore';
      restoreBtn.onclick = () => personalityConfirmRestore(r.uuid);
      actions.appendChild(restoreBtn);
    }
    box.appendChild(tr);
  });
}

async function personalityShowRevisionDiff(revisionUuid){
  const uuid = personalitySelectedItem;
  const box = document.getElementById('personality-history-diff');
  box.hidden = false;
  box.textContent = 'Loading…';
  let data;
  try {
    const resp = await fetch('/personality/api/personalities/' + uuid +
                             '/revisions/' + revisionUuid + '/diff');
    data = await resp.json();
    if (!resp.ok) { box.textContent = data.error || 'could not diff'; return; }
  } catch (e) {
    box.textContent = 'could not diff';
    return;
  }
  box.innerHTML = '';
  if (!data.lines.length) {
    box.innerHTML = '<div class="personality-diff-line ctx">' +
      'Identical to the current text.</div>';
    return;
  }
  data.lines.forEach(line => {
    let cls = 'ctx';
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'hdr';
    else if (line.startsWith('@@')) cls = 'hunk';
    else if (line.startsWith('+')) cls = 'add';
    else if (line.startsWith('-')) cls = 'del';
    const div = document.createElement('div');
    div.className = 'personality-diff-line ' + cls;
    div.textContent = line;
    box.appendChild(div);
  });
}

function personalityConfirmRestore(revisionUuid){
  const row = personalityHistoryRows.find(r => r.uuid === revisionUuid);
  personalityRestoreUuid = revisionUuid;
  document.getElementById('personality-restore-msg').textContent =
    'Restore the version saved ' + personalityShortDate(row && row.created_at) + '?';
  document.getElementById('personality-restore-confirm').onclick =
    personalityRestoreConfirmed;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('personality-restore-modal').hidden = false;
}

function personalityCloseRestoreModal(){
  personalityRestoreUuid = null;
  document.getElementById('personality-restore-modal').hidden = true;
  document.getElementById('ui-modal-backdrop').hidden = true;
}

async function personalityRestoreConfirmed(){
  const uuid = personalitySelectedItem;
  const revisionUuid = personalityRestoreUuid;
  personalityCloseRestoreModal();
  if (!uuid || !revisionUuid) return;
  let data;
  try {
    const resp = await fetch('/personality/api/personalities/' + uuid +
                             '/revisions/' + revisionUuid + '/restore',
                             {method: 'POST'});
    data = await resp.json();
    if (!resp.ok) { personalityToastMsg(data.error || 'could not restore'); return; }
  } catch (e) {
    personalityToastMsg('could not restore');
    return;
  }
  personalityEditorSet(data.content);
  if (data.changed) {
    const row = personalityByUuid(uuid);
    if (row) row.revisionCount = (row.revisionCount || 0) + 1;
    personalityToastMsg('restored as a new version');
  } else {
    personalityToastMsg('already the current text');
  }
  await personalityLoadHistory(uuid);
  personalityRenderEditor();
}
```

- [ ] **Step 5: Close the History view when the selection changes**

In `personalitySelectItem`, before the render calls, add:

```javascript
  if (personalityHistoryOpen) {
    personalityHistoryOpen = false;
    document.getElementById('personality-history-btn').textContent = 'History';
    personalityHistoryVisible(false);
  }
```

- [ ] **Step 6: Block History while an edit is open**

In `personalitySyncEditButtons`, add:

```javascript
  document.getElementById('personality-history-btn').disabled = personalityEditMode;
```

- [ ] **Step 7: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py -v`
Expected: PASS (7 tests).

- [ ] **Step 8: Commit**

```bash
git add source/static/personality.js source/webapp/test_personality_views.py
git commit -m "feat: /personality history view with per-revision diff and restore"
```

---

## Task 9: Admin views and uuid resolution

**Files:**
- Modify: `webapp/core.py` (imports + three admin views), `db/find_uuid.py`
- Test: `webapp/test_personality_views.py` (append)

**Interfaces:**
- Consumes: the models from Task 1.
- Produces: `/admin` views under a "Personality" category; `/find` resolution for all three uuid kinds.

- [ ] **Step 1: Write the failing test**

Append to `webapp/test_personality_views.py`:

```python
def test_find_resolves_a_personality_uuid():
    import db
    c = app.test_client()
    made = c.post("/personality/api/personalities",
                  json={"name": "FindMe", "folderId": None}).get_json()
    uuid = made["personality"]["uuid"]
    try:
        a = db.make_app()
        db.init_db(a)
        with a.app_context():
            hits = db.find_uuid(uuid)
        assert hits, "personality uuid did not resolve"
        assert hits[0]["url"] == f"/personality?id={uuid}"
    finally:
        c.delete(f"/personality/api/personalities/{uuid}")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py::test_find_resolves_a_personality_uuid -v`
Expected: FAIL — `assert hits` trips; nothing resolves the uuid.

Note: check `db/find_uuid.py`'s public entry point name before writing the
test body — if the exported function is not `find_uuid`, use the real name in
both the test and Step 4.

- [ ] **Step 3: Add the admin views**

In `webapp/core.py`, add `Personality`, `PersonalityFolder` and
`PersonalityRevision` to the `db.models` import list, then append after the
Profile admin views (around line 1093):

```python
def _personality_link(row):
    return Markup(f'<a href="/personality?id={row.uuid}">open</a>')


class PersonalityFolderView(ModelView):
    column_list = ("name", "description", "parent_uuid", "position",
                   "uuid", "personality_link", "updated_at")
    column_formatters = {"personality_link": lambda v, c, m, p: _personality_link(m)}
    column_labels = {"personality_link": "Personality page"}
    column_default_sort = ("position", False)


class PersonalityView(ModelView):
    column_list = ("name", "folder_uuid", "position", "uuid",
                   "personality_link", "updated_at")
    column_formatters = {"personality_link": lambda v, c, m, p: _personality_link(m)}
    column_labels = {"personality_link": "Personality page"}
    column_default_sort = ("position", False)


class PersonalityRevisionView(ModelView):
    """Read-only: the history is append-only by design — editing it here would
    break the invariant that the newest revision mirrors the personality's
    current text."""
    can_create = False
    can_edit = False
    can_delete = False
    column_list = ("personality_uuid", "created_at", "uuid")
    column_default_sort = ("id", True)


admin.add_view(PersonalityFolderView(PersonalityFolder, db, category="Personality"))
admin.add_view(PersonalityView(Personality, db, category="Personality"))
admin.add_view(PersonalityRevisionView(PersonalityRevision, db, category="Personality"))
```

Match the surrounding code: if the Profile views use a different link-builder
or `Markup` import style, follow theirs.

- [ ] **Step 4: Register the find_uuid sources**

In `db/find_uuid.py`, add the models to the `db.models` import, then add the
formatters next to `_prompt` / `_prompt_folder`:

```python
def _personality(row: Any) -> dict:
    return {"name": row.name, "url": f"/personality?id={row.uuid}",
            "parents": _folder_chain(PersonalityFolder, "personality folder",
                                     row.folder_uuid)}


def _personality_folder(row: Any) -> dict:
    return {"name": row.name, "url": f"/personality?id={row.uuid}",
            "parents": _folder_chain(PersonalityFolder, "personality folder",
                                     row.parent_uuid)}


def _personality_revision(row: Any) -> dict:
    # A revision has no page of its own — it resolves to the personality whose
    # history holds it.
    return {"name": f"version of {row.personality_uuid}",
            "url": f"/personality?id={row.personality_uuid}",
            "parents": []}
```

and register them in the `_Source` / `_TextSource` lists next to the prompt
entries:

```python
    _Source("personality folder", PersonalityFolder, _personality_folder),
    _Source("personality", Personality, _personality),
    _Source("personality version", PersonalityRevision, _personality_revision),
```

```python
    _TextSource(Personality, ("content",), "personality"),
```

Check the exact `_folder_chain` signature and `_Source` field order in the
file before writing — copy the prompt entries' shape.

- [ ] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_personality_views.py db/test_find_uuid.py -v`
Expected: PASS, including the existing find_uuid tests.

- [ ] **Step 6: Verify the admin pages render**

```bash
./venv/bin/python -c "
from webapp.core import app
c = app.test_client()
for path in ('/admin/personality/', '/admin/personalityrevision/'):
    r = c.get(path)
    print(path, r.status_code)
"
```

Expected: both `200` (or a `30x` to the canonical admin URL — follow it and
confirm `200`). If the URL slugs differ, list them from `/admin/` and use the
real ones.

- [ ] **Step 7: Commit**

```bash
git add source/webapp/core.py source/db/find_uuid.py source/webapp/test_personality_views.py
git commit -m "feat: personality admin views and uuid resolution"
```

---

## Task 10: Browser verification, design doc, full regression

**Files:**
- Create: `docs/personality-design.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: everything above.

**Why a browser pass is mandatory:** `docs/ui-left-panel-tree.md` §8 records
that `/git` shipped with a broken root-drop strip *because* its JS was a
faithful copy — the fault was in CSS layout, invisible to both marker tests
and code review. This task is where that class of bug gets caught.

- [ ] **Step 1: Start the app**

```bash
./venv/bin/python main.py
```

Leave it running; the page is at `http://127.0.0.1:5000/personality`.

- [ ] **Step 2: Walk the tree interactions in a real browser**

Confirm each, and fix what fails before moving on:

- `+ Folder` and `+ Personality` create nodes that appear immediately.
- Clicking a folder selects it; clicking it again toggles expand/collapse.
- The selected row is tinted **and bold** — folders and leaves alike.
- The kebab appears only on the selected row, and row height does not change
  when a row becomes selected.
- Dragging a personality onto the "Move to top level" strip works, and the
  strip sits directly under the tree (not pinned to the panel's bottom).
- Dragging a folder onto its own descendant is refused.
- CMD-click (or middle-click) a row opens it in a new tab at `?id=<uuid>`.
- Reloading with `?id=<uuid>` restores that selection.

- [ ] **Step 3: Walk the editor and history**

- The text is read-only until **Edit**; the rest of the page greys out during
  an edit; **Cancel** restores the prior text.
- **Save** toasts "saved — version N"; saving again with no change toasts
  "no changes" and does not add a version.
- **History** lists the versions newest first with the newest marked current.
- **Diff** on an older version shows red/green lines against the current text.
- **Restore** on an older version asks for confirmation, then makes that text
  current and adds a *new* version — the history gets longer, never shorter.
- Deleting a personality with versions requires typing its name.

- [ ] **Step 4: Confirm the content edge lines up**

In the browser console on `/personality`, and again on `/chat`:

```javascript
document.querySelector('.personality-main').getBoundingClientRect().left
```

Expected: 276 (the standard 260px tree + 16px padding), matching `/chat`'s
`.room-title`. Anything else means the main-pane padding drifted (§8).

- [ ] **Step 5: Write the design doc**

Create `docs/personality-design.md`, following the shape of
`docs/prompt-design.md`: status line and date, the idea, a "Where things live"
file table, the data model with the three tables, the revision rules (append
on change, newest mirrors `content`, restore appends), the HTTP API table, the
frontend description, deliberate tradeoffs, and open questions (prompt wiring,
active-personality selection, usage back-references). Describe the page as it
is now — no migration notes, no "ported from" history.

Then index it in `docs/README.md` next to the prompt entry:

```markdown
- [personality-design.md](personality-design.md) — the /personality page:
  a folder tree of assistant personalities with append-only revisions.
```

- [ ] **Step 6: Full regression**

```bash
./venv/bin/python -m pytest db/test_personality_tree.py webapp/test_personality_api.py webapp/test_personality_views.py -v
./venv/bin/python -m pytest db/ webapp/ -q
```

Expected: the three new files fully green. For the broad run, compare failures
against `git stash`-ed baseline if any appear — the repo has known pre-existing
failures unrelated to this feature; do not "fix" those here, but do report them.

- [ ] **Step 7: Commit**

```bash
git add source/docs/personality-design.md source/docs/README.md
git commit -m "docs: /personality design doc"
```

---

## Self-Review (completed during planning)

**Spec coverage:**

| Spec section | Task |
|---|---|
| Three tables, no FKs, revision ordered by id | 1 |
| Tree load/validate/save/version, no create-or-delete in the PUT | 2 |
| create/delete + cascade, content, revisions, diff, restore | 3 |
| Tree/create/delete endpoints, version on every mutation | 4 |
| Content + revision endpoints, foreign revision = 404 | 5 |
| Page shell, CSS, Assistant-dropdown nav entry | 6 |
| Left panel, folder table, selection, drag-drop, deep-link | 7 |
| Editor (explicit Edit→Save), History list, diff, restore modal | 8 |
| Admin views, find_uuid registration | 9 |
| Real-browser pass, design doc | 10 |

Spec items deliberately absent from every task, matching the spec's
"Not in this step": assistant prompt wiring, the `/chat` picker, a seeded
default personality, retention caps, per-revision labels.

**Placeholder scan:** none — every code step carries the code, every command
carries its expected output. Three steps (Task 6 step 3, Task 7 step 3, Task 9
steps 3-4) direct the implementer to copy an existing file or match a
neighboring pattern; each names the exact source file and lists every
divergence in full, because the on-disk file is a more reliable source than a
transcription of it.

**Type consistency:** `personality_create` / `personality_create_folder`
return the `_personality_dict` / `_folder_dict` shapes used by
`personality_load_tree`, so Task 7's JS can push a POST response straight into
`personalityItems` / `personalityFolders`. `revisionCount` is present on every
personality dict (tree rows, create responses, `personality_get`) and is what
Tasks 7-8 read for the folder table, the delete modal and the version counter.
`personality_update_content` and `personality_restore_revision` both report
`changed`, which Task 8's JS branches on identically.
