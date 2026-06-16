# /kanban Folder Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `/kanban` left panel the same nested folder-tree `/chat` and `/cron` have (folders → boards), as a full port of the pattern in `docs/left-panel-tree.md`.

**Architecture:** A new **tree layer** (folders + which folder each board sits in + ordering) saves through a placement-only `GET/PUT /kanban/api/tree`, completely separate from the existing per-board contents save (`PUT /kanban/api/board/<uuid>`, untouched). Folder create/delete get their own endpoints; folder delete reparents children (never deletes boards). The frontend gains a recursive tree render, drag-and-drop, a folder-contents detail table, and a static "All boards" root node.

**Tech Stack:** Python 3.14 / Flask / SQLAlchemy / Postgres (`rainbox_claude` for tests, pinned by `conftest.py`); vanilla JS frontend (no framework, no JS test runner).

**Spec:** `docs/superpowers/specs/2026-06-16-kanban-folder-tree-design.md`

**Test command:** `python -m pytest <path> -v` (conftest pins every run to `rainbox_claude` — never touches production).

**Conventions to follow:**
- DB tree functions live in `db/kanban.py`, re-exported via `from db.kanban import *` in `db/__init__.py` (so tests call `db.kanban_load_tree(...)`).
- Mirror the cron tree exactly: `db/cron.py` (`cron_load_tree`/`cron_tree_version`/`validate_cron_tree`/`cron_save_tree`) and `webapp/cron_api.py` (`/cron/api/tree`).
- Reuse the existing `KanbanError` (→ 400) and `KanbanConflict` (→ 409) exceptions and the `_to_uuid` helper already in `db/kanban.py`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `db/models.py` | `KanbanBoardFolder` table; `KanbanBoard.folder_uuid` | Modify |
| `db/__init__.py` | `_add_column_if_missing("kanban_board", "folder_uuid", ...)` | Modify |
| `db/kanban.py` | `kanban_load_tree`, `kanban_tree_version`, `validate_kanban_tree`, `kanban_save_tree`, `kanban_create_folder`, `kanban_delete_folder`; `folder_uuid` param on `kanban_create_board` | Modify |
| `webapp/kanban_api.py` | `GET/PUT /kanban/api/tree`; `POST /kanban/api/folders`; `DELETE /kanban/api/folders/<uuid>`; `folderId` on board create | Modify |
| `webapp/test_kanban_tree.py` | DB-level tree tests | Create |
| `webapp/test_kanban_api.py` | API-level tree/folder tests | Modify (append) |
| `webapp/kanban_views.py` | tree CSS, HTML shell (tree root, folder-detail pane, root-drop strip, folder modal) | Modify |
| `static/kanban.js` | tree state, render, selection, expand persistence, drag-drop, folder-detail pane, folder modals, deep-linking | Modify |

---

## Task 1: DB model — folder table + `folder_uuid` column

**Files:**
- Modify: `db/models.py` (after `KanbanBoard`, ~line 306; and add `folder_uuid` to `KanbanBoard`)
- Modify: `db/__init__.py` (in `init_db`, near the existing kanban column adds ~line 270)
- Test: `webapp/test_kanban_tree.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `webapp/test_kanban_tree.py`:

```python
"""Tests for the /kanban folder tree: schema, load/version/validate/save,
folder create + reparenting delete.

Hits the live local Postgres (conftest pins every pytest run to
rainbox_claude). Each test cleans up the rows it creates.
"""
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

import db
from db import KanbanBoardFolder


def _u(s):
    return UUID(s)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        # Close any read transaction so its ACCESS SHARE locks don't block
        # the next test's init_db ALTERs (single-process lock self-deadlock).
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def board(app_ctx):
    """A fresh board; deleted with all its rows after."""
    b = db.kanban_create_board("Tree test board")
    try:
        yield b
    finally:
        db.kanban_delete_board(_u(b["uuid"]))


def _new_folder(name="F", parent=None):
    """Create a folder directly; returns its dict. Caller cleans up."""
    return db.kanban_create_folder(name, parent_uuid=parent)


def test_folder_table_and_board_column_exist(app_ctx):
    # The folder table is creatable and round-trips.
    f = KanbanBoardFolder(uuid=uuid4(), name="schema check")
    db.db.session.add(f)
    db.db.session.commit()
    got = db.db.session.execute(
        sa.select(KanbanBoardFolder).where(KanbanBoardFolder.uuid == f.uuid)
    ).scalar_one()
    assert got.name == "schema check"
    assert got.parent_uuid is None and got.position == 0
    db.db.session.delete(got)
    db.db.session.commit()
    # The board carries a folder_uuid column (null by default).
    col = db.db.session.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='kanban_board' AND column_name='folder_uuid'"
    )).first()
    assert col is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest webapp/test_kanban_tree.py::test_folder_table_and_board_column_exist -v`
Expected: FAIL — `ImportError`/`cannot import name 'KanbanBoardFolder'`.

- [ ] **Step 3: Add the model + column + migration**

In `db/models.py`, immediately after the `KanbanBoard` class (before `KanbanColumn`), add:

```python
class KanbanBoardFolder(db.Model):
    """An organizational folder in the /kanban left-panel tree (folders →
    boards). Purely organizational: boards reference a folder by a plain
    `folder_uuid` column and folders nest via `parent_uuid` — both plain uuid
    columns with no FK (the cron/chat folder pattern; app-side validation in
    validate_kanban_tree catches dangling/cyclic refs)."""

    __tablename__ = "kanban_board_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("kanban_board_folder_children", "parent_uuid", "position"),)
```

In the same file, add `folder_uuid` to `KanbanBoard` (after its `position` line, ~line 298) and an index. Replace:

```python
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
```

with (only in the `KanbanBoard` class):

```python
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = unfiled/root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("kanban_board_in_folder", "folder_uuid", "position"),)
```

In `db/__init__.py`, in `init_db`, after the three `kanban_task` claim-column adds (~line 270, right before the `# Chat-folder columns` block), add:

```python
        # kanban board folders (the left-panel tree, added after the board
        # table's first cut). New table kanban_board_folder is created by
        # create_all() above; the placement column is back-filled here.
        _add_column_if_missing("kanban_board", "folder_uuid", "folder_uuid UUID")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest webapp/test_kanban_tree.py::test_folder_table_and_board_column_exist -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db/models.py db/__init__.py webapp/test_kanban_tree.py
git commit -m "feat(kanban): add board-folder table + folder_uuid column"
```

---

## Task 2: `kanban_create_folder` + `kanban_delete_folder` (reparenting)

**Files:**
- Modify: `db/kanban.py` (in the boards section, after `kanban_list_boards`, ~line 153)
- Test: `webapp/test_kanban_tree.py`

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_kanban_tree.py`:

```python
def test_create_folder_defaults_and_position(app_ctx):
    a = db.kanban_create_folder("Alpha")
    b = db.kanban_create_folder("Beta")
    try:
        assert a["name"] == "Alpha" and a["parentId"] is None
        assert UUID(a["uuid"]) and a["position"] == 0
        assert b["position"] == 1  # appended after its sibling
    finally:
        db.kanban_delete_folder(_u(a["uuid"]))
        db.kanban_delete_folder(_u(b["uuid"]))


def test_create_folder_requires_name(app_ctx):
    with pytest.raises(db.KanbanError):
        db.kanban_create_folder("   ")


def test_delete_folder_reparents_children_and_keeps_boards(app_ctx):
    parent = db.kanban_create_folder("parent")
    child = db.kanban_create_folder("child", parent_uuid=_u(parent["uuid"]))
    board = db.kanban_create_board("filed board")
    try:
        # File the board + a sub-board-folder under `child`.
        db.kanban_save_tree(
            folders=[{"uuid": parent["uuid"], "name": "parent", "parentId": None},
                     {"uuid": child["uuid"], "name": "child", "parentId": parent["uuid"]}],
            boards=[{"uuid": board["uuid"], "folderId": child["uuid"]}],
        )
        # Delete the middle folder `child`: its board reparents up to `parent`,
        # the board (and its columns/tasks) survives.
        assert db.kanban_delete_folder(_u(child["uuid"])) is True
        tree = db.kanban_load_tree()
        folder_ids = {f["uuid"] for f in tree["folders"]}
        assert child["uuid"] not in folder_ids and parent["uuid"] in folder_ids
        moved = next(b for b in tree["boards"] if b["uuid"] == board["uuid"])
        assert moved["folderId"] == parent["uuid"]
        assert db.kanban_load_board(_u(board["uuid"])) is not None  # board kept
        assert db.kanban_delete_folder(_u(child["uuid"])) is False  # already gone
    finally:
        db.kanban_delete_board(_u(board["uuid"]))
        db.kanban_delete_folder(_u(parent["uuid"]))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest webapp/test_kanban_tree.py -k "folder" -v`
Expected: FAIL — `module 'db' has no attribute 'kanban_create_folder'`.

- [ ] **Step 3: Implement the folder ops**

In `db/kanban.py`, add the import of the new model. Change the existing import line:

```python
from db.models import KanbanBoard, KanbanColumn, KanbanTask, KanbanTaskEvent, db
```

to:

```python
from db.models import (KanbanBoard, KanbanBoardFolder, KanbanColumn,
                       KanbanTask, KanbanTaskEvent, db)
```

Then, after `kanban_list_boards` (~line 153), add:

```python
# ---- folder tree: create / delete / load / version / validate / save ----
# A separate concern from board CONTENTS (columns/tasks): this layer manages
# folders and which folder each board sits in. The save is placement-only (the
# /chat shape) — it never creates or deletes boards, and folder create/delete
# have their own endpoints (delete reparents, never cascades to boards).

