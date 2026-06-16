# Left-panel folder tree (nested hierarchy pattern)

Several pages have a left panel that shows a **tree of folders** (which nest
arbitrarily deep) containing **leaf items**. `/chat` (folders → chatrooms),
`/cron` (folders → jobs), and `/kanban` (folders → boards) all implement it;
this doc describes the shared pattern and the reference implementations.
`/kanban` is the placement-only variant whose tree layer (folders + board
placement) is kept separate from board contents: `webapp/kanban_views.py`
(markup + CSS), `static/kanban.js` (tree JS), `webapp/kanban_api.py` +
`db/kanban.py` (`kanban_load_tree`/`kanban_save_tree`/`kanban_tree_version`/
`validate_kanban_tree`, folder create + reparenting delete).

Folder create/rename/delete dialogs use the app-wide modal pattern — see
[`ui-modals.md`](ui-modals.md).

## Reference implementations

| | `/chat` (folders → rooms) | `/cron` (folders → jobs) |
|---|---|---|
| Markup + CSS | `webapp/chat_template.py` | `webapp/cron_views.py` |
| Tree JS | inline in `chat_template.py` | `static/cron.js` |
| API | `webapp/chat_api.py` | `webapp/cron_api.py` |
| DB | `db/chat.py` | `db/cron.py` |

`/chat` is the simpler one; `/cron` is the fuller one (folder enable/disable +
description, a static "All jobs" root node, URL deep-linking, a delete guard).
Read both; pick the feature set the new page needs.

## 1. Data model — flat arrays, parent pointers

Both pages hold the tree as **two flat client-side arrays** and represent
nesting with a **parent pointer**, never a nested-children structure. Children
are computed on demand by filtering.

```js
let folders = [];   // [{ id, name, parentId, ...page-specific }]   parentId=null → root
let items   = [];   // [{ uuid, name, folderId, ...page-specific }] folderId=null  → top level

const childFolders   = (parentId) => folders.filter(f => (f.parentId || null) === parentId);
const itemsInFolder  = (id)       => items.filter(i => (i.folderId || null) === id);
const folderById     = (id)       => folders.find(f => f.id === id) || null;
```

- `/chat`: `folders {id,name,parentId}`, `rooms {uuid,name,folderId,member_count,last_message_id}` — `chat_template.py:296-311`.
- `/cron`: `cronFolders {id,name,description,parentId,enabled,...}`, `cronRowsState` (jobs, with `folderId`,`enabled`,`cron`,…) — `static/cron.js:170-177`.

DB side: folders and items each carry `parent_uuid`/`folder_uuid` (null = root)
plus a `position` integer for ordering within a parent. There are **no FK
constraints** — parent refs are plain UUID columns validated in the application
layer (`validate_chat_tree` / `validate_cron_tree`), which is what catches
dangling refs and cycles.

## 2. Persistence — whole-tree PUT, version-guarded, debounced

Hydrate once with `GET /<page>/api/tree` → `{folders, items, version, …}`. Save
with `PUT /<page>/api/tree` sending the **entire** folder + item lists; array
order becomes each row's `position`.

- **Version guard (optimistic concurrency).** `version` is a hash
  (`chat_tree_version` / `cron_tree_version`) over only the *structural* fields
  (uuid, name, parent, position, …) — **volatile bookkeeping is excluded**
  (`last_message_id`, `next_run_at`, `updated_at`), so a background message or a
  scheduler tick never invalidates an open page. The client sends the version it
  hydrated with; if it's stale the server returns **409** and the client
  re-hydrates (and, on `/cron`, shows a "tree changed elsewhere — reloaded"
  toast). `chat_api.py:104-127` / `db/chat.py:320-376`; `cron_api.py:22-53` /
  `db/cron.py:319-412`.
- **Debounce + serialize.** Edits coalesce (chat 300ms `saveTree`→`saveTreePush`;
  cron 250ms `cronSave`) and only one PUT is in flight at a time; a save
  requested mid-flight is queued and re-sent after.
