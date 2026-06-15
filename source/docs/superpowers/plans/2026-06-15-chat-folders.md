# Chat Folders + Reordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the `/chat` left panel group chatrooms into nested folders and reorder rooms/folders by drag-and-drop, mirroring the existing `/cron` tree, with destructive folder deletion guarded by a count-aware type-to-confirm dialog.

**Architecture:** A new `chatroom_folder` table (`parent_uuid` for nesting, `position` for order) plus `folder_uuid` + `position` columns on `chatroom`. A version-guarded bulk `/chat/api/tree` GET/PUT (ported from `/cron/api/tree`) persists structure; the tree-save deliberately never creates or deletes rooms (only reparents/reorders them) so a truncated payload can't wipe message history. Folder deletion is a separate recursive DELETE endpoint with an authoritative count-preview. The frontend ports cron's HTML5 drag-drop tree into the inline chat template, reusing cron's folder/folder-open Lucide icons verbatim.

**Tech Stack:** Python 3.14, Flask, SQLAlchemy (declarative `Mapped` models), Postgres, vanilla JS (no framework), pytest. Schema changes via `db.create_all()` + `_add_column_if_missing()` in `db.init_db` (no Alembic).

---

## Reference: existing code this mirrors

- **Cron tree backend** (the template to port): `db/cron.py` — `cron_load_tree` (L56), `cron_tree_version` (L148), `validate_cron_tree` (L219), `cron_save_tree` (L319). Exception types `CronTreeError`/`CronTreeConflict` (L136/L142). `_to_uuid` helper (L196).
- **Cron tree API**: `webapp/cron_api.py` — `/cron/api/tree` GET/PUT (L22).
- **Chat backend**: `db/chat.py` — `list_chatrooms` (L168), `delete_chatroom` (L80, already cascades messages+members+workspace_shell_state via FK), `get_chatroom` (L66).
- **Models**: `db/models.py` — `Chatroom` (L495), `ChatroomFolder` template is `CronFolder` (L203), `ChatMessage` (L523).
- **Migration site**: `db/__init__.py` — `init_db` (L136), `_add_column_if_missing` (L112), `_column_exists` (L102).
- **Chat frontend**: `webapp/chat_template.py` — panel HTML (L149-164), CSS (L20-72), JS state (L212-221), `renderRooms` (L538), `buildRoomMenu` (L579), `deleteRoom` (L623), `selectRoom` (L655), `loadRooms` (L687).
- **Cron frontend (port source)**: `static/cron.js` — icons (L657-658), `cronFolderLi`/`cronRenderTree` (L962-1009), drag-drop (L1202-1425).
- **Test patterns**: `db/test_chat_membership.py` (`app_ctx` fixture), `webapp/test_cron_api.py` (test client via `app_ctx.test_client()`).