def _folder_brief(f: "KanbanBoardFolder") -> dict[str, Any]:
    return {"uuid": str(f.uuid), "name": f.name, "description": f.description,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
            "position": f.position}


def kanban_create_folder(
    name: str, parent_uuid: UUID | None = None, description: str = "",
) -> dict[str, Any]:
    """Create a folder (appended after its siblings); returns its brief dict."""
    if not isinstance(name, str) or not name.strip():
        raise KanbanError("folder name is required")
    position = db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanBoardFolder.position), -1))
        .where(KanbanBoardFolder.parent_uuid == parent_uuid)
    ).scalar_one() + 1
    folder = KanbanBoardFolder(uuid=uuid4(), name=name.strip(),
                               description=str(description or ""),
                               parent_uuid=parent_uuid, position=position)
    db.session.add(folder)
    db.session.commit()
    return _folder_brief(folder)


def kanban_delete_folder(folder_uuid: UUID) -> bool:
    """Delete a folder NON-DESTRUCTIVELY: its direct child folders and boards
    reparent up to the deleted folder's own parent (root if it had none), then
    the folder row is removed. Boards (and their columns/tasks) are never
    deleted by a folder delete. False if the folder doesn't exist."""
    folder = db.session.execute(
        sa.select(KanbanBoardFolder).where(KanbanBoardFolder.uuid == folder_uuid)
    ).scalar_one_or_none()
    if folder is None:
        return False
    grandparent = folder.parent_uuid
    db.session.execute(
        sa.update(KanbanBoardFolder)
        .where(KanbanBoardFolder.parent_uuid == folder_uuid)
        .values(parent_uuid=grandparent))
    db.session.execute(
        sa.update(KanbanBoard)
        .where(KanbanBoard.folder_uuid == folder_uuid)
        .values(folder_uuid=grandparent))
    db.session.delete(folder)
    db.session.commit()
    return True
```

- [ ] **Step 4: Run to verify (folder create tests pass; reparent test still fails on `kanban_save_tree`/`kanban_load_tree`)**

Run: `python -m pytest webapp/test_kanban_tree.py -k "create_folder" -v`
Expected: PASS (both create tests).

Run: `python -m pytest webapp/test_kanban_tree.py::test_delete_folder_reparents_children_and_keeps_boards -v`
Expected: FAIL — `module 'db' has no attribute 'kanban_save_tree'` (implemented in Task 4). This is expected; it passes after Task 4.

- [ ] **Step 5: Commit**

```bash
git add db/kanban.py webapp/test_kanban_tree.py
git commit -m "feat(kanban): create_folder + reparenting delete_folder"
```

---

## Task 3: `kanban_load_tree` + `kanban_tree_version`

**Files:**
- Modify: `db/kanban.py` (after the folder ops from Task 2)
- Test: `webapp/test_kanban_tree.py`

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_kanban_tree.py`:

```python
def test_load_tree_shape(board):
    f = db.kanban_create_folder("Inbox")
    try:
        tree = db.kanban_load_tree()
        assert isinstance(tree["version"], str) and tree["version"]
        folder = next(x for x in tree["folders"] if x["uuid"] == f["uuid"])
        assert set(folder) == {"uuid", "name", "description", "parentId", "position"}
        b = next(x for x in tree["boards"] if x["uuid"] == board["uuid"])
        assert set(b) == {"uuid", "name", "folderId", "position", "taskCount"}
        assert b["folderId"] is None and b["taskCount"] == 0
    finally:
        db.kanban_delete_folder(_u(f["uuid"]))


def test_tree_version_excludes_board_name_and_taskcount(board):
    """A board rename (board PUT) and a new task (agent op) must NOT bump the
    tree version — those are not structural tree fields, or the tree would 409
    on every background board/task change."""
    bu = _u(board["uuid"])
    v0 = db.kanban_load_tree()["version"]
    # Rename the board via the board-contents save: tree version unchanged.
    db.kanban_save_board(bu, {**board, "name": "renamed"})
    assert db.kanban_load_tree()["version"] == v0
    # Add a task via the board-contents save: tree version unchanged.
    todo = board["columns"][0]["uuid"]
    after = db.kanban_load_board(bu)
    db.kanban_save_board(bu, {**after,
        "tasks": [{"uuid": str(uuid4()), "columnUuid": todo, "title": "t",
                   "description": "", "agentUuid": None}]})
    assert db.kanban_load_tree()["version"] == v0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest webapp/test_kanban_tree.py -k "load_tree or tree_version" -v`
Expected: FAIL — `module 'db' has no attribute 'kanban_load_tree'`.

- [ ] **Step 3: Implement load + version**

In `db/kanban.py`, after `kanban_delete_folder`, add:

```python
def kanban_tree_version() -> str:
    """Opaque optimistic-concurrency token for the TREE (folders + board
    placement), over STRUCTURAL fields only — folder (uuid,name,parentId,
    position) and board (uuid,folderId,position). Board NAME and TASK COUNT are
    excluded on purpose: a board rename goes through the board PUT and agents
    add tasks in the background; including either would 409 the next tree save
    on every such event (the cron/chat 'exclude volatile fields' rule). The
    displayed name/count are kept in sync client-side instead."""
    folders = db.session.execute(
        sa.select(KanbanBoardFolder).order_by(KanbanBoardFolder.uuid)
    ).scalars().all()
    boards = db.session.execute(
        sa.select(KanbanBoard).order_by(KanbanBoard.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name, f.description,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(b.uuid), str(b.folder_uuid) if b.folder_uuid else None, b.position]
         for b in boards],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def kanban_load_tree() -> dict[str, Any]:
    """The /kanban left-panel tree: folders + boards (with task counts), each
    list in saved order. The page hydrates from this and PUTs it back. Board
    contents (columns/tasks) are NOT here — those load per-board."""
    folders = db.session.execute(
        sa.select(KanbanBoardFolder)
        .order_by(KanbanBoardFolder.position, KanbanBoardFolder.id)
    ).scalars().all()
    boards = db.session.execute(
        sa.select(KanbanBoard).order_by(KanbanBoard.position, KanbanBoard.id)
    ).scalars().all()
    counts = {b: n for b, n in db.session.execute(
        sa.select(KanbanTask.board_uuid, sa.func.count())
        .group_by(KanbanTask.board_uuid)
    ).all()}
    return {
        "folders": [_folder_brief(f) for f in folders],
        "boards": [
            {"uuid": str(b.uuid), "name": b.name,
             "folderId": str(b.folder_uuid) if b.folder_uuid else None,
             "position": b.position, "taskCount": counts.get(b.uuid, 0)}
            for b in boards
        ],
        "version": kanban_tree_version(),
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest webapp/test_kanban_tree.py -k "load_tree or tree_version" -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add db/kanban.py webapp/test_kanban_tree.py
git commit -m "feat(kanban): load_tree + tree_version (structural fields only)"
```

---

## Task 4: `validate_kanban_tree` + `kanban_save_tree` (placement-only)

**Files:**
- Modify: `db/kanban.py` (after `kanban_load_tree`)
- Test: `webapp/test_kanban_tree.py`

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_kanban_tree.py`:

```python
def test_validate_rejects_dangling_and_cyclic_and_dups(app_ctx):
    good = str(uuid4())
    # Dangling folder parent.
    with pytest.raises(db.KanbanError):
        db.validate_kanban_tree([{"uuid": good, "name": "x", "parentId": str(uuid4())}], [])
    # Cycle: a -> b -> a.
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.KanbanError):
        db.validate_kanban_tree(
            [{"uuid": a, "name": "a", "parentId": b},
             {"uuid": b, "name": "b", "parentId": a}], [])
    # Board folderId references a missing folder.
    with pytest.raises(db.KanbanError):
        db.validate_kanban_tree([], [{"uuid": str(uuid4()), "folderId": str(uuid4())}])
    # Duplicate folder uuid.
    with pytest.raises(db.KanbanError):
        db.validate_kanban_tree(
            [{"uuid": a, "name": "a", "parentId": None},
             {"uuid": a, "name": "dup", "parentId": None}], [])
    # Board uuid collides with a folder uuid (deep links are by uuid).
    with pytest.raises(db.KanbanError):
        db.validate_kanban_tree(
            [{"uuid": a, "name": "a", "parentId": None}],
            [{"uuid": a, "folderId": None}])


def test_save_tree_round_trips_placement(app_ctx):
    f1 = db.kanban_create_folder("one")
    f2 = db.kanban_create_folder("two")
    b1 = db.kanban_create_board("b1")
    b2 = db.kanban_create_board("b2")
    try:
        # Nest f2 under f1; file b1 under f2, b2 under f1; reorder folders.
        db.kanban_save_tree(
            folders=[{"uuid": f2["uuid"], "name": "two", "parentId": None},
                     {"uuid": f1["uuid"], "name": "one-renamed", "parentId": f2["uuid"]}],
            boards=[{"uuid": b2["uuid"], "folderId": f1["uuid"]},
                    {"uuid": b1["uuid"], "folderId": f2["uuid"]}])
        tree = db.kanban_load_tree()
        f1_out = next(f for f in tree["folders"] if f["uuid"] == f1["uuid"])
        assert f1_out["name"] == "one-renamed" and f1_out["parentId"] == f2["uuid"]
        assert next(b for b in tree["boards"] if b["uuid"] == b1["uuid"])["folderId"] == f2["uuid"]
        # Position reflects payload order (f2 before f1).
        assert f1_out["position"] == 1
    finally:
        db.kanban_delete_board(_u(b1["uuid"]))
        db.kanban_delete_board(_u(b2["uuid"]))
        db.kanban_delete_folder(_u(f1["uuid"]))
        db.kanban_delete_folder(_u(f2["uuid"]))