- **Two save shapes — pick one:**
  - `/cron` PUT is a **full replace**: rows whose uuid is absent from the
    payload are *deleted*. A `deletes` counter (`cronPendingDeletes` →
    `expected_deletes`) guards against a frontend bug silently truncating the
    tree (server rejects if it would delete more than declared).
  - `/chat` PUT is **placement-only**: it upserts folders and updates room
    folder/position, but creation and deletion go through separate endpoints
    (`POST /chat/api/folders`, `DELETE /chat/api/folders/<uuid>` which cascades).
  The full-replace shape is more powerful (one path for create/move/delete) but
  needs the delete guard; the placement-only shape is simpler and safer by
  construction. New pages can start placement-only.

## 3. Rendering — recursive, computed children

Render the root, then recurse. The folder renderer emits a node row and, if the
folder is expanded and non-empty, a nested `<ul>` of its child folders
(recursing) followed by its leaf items.

```js
function renderTree(){
  const ul = document.createElement('ul');
  childFolders(null).forEach(f => ul.appendChild(folderLi(f)));   // root folders
  itemsInFolder(null).forEach(i => ul.appendChild(itemNode(i)));  // root items
  treeRoot.replaceChildren(ul);
}
function folderLi(f){
  const li = ...;                       // .chat-node / .cron-node row: icon + label + kebab
  if (isExpanded(f.id) && hasChildren(f)){
    const sub = document.createElement('ul');
    childFolders(f.id).forEach(c => sub.appendChild(folderLi(c)));   // recurse
    itemsInFolder(f.id).forEach(i => sub.appendChild(itemNode(i)));
    li.appendChild(sub);
  }
  return li;
}
```

`/chat`: `renderRooms`/`folderLi`/`roomNode` (`chat_template.py:640-855`).
`/cron`: `cronRenderTree`/`cronFolderLi`/`cronJobNode` (`static/cron.js:983-1030`).

**Indentation + guide line are pure CSS** — nesting comes from the nested `<ul>`,
and the vertical guide is a left border on *nested* lists only (the
double-descendant selector skips the root `<ul>`):

```css
#rooms ul{list-style:none;margin:0;padding:0}
#rooms ul ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
```

`/cron` uses the same values on `.cron-tree-list ul` (root is the classed
`<ul class="cron-tree-list">`, so `.cron-tree-list ul` matches only nested
lists). The folder icon swaps open/closed based on `isExpanded && hasChildren`.

**Icons — match `/chat` exactly; don't invent your own.** This bit `/kanban`
(emoji + a caret were used, then reworked):
- **Use the shared inline Lucide folder SVGs**, not emoji. `/chat` defines
  `CHAT_ICON_FOLDER` (closed) and `CHAT_ICON_FOLDER_OPEN` (open) verbatim from
  lucide.dev (`chat_template.py:293-294`); copy those two constants. They're
  `stroke="currentColor"` and sized by the wrapper span
  (`.chat-ficon{width:1.05em;height:1.05em}` + `.chat-ficon svg{width:100%}`),
  so they inherit row colour/size.
- **The folder icon IS the expand indicator** — it flips open↔closed on
  `isExpanded && hasChildren`. Do **not** add a separate twisty/caret (▾/▸)
  column; neither `/chat` nor `/cron` has one.
- **Leaf items carry no icon.** `/chat` rooms and `/cron` jobs render name-only;
  a leaf icon looks wrong next to the folders.
- A root "All X" node is name-only too (see §5).

## 4. Expand / collapse state

A plain map keyed by folder id, **default expanded**:

```js
let expanded = {};                       // folderId -> false when collapsed
const isExpanded = (id) => expanded[id] !== false;
```

`/chat` persists this to `localStorage` (`FOLDER_EXPAND_KEY = 'chat.expandedFolders'`,
`chat_template.py:300-312`) so it survives reload; `/cron` keeps it in memory
only (`cronExpanded`, `static/cron.js:174,706`) — it resets on refresh. Persisting
is the nicer UX; do that on new pages.

