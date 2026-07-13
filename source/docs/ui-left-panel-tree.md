# Left-panel folder tree (nested hierarchy pattern)

Several pages have a left panel that shows a **tree of folders** (which nest
arbitrarily deep) containing **leaf items**. `/chat` (folders → chatrooms),
`/cron` (folders → jobs), `/kanban` (folders → boards), `/git`
(folders → repos), and `/prompt` (folders → system prompts) all implement it;
this doc describes the shared pattern and the reference implementations.
`/kanban` is the placement-only variant whose tree layer (folders + board
placement) is kept separate from board contents: `webapp/kanban_views.py`
(markup + CSS), `static/kanban.js` (tree JS), `webapp/kanban_api.py` +
`db/kanban.py` (`kanban_load_tree`/`kanban_save_tree`/`kanban_tree_version`/
`validate_kanban_tree`, folder create + reparenting delete). `/git` follows
the same split (`webapp/git_views.py`, `static/git.js`, `webapp/git_api.py`,
`db/git.py`) — its build produced the CSS/layout gotchas in §8. `/prompt`
(`webapp/prompt_views.py`, `static/prompt.js`, `webapp/prompt_api.py`,
`db/prompt.py`) is a rule-for-rule port of `/git` whose leaf detail pane is an
editor: leaf content stays out of the tree payload and version hash, saved via
a separate per-item PUT, so saving content never 409s an open tree.

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
Read both; pick the feature set the new page needs. `/kanban` and `/git` are
ports of the pattern rather than references — consult them for how a port
goes (and, for `/git`, how it goes wrong: §8).

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

- `/chat`: `folders {id,name,parentId}`, `rooms {uuid,name,folderId,member_count,last_message_id}` — `chat_template.py`.
- `/cron`: `cronFolders {id,name,description,parentId,enabled,...}`, `cronRowsState` (jobs, with `folderId`,`enabled`,`cron`,…) — `static/cron.js`.

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
  toast). `chat_api.py` / `db/chat.py`; `cron_api.py` /
  `db/cron.py`.
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

`/chat`: `renderRooms`/`folderLi`/`roomNode` (`chat_template.py`).
`/cron`: `cronRenderTree`/`cronFolderLi`/`cronJobNode` (`static/cron.js`).

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
  lucide.dev (`chat_template.py`); copy those two constants. They're
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
`chat_template.py`) so it survives reload; `/cron` keeps it in memory
only (`cronExpanded`, `static/cron.js`) — it resets on refresh. Persisting
is the nicer UX; do that on new pages.

## 5. Selection model

- **Click = select-first, then toggle-expand.** First click on a folder
  *selects* it (shows its contents in the right pane); clicking the
  already-selected folder toggles its expand/collapse. `chat_template.py`
  / `cronFolderClick` `static/cron.js`.
- **Selected highlight** is a tint **and bold** — `background:#dbeafe;
  font-weight:600` — on the `.sel` folder node. **Apply the same to the selected
  leaf row, not just folders.** `/chat` and `/cron` first bolded only folders
  (their room/job rows changed background only), so a selected leaf didn't read
  as selected; both were fixed to add `font-weight:600` to `.room.active` /
  `.cron-job-node.sel`. `/kanban` shares one `.kb-node.sel` for folders and
  boards, so it bolds both by construction — the simplest way to not miss it.
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
  its click (`cron.js`). `/kanban` first rendered it as the top tree
  `<li>`, then had to move it out to match the sidebar layout below.
- **Sidebar layout — copy `/cron`'s chrome order exactly** (`cron_views.py`):
  the static "All X" node, an `<hr class="*-tree-sep">`, the action buttons
  (`+ Folder` / `+ Item`), another `<hr>`, then the tree `<ul>`, then the
  drag-only root-drop strip. Separator rule:
  `.*-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}`. Don't
  put the action buttons at the very top with no separators — that diverges from
  `/cron` and was a `/kanban` rework.