def test_save_tree_never_deletes_boards(app_ctx):
    """Placement-only: a board absent from the payload is NOT deleted."""
    f = db.kanban_create_folder("f")
    keep = db.kanban_create_board("keep")
    absent = db.kanban_create_board("absent from payload")
    try:
        db.kanban_save_tree(
            folders=[{"uuid": f["uuid"], "name": "f", "parentId": None}],
            boards=[{"uuid": keep["uuid"], "folderId": f["uuid"]}])
        assert db.kanban_load_board(_u(absent["uuid"])) is not None  # survives
    finally:
        db.kanban_delete_board(_u(keep["uuid"]))
        db.kanban_delete_board(_u(absent["uuid"]))
        db.kanban_delete_folder(_u(f["uuid"]))


def test_save_tree_stale_version_conflicts(app_ctx):
    f = db.kanban_create_folder("f")
    try:
        v0 = db.kanban_load_tree()["version"]
        # Another writer renames the folder, rotating the version.
        db.kanban_save_tree(folders=[{"uuid": f["uuid"], "name": "theirs", "parentId": None}],
                            boards=[])
        with pytest.raises(db.KanbanConflict):
            db.kanban_save_tree(
                folders=[{"uuid": f["uuid"], "name": "mine", "parentId": None}],
                boards=[], base_version=v0)
        assert next(x for x in db.kanban_load_tree()["folders"]
                    if x["uuid"] == f["uuid"])["name"] == "theirs"
    finally:
        db.kanban_delete_folder(_u(f["uuid"]))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest webapp/test_kanban_tree.py -k "validate or save_tree" -v`
Expected: FAIL — `module 'db' has no attribute 'validate_kanban_tree'`.

- [ ] **Step 3: Implement validate + save**

In `db/kanban.py`, after `kanban_load_tree`, add:

```python
def validate_kanban_tree(
    folders: list[dict[str, Any]], boards: list[dict[str, Any]]
) -> None:
    """Structural integrity check for an incoming tree, run before any write.
    Raises KanbanError on the first problem; does not touch the DB. uuids are
    normalized so case/format-variant spellings of the same id collide here."""
    if not isinstance(folders, list):
        raise KanbanError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(boards, list):
        raise KanbanError(f"'boards' must be a list, got {type(boards).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise KanbanError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("uuid"))
        if fid is None:
            raise KanbanError(f"folder uuid is not a uuid: {f.get('uuid')!r}")
        if fid in parent_of:
            raise KanbanError(f"duplicate folder uuid: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise KanbanError(f"folder {fid} name must be a string")
        if not isinstance(f.get("description", ""), str):
            raise KanbanError(f"folder {fid} description must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise KanbanError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise KanbanError(f"folder {fid} references missing parent {pid}")
    # Acyclic: walking parents from any folder must terminate at a root.
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise KanbanError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    board_uuids: set[UUID] = set()
    for b in boards:
        if not isinstance(b, dict):
            raise KanbanError(f"board entry must be an object, got {type(b).__name__}")
        bu = _to_uuid(b.get("uuid"))
        if bu is None:
            raise KanbanError(f"board uuid is not a uuid: {b.get('uuid')!r}")
        if bu in board_uuids:
            raise KanbanError(f"duplicate board uuid: {bu}")
        # uuids are globally unique across kinds: a node is deep-linked by uuid,
        # so a board sharing a folder's uuid would make the link ambiguous.
        if bu in parent_of:
            raise KanbanError(f"board uuid {bu} collides with a folder uuid")
        board_uuids.add(bu)
        fld_raw = b.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise KanbanError(f"board {bu} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise KanbanError(f"board {bu} references missing folder {fld}")


def kanban_save_tree(
    folders: list[dict[str, Any]], boards: list[dict[str, Any]],
    *, base_version: str | None = None,
) -> None:
    """Placement-only save of the tree: upsert folder name/description/parent/
    position and update each board's folder_uuid/position from list order.
    NEVER creates or deletes boards, and does not delete folders (deletion is
    kanban_delete_folder). A folder present in the DB but absent from the
    payload is left untouched. Validates first (KanbanError before any write);
    a stale base_version raises KanbanConflict."""
    validate_kanban_tree(folders, boards)
    if base_version is not None and base_version != kanban_tree_version():
        raise KanbanConflict("kanban tree changed since it was loaded")
    existing_f = {f.uuid: f for f in db.session.execute(
        sa.select(KanbanBoardFolder)).scalars().all()}
    existing_b = {b.uuid: b for b in db.session.execute(
        sa.select(KanbanBoard)).scalars().all()}
    for i, f in enumerate(folders):
        fu = _to_uuid(f["uuid"])
        row = existing_f.get(fu)
        if row is None:
            row = KanbanBoardFolder(uuid=fu)
            db.session.add(row)
        row.name = f.get("name", "")
        row.description = f.get("description", "")
        row.parent_uuid = _to_uuid(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for i, b in enumerate(boards):
        bu = _to_uuid(b["uuid"])
        row = existing_b.get(bu)
        if row is None:
            continue  # placement-only: never create a board here
        row.folder_uuid = _to_uuid(b["folderId"]) if b.get("folderId") else None
        row.position = i
    db.session.commit()
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest webapp/test_kanban_tree.py -v`
Expected: PASS — all tests in the file, including `test_delete_folder_reparents_children_and_keeps_boards` from Task 2 (which depended on `kanban_save_tree`).

- [ ] **Step 5: Commit**

```bash
git add db/kanban.py webapp/test_kanban_tree.py
git commit -m "feat(kanban): validate_kanban_tree + placement-only save_tree"
```

---

## Task 5: `folder_uuid` on `kanban_create_board`

**Files:**
- Modify: `db/kanban.py` (`kanban_create_board`, ~line 68)
- Test: `webapp/test_kanban_tree.py`

- [ ] **Step 1: Write the failing test**

Append to `webapp/test_kanban_tree.py`:

```python
def test_create_board_into_folder(app_ctx):
    f = db.kanban_create_folder("dest")
    b = db.kanban_create_board("filed at birth", folder_uuid=_u(f["uuid"]))
    try:
        placed = next(x for x in db.kanban_load_tree()["boards"]
                      if x["uuid"] == b["uuid"])
        assert placed["folderId"] == f["uuid"]
    finally:
        db.kanban_delete_board(_u(b["uuid"]))
        db.kanban_delete_folder(_u(f["uuid"]))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest webapp/test_kanban_tree.py::test_create_board_into_folder -v`
Expected: FAIL — `kanban_create_board() got an unexpected keyword argument 'folder_uuid'`.

- [ ] **Step 3: Add the parameter**

In `db/kanban.py`, change the `kanban_create_board` signature and the `KanbanBoard(...)` construction. Replace:

```python
def kanban_create_board(name: str, description: str = "") -> dict[str, Any]:
    """Create a board with the default columns; returns the load payload."""
    if not isinstance(name, str) or not name.strip():
        raise KanbanError("board name is required")
    position = db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanBoard.position), -1))
    ).scalar_one() + 1
    board = KanbanBoard(uuid=uuid4(), name=name.strip(),
                        description=str(description or ""), position=position)
```

with:

```python
def kanban_create_board(name: str, description: str = "",
                        folder_uuid: UUID | None = None) -> dict[str, Any]:
    """Create a board with the default columns; returns the load payload.
    `folder_uuid` files it under a tree folder (None = unfiled/root)."""
    if not isinstance(name, str) or not name.strip():
        raise KanbanError("board name is required")
    position = db.session.execute(
        sa.select(sa.func.coalesce(sa.func.max(KanbanBoard.position), -1))
    ).scalar_one() + 1
    board = KanbanBoard(uuid=uuid4(), name=name.strip(),
                        description=str(description or ""), position=position,
                        folder_uuid=folder_uuid)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest webapp/test_kanban_tree.py::test_create_board_into_folder -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db/kanban.py webapp/test_kanban_tree.py
git commit -m "feat(kanban): create_board accepts folder_uuid"
```

---

## Task 6: API endpoints — tree GET/PUT, folder POST/DELETE, board folderId

**Files:**
- Modify: `webapp/kanban_api.py` (add tree + folder routes; extend board POST)
- Test: `webapp/test_kanban_api.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_kanban_api.py`:

```python
# ---- folder tree API ----

def test_tree_get_and_put_roundtrip(board):
    client = _client()
    folder = db.kanban_create_folder("api folder")
    try:
        tree = client.get("/kanban/api/tree").get_json()
        assert any(f["uuid"] == folder["uuid"] for f in tree["folders"])
        # File the board under the folder via PUT.
        body = {"folders": tree["folders"],
                "boards": [{**b, "folderId": folder["uuid"]} if b["uuid"] == board["uuid"]
                           else b for b in tree["boards"]],
                "version": tree["version"]}
        resp = client.put("/kanban/api/tree", json=body)
        assert resp.status_code == 200 and isinstance(resp.get_json()["version"], str)
        out = client.get("/kanban/api/tree").get_json()
        assert next(b for b in out["boards"] if b["uuid"] == board["uuid"])["folderId"] == folder["uuid"]
    finally:
        db.kanban_delete_folder(_u(folder["uuid"]))


def test_tree_put_missing_version_400_and_stale_409(board):
    client = _client()
    tree = client.get("/kanban/api/tree").get_json()
    assert client.put("/kanban/api/tree",
                      json={"folders": tree["folders"], "boards": tree["boards"]}
                      ).status_code == 400
    # Rotate the version out from under us.
    f = db.kanban_create_folder("rotate")
    try:
        resp = client.put("/kanban/api/tree",
                          json={**tree, "folders": tree["folders"], "boards": tree["boards"]})
        assert resp.status_code == 409 and isinstance(resp.get_json()["version"], str)
    finally:
        db.kanban_delete_folder(_u(f["uuid"]))


def test_folder_create_and_delete_endpoints(app_ctx):
    client = _client()
    r = client.post("/kanban/api/folders", json={"name": "made via api"})
    assert r.status_code == 200 and r.get_json()["folder"]["name"] == "made via api"
    fu = r.get_json()["folder"]["uuid"]
    assert client.delete(f"/kanban/api/folders/{fu}").status_code == 200
    assert client.delete(f"/kanban/api/folders/{fu}").status_code == 404
    assert client.post("/kanban/api/folders", json={"name": "  "}).status_code == 400


def test_board_create_honors_folderId(app_ctx):
    client = _client()
    f = client.post("/kanban/api/folders", json={"name": "dest"}).get_json()["folder"]
    r = client.post("/kanban/api/boards", json={"name": "filed", "folderId": f["uuid"]})
    bu = r.get_json()["board"]["uuid"]
    try:
        placed = next(b for b in db.kanban_load_tree()["boards"] if b["uuid"] == bu)
        assert placed["folderId"] == f["uuid"]
    finally:
        db.kanban_delete_board(_u(bu))
        db.kanban_delete_folder(_u(f["uuid"]))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest webapp/test_kanban_api.py -k "tree or folder_create or folderId" -v`
Expected: FAIL — 404s (routes not registered yet).

- [ ] **Step 3: Add the routes**

In `webapp/kanban_api.py`, after the `kanban_boards` function (the `/kanban/api/boards` route, ~line 44), add the board-create `folderId` support and the new routes. First, replace the POST branch of `kanban_boards`:

```python
        try:
            board = db.kanban_create_board(data.get("name", ""),
                                           data.get("description", ""))
        except db.KanbanError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "board": board})