## 5. Selection model

- **Click = select-first, then toggle-expand.** First click on a folder
  *selects* it (shows its contents in the right pane); clicking the
  already-selected folder toggles its expand/collapse. `chat_template.py:801-814`
  / `cronFolderClick` `static/cron.js:857-871`.
- **Selected highlight** is `.sel` on the folder node (`background:#dbeafe;
  font-weight:600`).
- **Kebab visible only on the selected/active node** (`.chat-node.sel >
  .room-actions`, `.cron-node.sel .cron-kebab`) — never on hover. Leaf items
  show their kebab only when active/open.
- **Mutual exclusivity:** a folder being selected and an item being open are
  exclusive — selecting a folder clears the open item and vice-versa
  (`selectedFolder` ↔ `currentRoom` / `cronSelectedFolder` ↔ `cronEditUuid`).
- **Root pseudo-node (optional).** `/cron` has a static `#cron-all-jobs` node
  where `selectedFolder === null` shows the whole flattened tree. `/chat` has no
  such node (nothing selected → no table). Add one if "show everything" is useful.
  **It is a static element in the markup, NOT the first row of the rendered
  tree** — render only toggles its `.sel` class and a one-time listener wires
  its click (`cron.js:1019,1449`). `/kanban` first rendered it as the top tree
  `<li>`, then had to move it out to match the sidebar layout below.
- **Sidebar layout — copy `/cron`'s chrome order exactly** (`cron_views.py:186-200`):
  the static "All X" node, an `<hr class="*-tree-sep">`, the action buttons
  (`+ Folder` / `+ Item`), another `<hr>`, then the tree `<ul>`, then the
  drag-only root-drop strip. Separator rule:
  `.*-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}`. Don't
  put the action buttons at the very top with no separators — that diverges from
  `/cron` and was a `/kanban` rework.
- **URL deep-linking — one `?id=<uuid>` param, not per-kind params.** `/cron`
  mirrors the selection to a single `?id=<uuid>` that addresses **either** a
  folder or an item (uuids are globally unique across kinds — see the validator
  collision check in §1/§9), restoring it on load by trying folder-by-id then
  item-by-id (`cronSyncUrl`/init `static/cron.js:893-899,1625-1633`). The root
  "All X" node has no uuid, so it maps to **no `?id=`** (the default view) —
  don't invent an `?id=all`. All three pages now use this single-`?id=` form;
  both `/kanban` (which first shipped `?board=`/`?folder=`/`?folder=all`) and
  `/chat` (which first shipped `?room=` only, with folders not deep-linked) were
  reworked to it. So: **use one `?id=` from the start, covering folder *and*
  item, and don't add per-kind params.**

## 6. Drag-and-drop reorder / nest

One drag state object (`{type:'folder'|'item', id}`), set on `dragstart`,
cleared on `dragend`. Drop targets:

- **Folder node → three zones** by pointer Y: top third = drop **before**
  (reorder as sibling), bottom third = **after**, middle third = **into** (nest
  as child). A dragged *item* always means "into". `makeFolderDrop`
  (`chat_template.py:951-997`) / `cronMakeFolderDrop` (`static/cron.js:1343-1391`).
- **Leaf node → two zones** (before / after) for reordering within its folder.
- **Root drop zone:** a "Move to top level" strip (`#chat-root-drop` /
  `#cron-root-drop`) revealed only while dragging (`.dragging-on` toggled on the
  panel), dropping to `parentId = null`.
- **Cycle prevention is mandatory:** before nesting a folder, walk the target's
  ancestor chain and refuse if the dragged folder is an ancestor —
  `folderInSubtree(candidateId, rootId)` (`chat_template.py:858-865`) /
  `cronFolderInSubtree` (`static/cron.js:1244-1251`). The DB validator enforces
  it again server-side (belt and suspenders).
- On drop: update the moved node's `parentId`/`folderId` + reorder the array,
  **auto-expand the destination folder**, re-render, then debounced-save.

