# /kanban folder tree — design

Give the `/kanban` left panel the same nested folder-tree the `/chat` and
`/cron` pages have (folders → leaf items), as documented in
[`docs/left-panel-tree.md`](../../left-panel-tree.md). Here the leaf item is a
**board**: folders nest arbitrarily deep and contain boards. This is a **full
port** of the pattern (matching `/cron`, the fuller reference): nested folders,
drag-and-drop reorder/nest, expand-state persisted to `localStorage`, deep
linking, a static "All boards" root node, and a folder-contents detail table.

## Key architectural decision: two independent save layers

`/kanban` already has a two-level model and we keep them **separate**:

- **Board contents** (columns + tasks) save through the existing per-board
  `PUT /kanban/api/board/<uuid>` — completely untouched by this work.
- **The tree** (folders + which folder each board sits in + ordering) is a new
  layer with its own `GET/PUT /kanban/api/tree`.

The tree save is **placement-only** (the `/chat` shape, not `/cron`'s
full-replace): it upserts folder name/parent/position and updates each board's
`folderId`/`position`, but it **never creates or deletes boards** and never
touches board contents. Board create/delete/duplicate keep their existing
endpoints; folder create/delete get their own endpoints. This avoids `/cron`'s
delete-guard complexity entirely.

## 1. Database

### New table `kanban_board_folder` (`db/models.py`)
Mirror of `CronFolder`:

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `uuid` | UUID unique | |
| `name` | Text default `""` | |
| `description` | Text default `""` | notes about child nodes |
| `parent_uuid` | UUID nullable | null = root; plain column, **no FK** |
| `position` | int default 0 | order within parent |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

Index `kanban_board_folder_children (parent_uuid, position)`. Auto-created by
`db.create_all()` in `init_db` — no hand-written migration.

### `kanban_board.folder_uuid` (`db/models.py` + `db/__init__.py`)
Add `folder_uuid: Mapped[UUID | None]` (null = unfiled/root, plain column, no
FK) plus index `kanban_board_in_folder (folder_uuid, position)`. Backfilled on
existing DBs via:
```python
_add_column_if_missing("kanban_board", "folder_uuid", "folder_uuid UUID")
```
`kanban_board.position` already exists, so ordering needs no backfill (existing
boards are all unfiled at root, which is valid).

### New functions in `db/kanban.py`
- `kanban_load_tree() -> {folders, boards, version}`:
  - `folders`: `[{uuid, name, description, parentId, position}]`
  - `boards`: `[{uuid, name, folderId, position, taskCount}]`
  - `version`: the structural token below.
- `kanban_tree_version() -> str`: sha256 (first 16 hex) over **structural
  fields only**:
  - folder `(uuid, name, parentId, position)`
  - board `(uuid, folderId, position)`
  - **Excluded: board `name` and `taskCount`.** Board renames go through the
    board PUT and agents add tasks in the background; including either would
    make every such background event 409 the next tree save (the doc's
    "exclude volatile fields" gotcha). The displayed board name/count are kept
    in sync client-side (the page already does this for the flat index).
- `validate_kanban_tree(payload)`: folder + board uuids parse and are unique;
  every `parentId` references a folder present in the payload or is null; every
  `folderId` references a folder present in the payload or is null; **reject
  cycles** by walking each folder's ancestor chain. Raises `KanbanError`.
- `kanban_save_tree(payload, *, base_version, actor="human")`: placement-only.
  Validate, then (if `base_version` is stale → `KanbanConflict`) upsert folder
  `name`/`description`/`parent`/`position` and update each board's
  `folder_uuid`/`position` from array order. Folders absent from the payload
  are **not** deleted here (deletion is its own endpoint); boards are never
  created or deleted here.
- `kanban_create_folder(name, parent_uuid=None, description="") -> dict`:
  position = max sibling + 1; returns the folder dict.
- `kanban_delete_folder(folder_uuid) -> bool`: **non-destructive reparent** —
  the folder's direct child folders and boards move up to the deleted folder's
  `parent_uuid` (root if it had none), then the folder row is deleted. Boards
  and their tasks are never deleted by a folder delete. Returns False if the
  folder doesn't exist.
- `kanban_create_board(...)` gains an optional `folder_uuid` so "+ Board" can
  create directly into the selected folder.

## 2. API (`webapp/kanban_api.py`)
- `GET /kanban/api/tree` → `kanban_load_tree()`.
- `PUT /kanban/api/tree` → require a string `version`; `kanban_save_tree(...,
  base_version=version)`; 409 (with fresh `version`) on `KanbanConflict`, 400
  on `KanbanError`; return the fresh `version` on success. Mirrors
  `/cron/api/tree`.
- `POST /kanban/api/folders` → `{name, parentId?, description?}` →
  `kanban_create_folder`; returns `{ok, folder}`.
- `DELETE /kanban/api/folders/<uuid>` → `kanban_delete_folder`; 404 if absent.
- `POST /kanban/api/boards` accepts an optional `folderId` passed through to
  `kanban_create_board`.

## 3. Frontend