```

with:

```python
        folder = None
        if data.get("folderId") is not None:
            folder = _uuid_or_none(str(data.get("folderId")))
            if folder is None:
                return jsonify({"ok": False, "error": "'folderId' must be a uuid"}), 400
        try:
            board = db.kanban_create_board(data.get("name", ""),
                                           data.get("description", ""),
                                           folder_uuid=folder)
        except db.KanbanError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "board": board})
```

Then append these routes at the end of `webapp/kanban_api.py`:

```python
# ---- folder tree (the left-panel hierarchy) ----

@app.route("/kanban/api/tree", methods=["GET", "PUT"])
def kanban_tree() -> tuple[Response, int] | Response:
    """Hydrate / placement-only save the folder tree (folders + board
    placement). The PUT echoes the version token GET returned; a stale token
    is a 409 and the page re-hydrates instead of clobbering another writer."""
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False, "error":
                            "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.kanban_save_tree(data.get("folders", []), data.get("boards", []),
                                base_version=version)
        except db.KanbanConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.kanban_tree_version()}), 409
        except db.KanbanError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.kanban_tree_version()})
    return jsonify(db.kanban_load_tree())


@app.route("/kanban/api/folders", methods=["POST"])
def kanban_folders() -> tuple[Response, int] | Response:
    """Create a folder; returns it. Body: {name, parentId?, description?}."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
    parent = None
    if data.get("parentId") is not None:
        parent = _uuid_or_none(str(data.get("parentId")))
        if parent is None:
            return jsonify({"ok": False, "error": "'parentId' must be a uuid"}), 400
    try:
        folder = db.kanban_create_folder(data.get("name", ""), parent,
                                         data.get("description", ""))
    except db.KanbanError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "folder": folder})


@app.route("/kanban/api/folders/<folder_uuid>", methods=["DELETE"])
def kanban_folder_delete(folder_uuid: str) -> tuple[Response, int] | Response:
    """Delete a folder; its child folders + boards reparent up one level
    (boards are never deleted). 404 if the folder doesn't exist."""
    fu = _uuid_or_none(folder_uuid)
    if fu is None:
        return jsonify({"ok": False, "error": "bad uuid"}), 400
    if not db.kanban_delete_folder(fu):
        return jsonify({"ok": False, "error": "folder not found"}), 404
    return jsonify({"ok": True})
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest webapp/test_kanban_api.py -k "tree or folder_create or folderId" -v`
Expected: PASS.

- [ ] **Step 5: Run the whole kanban backend suite to confirm no regressions**

Run: `python -m pytest webapp/test_kanban_api.py webapp/test_kanban_tree.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/kanban_api.py webapp/test_kanban_api.py
git commit -m "feat(kanban): tree GET/PUT + folder create/delete API"
```

---

## Task 7: Frontend CSS + HTML shell

The backend is complete. The remaining tasks are vanilla-JS frontend with **manual browser verification** (no JS test runner in this repo). Reference `/cron`'s tree CSS/JS (`webapp/cron_views.py`, `static/cron.js`) and `/chat`'s (`webapp/chat_template.py`) when a detail is ambiguous.

**Files:**
- Modify: `webapp/kanban_views.py` (CSS block + HTML shell)

- [ ] **Step 1: Add tree CSS**

In `webapp/kanban_views.py`, inside the first `<style>` block, replace the existing board-list rules:

```css
  .kb-board-list{list-style:none;margin:0;padding:0}
  .kb-board-item{display:flex;align-items:center;gap:4px;padding:8px 6px;border-radius:6px;cursor:pointer;
    -webkit-user-select:none;user-select:none;white-space:nowrap}
  .kb-board-item:hover{background:#f1f5f9}
  .kb-board-item.sel{background:#dbeafe;font-weight:600}
  .kb-board-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
```

with:

```css
  /* Tree: nested <ul>s. Indentation + guide line are pure CSS on NESTED lists
     only (the double-descendant selector skips the root list). */
  .kb-tree-list{list-style:none;margin:0;padding:0}
  .kb-tree-list ul{list-style:none;margin:0;padding:0}
  .kb-tree-list ul ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .kb-node{box-sizing:border-box;display:flex;align-items:center;gap:4px;padding:6px;border-radius:6px;
    cursor:pointer;-webkit-user-select:none;user-select:none;white-space:nowrap}
  .kb-node:hover{background:#f1f5f9}
  .kb-node.sel{background:#dbeafe;font-weight:600}
  .kb-node.kb-drop-into{outline:2px dashed #2563eb;outline-offset:-2px}
  .kb-node.kb-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .kb-node.kb-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .kb-twisty{flex:0 0 auto;width:1rem;text-align:center;color:#6b7280;font-size:0.8rem}
  .kb-node-icon{flex:0 0 auto}
  .kb-node-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* "All boards" root pseudo-node + the drag-only "move to top level" strip. */
  .kb-root-drop{margin:6px 0;padding:7px 6px;border:1px dashed #cbd5e1;border-radius:6px;color:#64748b;
    font-size:0.82rem;text-align:center;display:none}
  .kb-side.dragging-on .kb-root-drop{display:block}
  .kb-side.dragging-on .kb-root-drop.kb-drop-into{outline:2px dashed #2563eb;background:#eff6ff}
  /* Folder-contents detail table (shown in the main area when a folder is
     selected, instead of the board canvas). */
  .kb-folder-table{width:100%;border-collapse:collapse;font-size:0.88rem}
  .kb-folder-table th{text-align:left;color:#6b7280;font-weight:600;font-size:0.78rem;
    text-transform:uppercase;letter-spacing:0.03em;padding:6px 8px;border-bottom:1px solid #e5e7eb}
  .kb-folder-table td{padding:6px 8px;border-bottom:1px solid #f1f5f9}
  .kb-folder-table tr:hover td{background:#f8fafc}
  .kb-ft-name{display:flex;align-items:center;gap:6px}
  .kb-ft-link{color:#2563eb;cursor:pointer;background:none;border:none;font:inherit;padding:0}
  .kb-ft-link:hover{text-decoration:underline}
  /* The two main panes (board canvas vs folder table) toggle via `hidden`;
     this makes the bare attribute win even though panes set display. */
  .kb-main [hidden]{display:none}
```

- [ ] **Step 2: Update the HTML shell**

In `webapp/kanban_views.py`, replace the `<aside class="kb-side">` block:

```html
<aside class="kb-side">
  <div class="kb-side-head">
    <button onclick="kbNewBoard()">+ Board</button>
  </div>
  <ul id="kb-board-list" class="kb-board-list"></ul>
</aside>
```

with:

```html
<aside class="kb-side" id="kb-side">
  <div class="kb-side-head">
    <button onclick="kbNewBoard()">+ Board</button>
    <button class="kb-secondary" onclick="kbNewFolder()">+ Folder</button>
  </div>
  <div id="kb-tree-root" class="kb-tree-list"></div>
  <div id="kb-root-drop" class="kb-root-drop">Move to top level</div>
</aside>
```

In the same file, replace the `<section class="kb-main">` block:

```html
<section class="kb-main">
  <div id="kb-empty" class="muted">No boards yet &mdash; create one with &ldquo;+ Board&rdquo;.</div>
  <div id="kb-board" hidden>
```

with (adds the folder-contents pane; the existing `#kb-board` block stays):

```html
<section class="kb-main">
  <div id="kb-empty" class="muted">No boards yet &mdash; create one with &ldquo;+ Board&rdquo;.</div>
  <div id="kb-folder-view" hidden>
    <div class="kb-board-head">
      <span id="kb-folder-view-name" class="kb-board-title"></span>
    </div>
    <div id="kb-folder-view-body"></div>
  </div>
  <div id="kb-board" hidden>
```

Finally, add the folder create/rename modal next to the board modal (after the `<!-- Board create/edit -->` modal block):

```html
<!-- Folder create/rename -->
<div id="kb-folder-modal" class="ui-modal" hidden>
  <h3 id="kb-folder-modal-title">New folder</h3>
  <div class="kb-row">
    <label style="width:100%">Name <input type="text" id="kb-f-name" autocomplete="off" placeholder="folder name"></label>
  </div>
  <span class="err" id="kb-f-err"></span>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="kbCloseModals()">Cancel</button>
    <button id="kb-f-save" class="btn-primary" onclick="kbSaveFolderModal()">Create folder</button>
  </div>
</div>
```

- [ ] **Step 3: Verify the page still loads (JS not updated yet — expect a broken tree, but no 500)**

Run the app and load `/kanban`. (Use the `run` skill / project launch.) Expected: the page renders the shell without a server error. The tree will be empty until Task 8 wires the JS. This step only confirms the template has no syntax error.

- [ ] **Step 4: Commit**

```bash
git add webapp/kanban_views.py
git commit -m "feat(kanban): tree CSS + HTML shell (tree root, folder pane, modals)"
```

---

## Task 8: Frontend JS — tree state, hydrate, render, selection, expand persistence

**Files:**
- Modify: `static/kanban.js`

This replaces the flat `kbIndex` model and its rendering/selection with the tree. The save chain, board canvas, task modal, and serialization code from the existing file are reused unchanged.

- [ ] **Step 1: Replace the state block + index loader**

In `static/kanban.js`, replace the `// ---- state ----` block (the `let kbIndex = [];` through `let kbDrag = null;` lines):

```javascript
let kbIndex = [];          // sidebar: [{uuid, name, taskCount}]
let kbCurrent = null;      // the loaded board payload (see shape above)
let kbSelected = null;     // selected board uuid
let kbEditingTask = null;  // task uuid while the task modal edits (null = create)
let kbModalColumn = null;  // column uuid the task modal creates into
let kbEditingBoard = false;// board modal mode: false = create, true = edit selected
let kbDrag = null;         // task uuid while a card is dragged
```

with:

```javascript
// Tree state: two flat arrays + parent/folder pointers (the left-panel-tree
// pattern). Children are computed on demand by filtering.
let kbFolders = [];        // [{uuid, name, description, parentId, position}]
let kbBoards = [];         // [{uuid, name, folderId, position, taskCount}]
let kbTreeVersion = '';    // optimistic-concurrency token for the tree PUT
let kbCurrent = null;      // the loaded board payload (board CONTENTS)
let kbSelected = null;     // selected board uuid (null when a folder is selected)
let kbSelectedFolder = null; // selected folder uuid, or 'all' (root node), or null
let kbEditingTask = null;  // task uuid while the task modal edits (null = create)
let kbModalColumn = null;  // column uuid the task modal creates into
let kbEditingBoard = false;// board modal mode: false = create, true = edit selected
let kbDrag = null;         // task uuid while a card is dragged (in-board)
let kbDragTree = null;     // {type:'folder'|'board', id} while a tree node is dragged
let kbFolderModalParent = null; // parent for a new subfolder (null = root)
let kbRenamingFolder = null;    // folder uuid while the folder modal renames (null = create)

// Expand/collapse state, default-expanded, persisted to localStorage.
const KB_EXPAND_KEY = 'kanban.expandedFolders';
let kbExpanded = {};       // folderId -> false when collapsed
try {
  const saved = JSON.parse(localStorage.getItem(KB_EXPAND_KEY) || '{}');
  if (saved && typeof saved === 'object') kbExpanded = saved;
} catch (e) { /* storage unavailable: default expanded */ }
function kbSaveExpanded(){
  try { localStorage.setItem(KB_EXPAND_KEY, JSON.stringify(kbExpanded)); } catch (e) {}
}
const kbIsExpanded = (id) => kbExpanded[id] !== false;
const kbChildFolders = (parentId) => kbFolders
  .filter(f => (f.parentId || null) === parentId)
  .sort((a, b) => a.position - b.position);
const kbBoardsInFolder = (id) => kbBoards
  .filter(b => (b.folderId || null) === id)
  .sort((a, b) => a.position - b.position);
const kbFolderById = (id) => kbFolders.find(f => f.uuid === id) || null;
const kbFolderHasChildren = (id) =>
  kbChildFolders(id).length > 0 || kbBoardsInFolder(id).length > 0;
```

- [ ] **Step 2: Replace `kbLoadIndex` with a tree hydrate**

Replace the `kbLoadIndex` function:

```javascript
async function kbLoadIndex(){
  try {
    const r = await fetch('/kanban/api/boards');
    const data = await r.json();
    kbIndex = (data && data.boards) || [];
  } catch (e) { kbIndex = []; }
}
```

with:

```javascript
async function kbLoadIndex(){
  // Hydrate the whole tree (folders + board placement + counts + version).
  try {
    const r = await fetch('/kanban/api/tree');
    const data = await r.json();
    kbFolders = (data && data.folders) || [];
    kbBoards = (data && data.boards) || [];
    kbTreeVersion = (data && data.version) || '';
  } catch (e) { kbFolders = []; kbBoards = []; kbTreeVersion = ''; }
}
```

- [ ] **Step 3: Add a debounced tree save (mirrors the board save chain)**

Immediately after `kbLoadIndex`, add:

```javascript
// Debounced, serialized tree PUT — same shape as the board kbSave chain. On
// 409 or network failure we re-hydrate so the client converges to server truth.
let kbTreeSaveTimer = null;
let kbTreeSaveChain = Promise.resolve();
function kbSaveTree(){
  clearTimeout(kbTreeSaveTimer);
  kbTreeSaveTimer = setTimeout(kbTreeSavePush, 250);
}
function kbTreeSavePush(){
  clearTimeout(kbTreeSaveTimer);
  kbTreeSaveTimer = null;
  kbTreeSaveChain = kbTreeSaveChain.then(kbDoSaveTree);
  return kbTreeSaveChain;
}
async function kbDoSaveTree(){
  const body = {
    folders: kbFolders.map(f => ({uuid: f.uuid, name: f.name,
      description: f.description || '', parentId: f.parentId || null})),
    boards: kbBoards.map(b => ({uuid: b.uuid, folderId: b.folderId || null})),
    version: kbTreeVersion,
  };
  try {
    const r = await fetch('/kanban/api/tree', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      await kbLoadIndex();
      kbRenderTree();
      kbToast('Tree changed elsewhere — reloaded.');
    } else if (!r.ok){
      kbToast('Tree save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      kbTreeVersion = (j && j.version) || kbTreeVersion;
    }
  } catch (e) { /* network error: next edit retries */ }
}
```

- [ ] **Step 4: Replace `kbRefreshIndexCounts` to update the tree's board entry**

Replace:

```javascript
function kbRefreshIndexCounts(board){
  if (!board) return;
  const entry = kbIndex.find(b => b.uuid === board.uuid);
  if (entry){
    entry.name = board.name;
    entry.taskCount = board.tasks.length;
    kbRenderBoardList();
  }
}
```

with:

```javascript
// Keep the tree's board entry (name + count) in step with a board-contents
// save without a full refetch — these fields are NOT in the tree version, so
// they're synced here client-side.
function kbRefreshIndexCounts(board){
  board = board || kbCurrent;
  if (!board) return;
  const entry = kbBoards.find(b => b.uuid === board.uuid);
  if (entry){
    entry.name = board.name;
    entry.taskCount = board.tasks.length;
    kbRenderTree();
  }
}
```

- [ ] **Step 5: Replace `kbRenderBoardList` with the recursive tree renderer**

Replace the whole `kbRenderBoardList` function:

```javascript
function kbRenderBoardList(){
  const ul = document.getElementById('kb-board-list');
  ul.innerHTML = '';
  kbIndex.forEach(b => {
    const li = document.createElement('li');
    li.className = 'kb-board-item' + (b.uuid === kbSelected ? ' sel' : '');
    const name = document.createElement('span');
    name.className = 'kb-board-name';
    name.textContent = (b.name || '(unnamed board)') + ' (' + b.taskCount + ')';
    li.title = b.name;
    li.appendChild(name);
    li.addEventListener('click', () => kbSelectBoard(b.uuid));
    // The kebab is only visible (CSS) while this item is selected, so its
    // actions always target the loaded board.
    kbMakeKebab(li, [
      ['Duplicate', '', () => kbDuplicateBoard(b.uuid)],
      ['Delete', 'danger', () => kbConfirmDeleteBoard()],
    ]);
    ul.appendChild(li);
  });
}
```

with:

```javascript
// Recursive tree: an "All boards" root node, then top-level folders, then
// unfiled boards. Folders emit a node row and (when expanded + non-empty) a
// nested <ul> of child folders followed by their boards.
function kbRenderTree(){ kbRenderBoardList(); }  // alias kept for old callers
function kbRenderBoardList(){
  const root = document.getElementById('kb-tree-root');
  root.innerHTML = '';
  const ul = document.createElement('ul');
  ul.appendChild(kbAllBoardsNode());
  kbChildFolders(null).forEach(f => ul.appendChild(kbFolderLi(f)));
  kbBoardsInFolder(null).forEach(b => ul.appendChild(kbBoardNode(b)));
  root.appendChild(ul);
  kbWireRootDrop();
}

function kbAllBoardsNode(){
  const li = document.createElement('li');
  const node = document.createElement('div');
  node.className = 'kb-node' + (kbSelectedFolder === 'all' ? ' sel' : '');
  node.innerHTML = '<span class="kb-twisty"></span>' +
    '<span class="kb-node-icon">🗂️</span>' +
    '<span class="kb-node-name">All boards</span>';
  node.addEventListener('click', () => kbSelectFolder('all'));
  li.appendChild(node);
  return li;
}

function kbFolderLi(f){
  const li = document.createElement('li');
  const node = document.createElement('div');
  const expanded = kbIsExpanded(f.uuid);
  const hasKids = kbFolderHasChildren(f.uuid);
  node.className = 'kb-node' + (f.uuid === kbSelectedFolder ? ' sel' : '');
  node.draggable = true;
  node.innerHTML =
    '<span class="kb-twisty">' + (hasKids ? (expanded ? '▾' : '▸') : '') + '</span>' +
    '<span class="kb-node-icon">' + (expanded && hasKids ? '📂' : '📁') + '</span>' +
    '<span class="kb-node-name"></span>';
  node.querySelector('.kb-node-name').textContent = f.name || '(unnamed)';
  node.title = f.name;
  node.addEventListener('click', () => kbFolderClick(f.uuid));
  kbMakeKebab(node, [
    ['Rename', '', () => kbRenameFolder(f.uuid)],
    ['New subfolder', '', () => kbNewFolder(f.uuid)],
    ['Delete', 'danger', () => kbConfirmDeleteFolder(f.uuid)],
  ]);
  kbWireFolderDrag(node, f.uuid);
  li.appendChild(node);
  if (expanded && hasKids){
    const sub = document.createElement('ul');
    kbChildFolders(f.uuid).forEach(c => sub.appendChild(kbFolderLi(c)));
    kbBoardsInFolder(f.uuid).forEach(b => sub.appendChild(kbBoardNode(b)));
    li.appendChild(sub);
  }
  return li;
}

function kbBoardNode(b){
  const li = document.createElement('li');
  const node = document.createElement('div');
  node.className = 'kb-node' + (b.uuid === kbSelected ? ' sel' : '');
  node.draggable = true;
  node.innerHTML =
    '<span class="kb-twisty"></span>' +
    '<span class="kb-node-icon">📋</span>' +
    '<span class="kb-node-name"></span>';
  node.querySelector('.kb-node-name').textContent =
    (b.name || '(unnamed board)') + ' (' + b.taskCount + ')';
  node.title = b.name;
  node.addEventListener('click', () => kbSelectBoard(b.uuid));
  kbMakeKebab(node, [
    ['Duplicate', '', () => kbDuplicateBoard(b.uuid)],
    ['Delete', 'danger', () => kbConfirmDeleteBoard(b.uuid)],
  ]);
  kbWireBoardDrag(node, b.uuid);
  li.appendChild(node);
  return li;
}
```

- [ ] **Step 6: Add folder selection + click handlers**

After `kbBoardNode`, add:

```javascript
// Folder click: select-first, then toggle-expand on a second click of the
// already-selected folder (the chat/cron behavior).
function kbFolderClick(folderId){
  if (kbSelectedFolder === folderId){
    kbExpanded[folderId] = !kbIsExpanded(folderId);
    kbSaveExpanded();
    kbRenderTree();
    return;
  }
  kbSelectFolder(folderId);
}
function kbSelectFolder(folderId){
  kbFlushSave();                 // persist any board edit in the debounce window
  kbSelectedFolder = folderId;   // 'all' or a uuid
  kbSelected = null;             // folder-selected and board-open are exclusive
  kbCurrent = null;
  kbRenderTree();
  kbRenderFolderView();
  kbRenderBoard();               // hides the board canvas
  kbSyncUrl();
}
```

- [ ] **Step 7: Update `kbRenderBoard` to coordinate with the folder pane**

In `kbRenderBoard`, replace the first two lines:

```javascript
function kbRenderBoard(){
  const board = kbCurrent;
  document.getElementById('kb-empty').hidden = !!board;
  document.getElementById('kb-board').hidden = !board;
```

with:

```javascript
function kbRenderBoard(){
  const board = kbCurrent;
  const folderShown = !board && kbSelectedFolder !== null;
  document.getElementById('kb-empty').hidden = !!board || folderShown;
  document.getElementById('kb-board').hidden = !board;
  document.getElementById('kb-folder-view').hidden = !folderShown;
```

- [ ] **Step 8: Point `kbRender` at the tree and clear `kbSelected` on folder select**

Replace:

```javascript
function kbRender(){
  kbRenderBoardList();
  kbRenderBoard();
  kbSyncUrl();
}
```

with:

```javascript
function kbRender(){
  kbRenderTree();
  kbRenderBoard();
  kbRenderFolderView();
  kbSyncUrl();
}
```

In `kbSelectBoard`, clear the folder selection. Replace:

```javascript
async function kbSelectBoard(uuid){
  kbFlushSave();  // an edit inside the debounce window must not be dropped
  kbSelected = uuid;
  kbCurrent = await kbLoadBoard(uuid);
  if (!kbCurrent){ kbSelected = null; kbToast('Board could not be loaded.'); }
  kbRender();
}
```

with:

```javascript
async function kbSelectBoard(uuid){
  kbFlushSave();  // an edit inside the debounce window must not be dropped
  kbSelected = uuid;
  kbSelectedFolder = null;  // board-open and folder-selected are exclusive
  kbCurrent = await kbLoadBoard(uuid);
  if (!kbCurrent){ kbSelected = null; kbToast('Board could not be loaded.'); }
  kbRender();
}
```

- [ ] **Step 9: Update `kbSyncUrl` for the folder param**

Replace:

```javascript
function kbSyncUrl(){
  const url = new URL(window.location);
  if (kbSelected) url.searchParams.set('board', kbSelected);
  else url.searchParams.delete('board');
  history.replaceState(null, '', url);
}
```

with:

```javascript
function kbSyncUrl(){
  const url = new URL(window.location);
  url.searchParams.delete('board');
  url.searchParams.delete('folder');
  if (kbSelected) url.searchParams.set('board', kbSelected);
  else if (kbSelectedFolder) url.searchParams.set('folder', kbSelectedFolder);
  history.replaceState(null, '', url);
}
```

- [ ] **Step 10: Add stub render/drag/folder-view functions so the file parses**

The functions referenced above but defined in Tasks 9–10 (`kbRenderFolderView`, `kbWireFolderDrag`, `kbWireBoardDrag`, `kbWireRootDrop`, `kbNewFolder`, `kbRenameFolder`, `kbSaveFolderModal`, `kbConfirmDeleteFolder`) must exist for the page to load. Add temporary stubs at the end of the file (each replaced with the real body in the next tasks):

```javascript
// ---- placeholders filled in by later tasks ----
function kbRenderFolderView(){}
function kbWireFolderDrag(node, folderId){}
function kbWireBoardDrag(node, boardId){}
function kbWireRootDrop(){}
function kbNewFolder(parentId){}
function kbRenameFolder(folderId){}
function kbSaveFolderModal(){}
function kbConfirmDeleteFolder(folderId){}
```

- [ ] **Step 11: Update init to restore folder deep-link**

Replace the `kbInit` IIFE:

```javascript
(async function kbInit(){
  await kbLoadIndex();
  const want = new URLSearchParams(window.location.search).get('board');
  const first = (want && kbIndex.some(b => b.uuid === want)) ? want
              : (kbIndex.length ? kbIndex[0].uuid : null);
  if (first) await kbSelectBoard(first);
  else kbRender();
})();
```

with:

```javascript
(async function kbInit(){
  await kbLoadIndex();
  const params = new URLSearchParams(window.location.search);
  const wantBoard = params.get('board');
  const wantFolder = params.get('folder');
  if (wantBoard && kbBoards.some(b => b.uuid === wantBoard)){
    await kbSelectBoard(wantBoard);
  } else if (wantFolder === 'all' ||
             (wantFolder && kbFolders.some(f => f.uuid === wantFolder))){
    kbSelectFolder(wantFolder);
  } else if (kbBoards.length){
    await kbSelectBoard(kbBoards[0].uuid);
  } else {
    kbRender();
  }
})();
```

- [ ] **Step 12: Update board create/delete/duplicate to refresh the tree**

`kbDuplicateBoard`, `kbConfirmDeleteBoard`, and `kbSaveBoardModal` call `kbLoadIndex()` then `kbRender()` — those now hydrate/render the tree, so they work unchanged. But `kbConfirmDeleteBoard` is called both from the board header (no arg) and the tree kebab (with a uuid). Replace its signature line:

```javascript
function kbConfirmDeleteBoard(){
  if (!kbCurrent) return;
  const board = kbCurrent;
```

with:

```javascript
function kbConfirmDeleteBoard(uuid){
  // From the board header (no arg) or a tree kebab (uuid). Resolve to a board:
  // prefer the explicit uuid, else the loaded board.
  const board = uuid
    ? (kbBoards.find(b => b.uuid === uuid) || kbCurrent)
    : kbCurrent;
  if (!board) return;
```

Then, since the tree entry has `taskCount` (not `tasks`), replace inside that function the confirm text line:

```javascript
    '“' + board.name + '” and its ' + board.tasks.length + ' task(s) will be deleted.',
```

with:

```javascript
    '“' + board.name + '” and its ' +
      (board.tasks ? board.tasks.length : board.taskCount) + ' task(s) will be deleted.',
```

- [ ] **Step 13: Manual browser verification**

Run the app and load `/kanban`. Verify:
- The tree shows "All boards", existing boards appear (as unfiled, top level), counts shown.
- Clicking a board opens its canvas (unchanged behavior); `?board=` appears in the URL.
- Clicking "All boards" selects it and the folder pane area toggles (empty body until Task 9); `?folder=all` in URL.
- No console errors. Board create/duplicate/delete still work and refresh the tree.

- [ ] **Step 14: Commit**

```bash
git add static/kanban.js
git commit -m "feat(kanban): tree state, hydrate, recursive render, selection, deep-link"
```

---

## Task 9: Frontend JS — folder modals + folder-contents detail pane

**Files:**
- Modify: `static/kanban.js` (replace the Task-8 stubs for folder modals + `kbRenderFolderView`)

- [ ] **Step 1: Implement the folder create/rename modal**

Replace the stubs `kbNewFolder`, `kbRenameFolder`, `kbSaveFolderModal`:

```javascript
function kbNewFolder(parentId){}
function kbRenameFolder(folderId){}
function kbSaveFolderModal(){}
```

with:

```javascript
// Folder create + rename share one modal; kbRenamingFolder picks the mode.
function kbNewFolder(parentId){
  kbRenamingFolder = null;
  kbFolderModalParent = (typeof parentId === 'string') ? parentId : null;
  document.getElementById('kb-folder-modal-title').textContent =
    kbFolderModalParent ? 'New subfolder' : 'New folder';
  document.getElementById('kb-f-name').value = '';
  document.getElementById('kb-f-save').textContent = 'Create folder';
  document.getElementById('kb-f-err').textContent = '';
  kbOpenModal('kb-folder-modal');
  document.getElementById('kb-f-name').focus();
}
function kbRenameFolder(folderId){
  const f = kbFolderById(folderId);
  if (!f) return;
  kbRenamingFolder = folderId;
  document.getElementById('kb-folder-modal-title').textContent = 'Rename folder';
  document.getElementById('kb-f-name').value = f.name;
  document.getElementById('kb-f-save').textContent = 'Save';
  document.getElementById('kb-f-err').textContent = '';
  kbOpenModal('kb-folder-modal');
  document.getElementById('kb-f-name').focus();
}
async function kbSaveFolderModal(){
  const name = document.getElementById('kb-f-name').value.trim();
  if (!name){
    document.getElementById('kb-f-err').textContent = 'Name is required.'; return;
  }
  if (kbRenamingFolder){
    const f = kbFolderById(kbRenamingFolder);
    if (f){ f.name = name; kbSaveTree(); }
    kbCloseModals();
    kbRenderTree();
    if (kbSelectedFolder) kbRenderFolderView();
    return;
  }
  // Create server-side (the server assigns the uuid + position).
  try {
    const r = await fetch('/kanban/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, parentId: kbFolderModalParent}),
    });
    const j = await r.json();
    if (!r.ok || !j.ok){
      document.getElementById('kb-f-err').textContent = (j && j.error) || 'Create failed.';
      return;
    }
    kbCloseModals();
    if (kbFolderModalParent){ kbExpanded[kbFolderModalParent] = true; kbSaveExpanded(); }
    await kbLoadIndex();   // re-hydrate so the new folder + fresh version land
    kbRenderTree();
  } catch (e) {
    document.getElementById('kb-f-err').textContent = 'Network error.';
  }
}
```

- [ ] **Step 2: Implement folder delete (reparenting confirm)**

Replace the stub `function kbConfirmDeleteFolder(folderId){}` with:

```javascript
function kbConfirmDeleteFolder(folderId){
  const f = kbFolderById(folderId);
  if (!f) return;
  kbConfirm('Delete folder?',
    '“' + f.name + '” will be deleted. Any boards and subfolders inside it ' +
    'move up one level — boards and their tasks are NOT deleted.',
    async () => {
      const r = await fetch('/kanban/api/folders/' + encodeURIComponent(folderId),
                            {method: 'DELETE'}).catch(() => null);
      if (!r || !r.ok){ kbToast('Delete failed.'); return; }
      if (kbSelectedFolder === folderId) kbSelectedFolder = null;
      await kbLoadIndex();
      kbRender();
    });
}
```

Note: `kbConfirm`'s yes-button uses the danger class `kb-confirm-yes` labeled "Delete" — fine for a reparenting delete (the wording clarifies boards survive).

- [ ] **Step 3: Implement the folder-contents detail table**

Replace the stub `function kbRenderFolderView(){}` with:

```javascript
// Depth-first flatten of a folder's subtree → [{kind:'folder'|'board', node, depth}].
// 'all' flattens from the root (parentId/folderId === null).
function kbFlattenTree(rootId){
  const out = [];
  const walk = (parentId, depth) => {
    kbChildFolders(parentId).forEach(f => {
      out.push({kind: 'folder', node: f, depth});
      walk(f.uuid, depth + 1);
    });
    kbBoardsInFolder(parentId).forEach(b =>
      out.push({kind: 'board', node: b, depth}));
  };
  walk(rootId === 'all' ? null : rootId, 0);
  return out;
}
function kbRenderFolderView(){
  if (kbSelectedFolder === null) return;
  const f = kbSelectedFolder === 'all' ? null : kbFolderById(kbSelectedFolder);
  document.getElementById('kb-folder-view-name').textContent =
    kbSelectedFolder === 'all' ? 'All boards' : (f ? f.name : '(folder)');
  const body = document.getElementById('kb-folder-view-body');
  const rows = kbFlattenTree(kbSelectedFolder);
  if (!rows.length){
    body.innerHTML = '<div class="muted">This folder is empty.</div>';
    return;
  }
  const table = document.createElement('table');
  table.className = 'kb-folder-table';
  table.innerHTML =
    '<thead><tr><th>Name</th><th>Tasks</th><th></th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector('tbody');
  rows.forEach(({kind, node, depth}) => {
    const tr = document.createElement('tr');
    const nameTd = document.createElement('td');
    nameTd.style.paddingLeft = (8 + depth * 18) + 'px';
    const nameWrap = document.createElement('span');
    nameWrap.className = 'kb-ft-name';
    nameWrap.innerHTML = (kind === 'folder' ? '📁 ' : '📋 ');
    const label = document.createElement('span');
    label.textContent = node.name || (kind === 'folder' ? '(unnamed)' : '(unnamed board)');
    nameWrap.appendChild(label);
    nameTd.appendChild(nameWrap);
    const countTd = document.createElement('td');
    countTd.textContent = kind === 'board' ? node.taskCount : '';
    const linkTd = document.createElement('td');
    const link = document.createElement('button');
    link.className = 'kb-ft-link';
    link.textContent = kind === 'folder' ? 'Open folder' : 'Open board';
    link.addEventListener('click', () =>
      kind === 'folder' ? kbSelectFolder(node.uuid) : kbSelectBoard(node.uuid));
    linkTd.appendChild(link);
    tr.appendChild(nameTd); tr.appendChild(countTd); tr.appendChild(linkTd);
    tbody.appendChild(tr);
  });
  body.replaceChildren(table);
}
```

- [ ] **Step 4: Add folder modal to the close + dirty-dismiss lists**

In `kbCloseModals`, add the folder modal id. Replace:

```javascript
  ['kb-board-modal','kb-task-modal','kb-md-modal','kb-confirm-modal'].forEach(id =>
    document.getElementById(id).hidden = true);
```

with:

```javascript
  ['kb-board-modal','kb-folder-modal','kb-task-modal','kb-md-modal','kb-confirm-modal']
    .forEach(id => document.getElementById(id).hidden = true);
```

(The folder modal is a single short field; the existing `kbModalDirty` returns false for it, so Escape/backdrop just close it — acceptable.)

- [ ] **Step 5: Manual browser verification**

Reload `/kanban`. Verify:
- "+ Folder" creates a top-level folder; it appears in the tree.
- A folder kebab → New subfolder creates a nested folder (parent auto-expands).
- Rename updates the name live; Delete shows the reparenting confirm and, on confirm, boards inside move up (not deleted).
- Selecting a folder (or "All boards") shows the contents table with depth indentation; "Open board" opens the canvas, "Open folder" drills in.

- [ ] **Step 6: Commit**

```bash
git add static/kanban.js
git commit -m "feat(kanban): folder modals + folder-contents detail pane"
```

---

## Task 10: Frontend JS — drag-and-drop (folder 3-zone, board 2-zone, root strip)

**Files:**
- Modify: `static/kanban.js` (replace the Task-8 drag stubs)

- [ ] **Step 1: Implement the cycle guard + reorder helpers**

Replace the stub `function kbWireRootDrop(){}` (keep its name) and add helpers — replace:

```javascript
function kbWireFolderDrag(node, folderId){}
function kbWireBoardDrag(node, boardId){}
function kbWireRootDrop(){}
```

with:

```javascript
// True if `rootId` is `candidateId` or lives anywhere in its subtree — used to
// refuse nesting a folder inside itself/its descendants (the DB validator
// enforces it again server-side).
function kbFolderInSubtree(candidateId, rootId){
  if (candidateId === rootId) return true;
  return kbChildFolders(rootId).some(c => kbFolderInSubtree(candidateId, c.uuid));
}
// Reorder `arr` so `movedId` sits just before `beforeId` (or at end if null),
// among siblings sharing the same (already-updated) parent/folder pointer.
function kbReorderSiblings(arr, keyOf, movedId, beforeId){
  const moved = arr.find(x => x.uuid === movedId);
  if (!moved) return;
  const rest = arr.filter(x => x.uuid !== movedId);
  let idx = rest.length;
  if (beforeId){
    const i = rest.findIndex(x => x.uuid === beforeId);
    if (i >= 0) idx = i;
  }
  rest.splice(idx, 0, moved);
  // Renumber positions within each parent group from the new array order.
  const counters = {};
  rest.forEach(x => {
    const k = keyOf(x) || 'root';
    counters[k] = (counters[k] || 0);
    x.position = counters[k]++;
  });
  arr.length = 0; arr.push(...rest);
}

function kbWireBoardDrag(node, boardId){
  node.addEventListener('dragstart', e => {
    kbDragTree = {type: 'board', id: boardId};
    e.dataTransfer.effectAllowed = 'move';
    e.stopPropagation();
    document.getElementById('kb-side').classList.add('dragging-on');
  });
  node.addEventListener('dragend', () => kbEndTreeDrag());
  // A board node is a 2-zone (before/after) reorder target within its folder.
  node.addEventListener('dragover', e => {
    if (!kbDragTree) return;
    e.preventDefault(); e.stopPropagation();
    const after = kbPointerAfter(node, e);
    node.classList.toggle('kb-drop-after', after);
    node.classList.toggle('kb-drop-before', !after);
  });
  node.addEventListener('dragleave', () =>
    node.classList.remove('kb-drop-before', 'kb-drop-after'));
  node.addEventListener('drop', e => {
    if (!kbDragTree) return;
    e.preventDefault(); e.stopPropagation();
    const after = kbPointerAfter(node, e);
    node.classList.remove('kb-drop-before', 'kb-drop-after');
    const target = kbBoards.find(b => b.uuid === boardId);
    kbDropAsSibling(target ? (target.folderId || null) : null, boardId, after);
  });
}

function kbWireFolderDrag(node, folderId){
  node.addEventListener('dragstart', e => {
    kbDragTree = {type: 'folder', id: folderId};
    e.dataTransfer.effectAllowed = 'move';
    e.stopPropagation();
    document.getElementById('kb-side').classList.add('dragging-on');
  });
  node.addEventListener('dragend', () => kbEndTreeDrag());
  // A folder node is a 3-zone target: top=before, bottom=after, middle=into.
  node.addEventListener('dragover', e => {
    if (!kbDragTree) return;
    e.preventDefault(); e.stopPropagation();
    const zone = kbFolderZone(node, e);
    node.classList.toggle('kb-drop-before', zone === 'before');
    node.classList.toggle('kb-drop-after', zone === 'after');
    node.classList.toggle('kb-drop-into', zone === 'into');
  });
  node.addEventListener('dragleave', () =>
    node.classList.remove('kb-drop-before', 'kb-drop-after', 'kb-drop-into'));
  node.addEventListener('drop', e => {
    if (!kbDragTree) return;
    e.preventDefault(); e.stopPropagation();
    const zone = kbFolderZone(node, e);
    node.classList.remove('kb-drop-before', 'kb-drop-after', 'kb-drop-into');
    if (zone === 'into') kbDropInto(folderId);
    else {
      const target = kbFolderById(folderId);
      kbDropAsSibling(target ? (target.parentId || null) : null, folderId, zone === 'after');
    }
  });
}

function kbWireRootDrop(){
  const strip = document.getElementById('kb-root-drop');
  if (!strip) return;
  strip.ondragover = e => { if (kbDragTree){ e.preventDefault(); strip.classList.add('kb-drop-into'); } };
  strip.ondragleave = () => strip.classList.remove('kb-drop-into');
  strip.ondrop = e => {
    if (!kbDragTree) return;
    e.preventDefault();
    strip.classList.remove('kb-drop-into');
    kbDropInto(null);   // move to top level
  };
}

// ---- drag geometry + apply ----
function kbPointerAfter(node, e){
  const r = node.getBoundingClientRect();
  return (e.clientY - r.top) > r.height / 2;
}
function kbFolderZone(node, e){
  const r = node.getBoundingClientRect();
  const y = e.clientY - r.top;
  if (y < r.height / 3) return 'before';
  if (y > r.height * 2 / 3) return 'after';
  return 'into';
}
function kbEndTreeDrag(){
  kbDragTree = null;
  document.getElementById('kb-side').classList.remove('dragging-on');
  document.querySelectorAll('.kb-drop-before, .kb-drop-after, .kb-drop-into')
    .forEach(x => x.classList.remove('kb-drop-before', 'kb-drop-after', 'kb-drop-into'));
}
// Nest the dragged node INTO folder `destId` (null = top level).
function kbDropInto(destId){
  const d = kbDragTree;
  if (!d) return;
  if (d.type === 'folder'){
    if (destId && kbFolderInSubtree(destId, d.id)){
      kbToast('Cannot move a folder into itself.'); return;
    }
    const f = kbFolderById(d.id);
    if (f) f.parentId = destId;
    kbReorderSiblings(kbFolders, x => x.parentId, d.id, null);
  } else {
    const b = kbBoards.find(x => x.uuid === d.id);
    if (b) b.folderId = destId;
    kbReorderSiblings(kbBoards, x => x.folderId, d.id, null);
  }
  if (destId){ kbExpanded[destId] = true; kbSaveExpanded(); }
  kbSaveTree();
  kbRenderTree();
}
// Place the dragged node as a sibling before/after `siblingId` under `parentId`.
function kbDropAsSibling(parentId, siblingId, after){
  const d = kbDragTree;
  if (!d || d.id === siblingId) return;
  if (d.type === 'folder'){
    if (parentId && kbFolderInSubtree(parentId, d.id)){
      kbToast('Cannot move a folder into itself.'); return;
    }
    const f = kbFolderById(d.id);
    if (f) f.parentId = parentId;
    const ordered = kbChildFolders(parentId).map(x => x.uuid);
    kbReorderSiblings(kbFolders, x => x.parentId, d.id, kbNeighbor(ordered, siblingId, after));
  } else {
    const b = kbBoards.find(x => x.uuid === d.id);
    if (b) b.folderId = parentId;
    const ordered = kbBoardsInFolder(parentId).map(x => x.uuid);
    kbReorderSiblings(kbBoards, x => x.folderId, d.id, kbNeighbor(ordered, siblingId, after));
  }
  kbSaveTree();
  kbRenderTree();
}
// The uuid to insert BEFORE so the moved node lands before/after `siblingId`.
function kbNeighbor(orderedIds, siblingId, after){
  if (!after) return siblingId;
  const i = orderedIds.indexOf(siblingId);
  return (i >= 0 && i + 1 < orderedIds.length) ? orderedIds[i + 1] : null;
}
```

- [ ] **Step 2: Manual browser verification**

Reload `/kanban`. Verify:
- Dragging a board onto a folder's **middle** files it into that folder (folder auto-expands); the tree persists across reload.
- Dragging a board onto another board's top/bottom half reorders it before/after within the folder.
- Dragging a folder onto another folder's top/bottom nests as sibling before/after; onto the middle nests as a child.
- The "Move to top level" strip appears while dragging; dropping on it un-files the node.
- Dragging a folder into its own descendant is refused with a toast.
- After each drop, a tree PUT fires (Network tab) and survives reload.

- [ ] **Step 3: Commit**

```bash
git add static/kanban.js
git commit -m "feat(kanban): tree drag-and-drop (folder 3-zone, board 2-zone, root strip)"
```

---

## Task 11: Final verification + docs

**Files:**
- Modify: `docs/left-panel-tree.md` (mark `/kanban` as a third implementation)

- [ ] **Step 1: Run the full backend suite**

Run: `python -m pytest webapp/test_kanban_api.py webapp/test_kanban_tree.py webapp/test_kanban_views.py -v`
Expected: all PASS.

- [ ] **Step 2: Run the broader suite for regressions**

Run: `python -m pytest db/ webapp/ -q`
Expected: no NEW failures versus the pre-existing baseline. (Note: `MEMORY.md` records some pre-existing failing tests unrelated to this work — confirm any failures match that baseline, not new breakage.)

- [ ] **Step 3: Full manual walkthrough**

Load `/kanban`, exercise end to end: create folders + subfolders; create a board into a selected folder (verify "+ Board" while a folder is selected lands it there — if not implemented, file via drag); drag boards/folders around; rename/delete folders; select "All boards" and folders to view the contents table; open boards from the table; reload and confirm the tree, expand state, and selection (`?board=`/`?folder=`) all restore. Confirm board contents save (columns/tasks) and the markdown/JSON serializations still work unchanged.

- [ ] **Step 4: Update the pattern doc**

In `docs/left-panel-tree.md`, update the intro so `/kanban` is listed as a built reference rather than a hypothetical. Replace:

```
`/chat` (folders → chatrooms) and
`/cron` (folders → jobs) both implement it; this doc describes the shared
pattern and the two reference implementations so a third page (e.g. `/kanban`:
folders → boards) can be built without re-deriving it.
```

with:

```
`/chat` (folders → chatrooms), `/cron` (folders → jobs), and `/kanban`
(folders → boards) all implement it; this doc describes the shared pattern and
the reference implementations. `/kanban` is the placement-only variant whose
tree layer (folders + board placement) is separate from board contents:
`webapp/kanban_views.py` (markup + CSS), `static/kanban.js` (tree JS),
`webapp/kanban_api.py` + `db/kanban.py` (`kanban_load_tree`/`kanban_save_tree`/
`kanban_tree_version`/`validate_kanban_tree`, folder create/reparenting-delete).
```

- [ ] **Step 5: Commit**

```bash
git add docs/left-panel-tree.md
git commit -m "docs(kanban): record the kanban tree as a third reference implementation"
```

---

## Self-Review notes (addressed)

- **Spec coverage:** DB table + column (T1), folder create/reparent-delete (T2), load+version with exclusion rule (T3), validate+placement-save (T4), create-board folderId (T5), all API routes (T6), CSS/HTML shell (T7), tree state/render/selection/deep-link/expand-persist (T8), folder modals + detail pane (T9), full drag-drop with cycle guard + root strip (T10), verification + doc (T11). All spec sections map to a task.
- **Type/name consistency:** tree functions are `kanban_load_tree`/`kanban_tree_version`/`validate_kanban_tree`/`kanban_save_tree`/`kanban_create_folder`/`kanban_delete_folder`; frontend state `kbFolders`/`kbBoards`/`kbExpanded`/`kbSelectedFolder`/`kbDragTree`/`kbTreeVersion`; render `kbRenderTree`/`kbFolderLi`/`kbBoardNode`/`kbRenderFolderView`/`kbFlattenTree`; DnD `kbWireFolderDrag`/`kbWireBoardDrag`/`kbWireRootDrop`/`kbFolderInSubtree`/`kbDropInto`/`kbDropAsSibling`. Used consistently across tasks.
- **Stub ordering:** Task 8 adds stubs for functions implemented in Tasks 9–10 so the file always parses and the page loads between tasks (incremental verification).