## 7. Folder-contents detail pane (right side)

Selecting a folder shows a table of its **recursive subtree** in the right pane
(instead of an item view). Build it by flattening the subtree depth-first with a
depth counter, then render depth-indented rows:

- `/cron`: `cronFlattenTree(parentId)` → `[{kind, node, depth}]`
  (`static/cron.js:233-245`); rows indented `depth*20px`.
- `/chat`: a pre-order `walk(folderId, depth)` in `renderFolderDetailRows`
  (`chat_template.py:691-717`); indent via non-breaking spaces.

Each row has a **Details** link: on a **subfolder** row it drills in (re-selects
that folder); on a **leaf** row it opens the item (the normal item view). Columns
are page-specific (chat: Name / Agents / Messages / Last message; cron: Active /
id / Name / Schedule / Next / Health / Command / Description).

The right pane shows exactly one of: an item view, or the folder table — toggle
visibility with the `hidden` attribute. Note: if the panes set `display` at
class specificity, the bare `hidden` attribute won't hide them — add explicit
`.pane[hidden]{display:none}` rules (this bit `/chat`'s chat-log/compose).

## 8. Gotchas

- **`.node{width:100%}` + padding + default `content-box` overflows right.** If
  the folder row sets `width:100%` *and* horizontal padding, its box overflows
  its `<li>` by the padding, pushing an absolutely-positioned kebab further right
  than the leaf rows' kebab. Fix: `box-sizing:border-box` on the node (or don't
  set `width` — `/cron`'s `.cron-node` sets none). This was a real `/chat` bug.
- **Guide-line selector must skip the root list** — use `ul ul` (or
  `.tree-root ul`), else the top level gets an unwanted left border.
- **Exclude volatile fields from the version hash**, or every background event
  (new message, scheduler tick) makes the next save 409.
- **Re-hydrate on any save failure** (409 *or* network error) so the client
  converges to server truth instead of drifting.
- **Kebab only on the selected node, not on hover** — matches across pages.

## 9. Porting checklist (e.g. `/kanban`: folders → boards)

1. **DB:** folder + item tables with `parent_uuid`/`folder_uuid` (null = root) +
   `position`; `*_load_tree`, `*_save_tree(base_version)`, `*_tree_version`
   (structural fields only), `validate_*_tree` (reject dangling/cyclic). No FKs.
2. **API:** `GET/PUT /<page>/api/tree` (+ `POST/DELETE folders` if going
   placement-only like `/chat`). Return 409 on version mismatch.
3. **State:** `folders`/`items` arrays, `childFolders`/`itemsInFolder`/`folderById`,
   `expanded` map (persist to localStorage), `selectedFolder`, open-item id,
   `dragState`, `treeVersion`.
4. **Render:** recursive `folderLi`/`itemNode`; nested `<ul>`; the `ul ul`
   border-left guide; `box-sizing:border-box` on the node row. **Icons: the two
   shared Lucide folder SVGs (open/closed, flipping on `isExpanded &&
   hasChildren`) — no emoji, no twisty caret, no leaf icon (see §3).**
5. **Selection:** select-first/toggle-expand; `.sel` highlight; kebab only on
   selected; folder-vs-item mutual exclusivity. **Sidebar layout = `/cron`'s
   chrome:** static "All X" node → `<hr>` → action buttons → `<hr>` → tree (§5).
   **Deep-link with one `?id=<uuid>`** (folder or item; "All X" → no param),
   never per-kind params.
6. **Drag-drop:** folder 3-zone (before/after/into) + leaf 2-zone + root drop;
   `folderInSubtree` cycle guard; auto-expand on drop; debounced save.
7. **Detail pane:** flatten subtree with depth; depth-indented rows; per-row
   Details link (folder drills in, item opens); `hidden`-attr pane toggle (+
   explicit `[hidden]{display:none}` if the pane sets `display`).
8. **Modals** for folder create/rename/delete per [`ui-modals.md`](ui-modals.md).