### CSS (inline `<style>` in `webapp/kanban_views.py`)
Add tree styles modeled on `/cron`'s:
- `.kb-tree-list ul {list-style:none;margin:0;padding:0}` and
  `.kb-tree-list ul ul {margin-left:.85em;border-left:1px solid #e5e7eb;
  padding-left:.35em}` — indentation + guide line on **nested** lists only
  (the root `<ul class="kb-tree-list">` is skipped).
- `.kb-node` folder row: `box-sizing:border-box`, icon + label + kebab; `.sel`
  highlight (`background:#dbeafe;font-weight:600`); kebab visible only on
  `.kb-node.sel` (never on hover). Open/closed folder icon by
  `isExpanded && hasChildren`.
- "All boards" root pseudo-node style; a "Move to top level" root drop strip
  revealed only while dragging (`.dragging-on` on the panel).
- Folder-contents detail table + explicit `.kb-main [hidden]{display:none}` so
  the bare `hidden` attribute hides panes even though they set `display`.

### State (`static/kanban.js`)
Replace `kbIndex` with:
- `kbFolders` `[{uuid,name,description,parentId,position}]`,
  `kbBoards` `[{uuid,name,folderId,position,taskCount}]`
- `kbExpanded` map (folderId → false when collapsed; default expanded),
  persisted to `localStorage` key `kanban.expandedFolders`
- `kbSelectedFolder` (uuid, or the `"all"` sentinel for the root node, or null)
- `kbTreeVersion`
- `kbDragTree` `{type:'folder'|'board', id}` for tree DnD (separate from the
  existing in-board card drag `kbDrag`)
- a debounced `kbSaveTree` + serialized PUT chain mirroring the existing
  `kbSave`/`kbSaveChain` (250ms; re-hydrate on 409 or network error with a
  toast). Helpers: `kbChildFolders(parentId)`, `kbBoardsInFolder(id)`,
  `kbFolderById(id)`.

### Render
- Recursive `kbFolderLi(f)` / `kbBoardNode(b)` building nested `<ul>`s under
  `kb-board-list` (renamed conceptually to the tree root). Root: "All boards"
  node, then top-level folders, then unfiled boards.
- Folder click = **select-first, then toggle-expand**; board click opens its
  canvas (existing `kbSelectBoard`). Folder-selected and board-open are
  mutually exclusive (selecting one clears the other).
- Kebab actions: on a folder — Rename, New subfolder, Delete; on a board —
  Duplicate, Delete (the existing actions).

### Drag-and-drop (full)
- Folder node → 3 zones by pointer Y: top third **before**, bottom third
  **after**, middle **into** (nest). A dragged board always means "into".
- Board node → 2 zones (before/after) to reorder within its folder.
- "Move to top level" root strip → `parentId/folderId = null`.
- **Cycle guard** `kbFolderInSubtree(candidateId, rootId)` before nesting a
  folder; the DB validator enforces it again server-side.
- On drop: update `parentId`/`folderId` + array order, auto-expand the
  destination, re-render, debounced `kbSaveTree`.

### Folder-contents detail pane
Selecting a folder (or "All boards") shows, in the main area instead of the
board canvas, a depth-indented table of the folder's subtree:
- Flatten subtree depth-first with a depth counter (`kbFlattenTree(parentId)`);
  `"all"` flattens from root.
- Columns: Name (indented by depth, folder vs board icon), Boards/Tasks count,
  and a **Details/Open** link — a sub-folder row drills in (re-selects that
  folder), a board row opens its canvas.
- Toggle the board canvas vs. the folder table with the `hidden` attribute
  (plus the explicit `[hidden]{display:none}` rule above).

### Modals
Folder create / rename use a small modal (name + optional description) via the
shared `ui-modal.css` pattern; folder delete uses the existing `kbConfirm`
overlay (worded as non-destructive: "boards inside move up one level").

### Deep linking
Extend the existing `?board=<uuid>` mirroring with `?folder=<uuid>` (and
`?folder=all`) so folder / All-boards selection is restorable, mutually
exclusive with `?board=`.

## 4. Testing
Mirror the cron/chat tree tests:
- **db** (`db/test_kanban_*` or extend existing): `load_tree` shape;
  `save_tree` round-trip (reorder, nest, move board between folders);
  `tree_version` — **a board rename via the board PUT and a new task added by
  an agent must NOT change the tree version**; `save_tree` 409 on stale
  `base_version`; `validate_kanban_tree` rejects dangling `parentId`/`folderId`
  and cycles; `kanban_delete_folder` reparents children (boards + tasks
  survive, child folders move to grandparent).
- **webapp** (`webapp/test_kanban_api.py`): tree GET/PUT happy path + 409;
  folder POST/DELETE; `POST /boards` honoring `folderId`.

## Out of scope
- No change to board contents save, the markdown/JSON serializations, or the
  agent task operations (claim/move/complete/etc.).
- No FK constraints (matches the cron/chat tables — app-side validation only).
- Folder delete never cascades to boards (deliberate; reparent instead).