- **Leaf rows are real links — `<a href="/<page>?id=<uuid>">`, not
  buttons/divs.** CMD/Ctrl/Shift-click and middle-click must open the item in a
  new tab (via the `?id=` deep link below); a JS-only click handler on a
  non-anchor gives the browser nothing to open. A plain click is intercepted
  and selects in-page as before; modified clicks return early so the browser
  handles them:

  ```js
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    selectItem(id);
  });
  ```

  CSS: the anchor needs `text-decoration:none` (plus `color:inherit` if the
  row class sets no color), or rows render as blue underlined links. Two
  structures exist, differing in where the kebab and drag source live:
  - `/chat`: the anchor sits **inside** a `.room-row` wrapper that is the drag
    source and kebab host — set `draggable = false` on the anchor so dragging
    moves the row, not the link's URL (`roomNode`, `chat_template.py`).
  - `/cron` and `/kanban`: the node element **is** the anchor (drag source and
    kebab host in one). The kebab and its menu are then nested inside the
    anchor, so their click handlers must call `e.preventDefault()` in addition
    to `stopPropagation()` — otherwise a menu click can follow the link
    (`cronJobNode`/`cronMakeKebab`, `static/cron.js`; `kbBoardNode`/
    `kbMakeKebab`, `static/kanban.js`).

  Folders stay non-link divs on all pages — their click is select/toggle, and
  the folder deep link remains reachable via the URL. `/git` and `/prompt`
  still render div leaves (no new-tab support yet); use the anchor form when
  touching them or building a new page.
- **URL deep-linking — one `?id=<uuid>` param, not per-kind params.** `/cron`
  mirrors the selection to a single `?id=<uuid>` that addresses **either** a
  folder or an item (uuids are globally unique across kinds — see the validator
  collision check in §1/§9), restoring it on load by trying folder-by-id then
  item-by-id (`cronSyncUrl`/init `static/cron.js`). The root
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
  (`chat_template.py`) / `cronMakeFolderDrop` (`static/cron.js`).
- **Leaf node → two zones** (before / after) for reordering within its folder.
- **Root drop zone:** a "Move to top level" strip (`#chat-root-drop` /
  `#cron-root-drop`) revealed only while dragging (`.dragging-on` toggled on the
  panel), dropping to `parentId = null`.
- **Cycle prevention is mandatory:** before nesting a folder, walk the target's
  ancestor chain and refuse if the dragged folder is an ancestor —
  `folderInSubtree(candidateId, rootId)` (`chat_template.py`) /
  `cronFolderInSubtree` (`static/cron.js`). The DB validator enforces
  it again server-side (belt and suspenders).
- On drop: update the moved node's `parentId`/`folderId` + reorder the array,
  **auto-expand the destination folder**, re-render, then debounced-save.

## 7. Folder-contents detail pane (right side)

Selecting a folder shows a table of its **recursive subtree** in the right pane
(instead of an item view). Build it by flattening the subtree depth-first with a
depth counter, then render depth-indented rows:

- `/cron`: `cronFlattenTree(parentId)` → `[{kind, node, depth}]`
  (`static/cron.js`); rows indented `depth*20px`.
- `/chat`: a pre-order `walk(folderId, depth)` in `renderFolderDetailRows`
  (`chat_template.py`); indent via non-breaking spaces.

Each row has a **Details** link: on a **subfolder** row it drills in (re-selects
that folder); on a **leaf** row it opens the item (the normal item view). Columns
are page-specific (chat: Name / Agents / Messages / Last message; cron: Active /
id / Name / Schedule / Next / Health / Command / Description). **Show the leaf's
plain name in the Name column** — the same string the tree shows, with no
view-specific decoration (`/chat` prefixed room names with `# ` here and it had
to be removed; the tree itself never prefixed them).