> **Frontend placement decision (from the spec's open decisions):** keep all tree JS **inline in `chat_template.py`**. The tree needs tight access to the existing inline `rooms`, `currentRoom`, `selectRoom`, `unread`, `renderRooms` state; splitting it into `static/chat.js` would fragment that state. Decision 2 (retire `GET /chat/api/rooms`): **keep it** — `chat_load_tree` reuses `list_chatrooms`, and the old endpoint stays as harmless back-compat. Decision 3 (server-side delete hardening): **none** — rely on the explicit DELETE + browser type-confirm.

---

## Task 1: Data model — `ChatroomFolder` table + `Chatroom` columns

**Files:**
- Modify: `db/models.py` (add `ChatroomFolder` after `Chatroom`, L504; add two columns to `Chatroom`, L495-503)

- [ ] **Step 1: Add the two new columns to `Chatroom`**

In `db/models.py`, edit the `Chatroom` class (currently L495-503) to add `folder_uuid` and `position`:

```python
class Chatroom(db.Model):
    __tablename__ = "chatroom"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column()  # chat_user.uuid (the human)
    # Left-panel folder placement (mirrors cron's folder tree). null = top level;
    # plain col, no FK (house style — app-side validation). `position` orders
    # rooms within their folder (or among top-level rooms).
    folder_uuid: Mapped[UUID | None] = mapped_column(default=None)
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
```

- [ ] **Step 2: Add the `ChatroomFolder` model**

Insert immediately after the `Chatroom` class (before `ChatroomMember` at L506):

```python
class ChatroomFolder(db.Model):
    """A left-panel folder grouping chatrooms (and other folders). Mirrors
    CronFolder minus the scheduling-only fields (description/enabled): chat
    folders are purely organizational. parent_uuid is a plain uuid column (no
    FK, app-side validation — the cron/kanban house style); null = root.
    position orders folders within their parent."""

    __tablename__ = "chatroom_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (Index("chatroom_folder_children", "parent_uuid", "position"),)
```

- [ ] **Step 3: Verify the models import cleanly**

Run: `DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude python -c "from db.models import ChatroomFolder, Chatroom; print(ChatroomFolder.__tablename__, [c.name for c in Chatroom.__table__.columns])"`
Expected: prints `chatroom_folder` and a column list including `folder_uuid` and `position`.

- [ ] **Step 4: Commit**

```bash
git add db/models.py
git commit -m "feat(db): ChatroomFolder model + folder_uuid/position on Chatroom"
```

---

## Task 2: Migration — create table, add columns, backfill positions

**Files:**
- Modify: `db/__init__.py` (add column migrations in `init_db` ~L249, add `_backfill_chatroom_positions` helper near `_add_column_if_missing`)
- Test: `db/test_chat_folders.py` (new)

- [ ] **Step 1: Write the failing test for the backfill helper**

Create `db/test_chat_folders.py`:

```python
"""Tests for chat folders: schema, tree load/validate/save, recursive delete.

Uses the live local Postgres (rainbox_claude via conftest). Each test cleans up
the rows it creates so artifacts don't accumulate.
"""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import Chatroom, ChatroomFolder


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


def test_backfill_orders_top_level_rooms_by_created_at(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    # Three rooms all at position 0 (the freshly-migrated state).
    r1 = db.create_chatroom(f"bf-1-{uuid4().hex[:6]}", human.uuid, [])
    r2 = db.create_chatroom(f"bf-2-{uuid4().hex[:6]}", human.uuid, [])
    r3 = db.create_chatroom(f"bf-3-{uuid4().hex[:6]}", human.uuid, [])
    mine = [r1.uuid, r2.uuid, r3.uuid]
    try:
        s.execute(sa.update(Chatroom).values(position=0))
        s.commit()
        db._backfill_chatroom_positions()
        rows = {r.uuid: r.position for r in s.execute(
            sa.select(Chatroom).where(Chatroom.uuid.in_(mine))).scalars()}
        # created_at order is r1 < r2 < r3, so their positions strictly increase.
        assert rows[r1.uuid] < rows[r2.uuid] < rows[r3.uuid]
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid.in_(mine)))
        s.commit()


def test_backfill_is_noop_when_positions_already_distinct(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    r1 = db.create_chatroom(f"nb-1-{uuid4().hex[:6]}", human.uuid, [])
    r2 = db.create_chatroom(f"nb-2-{uuid4().hex[:6]}", human.uuid, [])
    try:
        s.execute(sa.update(Chatroom).where(Chatroom.uuid == r1.uuid).values(position=5))
        s.execute(sa.update(Chatroom).where(Chatroom.uuid == r2.uuid).values(position=9))
        s.commit()
        db._backfill_chatroom_positions()  # distinct positions exist -> must not touch anything
        p1 = s.execute(sa.select(Chatroom.position).where(Chatroom.uuid == r1.uuid)).scalar()
        p2 = s.execute(sa.select(Chatroom.position).where(Chatroom.uuid == r2.uuid)).scalar()
        assert (p1, p2) == (5, 9)
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid.in_([r1.uuid, r2.uuid])))
        s.commit()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest db/test_chat_folders.py -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute '_backfill_chatroom_positions'`.

- [ ] **Step 3: Add the backfill helper + column migrations**

In `db/__init__.py`, add this helper right after `_add_column_if_missing` (ends L125):

```python
def _backfill_chatroom_positions() -> None:
    """One-time: give existing chatrooms a `position` reflecting their current
    visible order (created_at, then id) so adding the column doesn't visibly
    reshuffle the left panel. Idempotent: runs only while every row still shares
    one position value (the freshly-migrated state — COUNT(DISTINCT position) <=
    1); once positions diverge (a real reorder, or this backfill) it's a no-op,
    so a later reorder is never clobbered."""
    distinct = db.session.execute(
        sa.text("SELECT COUNT(DISTINCT position) FROM chatroom")
    ).scalar()
    if (distinct or 0) > 1:
        return
    db.session.execute(sa.text(
        "UPDATE chatroom c SET position = sub.rn FROM ("
        "  SELECT id, (ROW_NUMBER() OVER (ORDER BY created_at, id) - 1) AS rn"
        "  FROM chatroom"
        ") sub WHERE c.id = sub.id"
    ))
    db.session.commit()
```

Then, in `init_db`, after the kanban_task column block (after L249, before the `_status_def` block at L250), add:

```python
        # Chat-folder columns (added after chatroom's first cut). New table
        # chatroom_folder is created by create_all() above.
        _add_column_if_missing("chatroom", "folder_uuid", "folder_uuid UUID")
        _add_column_if_missing("chatroom", "position",
                               "position INTEGER NOT NULL DEFAULT 0")
        _backfill_chatroom_positions()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest db/test_chat_folders.py -v`
Expected: PASS (2 passed). The `chatroom_folder` table and new columns now exist (created during the fixture's `init_db`).

- [ ] **Step 5: Commit**

```bash
git add db/__init__.py db/test_chat_folders.py
git commit -m "feat(db): migrate chatroom folder columns + backfill positions"
```

---

## Task 3: DB helpers — folder CRUD, tree load, version token

**Files:**
- Modify: `db/chat.py` (imports at L15-28; add `_to_uuid` + folder helpers + tree functions; update `list_chatrooms` at L168)
- Test: `db/test_chat_folders.py`

- [ ] **Step 1: Write the failing tests**

Append to `db/test_chat_folders.py`:

```python
def test_create_and_list_folders(app_ctx):
    s = db.db.session
    f = db.create_chatroom_folder("Work")
    sub = db.create_chatroom_folder("Sub", parent_uuid=f.uuid)
    try:
        ids = {row["id"] for row in db.list_chatroom_folders()}
        assert str(f.uuid) in ids and str(sub.uuid) in ids
        sub_row = next(r for r in db.list_chatroom_folders() if r["id"] == str(sub.uuid))
        assert sub_row["parentId"] == str(f.uuid)
        assert sub_row["name"] == "Sub"
    finally:
        s.execute(sa.delete(ChatroomFolder).where(
            ChatroomFolder.uuid.in_([f.uuid, sub.uuid])))
        s.commit()


def test_load_tree_shape_and_version_ignores_messages(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    f = db.create_chatroom_folder("TreeFolder")
    room = db.create_chatroom(f"tree-{uuid4().hex[:6]}", human.uuid, [])
    s.execute(sa.update(Chatroom).where(Chatroom.uuid == room.uuid).values(
        folder_uuid=f.uuid))
    s.commit()
    try:
        tree = db.chat_load_tree()
        assert {"folders", "rooms", "version"} <= set(tree)
        assert any(r["uuid"] == str(room.uuid) and r["folderId"] == str(f.uuid)
                   for r in tree["rooms"])
        assert all("member_count" in r and "folderId" in r for r in tree["rooms"])
        v1 = db.chat_tree_version()
        # A new message must NOT change the structural version token.
        db.post_chat_message(room.uuid, human.uuid, "hello")
        assert db.chat_tree_version() == v1
        # A structural change (reparent) MUST change it.
        s.execute(sa.update(Chatroom).where(Chatroom.uuid == room.uuid).values(
            folder_uuid=None))
        s.commit()
        assert db.chat_tree_version() != v1
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid == room.uuid))
        s.execute(sa.delete(ChatroomFolder).where(ChatroomFolder.uuid == f.uuid))
        s.commit()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest db/test_chat_folders.py -k "folders or load_tree" -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'create_chatroom_folder'`.

- [ ] **Step 3: Add imports and helpers to `db/chat.py`**

In `db/chat.py`, change the top imports. Add `hashlib` to the stdlib imports (currently `import json` / `import logging` at L8-9):

```python
import hashlib
import json
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID
```

Add `ChatroomFolder` to the `from db.models import (...)` block (L15-28), e.g. after `Chatroom,`:

```python
    Chatroom,
    ChatroomFolder,
    ChatroomMember,
```

Now add these functions to `db/chat.py` (place them right before `list_chatrooms` at L168):

```python
class ChatTreeError(ValueError):
    """A chat folder/room tree payload failed structural validation (bad uuid,
    dangling/cyclic folder ref, unknown room folderId, missing/unknown room).
    Callers turn this into a 4xx rather than a 500."""


class ChatTreeConflict(Exception):
    """The chat tree changed since the caller hydrated (stale base_version on
    save). Callers map this to HTTP 409 so the client re-hydrates instead of
    clobbering another writer's changes."""


def _to_uuid(value: Any) -> UUID | None:
    """Parse to a UUID (normalizing case/format) or None. Lets callers key
    dedup/reference checks on the normalized value (mirrors db.cron._to_uuid;
    duplicated here to avoid a db.chat <-> db.cron import cycle)."""
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def create_chatroom_folder(name: str, parent_uuid: UUID | None = None) -> ChatroomFolder:
    """Create a left-panel folder. New folders are appended after existing
    siblings under the same parent (position = current sibling count)."""
    sibling_count = db.session.execute(
        sa.select(sa.func.count()).select_from(ChatroomFolder)
        .where(ChatroomFolder.parent_uuid.is_(parent_uuid) if parent_uuid is None
               else ChatroomFolder.parent_uuid == parent_uuid)
    ).scalar() or 0
    folder = ChatroomFolder(name=name, parent_uuid=parent_uuid, position=int(sibling_count))
    db.session.add(folder)
    db.session.commit()
    return folder


def list_chatroom_folders() -> list[dict[str, Any]]:
    """All folders as {id, name, parentId}, ordered by (position, id)."""
    folders = db.session.execute(
        sa.select(ChatroomFolder).order_by(ChatroomFolder.position, ChatroomFolder.id)
    ).scalars().all()
    return [
        {
            "id": str(f.uuid),
            "name": f.name,
            "parentId": str(f.parent_uuid) if f.parent_uuid else None,
        }
        for f in folders
    ]


def chat_tree_version() -> str:
    """Opaque version token over the user-managed tree fields only (folder:
    uuid/name/parentId/position; room: uuid/folderId/position). Volatile fields
    (a room's message count / last id) are excluded, so a new message never
    invalidates an open page — only a structural edit by another writer does.
    The page hydrates with this token and echoes it on PUT (409 if stale)."""
    folders = db.session.execute(
        sa.select(ChatroomFolder).order_by(ChatroomFolder.uuid)
    ).scalars().all()
    rooms = db.session.execute(
        sa.select(Chatroom).order_by(Chatroom.uuid)
    ).scalars().all()
    payload = [
        [[str(f.uuid), f.name,
          str(f.parent_uuid) if f.parent_uuid else None, f.position]
         for f in folders],
        [[str(r.uuid),
          str(r.folder_uuid) if r.folder_uuid else None, r.position]
         for r in rooms],
    ]
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def chat_load_tree() -> dict[str, Any]:
    """The whole left-panel tree: folders, rooms (with member_count/last id +
    folderId, reusing list_chatrooms), and the version token."""
    return {
        "folders": list_chatroom_folders(),
        "rooms": list_chatrooms(),
        "version": chat_tree_version(),
    }
```

- [ ] **Step 4: Update `list_chatrooms` to carry folder + position order**

Edit `list_chatrooms` (L168-190). Change the ordering and add `folderId` to each dict:

```python
def list_chatrooms() -> list[dict[str, Any]]:
    """Rooms for the left panel, ordered by saved position (then id), each with
    member count, last-message id, and its folder placement (folderId, null =
    top level)."""
    rooms = (
        db.session.query(Chatroom)
        .order_by(Chatroom.position.asc(), Chatroom.id.asc())
        .all()
    )
    member_counts = dict(
        db.session.query(ChatroomMember.room_uuid, sa.func.count())
        .group_by(ChatroomMember.room_uuid)
        .all()
    )
    last_ids = dict(
        db.session.query(ChatMessage.room_uuid, sa.func.max(ChatMessage.id))
        .group_by(ChatMessage.room_uuid)
        .all()
    )
    return [
        {
            "uuid": str(r.uuid),
            "name": r.name,
            "member_count": int(member_counts.get(r.uuid, 0)),
            "last_message_id": int(last_ids.get(r.uuid) or 0),
            "folderId": str(r.folder_uuid) if r.folder_uuid else None,
        }
        for r in rooms
    ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest db/test_chat_folders.py -k "folders or load_tree" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add db/chat.py db/test_chat_folders.py
git commit -m "feat(db): chat folder CRUD + tree load + version token"
```

---

## Task 4: DB — `validate_chat_tree`

**Files:**
- Modify: `db/chat.py` (add `validate_chat_tree` after `chat_load_tree`)
- Test: `db/test_chat_folders.py`

- [ ] **Step 1: Write the failing tests**

Append to `db/test_chat_folders.py`:

```python
def test_validate_rejects_bad_structures(app_ctx):
    rid = str(uuid4())
    fid = str(uuid4())
    # dangling folder parent
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([{"id": fid, "name": "F", "parentId": str(uuid4())}], [])
    # folder cycle (a -> b -> a)
    a, b = str(uuid4()), str(uuid4())
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree(
            [{"id": a, "name": "A", "parentId": b},
             {"id": b, "name": "B", "parentId": a}], [])
    # room references a missing folder
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([], [{"uuid": rid, "folderId": str(uuid4())}])
    # room uuid collides with a folder id
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([{"id": fid, "name": "F", "parentId": None}],
                              [{"uuid": fid, "folderId": None}])
    # duplicate folder id
    with pytest.raises(db.ChatTreeError):
        db.validate_chat_tree([{"id": fid, "name": "A", "parentId": None},
                               {"id": fid, "name": "B", "parentId": None}], [])


def test_validate_accepts_a_well_formed_tree(app_ctx):
    fid = str(uuid4())
    db.validate_chat_tree(
        [{"id": fid, "name": "Work", "parentId": None}],
        [{"uuid": str(uuid4()), "folderId": fid},
         {"uuid": str(uuid4()), "folderId": None}],
    )  # no exception
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest db/test_chat_folders.py -k validate -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'validate_chat_tree'`.

- [ ] **Step 3: Add `validate_chat_tree`**

Add to `db/chat.py` after `chat_load_tree`:

```python
def validate_chat_tree(
    folders: list[dict[str, Any]], rooms: list[dict[str, Any]]
) -> None:
    """Structural integrity check for an incoming chat tree, run before any DB
    write (mirrors validate_cron_tree). Rejects bad uuids, duplicate/dangling/
    cyclic folder refs, a room folderId that names no folder in the payload, and
    a room uuid that collides with a folder id (a node is identified globally by
    uuid). Does NOT touch the DB; raises ChatTreeError on the first problem."""
    if not isinstance(folders, list):
        raise ChatTreeError(f"'folders' must be a list, got {type(folders).__name__}")
    if not isinstance(rooms, list):
        raise ChatTreeError(f"'rooms' must be a list, got {type(rooms).__name__}")
    parent_of: dict[UUID, UUID | None] = {}
    for f in folders:
        if not isinstance(f, dict):
            raise ChatTreeError(f"folder entry must be an object, got {type(f).__name__}")
        fid = _to_uuid(f.get("id"))
        if fid is None:
            raise ChatTreeError(f"folder id is not a uuid: {f.get('id')!r}")
        if fid in parent_of:
            raise ChatTreeError(f"duplicate folder id: {fid}")
        if not isinstance(f.get("name", ""), str):
            raise ChatTreeError(f"folder {fid} name must be a string")
        pid_raw = f.get("parentId")
        if pid_raw is None:
            pid: UUID | None = None
        else:
            pid = _to_uuid(pid_raw)
            if pid is None:
                raise ChatTreeError(f"folder {fid} parentId is not a uuid: {pid_raw!r}")
        parent_of[fid] = pid
    for fid, pid in parent_of.items():
        if pid is not None and pid not in parent_of:
            raise ChatTreeError(f"folder {fid} references missing parent {pid}")
    # Acyclic: walking parents from any folder must terminate at a root.
    for start in parent_of:
        seen: set[UUID] = set()
        cur = parent_of[start]
        while cur is not None:
            if cur == start or cur in seen:
                raise ChatTreeError(f"folder cycle detected involving {start}")
            seen.add(cur)
            cur = parent_of.get(cur)
    room_uuids: set[UUID] = set()
    for r in rooms:
        if not isinstance(r, dict):
            raise ChatTreeError(f"room entry must be an object, got {type(r).__name__}")
        ru = _to_uuid(r.get("uuid"))
        if ru is None:
            raise ChatTreeError(f"room uuid is not a uuid: {r.get('uuid')!r}")
        if ru in room_uuids:
            raise ChatTreeError(f"duplicate room uuid: {ru}")
        if ru in parent_of:
            raise ChatTreeError(f"room uuid {ru} collides with a folder id")
        room_uuids.add(ru)
        fld_raw = r.get("folderId")
        if fld_raw is not None:
            fld = _to_uuid(fld_raw)
            if fld is None:
                raise ChatTreeError(f"room {ru} folderId is not a uuid: {fld_raw!r}")
            if fld not in parent_of:
                raise ChatTreeError(f"room {ru} references missing folder {fld}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest db/test_chat_folders.py -k validate -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db/chat.py db/test_chat_folders.py
git commit -m "feat(db): validate_chat_tree structural checks"
```

---

## Task 5: DB — `chat_save_tree` (version-guarded, never deletes rooms)

**Files:**
- Modify: `db/chat.py` (add `chat_save_tree` after `validate_chat_tree`)
- Test: `db/test_chat_folders.py`

- [ ] **Step 1: Write the failing tests**

Append to `db/test_chat_folders.py`:

```python
@pytest.fixture
def two_rooms(app_ctx):
    """Two fresh top-level rooms. Yields (room_a_uuid, room_b_uuid)."""
    s = db.db.session
    human = db.get_human_user()
    a = db.create_chatroom(f"sv-a-{uuid4().hex[:6]}", human.uuid, [])
    b = db.create_chatroom(f"sv-b-{uuid4().hex[:6]}", human.uuid, [])
    try:
        yield a.uuid, b.uuid
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid.in_([a.uuid, b.uuid])))
        s.execute(sa.delete(ChatroomFolder).where(ChatroomFolder.name.like("svf-%")))
        s.commit()


def _all_rooms_payload(extra_overrides=None):
    """Every existing room as {uuid, folderId}, so a save never omits a room.
    extra_overrides: {room_uuid_str: folderId} to set placement for some."""
    overrides = extra_overrides or {}
    return [
        {"uuid": r["uuid"], "folderId": overrides.get(r["uuid"], r["folderId"])}
        for r in db.list_chatrooms()
    ]


def test_save_tree_moves_room_into_folder(two_rooms):
    a, b = two_rooms
    fid = str(uuid4())
    folders = [{"id": fid, "name": "svf-work", "parentId": None}]
    rooms = _all_rooms_payload({str(a): fid})
    db.chat_save_tree(folders, rooms, base_version=db.chat_tree_version())
    moved = next(r for r in db.list_chatrooms() if r["uuid"] == str(a))
    assert moved["folderId"] == fid


def test_save_tree_refuses_to_drop_a_room(two_rooms):
    a, b = two_rooms
    # Payload omitting room b would silently delete it -> must raise.
    rooms = [r for r in _all_rooms_payload() if r["uuid"] != str(b)]
    with pytest.raises(db.ChatTreeError):
        db.chat_save_tree([], rooms, base_version=db.chat_tree_version())


def test_save_tree_409_on_stale_version(two_rooms):
    with pytest.raises(db.ChatTreeConflict):
        db.chat_save_tree([], _all_rooms_payload(), base_version="staleversion0000")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest db/test_chat_folders.py -k save_tree -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'chat_save_tree'`.

- [ ] **Step 3: Add `chat_save_tree`**

Add to `db/chat.py` after `validate_chat_tree`:

```python
def chat_save_tree(
    folders: list[dict[str, Any]], rooms: list[dict[str, Any]],
    *, base_version: str | None = None,
) -> None:
    """Upsert the left-panel tree. Folders are created/updated/reordered by
    uuid (list order becomes `position`); a folder uuid absent from the payload
    is deleted (only ever an emptied folder — room placement is reassigned
    first by the caller). Rooms are NEVER created or deleted here: only their
    `folder_uuid` + `position` change, and the payload MUST list exactly the
    existing rooms. A missing room would otherwise be silently dropped (and its
    messages with it via cascade) on a truncated payload — destructive folder/
    room deletion goes through the dedicated endpoints instead.

    Validates first (raises ChatTreeError before any mutation). base_version,
    when given, is the chat_tree_version() the caller hydrated with: a stale
    token raises ChatTreeConflict (HTTP 409 upstream)."""
    validate_chat_tree(folders, rooms)
    if base_version is not None and base_version != chat_tree_version():
        raise ChatTreeConflict("chat tree changed since it was loaded")
    existing_f = {
        f.uuid: f for f in db.session.execute(sa.select(ChatroomFolder)).scalars().all()
    }
    existing_r = {
        r.uuid: r for r in db.session.execute(sa.select(Chatroom)).scalars().all()
    }
    incoming_rooms = {UUID(r["uuid"]) for r in rooms}
    missing = set(existing_r) - incoming_rooms
    if missing:
        raise ChatTreeError(
            f"chat tree save omitted {len(missing)} existing room(s) — refusing "
            f"(the tree save never deletes rooms)"
        )
    unknown = incoming_rooms - set(existing_r)
    if unknown:
        raise ChatTreeError(f"chat tree save references {len(unknown)} unknown room(s)")
    # Folders: update existing by uuid, insert new, delete the rest.
    seen_f: set[UUID] = set()
    for i, f in enumerate(folders):
        fu = UUID(f["id"])
        seen_f.add(fu)
        row = existing_f.get(fu)
        if row is None:
            row = ChatroomFolder(uuid=fu)
            db.session.add(row)
        row.name = f.get("name", "")
        row.parent_uuid = UUID(f["parentId"]) if f.get("parentId") else None
        row.position = i
    for fu, row in existing_f.items():
        if fu not in seen_f:
            db.session.delete(row)
    # Rooms: only placement + order (never name/membership/messages).
    for i, r in enumerate(rooms):
        row = existing_r[UUID(r["uuid"])]
        row.folder_uuid = UUID(r["folderId"]) if r.get("folderId") else None
        row.position = i
    db.session.commit()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest db/test_chat_folders.py -k save_tree -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db/chat.py db/test_chat_folders.py
git commit -m "feat(db): chat_save_tree (version-guarded; never deletes rooms)"
```

---

## Task 6: DB — recursive delete preview + recursive folder delete

**Files:**
- Modify: `db/chat.py` (add `_descendant_chatroom_folder_uuids`, `chatroom_folder_delete_preview`, `delete_chatroom_folder`, `chatroom_delete_preview`)
- Test: `db/test_chat_folders.py`

- [ ] **Step 1: Write the failing tests**

Append to `db/test_chat_folders.py`:

```python
def test_recursive_delete_preview_and_delete(app_ctx):
    s = db.db.session
    human = db.get_human_user()
    # parent -> child folder; one room in each; an unrelated top-level room.
    parent = db.create_chatroom_folder("svf-parent")
    child = db.create_chatroom_folder("svf-child", parent_uuid=parent.uuid)
    r_parent = db.create_chatroom(f"rp-{uuid4().hex[:6]}", human.uuid, [])
    r_child = db.create_chatroom(f"rc-{uuid4().hex[:6]}", human.uuid, [])
    r_other = db.create_chatroom(f"ro-{uuid4().hex[:6]}", human.uuid, [])
    s.execute(sa.update(Chatroom).where(Chatroom.uuid == r_parent.uuid).values(folder_uuid=parent.uuid))
    s.execute(sa.update(Chatroom).where(Chatroom.uuid == r_child.uuid).values(folder_uuid=child.uuid))
    s.commit()
    db.post_chat_message(r_parent.uuid, human.uuid, "one")
    db.post_chat_message(r_child.uuid, human.uuid, "two")
    db.post_chat_message(r_child.uuid, human.uuid, "three")
    try:
        preview = db.chatroom_folder_delete_preview(parent.uuid)
        assert preview["folder_name"] == "svf-parent"
        assert preview["room_count"] == 2          # r_parent + r_child
        assert preview["message_count"] == 3       # 1 + 2
        db.delete_chatroom_folder(parent.uuid)
        # Both folders + both contained rooms gone; the unrelated room survives.
        remaining = {r["uuid"] for r in db.list_chatrooms()}
        assert str(r_parent.uuid) not in remaining
        assert str(r_child.uuid) not in remaining
        assert str(r_other.uuid) in remaining
        folder_ids = {f["id"] for f in db.list_chatroom_folders()}
        assert str(parent.uuid) not in folder_ids and str(child.uuid) not in folder_ids
    finally:
        s.execute(sa.delete(Chatroom).where(Chatroom.uuid == r_other.uuid))
        s.commit()


def test_chatroom_delete_preview(app_ctx):
    human = db.get_human_user()
    room = db.create_chatroom(f"dp-{uuid4().hex[:6]}", human.uuid, [])
    db.post_chat_message(room.uuid, human.uuid, "x")
    try:
        preview = db.chatroom_delete_preview(room.uuid)
        assert preview["room_name"] == room.name
        assert preview["message_count"] == 1
    finally:
        db.delete_chatroom(room.uuid)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest db/test_chat_folders.py -k "delete_preview or recursive_delete" -v`
Expected: FAIL with `AttributeError: module 'db' has no attribute 'chatroom_folder_delete_preview'`.

- [ ] **Step 3: Add the delete helpers**

Add to `db/chat.py` (after `chat_save_tree`):

```python
def _descendant_chatroom_folder_uuids(folder_uuid: UUID) -> list[UUID]:
    """`folder_uuid` plus every folder nested under it (any depth). Cycle-guarded
    via a visited set so a malformed parent loop can't spin forever."""
    children: dict[UUID | None, list[UUID]] = defaultdict(list)
    for f in db.session.execute(sa.select(ChatroomFolder)).scalars().all():
        children[f.parent_uuid].append(f.uuid)
    result: list[UUID] = []
    seen: set[UUID] = set()
    stack = [folder_uuid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        stack.extend(children.get(cur, []))
    return result


def chatroom_folder_delete_preview(folder_uuid: UUID) -> dict[str, Any]:
    """Authoritative rollup for the delete-confirm dialog: the folder's name and
    the total chatrooms + messages that a recursive delete would remove (across
    all nested subfolders). Raises LookupError if the folder is gone."""
    folder = db.session.execute(
        sa.select(ChatroomFolder).where(ChatroomFolder.uuid == folder_uuid)
    ).scalar_one_or_none()
    if folder is None:
        raise LookupError(f"chatroom folder {folder_uuid} not found")
    folder_uuids = _descendant_chatroom_folder_uuids(folder_uuid)
    room_uuids = db.session.execute(
        sa.select(Chatroom.uuid).where(Chatroom.folder_uuid.in_(folder_uuids))
    ).scalars().all()
    message_count = 0
    if room_uuids:
        message_count = int(db.session.execute(
            sa.select(sa.func.count()).select_from(ChatMessage)
            .where(ChatMessage.room_uuid.in_(room_uuids))
        ).scalar() or 0)
    return {
        "folder_name": folder.name,
        "room_count": len(room_uuids),
        "message_count": message_count,
    }


def delete_chatroom_folder(folder_uuid: UUID) -> None:
    """Recursively delete a folder: every nested subfolder, every chatroom in
    that subtree, and (via the chatroom row's ON DELETE CASCADE) those rooms'
    messages, members, and workspace-shell state. Raises LookupError if the
    folder is gone. This is the destructive op the type-to-confirm dialog
    guards — chat_save_tree never deletes rooms."""
    folder = db.session.execute(
        sa.select(ChatroomFolder).where(ChatroomFolder.uuid == folder_uuid)
    ).scalar_one_or_none()
    if folder is None:
        raise LookupError(f"chatroom folder {folder_uuid} not found")
    folder_uuids = _descendant_chatroom_folder_uuids(folder_uuid)
    rooms = db.session.execute(
        sa.select(Chatroom).where(Chatroom.folder_uuid.in_(folder_uuids))
    ).scalars().all()
    for room in rooms:
        db.session.delete(room)  # cascades messages + members + workspace_shell_state
    db.session.execute(
        sa.delete(ChatroomFolder).where(ChatroomFolder.uuid.in_(folder_uuids))
    )
    db.session.commit()


def chatroom_delete_preview(room_uuid: UUID) -> dict[str, Any]:
    """Rollup for a single-room delete-confirm dialog: the room's name and how
    many messages it holds. Raises LookupError if the room is gone."""
    room = get_chatroom(room_uuid)
    if room is None:
        raise LookupError(f"chatroom {room_uuid} not found")
    message_count = int(db.session.execute(
        sa.select(sa.func.count()).select_from(ChatMessage)
        .where(ChatMessage.room_uuid == room_uuid)
    ).scalar() or 0)
    return {"room_name": room.name, "message_count": message_count}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest db/test_chat_folders.py -v`
Expected: PASS (whole file green).

- [ ] **Step 5: Commit**

```bash
git add db/chat.py db/test_chat_folders.py
git commit -m "feat(db): recursive chat folder delete + delete previews"
```

---

## Task 7: API endpoints

**Files:**
- Modify: `webapp/chat_api.py` (add tree GET/PUT, folder POST, delete-preview GET ×2, folder DELETE)
- Test: `webapp/test_chat_folders_api.py` (new)

> `db/__init__.py` re-exports `db.chat` via `from db.chat import *`, so all the new public names (`chat_load_tree`, `chat_save_tree`, `validate_chat_tree`, `create_chatroom_folder`, `delete_chatroom_folder`, `chatroom_folder_delete_preview`, `chatroom_delete_preview`, `ChatTreeError`, `ChatTreeConflict`) are already on `db` — no edit to `db/__init__.py` is needed for exports.

- [ ] **Step 1: Write the failing API tests**

Create `webapp/test_chat_folders_api.py`:

```python
"""Tests for the /chat folder/tree HTTP endpoints (webapp/chat_api.py).

Uses the live local Postgres (rainbox_claude via conftest).
"""
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import Chatroom, ChatroomFolder


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


def test_get_tree_shape(app_ctx):
    client = app_ctx.test_client()
    tree = client.get("/chat/api/tree").get_json()
    assert {"folders", "rooms", "version"} <= set(tree)
    assert isinstance(tree["version"], str) and tree["version"]


def test_create_folder_then_appears_in_tree(app_ctx):
    client = app_ctx.test_client()
    resp = client.post("/chat/api/folders", json={"name": "apitest-folder"})
    assert resp.status_code == 201
    fid = resp.get_json()["id"]
    try:
        tree = client.get("/chat/api/tree").get_json()
        assert any(f["id"] == fid for f in tree["folders"])
    finally:
        db.db.session.execute(sa.delete(ChatroomFolder).where(ChatroomFolder.uuid == fid))
        db.db.session.commit()


def test_put_tree_stale_version_is_409(app_ctx):
    client = app_ctx.test_client()
    tree = client.get("/chat/api/tree").get_json()
    body = {"folders": [], "rooms": [{"uuid": r["uuid"], "folderId": r["folderId"]}
                                     for r in tree["rooms"]],
            "version": "staleversion0000"}
    resp = client.put("/chat/api/tree", json=body)
    assert resp.status_code == 409
    assert "version" in resp.get_json()  # fresh token returned for re-hydration


def test_put_tree_missing_version_is_400(app_ctx):
    client = app_ctx.test_client()
    resp = client.put("/chat/api/tree", json={"folders": [], "rooms": []})
    assert resp.status_code == 400


def test_folder_delete_preview_and_delete(app_ctx):
    client = app_ctx.test_client()
    human = db.get_human_user()
    folder = db.create_chatroom_folder("apitest-del")
    room = db.create_chatroom(f"apidel-{uuid4().hex[:6]}", human.uuid, [])
    db.db.session.execute(sa.update(Chatroom).where(Chatroom.uuid == room.uuid)
                          .values(folder_uuid=folder.uuid))
    db.db.session.commit()
    db.post_chat_message(room.uuid, human.uuid, "hi")
    preview = client.get(f"/chat/api/folders/{folder.uuid}/delete-preview").get_json()
    assert preview["room_count"] == 1 and preview["message_count"] == 1
    resp = client.delete(f"/chat/api/folders/{folder.uuid}")
    assert resp.status_code == 200
    assert db.db.session.execute(
        sa.select(Chatroom).where(Chatroom.uuid == room.uuid)).scalar_one_or_none() is None


def test_folder_delete_unknown_is_404(app_ctx):
    client = app_ctx.test_client()
    resp = client.delete(f"/chat/api/folders/{uuid4()}")
    assert resp.status_code == 404


def test_room_delete_preview(app_ctx):
    client = app_ctx.test_client()
    human = db.get_human_user()
    room = db.create_chatroom(f"apirp-{uuid4().hex[:6]}", human.uuid, [])
    db.post_chat_message(room.uuid, human.uuid, "x")
    try:
        preview = client.get(f"/chat/api/rooms/{room.uuid}/delete-preview").get_json()
        assert preview["message_count"] == 1 and preview["room_name"] == room.name
    finally:
        db.delete_chatroom(room.uuid)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest webapp/test_chat_folders_api.py -v`
Expected: FAIL (404s — the routes don't exist yet).

- [ ] **Step 3: Add the endpoints**

In `webapp/chat_api.py`, add these routes (e.g. after `chat_rooms` at L93). The `_parse_uuid` helper (L49) and `db` import are already present:

```python
@app.route("/chat/api/tree", methods=["GET", "PUT"])
def chat_tree() -> Response | tuple[Response, int]:
    """The left-panel folder/room tree. GET hydrates {folders, rooms, version};
    PUT bulk-saves folder placement + room ordering (version-guarded). The PUT
    never creates or deletes rooms — folder/room deletion has dedicated
    endpoints (mirrors /cron/api/tree, but without room destruction)."""
    if request.method == "PUT":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return jsonify({"ok": False,
                            "error": "missing tree 'version' (hydrate via GET first)"}), 400
        try:
            db.chat_save_tree(data.get("folders", []), data.get("rooms", []),
                              base_version=version)
        except db.ChatTreeConflict as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "version": db.chat_tree_version()}), 409
        except db.ChatTreeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "version": db.chat_tree_version()})
    return jsonify(db.chat_load_tree())


@app.route("/chat/api/folders", methods=["POST"])
def chat_create_folder() -> tuple[Response, int]:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "folder name required")
    parent_raw = data.get("parent_uuid")
    parent_uuid = _parse_uuid(parent_raw) if parent_raw else None
    folder = db.create_chatroom_folder(name, parent_uuid)
    return jsonify({
        "id": str(folder.uuid),
        "name": folder.name,
        "parentId": str(folder.parent_uuid) if folder.parent_uuid else None,
    }), 201


@app.route("/chat/api/folders/<folder_uuid>/delete-preview")
def chat_folder_delete_preview(folder_uuid: str) -> Response:
    fuuid = _parse_uuid(folder_uuid)
    try:
        return jsonify(db.chatroom_folder_delete_preview(fuuid))
    except LookupError:
        abort(404, "folder not found")


@app.route("/chat/api/folders/<folder_uuid>", methods=["DELETE"])
def chat_delete_folder(folder_uuid: str) -> Response:
    fuuid = _parse_uuid(folder_uuid)
    try:
        db.delete_chatroom_folder(fuuid)
    except LookupError:
        abort(404, "folder not found")
    return jsonify({"id": str(fuuid), "deleted": True})


@app.route("/chat/api/rooms/<room_uuid>/delete-preview")
def chat_room_delete_preview(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    try:
        return jsonify(db.chatroom_delete_preview(ruuid))
    except LookupError:
        abort(404, "room not found")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest webapp/test_chat_folders_api.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full backend suite for regressions**

Run: `python -m pytest db/test_chat_folders.py db/test_chat_membership.py webapp/test_chat_folders_api.py webapp/test_cron_api.py -q`
Expected: all PASS (the `list_chatrooms` ordering change and new columns don't break existing chat/cron tests).

- [ ] **Step 6: Commit**

```bash
git add webapp/chat_api.py webapp/test_chat_folders_api.py
git commit -m "feat(chat): /chat/api tree + folder CRUD + delete-preview endpoints"
```

---

## Task 8: Frontend — tree CSS + modal HTML

**Files:**
- Modify: `webapp/chat_template.py` (CSS block ~L72; panel HTML ~L163; add modals before the closing of `.chat-split` at L182)

- [ ] **Step 1: Add tree + modal CSS**

In `webapp/chat_template.py`, insert this CSS right before the `.room-main{...}` rule (L74). These class names port cron's tree styling, scoped with a `chat-` prefix:

```css
  /* ---- folder tree (ported from /cron) ---- */
  #rooms ul{list-style:none;margin:0;padding:0}
  #rooms ul ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .chat-node{position:relative;display:flex;align-items:center;gap:0.4em;width:100%;
             padding:0.4em 0.6em;border-radius:6px;cursor:pointer;color:#333;font-size:0.9rem}
  .chat-node:hover{background:#eef0f6}
  .chat-node.sel{background:#e3ebfb}
  .chat-ficon{display:inline-flex;width:1.05em;height:1.05em;color:#6b7280;flex:0 0 auto}
  .chat-ficon svg{width:100%;height:100%}
  .chat-folder-label{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
  /* drag feedback (ported from cron) */
  .chat-dragging{opacity:0.4}
  .chat-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .chat-drop-before{box-shadow:inset 0 2px 0 #2563eb}
  .chat-drop-after{box-shadow:inset 0 -2px 0 #2563eb}
  .chat-root-drop{margin:0.4em 0.3em 0;padding:0.4em;border:1px dashed #cbd5e1;border-radius:6px;
                  color:#94a3b8;font-size:0.78rem;text-align:center;display:none}
  .rooms.dragging-on .chat-root-drop{display:block}
  .chat-root-drop.over{border-color:#2563eb;color:#2563eb;background:#eff6ff}
  .new-folder-btn{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
                  padding:0.25em 0.6em;cursor:pointer;font:inherit;font-size:0.78rem;margin-left:0.4em}
  .new-folder-btn:hover{border-color:#2563eb;color:#2563eb}
  /* modal (folder create + delete-confirm) */
  .chat-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.35);z-index:1500}
  .chat-modal-backdrop[hidden]{display:none}
  .chat-modal{position:fixed;z-index:1600;left:50%;top:50%;transform:translate(-50%,-50%);
              background:#fff;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,0.25);
              padding:1.2em 1.3em;width:min(420px,92vw)}
  .chat-modal[hidden]{display:none}
  .chat-modal h3{margin:0 0 0.6em;font-size:1.05rem}
  .chat-modal p{margin:0 0 0.8em;color:#444;font-size:0.9rem;line-height:1.45}
  .chat-modal input[type=text]{width:100%;box-sizing:border-box;padding:0.5em;border:1px solid #ccc;
                               border-radius:6px;font:inherit}
  .chat-modal .modal-actions{display:flex;justify-content:flex-end;gap:0.5em;margin-top:1em}
  .chat-modal button{border:none;border-radius:6px;padding:0.45em 1em;cursor:pointer;font:inherit}
  .chat-modal .btn-cancel{background:#e5e7eb;color:#374151}
  .chat-modal .btn-primary{background:#2563eb;color:#fff}
  .chat-modal .btn-danger{background:#dc2626;color:#fff}
  .chat-modal button:disabled{opacity:0.5;cursor:default}
```

- [ ] **Step 2: Add the "New folder" button + root drop zone to the panel**

Edit the rooms panel header (L151-153) to add a New-folder button beside New-room:

```html
    <div class="rooms-head">
      <span class="title">Rooms</span>
      <span>
        <button class="new-folder-btn" id="new-folder-btn" type="button">+ Folder</button>
        <button class="new-room-btn" id="new-room-btn" type="button">+ New room</button>
      </span>
    </div>
```

Then change the rooms container (L163) and add a root drop zone after it:

```html
    <div id="rooms"></div>
    <div class="chat-root-drop" id="chat-root-drop">Move to top level</div>
```

Also add the `dragging-on` toggle target: the `#rooms` panel is `.rooms` (L150) — the JS will add/remove `dragging-on` on it.

- [ ] **Step 3: Add the folder-create and delete-confirm modals**

Insert just before the closing `</div>` of `.chat-split` (before L182 `<div class="room-sidebar"...`). Two modals share one backdrop:

```html
  <div class="chat-modal-backdrop" id="chat-modal-backdrop" hidden></div>

  <div class="chat-modal" id="chat-folder-modal" hidden>
    <h3 id="chat-folder-title">New folder</h3>
    <input type="text" id="chat-folder-input" placeholder="Folder name" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-folder-cancel">Cancel</button>
      <button type="button" class="btn-primary" id="chat-folder-create" disabled>Create</button>
    </div>
  </div>

  <div class="chat-modal" id="chat-delete-modal" hidden>
    <h3 id="chat-delete-title">Delete</h3>
    <p id="chat-delete-msg"></p>
    <p style="margin-bottom:0.3em">Type <strong id="chat-delete-name"></strong> to confirm:</p>
    <input type="text" id="chat-delete-input" autocomplete="off">
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-delete-cancel">Cancel</button>
      <button type="button" class="btn-danger" id="chat-delete-confirm" disabled>Delete</button>
    </div>
  </div>
```

- [ ] **Step 4: Verify the page still renders**

Run: `python -c "import webapp.chat_template as t; assert 'chat-delete-modal' in t.CHAT_TEMPLATE and 'chat-root-drop' in t.CHAT_TEMPLATE; print('template ok')"`
Expected: prints `template ok`.

- [ ] **Step 5: Commit**

```bash
git add webapp/chat_template.py
git commit -m "feat(chat): tree + modal markup/styles in chat panel"
```

---

## Task 9: Frontend — tree state, load, and nested render

**Files:**
- Modify: `webapp/chat_template.py` (JS: state at L212; icons near L210; replace `renderRooms` at L538; update `loadRooms` at L687)

- [ ] **Step 1: Add icons + tree state**

In the JS block, after the `LUCIDE_THUMBS_DOWN_SVG` constant (L210), add cron's folder icons (verbatim from `static/cron.js` L657-658):

```javascript
const CHAT_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const CHAT_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';
```

After the `let rooms = [];` line (L212), add the tree state:

```javascript
let folders = [];               // [{id, name, parentId}]
let treeVersion = null;         // optimistic-concurrency token from /chat/api/tree
let dragNode = null;            // {type:'folder'|'room', id} during a drag
const FOLDER_EXPAND_KEY = 'chat.expandedFolders';
let expandedFolders = {};       // folderId -> false when collapsed (default expanded)
try {
  const saved = JSON.parse(localStorage.getItem(FOLDER_EXPAND_KEY) || '{}');
  if (saved && typeof saved === 'object') expandedFolders = saved;
} catch (e) {}
function saveExpandState(){
  try { localStorage.setItem(FOLDER_EXPAND_KEY, JSON.stringify(expandedFolders)); } catch (e) {}
}
function folderById(id){ return folders.find(f => f.id === id) || null; }
function childFolders(parentId){ return folders.filter(f => (f.parentId || null) === parentId); }
function roomsInFolder(id){ return rooms.filter(r => (r.folderId || null) === id); }
function isExpanded(id){ return expandedFolders[id] !== false; }
```

- [ ] **Step 2: Replace `renderRooms` with the nested tree renderer**

Replace the whole `renderRooms` function (L538-575) with:

```javascript
function renderRooms(){
  roomsEl.innerHTML = '';
  if (!rooms.length && !folders.length){
    const p = document.createElement('p');
    p.className = 'note';
    p.textContent = 'No rooms yet — create one above.';
    roomsEl.appendChild(p);
    return;
  }
  const rootUl = document.createElement('ul');
  childFolders(null).forEach(f => rootUl.appendChild(folderLi(f)));
  roomsInFolder(null).forEach(r => {
    const li = document.createElement('li');
    li.appendChild(roomNode(r));
    rootUl.appendChild(li);
  });
  roomsEl.appendChild(rootUl);
}

// A folder row: chevron-free; the folder icon flips open when expanded and the
// folder has children. Click toggles expand/collapse. Ported from cronFolderLi.
function folderLi(f){
  const li = document.createElement('li');
  const kids = childFolders(f.id);
  const kidRooms = roomsInFolder(f.id);
  const hasKids = (kids.length + kidRooms.length) > 0;
  const expanded = isExpanded(f.id);
  const node = document.createElement('div');
  node.className = 'chat-node';
  const icon = document.createElement('span');
  icon.className = 'chat-ficon';
  icon.innerHTML = (expanded && hasKids) ? CHAT_ICON_FOLDER_OPEN : CHAT_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'chat-folder-label';
  label.textContent = f.name;
  node.appendChild(icon);
  node.appendChild(label);
  node.title = f.name;
  node.addEventListener('click', () => {
    expandedFolders[f.id] = !isExpanded(f.id);
    saveExpandState();
    renderRooms();
  });
  makeDraggable(node, 'folder', f.id);
  makeFolderDrop(node, f.id);
  node.appendChild(buildFolderMenu(f.id));
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(folderLi(c)));
    kidRooms.forEach(r => { const rli = document.createElement('li'); rli.appendChild(roomNode(r)); ul.appendChild(rli); });
    li.appendChild(ul);
  }
  return li;
}

// A room row — keeps the existing .room-row/.room markup (name, sub, unread,
// kebab) so selection/menus look identical to today, wrapped for drag-drop.
function roomNode(r){
  const isActive = r.uuid === currentRoom;
  const row = document.createElement('div');
  row.className = 'room-row' + (isActive ? ' active' : '');
  const btn = document.createElement('button');
  btn.className = 'room' + (isActive ? ' active' : '');
  btn.type = 'button';
  btn.dataset.room = r.uuid;
  const name = document.createElement('span');
  name.className = 'room-name';
  name.textContent = '# ' + r.name;
  const sub = document.createElement('span');
  sub.className = 'room-sub';
  sub.textContent = r.member_count + (r.member_count === 1 ? ' member' : ' members');
  btn.appendChild(name);
  btn.appendChild(sub);
  const n = unread[r.uuid] || 0;
  if (n > 0){
    const dot = document.createElement('span');
    dot.className = 'unread';
    dot.textContent = n;
    btn.appendChild(dot);
  }
  btn.addEventListener('click', () => selectRoom(r.uuid));
  row.appendChild(btn);
  if (isActive) row.appendChild(buildRoomMenu(r.uuid));
  makeDraggable(row, 'room', r.uuid);
  makeRoomDrop(row, r.uuid);
  return row;
}
```

- [ ] **Step 3: Add `buildFolderMenu` (kebab for folders)**

Add after `buildRoomMenu` (ends L621). It reuses the existing `.room-actions`/`.room-kebab`/`.room-menu` styles, but the kebab must always show for folders (the CSS only reveals it on `.active` rows). Set it visible inline:

```javascript
// Folder kebab: Rename + Delete. Always visible (folders have no "active"
// state like rooms). Reuses the room-menu styles.
function buildFolderMenu(folderId){
  const wrap = document.createElement('div');
  wrap.className = 'room-actions';
  wrap.style.display = 'flex';  // folders show the kebab unconditionally
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'room-kebab';
  kebab.setAttribute('aria-label', 'Folder actions');
  const menu = document.createElement('div');
  menu.className = 'room-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  [['Rename', ''], ['Delete', 'danger']].forEach(([label, mod]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'item' + (mod ? ' ' + mod : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = label;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden = true;
      if (label === 'Delete') confirmDeleteFolder(folderId);
      else if (label === 'Rename') renameFolder(folderId);
    });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', (e) => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    document.querySelectorAll('.room-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      const rect = kebab.getBoundingClientRect();
      menu.style.left = rect.left + 'px';
      menu.style.top = (rect.bottom + 4) + 'px';
      menu.hidden = false;
    }
  });
  wrap.appendChild(kebab);
  wrap.appendChild(menu);
  return wrap;
}

// Inline-rename a folder: reuse the folder-create modal in "rename" mode.
function renameFolder(folderId){
  const f = folderById(folderId);
  if (!f) return;
  openFolderModal({mode: 'rename', folderId: folderId, current: f.name});
}
```

- [ ] **Step 4: Switch `loadRooms` to hydrate the whole tree**

Replace `loadRooms` (L687-689) with a tree-hydrating version. Keep the name so existing callers (`selectRoom`, post-rename, post-create) still work:

```javascript
async function loadRooms(selectUuid){
  const tree = await getJSON('/chat/api/tree');
  folders = (tree && tree.folders) || [];
  rooms = (tree && tree.rooms) || [];
  treeVersion = (tree && tree.version) || null;
  renderRooms();
  if (selectUuid && rooms.some(r => r.uuid === selectUuid)){
    await selectRoom(selectUuid);
  }
}
```

> Note: confirm the original `loadRooms` body (L687-700+) — if it had extra logic after `renderRooms()` (e.g. auto-selecting the first room or honoring `selectUuid`), preserve that behavior in the rewrite above. Read L687-705 before editing and fold any such logic in.

- [ ] **Step 5: Verify template parses and key symbols exist**

Run: `python -c "import webapp.chat_template as t; src=t.CHAT_TEMPLATE; [print('missing', s) for s in ['folderLi','roomNode','buildFolderMenu','CHAT_ICON_FOLDER_OPEN','treeVersion'] if s not in src] or print('all present')"`
Expected: prints `all present`.

- [ ] **Step 6: Commit**

```bash
git add webapp/chat_template.py
git commit -m "feat(chat): render nested folder/room tree in left panel"
```

---

## Task 10: Frontend — drag-drop + tree save

**Files:**
- Modify: `webapp/chat_template.py` (JS: add drag-drop + `saveTree`; wire root drop in init)

- [ ] **Step 1: Add the drag-drop functions + debounced save**

Add this block in the JS (e.g. right after `roomNode` / before `selectRoom`). It is a direct port of `static/cron.js` L1202-1425 with cron→chat naming (jobs→rooms, `folderId` on rooms, `parentId` on folders):

```javascript
// ---- drag & drop (ported from static/cron.js) ----
function folderInSubtree(candidateId, rootId){
  let cur = folderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? folderById(cur.parentId) : null;
  }
  return false;
}
function moveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && folderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = folderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  folders = folders.filter(x => x.id !== folderId);
  if (atStart){
    const i = folders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) folders.push(f); else folders.splice(i, 0, f);
  } else {
    let at = folders.length;
    for (let i = folders.length - 1; i >= 0; i--){
      if ((folders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    folders.splice(at, 0, f);
  }
  saveTree();
}
function moveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = folderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && folderInSubtree(newParent, folderId)) return;  // no cycles
  const f = folderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  folders = folders.filter(x => x.id !== folderId);
  const ti = folders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) folders.push(f);
  else folders.splice(after ? ti + 1 : ti, 0, f);
  saveTree();
}
function moveRoom(roomUuid, targetFolderId, beforeRoomUuid){
  targetFolderId = targetFolderId || null;
  const idx = rooms.findIndex(r => r.uuid === roomUuid);
  if (idx < 0) return;
  const room = rooms.splice(idx, 1)[0];
  room.folderId = targetFolderId;
  let insertAt = beforeRoomUuid ? rooms.findIndex(r => r.uuid === beforeRoomUuid) : -1;
  if (insertAt < 0){
    insertAt = rooms.length;
    for (let i = rooms.length - 1; i >= 0; i--){
      if ((rooms[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  rooms.splice(insertAt, 0, room);
  saveTree();
}
function makeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    dragNode = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // Firefox needs data to start a drag
    el.classList.add('chat-dragging');
    document.querySelector('.rooms').classList.add('dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    dragNode = null;
    document.querySelector('.rooms').classList.remove('dragging-on');
    renderRooms();
  });
}
function dropInto(folderId, atStart){
  if (!dragNode) return;
  const dragged = dragNode;
  if (dragged.type === 'room'){
    let beforeUuid = null;
    if (atStart){
      const first = rooms.find(r =>
        (r.folderId || null) === (folderId || null) && r.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    moveRoom(dragged.id, folderId, beforeUuid);
  } else {
    moveFolder(dragged.id, folderId, atStart);
  }
  if (folderId){ expandedFolders[folderId] = true; saveExpandState(); }
  dragNode = null;
  renderRooms();
}
function makeFolderDrop(node, folderId){
  const zoneOf = e => {
    if (dragNode && dragNode.type === 'room') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!dragNode) return false;
    if (dragNode.type === 'room') return z === 'into';
    if (folderId === dragNode.id) return false;
    if (z === 'into') return !folderInSubtree(folderId, dragNode.id);
    const t = folderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && folderInSubtree(np, dragNode.id));
  };
  const clear = () => node.classList.remove('chat-drop-before', 'chat-drop-after', 'chat-drop-target');
  node.addEventListener('dragover', e => {
    if (!dragNode) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('chat-drop-before', z === 'before');
    node.classList.toggle('chat-drop-after', z === 'after');
    node.classList.toggle('chat-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!dragNode) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      dropInto(folderId, false);
    } else {
      moveFolderBeside(dragNode.id, folderId, z === 'after');
      dragNode = null;
      renderRooms();
    }
  });
}
function makeRoomDrop(node, roomUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!dragNode) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('chat-drop-after', after);
    node.classList.toggle('chat-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('chat-drop-before', 'chat-drop-after'));
  node.addEventListener('drop', e => {
    if (!dragNode) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('chat-drop-before', 'chat-drop-after');
    dropOnRoom(roomUuid, after);
  });
}
function dropOnRoom(targetUuid, after){
  if (!dragNode) return;
  if (dragNode.type === 'room' && dragNode.id === targetUuid) return;  // onto itself
  const dragged = dragNode;
  const target = rooms.find(r => r.uuid === targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'room'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = rooms.findIndex(r => r.uuid === targetUuid);
      beforeUuid = (ti + 1 < rooms.length) ? rooms[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    moveRoom(dragged.id, targetFolder, beforeUuid);
  } else {
    moveFolder(dragged.id, targetFolder);
  }
  dragNode = null;
  renderRooms();
}
function wireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (dragNode){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (dragNode){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); dropInto(null, atStart); }
  });
}

// ---- persistence: debounced PUT of the whole tree ----
let saveTimer = null;
function saveTree(){
  renderRooms();
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveTreePush, 300);
}
async function saveTreePush(){
  if (!treeVersion){ await loadRooms(currentRoom); return; }  // no token -> re-hydrate, never blind-PUT
  const body = {
    folders: folders.map(f => ({id: f.id, name: f.name, parentId: f.parentId || null})),
    rooms: rooms.map(r => ({uuid: r.uuid, folderId: r.folderId || null})),
    version: treeVersion,
  };
  try {
    const resp = await fetch('/chat/api/tree', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.status === 409){ await loadRooms(currentRoom); return; }  // stale -> re-hydrate
    if (!resp.ok) throw new Error(data.error || ('PUT /chat/api/tree -> ' + resp.status));
    treeVersion = data.version || treeVersion;
  } catch (e) {
    await loadRooms(currentRoom);  // recover to server truth on any error
  }
}
```

- [ ] **Step 2: Wire the root drop zone once at init**

Find where the page initializes after DOM load (the `loadRooms(...)` call near the bottom of the script, and the existing global click/Escape handlers at L648-653). Add — once, near that init — wiring for the root drop zone and the empty-space drop on the rooms container:

```javascript
// Root drop targets: the explicit "Move to top level" zone + empty space in the
// rooms panel both move a dragged node to the root level.
wireRootDrop(document.getElementById('chat-root-drop'), false);
(function wireRoomsContainerRootDrop(){
  const panel = document.querySelector('.rooms');
  panel.addEventListener('dragover', e => {
    if (dragNode){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  panel.addEventListener('drop', e => {
    // Only when the drop wasn't already handled by a node (which stops propagation).
    if (dragNode){ e.preventDefault(); dropInto(null, false); }
  });
})();
```

> Place this AFTER the elements exist (the script runs at end of body, so the modal/zone elements from Task 8 are present). If the existing script wraps init in a function, add these lines inside it; otherwise add them at top level near the existing `loadRooms` call.

- [ ] **Step 3: Verify symbols present and template parses**

Run: `python -c "import webapp.chat_template as t; src=t.CHAT_TEMPLATE; [print('missing', s) for s in ['makeFolderDrop','moveRoom','saveTreePush','wireRootDrop'] if s not in src] or print('all present')"`
Expected: prints `all present`.

- [ ] **Step 4: Commit**

```bash
git add webapp/chat_template.py
git commit -m "feat(chat): drag-drop reorder/nest + version-guarded tree save"
```

---

## Task 11: Frontend — new-folder modal + type-to-confirm delete

**Files:**
- Modify: `webapp/chat_template.py` (JS: modal helpers; rewrite `deleteRoom` at L623 to use the confirm modal; wire buttons)

- [ ] **Step 1: Add folder-modal helpers and wiring**

Add to the JS (near the other modal/tree code). This drives the `#chat-folder-modal` for both create and rename:

```javascript
// ---- folder create / rename modal ----
let folderModalState = null;  // {mode:'create'|'rename', folderId?, parentId?}
function openFolderModal(opts){
  folderModalState = opts || {mode: 'create', parentId: null};
  document.getElementById('chat-folder-title').textContent =
    folderModalState.mode === 'rename' ? 'Rename folder' : 'New folder';
  const input = document.getElementById('chat-folder-input');
  input.value = folderModalState.current || '';
  document.getElementById('chat-folder-create').textContent =
    folderModalState.mode === 'rename' ? 'Rename' : 'Create';
  document.getElementById('chat-folder-create').disabled = !input.value.trim();
  document.getElementById('chat-modal-backdrop').hidden = false;
  document.getElementById('chat-folder-modal').hidden = false;
  input.focus();
  input.select();
}
function closeFolderModal(){
  document.getElementById('chat-folder-modal').hidden = true;
  document.getElementById('chat-modal-backdrop').hidden = true;
  folderModalState = null;
}
async function confirmFolderModal(){
  const name = document.getElementById('chat-folder-input').value.trim();
  if (!name || !folderModalState) return;
  if (folderModalState.mode === 'rename'){
    const f = folderById(folderModalState.folderId);
    if (f){ f.name = name; saveTree(); }   // rename persists via the tree PUT
    closeFolderModal();
    return;
  }
  // create: POST, then re-hydrate so the new folder gets a server position.
  try {
    const resp = await fetch('/chat/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name}),
    });
    if (!resp.ok) throw new Error('POST /chat/api/folders -> ' + resp.status);
  } catch (e) { alert(e); return; }
  closeFolderModal();
  await loadRooms(currentRoom);
}
document.getElementById('new-folder-btn').addEventListener('click', () => openFolderModal({mode: 'create', parentId: null}));
document.getElementById('chat-folder-cancel').addEventListener('click', closeFolderModal);
document.getElementById('chat-folder-create').addEventListener('click', confirmFolderModal);
document.getElementById('chat-folder-input').addEventListener('input', e => {
  document.getElementById('chat-folder-create').disabled = !e.target.value.trim();
});
document.getElementById('chat-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); confirmFolderModal(); }
});
```

- [ ] **Step 2: Add the type-to-confirm delete modal helper**

Add the shared delete-confirm driver. It fetches authoritative counts, requires typing the exact name, then calls the right DELETE:

```javascript
// ---- type-to-confirm destructive delete (folder or room) ----
let deleteModalState = null;  // {kind:'folder'|'room', id, name}
function fmtCount(n){ return Number(n).toLocaleString(); }
function openDeleteModal(state, message, confirmName){
  deleteModalState = state;
  document.getElementById('chat-delete-title').textContent =
    state.kind === 'folder' ? 'Delete folder' : 'Delete room';
  document.getElementById('chat-delete-msg').textContent = message;
  document.getElementById('chat-delete-name').textContent = confirmName;
  const input = document.getElementById('chat-delete-input');
  input.value = '';
  const confirmBtn = document.getElementById('chat-delete-confirm');
  confirmBtn.disabled = true;
  input.oninput = () => { confirmBtn.disabled = (input.value !== confirmName); };
  document.getElementById('chat-modal-backdrop').hidden = false;
  document.getElementById('chat-delete-modal').hidden = false;
  input.focus();
}
function closeDeleteModal(){
  document.getElementById('chat-delete-modal').hidden = true;
  document.getElementById('chat-modal-backdrop').hidden = true;
  deleteModalState = null;
}
async function confirmDeleteFolder(folderId){
  const f = folderById(folderId);
  if (!f) return;
  let preview;
  try {
    preview = await getJSON('/chat/api/folders/' + folderId + '/delete-preview');
  } catch (e) { alert(e); return; }
  const msg = 'Are you sure you want to delete ' +
    fmtCount(preview.room_count) + (preview.room_count === 1 ? ' chatroom' : ' chatrooms') +
    ' containing ' + fmtCount(preview.message_count) +
    (preview.message_count === 1 ? ' message' : ' messages') + '? This cannot be undone.';
  openDeleteModal({kind: 'folder', id: folderId, name: f.name}, msg, f.name);
}
async function deleteRoom(uuid){
  const room = rooms.find(r => r.uuid === uuid);
  if (!room) return;
  let preview;
  try {
    preview = await getJSON('/chat/api/rooms/' + uuid + '/delete-preview');
  } catch (e) { alert(e); return; }
  const msg = 'Are you sure you want to delete # ' + preview.room_name + ' containing ' +
    fmtCount(preview.message_count) +
    (preview.message_count === 1 ? ' message' : ' messages') + '? This cannot be undone.';
  openDeleteModal({kind: 'room', id: uuid, name: preview.room_name}, msg, preview.room_name);
}
async function performConfirmedDelete(){
  if (!deleteModalState) return;
  const {kind, id} = deleteModalState;
  const url = kind === 'folder' ? '/chat/api/folders/' + id : '/chat/api/rooms/' + id;
  try {
    const r = await fetch(url, {method: 'DELETE'});
    if (!r.ok) throw new Error('DELETE ' + url + ' -> ' + r.status);
  } catch (e) { alert(e); return; }
  const deletingCurrentRoom = (kind === 'room' && currentRoom === id);
  closeDeleteModal();
  await loadRooms(deletingCurrentRoom ? null : currentRoom);
  if (deletingCurrentRoom){
    currentRoom = null;
    if (rooms[0]){ await selectRoom(rooms[0].uuid); }
    else {
      titleNameEl.value = '';
      log.innerHTML = '';
      const url2 = new URL(window.location);
      url2.searchParams.delete('room');
      history.replaceState(null, '', url2);
      renderSidebar();
    }
  }
}
document.getElementById('chat-delete-cancel').addEventListener('click', closeDeleteModal);
document.getElementById('chat-delete-confirm').addEventListener('click', performConfirmedDelete);
```

> This **replaces** the old `deleteRoom` (L623-645), which used `confirm(...)` and a direct DELETE. Remove the old function body when adding the new one.

- [ ] **Step 3: Verify symbols present and template parses**

Run: `python -c "import webapp.chat_template as t; src=t.CHAT_TEMPLATE; assert src.count('function deleteRoom') == 1, 'old deleteRoom not removed'; [print('missing', s) for s in ['openDeleteModal','confirmDeleteFolder','performConfirmedDelete','openFolderModal'] if s not in src] or print('all present')"`
Expected: prints `all present` (and no assertion error — exactly one `deleteRoom`).

- [ ] **Step 4: Commit**

```bash
git add webapp/chat_template.py
git commit -m "feat(chat): new-folder modal + type-to-confirm destructive delete"
```

---

## Task 12: Manual verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest db/test_chat_folders.py db/test_chat_membership.py db/test_chat_streaming.py webapp/test_chat_folders_api.py webapp/test_chat_membership_api.py -q`
Expected: all PASS.

- [ ] **Step 2: Launch the app and verify in the browser via the `/verify` skill**

Invoke the `verify` skill (or `run` skill) to start the app and open `http://127.0.0.1:5000/chat`. Confirm each behavior:

- [ ] The left panel renders rooms (existing rooms keep their prior order).
- [ ] "+ Folder" opens the modal; creating a folder shows it with the closed-folder icon.
- [ ] Dragging a room onto a folder (middle zone) nests it; the folder icon flips to open.
- [ ] Dragging a room onto another room's top/bottom half reorders it.
- [ ] Dragging a folder onto another folder's top/bottom third reorders as a sibling; middle nests it. A folder cannot be dropped into its own descendant.
- [ ] The "Move to top level" zone appears during a drag and moves a node to root.
- [ ] Collapsing/expanding a folder persists across reload (localStorage).
- [ ] A reorder persists across reload (the PUT saved positions).
- [ ] Folder kebab → Delete shows "Are you sure you want to delete N chatrooms containing M messages?"; the Delete button stays disabled until the exact folder name is typed; confirming removes the folder, its rooms, and their messages.
- [ ] Room kebab → Delete shows the per-room confirm with message count and type-to-confirm.
- [ ] Open two `/chat` tabs; reorder in one, then reorder in the other → the second save 409s and silently re-hydrates (no console error, tree matches server).

- [ ] **Step 3: Confirm no leftover production-DB risk**

The app uses `rainbox_production` by default. For verification, either accept that you're exercising the operator's real `/chat` (read-mostly: creating a test folder + test room is fine), or run the dev server against `rainbox_claude`:
`DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude python webapp.py`
Prefer `rainbox_claude` for the destructive-delete checks so no real rooms/messages are removed.

- [ ] **Step 4: Final commit (if any verification fixups were needed)**

```bash
git add -A
git commit -m "fix(chat): verification fixups for folder tree"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage:**
- Data model (folder table + columns + migration + backfill) → Tasks 1-2. ✅
- DB tree load/validate/save with version token, never deletes rooms → Tasks 3-5. ✅
- Recursive delete preview + recursive folder delete → Task 6. ✅
- API endpoints (tree GET/PUT 409/400, folder create, delete-preview ×2, folder DELETE; room DELETE reuses existing) → Task 7. ✅
- Frontend nested tree + cron icons verbatim + drag-drop + localStorage expand + version-guarded save → Tasks 8-10. ✅
- Destructive type-to-confirm delete (folder + room) with authoritative counts → Task 11. ✅
- Testing (DB + API + manual) → Tasks 2-7, 12. ✅
- Decisions resolved: inline JS (a), keep `GET /chat/api/rooms`, no extra server-side delete hardening. ✅

**Type/name consistency:** browser globals `folders`/`rooms`/`treeVersion`/`dragNode`/`expandedFolders`; functions `folderLi`/`roomNode`/`makeDraggable`/`makeFolderDrop`/`makeRoomDrop`/`moveRoom`/`moveFolder`/`saveTree`/`saveTreePush`/`loadRooms`/`openDeleteModal`/`confirmDeleteFolder`/`deleteRoom`/`performConfirmedDelete` are referenced consistently across Tasks 9-11. Backend names `chat_load_tree`/`chat_tree_version`/`validate_chat_tree`/`chat_save_tree`/`create_chatroom_folder`/`list_chatroom_folders`/`chatroom_folder_delete_preview`/`delete_chatroom_folder`/`chatroom_delete_preview` and exceptions `ChatTreeError`/`ChatTreeConflict` are consistent across Tasks 3-7. API field names match: folders use `{id, name, parentId}`, rooms use `{uuid, folderId}`. ✅

**Known integration caution for the executor:** Tasks 9-11 add code into the existing inline script in `chat_template.py`. Before editing, read the script's bottom (init/`loadRooms` call site) and the existing `deleteRoom`/`buildRoomMenu` so the new code is inserted at the right scope and the old `deleteRoom` is fully replaced (verified by the Step-3 assertions).
