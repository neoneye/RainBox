# Chat folders + reordering — design

**Date:** 2026-06-15
**Status:** Approved (brainstorming), pending implementation plan

## Goal

The `/chat` left panel currently shows a flat list of chatrooms ordered by
`created_at`. We want to:

1. Group chatrooms into **nested folders** (folders can contain rooms and
   other folders, unlimited depth).
2. **Reorder** folders and rooms by drag-and-drop.

The model and UX mirror the existing `/cron` page (`CronFolder` tree +
`position` ordering + HTML5 drag-drop), reusing its folder/folder-open Lucide
icons pixel-for-pixel. Folder organization is **global/shared** (one tree for
the whole instance, stored on the rooms/folders themselves — exactly like
cron), not per-user.

One deliberate divergence from cron: **deleting a folder is destructive and
recursive**, guarded by a count-aware type-to-confirm dialog (see §5).

## Non-goals (YAGNI)

- No per-folder notes/description (cron has `description`; chat folders are
  purely organizational).
- No folder enable/disable cascade (cron's `enabled` exists for scheduling).
- No per-user folder layouts.
- No "create room/folder inside the selected folder" — new items are created at
  top level for now (can be moved by drag afterward).

## Existing code this builds on

- **DB layer:** `db/chat.py` (chatroom CRUD, `list_chatrooms`,
  `delete_chatroom` which already cascades messages/members via FK), `db/cron.py`
  (the tree load/validate/save pattern to copy).
- **Models:** `db/models.py` — `Chatroom` (~line 495), `CronFolder` (~line 203)
  as the structural template.
- **Schema migration:** `db/__init__.py` — `init_db()` calls `db.create_all()`
  (creates new tables) then a series of `_add_column_if_missing(table, column,
  ddl)` calls for new columns on existing tables. **There is no Alembic.** New
  table → automatic via `create_all`; new columns on `chatroom` → add
  `_add_column_if_missing` calls.
- **API:** `webapp/cron_api.py` (`/cron/api/tree` GET/PUT, version-guarded) as
  the template for `/chat/api/tree`. Existing chat API in `webapp/chat_api.py`.
- **Frontend:** `/chat` is rendered inline from `webapp/chat_template.py`
  (HTML/CSS/JS in one Python string). `/cron`'s tree + drag-drop lives in
  `static/cron.js` (external file with a cache-buster). The room list is
  rendered by `renderRooms()` in the chat template.

## 1. Data model

### New table: `ChatroomFolder` (`db/models.py`)

Mirrors `CronFolder` minus `description`/`enabled`:

```python
class ChatroomFolder(db.Model):
    __tablename__ = "chatroom_folder"
    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[UUID] = mapped_column(unique=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, default="")
    parent_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = root; plain col, no FK
    position: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC))
    __table_args__ = (Index("chatroom_folder_children", "parent_uuid", "position"),)
```

### `Chatroom` gains two columns

```python
folder_uuid: Mapped[UUID | None] = mapped_column(default=None)  # null = top level; plain col, no FK
position: Mapped[int] = mapped_column(default=0)
```

### Migration (`db/__init__.py` `init_db`)

- `create_all()` makes `chatroom_folder`.
- Add for existing DBs:
  - `_add_column_if_missing("chatroom", "folder_uuid", "folder_uuid UUID")`
  - `_add_column_if_missing("chatroom", "position", "position INTEGER NOT NULL DEFAULT 0")`
- **Backfill ordering once:** existing rooms must keep their current visible
  order (today: `created_at ASC`). After adding the column, run a one-time
  backfill that sets `position` by `created_at` rank for rooms still at
  `position = 0` default, so nothing visibly reorders on first load. (Implement
  as an idempotent guard in `init_db`, consistent with the existing
  one-time-DDL blocks there.)

No FKs on `folder_uuid`/`parent_uuid` (matches the cron/kanban "plain uuid
column, app-side validation" house style). A deleted folder's rooms are handled
explicitly by the delete op (§5), not by a DB cascade.

## 2. DB-layer functions (`db/chat.py`)

Port the cron tree functions, chat-flavored:

- `chat_load_tree() -> dict` — returns `{folders, rooms, version}`.
  - `folders`: `[{id, name, parentId, ...}]` ordered by `(position, id)`.
  - `rooms`: the current `list_chatrooms()` payload (`uuid, name,
    member_count, last_message_id`) **plus** `folderId` and ordered by
    `(position, id)`. Keep member_count/last_message_id so the existing left
    panel keeps working.
  - `version`: `chat_tree_version()` — SHA256-prefix hash over the
    user-managed fields only (folder: uuid/name/parentId/position; room:
    uuid/folderId/position). **Volatile fields are excluded** so a new message
    (changing `last_message_id`) does NOT invalidate an open page's version —
    only a structural edit by another writer does. (This mirrors cron excluding
    scheduler bookkeeping from its hash.)
- `validate_chat_tree(folders, rooms)` — structural check before any write:
  valid uuids, unique folder ids, parent is null-or-known, **acyclic** folder
  graph, room `folderId` null-or-known, no uuid collisions across kinds. Raises
  `ChatTreeError` (→ 400). Direct port of `validate_cron_tree`.
- `chat_save_tree(folders, rooms, *, base_version=None)` — upsert folders by
  uuid (insert new / update existing / **delete folders absent from payload**);
  update each room's `folder_uuid` + `position` by list index. Version-guarded:
  stale `base_version` raises `ChatTreeConflict` (→ 409).
  - **Important difference from cron:** `chat_save_tree` only ever
    creates/moves/reorders folders and reassigns rooms. It must **never delete a
    chatroom** (rooms carry messages; an accidental truncated payload must not
    wipe data). Rooms are never created or deleted by the tree save — only their
    `folder_uuid`/`position` change. A room uuid missing from the payload is an
    error (refuse the save), not a delete. Folder deletion goes exclusively
    through the dedicated destructive endpoint in §5.
- `ChatTreeError(ValueError)`, `ChatTreeConflict(Exception)` — exception types
  (mirror cron).

Re-export the new public names from `db/__init__.py` (or `db`'s re-export
surface) consistent with how `cron_*` names are exposed.

## 3. API (`webapp/chat_api.py`)

- `GET /chat/api/tree` → `db.chat_load_tree()`.
- `PUT /chat/api/tree` → body `{folders, rooms, version}`; calls
  `chat_save_tree(..., base_version=version)`. Maps `ChatTreeConflict` → 409
  (with fresh `version` so the page re-hydrates), `ChatTreeError` → 400. Returns
  `{ok, version}`. (Direct analog of `cron_tree`. We can keep cron's `deletes`
  guard out here since the tree save never deletes rooms; folder-only deletes go
  through §5.)
- `POST /chat/api/folders` → `{name}`; creates a folder (at root, position
  after existing roots). Returns the new folder.
- `GET /chat/api/folders/<uuid>/delete-preview` → `{room_count, message_count,
  folder_name}` — **authoritative recursive counts** of everything under the
  folder, for the confirmation dialog (§5).
- `DELETE /chat/api/folders/<uuid>` → recursive destructive delete (§5).
- `DELETE /chat/api/rooms/<uuid>` → delete one room + its messages (same
  confirmation UX; `delete_chatroom` already cascades). Add a matching
  `GET /chat/api/rooms/<uuid>/delete-preview` → `{message_count, room_name}`.

The existing `GET /chat/api/rooms` may remain (or be superseded by
`/chat/api/tree`); the left panel switches to consuming `/chat/api/tree`.

## 4. Frontend (`webapp/chat_template.py`)

Bring cron's tree UX into the inline chat template. Two viable placements for
the JS — decide in the plan:

- **(a)** keep it inline in `chat_template.py` (matches today's chat structure), or
- **(b)** factor a `static/chat.js` like cron (cleaner, cache-busted).

Recommended: **(a)** for now to stay consistent with the existing chat page,
unless the inline JS grows unwieldy — then split. The plan should make the call.

Behavior to port from `static/cron.js`:

- Render the left panel as a **nested tree**: collapsible folders containing
  subfolders and rooms, plus top-level rooms. Each room keeps its `#` prefix,
  unread badge, member-count subtitle, and selected-state kebab menu.
- **Icons reused pixel-for-pixel from cron:** closed-folder
  (`CRON_ICON_FOLDER`), open-folder (`CRON_ICON_FOLDER_OPEN`), and the
  expand/collapse chevron. Copy the inlined Lucide SVG strings.
- **Drag-and-drop (HTML5 API, no library):** folder nodes have three drop zones
  (top third = reorder before, middle = nest into, bottom third = reorder
  after); room nodes have two (before/after within a folder). A root drop zone
  handles "move to top level." Same visual feedback classes
  (`.dragging`, `.drop-target`, `.drop-before`, `.drop-after`).
- **Persistence:** drag operations mutate the in-browser arrays, reassign
  positions, and debounce-PUT to `/chat/api/tree` with the held version token;
  a 409 re-hydrates. Same pattern as cron's `cronSavePush`.
- **Expand/collapse state** persisted client-side (localStorage), like cron.
- **New-folder control** in the panel (calls `POST /chat/api/folders`, then
  re-hydrates / inserts the node). The existing create-room form stays; new
  rooms appear at top level.

Live updates: the chat page already has an SSE channel for new messages. Tree
structure changes are driven by the operator's own actions; the version-guard
handles the rare two-tab case. (No new realtime push for the tree is in scope.)

## 5. Destructive folder/room delete with type-to-confirm

This is the explicit requirement and the one real divergence from cron.

- **Delete preview:** before deleting, the client calls the
  `…/delete-preview` endpoint to get authoritative recursive counts.
- **Confirmation modal:**
  > "Are you sure you want to delete **51 chatrooms** containing **93,230
  > messages**?"
  - plus a text input; the **Delete** button stays disabled until the user
    types the folder's exact name (for a single-room delete: the room name).
    Counts are formatted with thousands separators.
  - For an empty folder the message degrades gracefully (e.g. "delete this empty
    folder?") but still requires typed confirmation for consistency.
- **Backend recursive delete (`db/chat.py`, e.g. `delete_chatroom_folder`):**
  1. Walk the folder subtree (folder + all descendant folders) — guard against
     a parent cycle.
  2. Delete every chatroom whose `folder_uuid` is in that set
     (`delete_chatroom` cascades messages + members + workspace_shell_state via
     existing FKs).
  3. Delete the folder rows.
  4. Commit.
  - The typed-name check is a UX guard in the browser; the server still
    requires the explicit `DELETE` call to act. (Optional belt-and-suspenders:
    the plan may add a server-side confirmation token, but it is not required.)

## 6. Testing

DB-layer (`db/test_chat_folders.py` or extend existing chat tests), all on
`rainbox_claude` via conftest:

- Folder create / nest / reparent; position ordering within a parent.
- `chat_load_tree` shape, ordering, and `chat_tree_version` stability (a new
  message must NOT change the version; a structural edit must).
- `validate_chat_tree`: rejects bad uuids, dangling parent, **cycles**, unknown
  room folderId, cross-kind uuid collision.
- `chat_save_tree`: upsert/move/reorder; **refuses to drop a room** missing from
  the payload; 409 on stale `base_version`.
- Recursive delete-preview counts (rooms + messages across nested subfolders).
- Recursive `delete_chatroom_folder`: removes the right rooms + messages +
  members + subfolders, leaves unrelated rooms untouched.

API (`webapp/test_chat_*`):

- `GET/PUT /chat/api/tree` incl. 409 on stale version, 400 on malformed.
- `POST /chat/api/folders` create.
- `…/delete-preview` counts; `DELETE` folder/room happy path + 404 unknown uuid.

Frontend: manual verification via `/verify` (drag-reorder, nest, collapse,
delete-confirm) — no JS test harness exists in this repo for the inline pages.

## Open decisions for the plan

1. Inline JS (a) vs `static/chat.js` (b) — recommend (a) unless it bloats.
2. Whether to retire `GET /chat/api/rooms` or keep it alongside `/chat/api/tree`.
3. Exact server-side confirmation hardening for destructive delete (token vs
   none) — default: none, rely on the explicit DELETE + browser type-confirm.