The right pane shows exactly one of: an item view, or the folder table — toggle
visibility with the `hidden` attribute. Note: if the panes set `display` at
class specificity, the bare `hidden` attribute won't hide them — add explicit
`.pane[hidden]{display:none}` rules (this bit `/chat`'s chat-log/compose).

**Hide the ENTIRE item view, not just its body — or the previous item's data
leaks into the folder view.** The item view is usually several sibling elements:
a header/title bar, the body, a footer/compose, *and* a secondary right sidebar.
`/chat` first hid only the chat-log + compose, so selecting a folder still showed
the last room's **name in the title-bar input** and its **members/stats in the
right sidebar**. Fix: on folder-select, hide every item-view element (title bar
included — add its own `[hidden]{display:none}` if it's `display:flex`) and
re-render the secondary sidebar (with no item selected it should clear itself —
`/chat`'s `renderSidebar()` early-returns to empty when `currentRoom` is null).
On item-select, restore them all (`hideFolderDetail` mirrors `showFolderDetail`).

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
- **Folder view must hide ALL item chrome (title/header, body, footer, right
  sidebar), not just the body** — else the previously-open item's name/members/
  stats leak into the folder view. This was a real `/chat` bug (see §7).

### Hard-won from the `/git` build (CSS/layout — copy `/cron` rule-for-rule)

`/git` re-derived the tree from `/cron`'s *JS* but hand-wrote its *CSS/layout*,
and a dozen "tiny" divergences piled up. The meta-lesson: **the layout is part
of the spec — diff your tree CSS against `/cron`'s rule-by-rule, don't eyeball
it.** Specific traps that each shipped before being caught:

- **The tree panel is block flow, NOT flex.** `/git` set the container
  `display:flex;flex-direction:column` and gave the root-drop strip
  `margin-top:auto` — which shoved the "move to top level" strip to the bottom
  of the sidebar, far from the tree, so a leaf **could not be dragged to top
  level at all**. `/cron`'s `.cron-tree` is plain `overflow:auto;min-height:0`;
  the root-drop strip sits in normal flow right under the `<ul>`
  (`margin-top:8px`). With block flow, the `*-tree-sep` dividers need their own
  `margin:6px 0` (don't lean on a flex `gap`). Real `/git` bug.
- **Don't invent node spacing/sizes — folder and leaf rows differ.** `/cron`:
  folder `.cron-node{padding:8px 4px}` vs leaf `.cron-job-node{padding:4px 4px}`
  (folders are taller), `gap:4px`, `border-radius:4px`, hover `#f1f5f9`, icon
  `15px`, panel `background:#fbfbfb`. `/git` guessed uniform `3px 6px`, `gap:6px`,
  radius `5px`, `16px` icons, white panel — every one read as subtly off.
- **Selected row = tint AND bold, on folder AND leaf.** Copying
  `background:#dbeafe` while forgetting `font-weight:600` makes selection look
  weak. Both `.node.sel` and `.leaf.sel` need it (§5 says so; `/git` still
  missed it).
- **Render the kebab on every row and show it via CSS `visibility` — do NOT
  conditionally create it in JS.** `/git` first created the kebab only on the
  selected node, but the kebab is `1.4rem` tall, so the selected row jumped
  taller than its neighbours. `/cron` renders it on every row at
  `visibility:hidden` and flips `.sel .kebab{visibility:visible}` — constant row
  height.
- **Kebab dots = a `box-shadow` triple-dot, not a unicode glyph.** Use
  `.kebab::before{content:"";width:3px;height:3px;border-radius:50%;
  box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}` + a `border-radius`
  hover box (`:hover{background:#d2ddf6}`). A `content:"\22EF"` glyph has no
  hover target and — inside a Python `"""…"""` template — `\22` is an *octal
  escape* (a control char), so it silently renders garbage unless you write
  `\\22EF`. The box-shadow form sidesteps both.
- **Drag needs `pointer-events:none` on the node's children** (with
  `pointer-events:auto` restored on the kebab + menu), or the icon/label
  swallow `dragover`/`drop` and dropping onto a row mis-fires. `/cron` has
  `.node>*{pointer-events:none}`.
- **Page chrome the panel sits in:** add `<style>.pp-nav{margin-bottom:0}</style>`
  *after* the `{% include "_nav.html" %}` (the shared nav sets
  `margin-bottom:1.5em`, which otherwise opens a big gap above the split), tint
  the tree `background:#fbfbfb`, and style the `+ Folder`/`+ Item` buttons (don't
  leave them as default grey browser buttons). `/git` missed all three.
- **The folder detail table is the RECURSIVE subtree, not direct children**
  (§7). `/git` first shipped direct-children-only and it diverged from
  `/cron`/`/chat`; use the depth-first `flattenTree` with depth-indented rows.

### Hard-won — process, not code

- **Verify drag-drop and selection in a REAL browser, never by asserting "it
  mirrors `/cron`".** The root-drop bug above shipped *because* the JS was a
  faithful copy of `/cron` — the fault was in the CSS layout, invisible to any
  "the code matches" review. Drive the live page (headless Chrome + the
  DevTools Protocol works with no extra deps) and actually drag a leaf to the
  root strip, open a kebab on the selected row, and type-to-confirm a delete.
  Marker-string tests and code-diffing reviewers both passed this page while it
  was visibly broken.

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
   never per-kind params. **Leaf rows are `<a href>` anchors** so CMD/Ctrl and
   middle click open the item in a new tab (§5): plain click `preventDefault()`
   + in-page select, modified clicks fall through, `text-decoration:none`, and
   `preventDefault()` on kebab/menu handlers nested inside the anchor.
6. **Drag-drop:** folder 3-zone (before/after/into) + leaf 2-zone + root drop;
   `folderInSubtree` cycle guard; auto-expand on drop; debounced save.
7. **Detail pane:** flatten subtree with depth; depth-indented rows (plain item
   names, no view-specific decoration); per-row Details link (folder drills in,
   item opens); `hidden`-attr pane toggle (+ explicit `[hidden]{display:none}`
   if the pane sets `display`). **Hide the WHOLE item view (title bar, body,
   footer, right sidebar) on folder-select**, or stale item info leaks (§7).
8. **Modals** for folder create/rename/delete per [`ui-modals.md`](ui-modals.md).
