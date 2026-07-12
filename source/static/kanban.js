// /kanban page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/kanban_views.py; this file is served at /static/kanban.js with an
// mtime cache-buster.
//
// State is DATABASE-BACKED (webapp/kanban_api.py → db_kanban): the sidebar
// hydrates from GET /kanban/api/boards, the selected board from
// GET /kanban/api/board/<uuid>, and edits save via a debounced whole-board
// PUT guarded like the cron tree save — the PUT echoes the version token it
// hydrated with (stale → 409 → re-hydrate + toast, never clobber) and
// declares its deletions. Per-task agent operations (claim/move/events/
// complete) are separate endpoints used by agents; this page reads their
// audit trail in the task modal.
//
//   board: { uuid, name, description, columns: [{uuid, name}],
//            tasks: [{uuid, columnUuid, title, description, agentUuid}],
//            version }
//
// Task order within a column = array order (the backend stores position).

'use strict';

// ---- state ----
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
// Deletions since the last successful save are tracked PER BOARD (a
// `pendingDeletes` counter on the board payload object), so a board switch
// can't misattribute them.

// Folder icons — the SAME inline Lucide SVGs /chat uses (chat_template.py
// CHAT_ICON_FOLDER / CHAT_ICON_FOLDER_OPEN), so the tree looks identical across
// pages: the closed folder flips to the open one when expanded. Leaf items
// (boards) carry NO icon, matching /chat's rooms.
const KB_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const KB_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

function escapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
// Display name for an agent uuid (the picker is server-injected {name, uuid}
// pairs); an unknown/retired agent falls back to its short uuid.
function kbAgentName(agentUuid){
  if (!agentUuid) return null;
  const a = (window.KANBAN_AGENTS || []).find(x => x.uuid === agentUuid);
  return a ? a.name : agentUuid.split('-')[0];
}

let kbToastTimer = null;
function kbToast(text){
  const el = document.getElementById('kb-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(kbToastTimer);
  kbToastTimer = setTimeout(() => el.classList.remove('show'), 4000);
}

// ---- persistence: hydrate from / save to the backend ----
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
async function kbLoadBoard(uuid){
  try {
    const r = await fetch('/kanban/api/board/' + encodeURIComponent(uuid));
    if (!r.ok) return null;
    const b = await r.json();
    b.pendingDeletes = 0;  // per-board deletion counter for the save tripwire
    return b;
  } catch (e) { return null; }
}

let kbSaveTimer = null;
let kbSaveBoardRef = null;          // the board payload captured when the save was scheduled
let kbSaveChain = Promise.resolve(); // serializes PUTs; every save appends to it
function kbSave(){
  // Debounce so a burst of edits coalesces into one PUT of the whole board.
  // Capture the board NOW: if the user switches boards inside the debounce
  // window, the save must still target the board that was edited (reading
  // the mutable kbCurrent when the timer fires would hit the wrong board).
  kbSaveBoardRef = kbCurrent;
  clearTimeout(kbSaveTimer);
  kbSaveTimer = setTimeout(kbSavePush, 250);
}
// Append the captured board's save to the chain. The chain (a) serializes
// PUTs so a save can't 409 against its own predecessor's version bump, and
// (b) gives every caller something to await: the returned promise resolves
// only after THIS save (and everything scheduled before it) has completed.
function kbSavePush(){
  const board = kbSaveBoardRef;
  clearTimeout(kbSaveTimer);
  kbSaveTimer = null;
  kbSaveBoardRef = null;
  if (!board) return kbSaveChain;
  kbSaveChain = kbSaveChain.then(() => kbDoSave(board));
  return kbSaveChain;
}
// Persist any scheduled-but-not-yet-due save and return a promise that
// resolves when ALL pending saves have hit the server. Await this before any
// action that snapshots server state (duplicate) or leaves the board.
function kbFlushSave(){
  return kbSavePush();
}
// Drop a scheduled save for a board that is about to be deleted (flushing it
// would race the DELETE and surface a pointless "Save refused" toast).
function kbCancelSave(boardUuid){
  if (kbSaveBoardRef && kbSaveBoardRef.uuid === boardUuid){
    clearTimeout(kbSaveTimer);
    kbSaveTimer = null;
    kbSaveBoardRef = null;
  }
}
async function kbDoSave(board){
  try {
    const r = await fetch('/kanban/api/board/' + encodeURIComponent(board.uuid), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: board.name, description: board.description,
                            columns: board.columns, tasks: board.tasks,
                            version: board.version,
                            deletes: board.pendingDeletes || 0}),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      // Another writer (an agent, a second tab) changed the board since we
      // hydrated. Their version wins: re-hydrate rather than clobber. When
      // the conflicted board is no longer on screen, just report it.
      board.pendingDeletes = 0;
      if (kbCurrent && kbCurrent.uuid === board.uuid){
        await kbReloadCurrent();
        kbToast('Board was changed elsewhere — reloaded. Your last edit was not saved.');
      } else {
        kbToast('“' + board.name + '” was changed elsewhere — your last edit there was not saved.');
      }
    } else if (!r.ok){
      kbToast('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      board.version = (j && j.version) || board.version;
      board.pendingDeletes = 0;
      kbRefreshIndexCounts(board);
    }
  } catch (e) {
    // Network error: keep local state + version; the next edit retries.
  }
}
async function kbReloadCurrent(){
  kbCurrent = kbSelected ? await kbLoadBoard(kbSelected) : null;
  if (!kbCurrent) kbSelected = null;
  await kbLoadIndex();
  kbRender();
}
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

// ---- helpers over the loaded board ----
function kbTask(uuid){ return kbCurrent ? kbCurrent.tasks.find(t => t.uuid === uuid) : null; }
function kbColumnTasks(columnUuid){
  return kbCurrent ? kbCurrent.tasks.filter(t => t.columnUuid === columnUuid) : [];
}

// ---- rendering ----
// 3-dot overflow menu on a board item (visible while the item is selected —
// same pattern as the cron tree's and the chat room list's kebab). The popup
// is fixed-positioned under the button; any outside click or Escape closes it.
function kbMakeKebab(node, items){
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'kb-kebab';
  kebab.setAttribute('aria-label', 'Board actions');
  kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'kb-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  items.forEach(([label, cls, fn]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'item' + (cls ? ' ' + cls : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = label;
    item.addEventListener('click', e => {
      e.stopPropagation();
      menu.hidden = true;
      fn();
    });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', e => {
    e.stopPropagation();  // don't re-select the underlying board item
    const willOpen = menu.hidden;
    document.querySelectorAll('.kb-menu').forEach(m => { m.hidden = true; });
    if (willOpen) kbPlaceMenu(menu, kebab.getBoundingClientRect());
  });
  node.appendChild(kebab);
  node.appendChild(menu);
}

// Position a fixed kebab menu near its anchor, clamped inside the viewport:
// below the anchor when it fits, flipped above when it would overflow the
// bottom edge (items at the bottom of a long board). Unhides the menu first so
// its offsetWidth/Height are measurable.
function kbPlaceMenu(menu, anchorRect){
  menu.hidden = false;
  const margin = 6;
  const left = Math.max(margin,
    Math.min(anchorRect.left, window.innerWidth - menu.offsetWidth - margin));
  let top = anchorRect.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - margin){
    top = anchorRect.top - menu.offsetHeight - 4;
  }
  menu.style.left = left + 'px';
  menu.style.top = Math.max(margin, top) + 'px';
}
// Dismiss any open kebab menu on an outside click or Escape.
document.addEventListener('click', () => {
  document.querySelectorAll('.kb-menu').forEach(m => { m.hidden = true; });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.kb-menu').forEach(m => { m.hidden = true; });
});

async function kbDuplicateBoard(uuid){
  // The duplicate snapshots SERVER state: edits still in the debounce window
  // must be fully persisted first, so await the whole save chain.
  await kbFlushSave();
  let j = null;
  try {
    const r = await fetch('/kanban/api/board/' + encodeURIComponent(uuid) + '/duplicate',
                          {method: 'POST'});
    j = await r.json();
  } catch (e) { /* fall through */ }
  if (!j || !j.ok){ kbToast('Duplicate failed.'); return; }
  kbSelected = j.board.uuid;
  kbCurrent = j.board;
  kbCurrent.pendingDeletes = 0;
  await kbLoadIndex();
  kbRender();
}

// Recursive tree: an "All boards" root node, then top-level folders, then
// unfiled boards. Folders emit a node row and (when expanded + non-empty) a
// nested <ul> of child folders followed by their boards.
function kbRenderTree(){ kbRenderBoardList(); }  // alias kept for old callers
function kbRenderBoardList(){
  // The static "All boards" node lives above the action buttons (like /cron's
  // "All jobs"); just highlight it here when selected.
  document.getElementById('kb-all-boards').className =
    'kb-node' + (kbSelectedFolder === 'all' ? ' sel' : '');
  const root = document.getElementById('kb-tree-root');
  root.innerHTML = '';
  const ul = document.createElement('ul');
  kbChildFolders(null).forEach(f => ul.appendChild(kbFolderLi(f)));
  kbBoardsInFolder(null).forEach(b => ul.appendChild(kbBoardNode(b)));
  root.appendChild(ul);
  kbWireRootDrop();
}

function kbFolderLi(f){
  const li = document.createElement('li');
  const node = document.createElement('div');
  const expanded = kbIsExpanded(f.uuid);
  const hasKids = kbFolderHasChildren(f.uuid);
  node.className = 'kb-node' + (f.uuid === kbSelectedFolder ? ' sel' : '');
  node.draggable = true;
  node.innerHTML =
    '<span class="kb-ficon">' + (expanded && hasKids ? KB_ICON_FOLDER_OPEN : KB_ICON_FOLDER) + '</span>' +
    '<span class="kb-node-name"></span>';
  node.querySelector('.kb-node-name').textContent = f.name || '(unnamed)';
  node.title = f.name;
  node.addEventListener('click', () => kbFolderClick(f.uuid));
  kbMakeKebab(node, [
    ['Rename', '', () => kbRenameFolder(f.uuid)],
    ['New subfolder', '', () => kbNewFolder(f.uuid)],
    ['Copy folder id', '', () => kbCopyId(f.uuid, 'Folder')],
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
  // No icon on leaf boards — matches /chat's room rows.
  node.innerHTML = '<span class="kb-node-name"></span>';
  node.querySelector('.kb-node-name').textContent =
    (b.name || '(unnamed board)') + ' (' + b.taskCount + ')';
  node.title = b.name;
  node.addEventListener('click', () => kbSelectBoard(b.uuid));
  kbMakeKebab(node, [
    ['Duplicate', '', () => kbDuplicateBoard(b.uuid)],
    ['Copy board id', '', () => kbCopyId(b.uuid, 'Board')],
    ['Delete', 'danger', () => kbConfirmDeleteBoard(b.uuid)],
  ]);
  kbWireBoardDrag(node, b.uuid);
  li.appendChild(node);
  return li;
}

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

function kbRenderBoard(){
  const board = kbCurrent;
  const folderShown = !board && kbSelectedFolder !== null;
  document.getElementById('kb-empty').hidden = !!board || folderShown;
  document.getElementById('kb-board').hidden = !board;
  document.getElementById('kb-folder-view').hidden = !folderShown;
  kbRenderSidebar();  // stats track every board change (and close on no board)
  if (!board) return;
  document.getElementById('kb-board-name').textContent = board.name;
  document.getElementById('kb-board-desc').textContent = board.description || '';
  const wrap = document.getElementById('kb-columns');
  wrap.innerHTML = '';
  board.columns.forEach(col => {
    const tasks = kbColumnTasks(col.uuid);
    const el = document.createElement('div');
    el.className = 'kb-col';
    el.innerHTML =
      '<div class="kb-col-head">' + escapeHtml(col.name) +
      ' <span class="kb-col-count">' + tasks.length + '</span></div>' +
      '<div class="kb-col-cards"></div>' +
      '<div class="kb-col-foot"><button>+ Add task</button></div>';
    const cards = el.querySelector('.kb-col-cards');
    if (!tasks.length){
      // The markdown serialization shows _(empty)_ for the same reason: an
      // empty column should read as deliberately empty, not as missing data.
      const empty = document.createElement('div');
      empty.className = 'kb-col-empty';
      empty.textContent = 'Empty';
      cards.appendChild(empty);
    }
    tasks.forEach(t => cards.appendChild(kbCardEl(t)));
    el.querySelector('.kb-col-foot button')
      .addEventListener('click', () => kbNewTask(col.uuid));
    kbWireColumnDrop(el, col.uuid);
    wrap.appendChild(el);
  });
}

function kbCardEl(t){
  const short = t.uuid.split('-')[0];
  const agentName = kbAgentName(t.agentUuid);
  const card = document.createElement('div');
  card.className = 'kb-card';
  card.draggable = true;
  card.innerHTML =
    '<div class="kb-card-title">' + escapeHtml(t.title || '(untitled)') + '</div>' +
    ((t.description || '').trim()
      ? '<div class="kb-card-desc">' + escapeHtml(t.description) + '</div>' : '') +
    '<div class="kb-card-meta">' +
      '<span class="kb-uuid" title="' + t.uuid + '">' + short + '</span>' +
      (agentName
        ? '<span class="kb-agent">@' + escapeHtml(agentName) + '</span>'
        : '<span class="kb-agent kb-unassigned">unassigned</span>') +
    '</div>';
  card.addEventListener('click', () => kbEditTask(t.uuid));
  card.addEventListener('dragstart', e => {
    kbDrag = t.uuid;
    card.classList.add('kb-dragging');
    e.dataTransfer.effectAllowed = 'move';
  });
  card.addEventListener('dragend', () => {
    kbDrag = null;
    card.classList.remove('kb-dragging');
    document.querySelectorAll('.kb-drop, .kb-drop-before').forEach(x =>
      x.classList.remove('kb-drop', 'kb-drop-before'));
  });
  // Drop ONTO a card = insert the dragged task before it.
  card.addEventListener('dragover', e => {
    if (kbDrag && kbDrag !== t.uuid){
      e.preventDefault(); e.stopPropagation();
      card.classList.add('kb-drop-before');
    }
  });
  card.addEventListener('dragleave', () => card.classList.remove('kb-drop-before'));
  card.addEventListener('drop', e => {
    e.preventDefault(); e.stopPropagation();
    card.classList.remove('kb-drop-before');
    if (kbDrag && kbDrag !== t.uuid) kbMoveTask(kbDrag, t.columnUuid, t.uuid);
  });
  return card;
}

function kbWireColumnDrop(colEl, columnUuid){
  colEl.addEventListener('dragover', e => {
    if (kbDrag){ e.preventDefault(); colEl.classList.add('kb-drop'); }
  });
  colEl.addEventListener('dragleave', e => {
    if (!colEl.contains(e.relatedTarget)) colEl.classList.remove('kb-drop');
  });
  colEl.addEventListener('drop', e => {
    e.preventDefault();
    colEl.classList.remove('kb-drop');
    if (kbDrag) kbMoveTask(kbDrag, columnUuid, null);  // append at the end
  });
}

// ---- right sidebar (picker: off / stats — same mechanics as /chat's) ----
// The mode is a UI preference, persisted like chat's chat.sidebarMode; board
// DATA never touches browser storage.
const KB_SIDEBAR_MODE_KEY = 'kanban.sidebarMode';
let kbSidebarMode = 'hidden';   // 'hidden' | 'stats' | 'dev'
try {
  const saved = localStorage.getItem(KB_SIDEBAR_MODE_KEY);
  if (saved === 'hidden' || saved === 'stats' || saved === 'dev') kbSidebarMode = saved;
} catch (e) { /* storage unavailable: session default */ }
const kbSidebarModeSel = document.getElementById('kb-sidebar-mode');
kbSidebarModeSel.value = kbSidebarMode;
kbSidebarModeSel.addEventListener('change', () => {
  kbSidebarMode = kbSidebarModeSel.value;
  try { localStorage.setItem(KB_SIDEBAR_MODE_KEY, kbSidebarMode); } catch (e) {}
  kbRenderSidebar();
});

function kbStatRow(label, value){
  const d = document.createElement('div');
  d.className = 'kb-stat';
  const s = document.createElement('span'); s.textContent = label;
  const b = document.createElement('b'); b.textContent = value;
  d.appendChild(s); d.appendChild(b);
  return d;
}

function kbRenderSidebar(){
  const split = document.querySelector('.kb-split');
  const el = document.getElementById('kb-sidebar');
  if (kbSidebarMode === 'hidden' || !kbCurrent){
    split.classList.remove('kb-sidebar-open');
    el.innerHTML = '';
    return;
  }
  split.classList.add('kb-sidebar-open');
  el.innerHTML = '';
  if (kbSidebarMode === 'dev'){ kbRenderSidebarDev(el); return; }
  const h = document.createElement('h3');
  h.className = 'kb-sidebar-title';
  h.textContent = 'Stats';
  el.appendChild(h);
  el.appendChild(kbStatRow('Tasks', kbCurrent.tasks.length));
  kbCurrent.columns.forEach(c =>
    el.appendChild(kbStatRow(c.name, kbColumnTasks(c.uuid).length)));
  const assigned = kbCurrent.tasks.filter(t => t.agentUuid).length;
  el.appendChild(kbStatRow('Assigned', assigned));
  el.appendChild(kbStatRow('Unassigned', kbCurrent.tasks.length - assigned));
  // Per-agent breakdown, busiest first.
  const byAgent = {};
  kbCurrent.tasks.forEach(t => {
    if (t.agentUuid) byAgent[t.agentUuid] = (byAgent[t.agentUuid] || 0) + 1;
  });
  const entries = Object.entries(byAgent).sort((a, b) => b[1] - a[1]);
  if (entries.length){
    const h2 = document.createElement('h3');
    h2.className = 'kb-sidebar-title';
    h2.style.marginTop = '1em';
    h2.textContent = 'By agent';
    el.appendChild(h2);
    entries.forEach(([uuid, n]) =>
      el.appendChild(kbStatRow('@' + kbAgentName(uuid), n)));
  }
}

function kbRender(){
  kbRenderTree();
  kbRenderBoard();
  kbRenderFolderView();
  kbSyncUrl();
}

// The id of the node currently inspected (board > folder > none). The root
// "All boards" pseudo-node has no uuid, so it deep-links to no ?id= (the
// default view) — mirrors /cron's "All jobs".
function kbCurrentSelectionId(){
  if (kbSelected) return kbSelected;
  if (kbSelectedFolder && kbSelectedFolder !== 'all') return kbSelectedFolder;
  return null;
}
// Deep link: a single ?id=<uuid> mirrors the selected board or folder, like
// /cron (e.g. /kanban?id=<uuid>). A shareable link to the node being inspected.
function kbSyncUrl(){
  const url = new URL(window.location);
  url.searchParams.delete('board');  // migrate off the old dual params
  url.searchParams.delete('folder');
  const id = kbCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}

// ---- board operations ----
async function kbSelectBoard(uuid){
  kbFlushSave();  // an edit inside the debounce window must not be dropped
  kbSelected = uuid;
  kbSelectedFolder = null;  // board-open and folder-selected are exclusive
  kbCurrent = await kbLoadBoard(uuid);
  if (!kbCurrent){ kbSelected = null; kbToast('Board could not be loaded.'); }
  kbRender();
}

function kbMoveTask(taskUuid, toColumnUuid, beforeTaskUuid){
  const t = kbTask(taskUuid);
  if (!t || !kbCurrent) return;
  // Reorder = remove + reinsert; array order is the column order.
  kbCurrent.tasks = kbCurrent.tasks.filter(x => x.uuid !== taskUuid);
  t.columnUuid = toColumnUuid;
  if (beforeTaskUuid){
    const i = kbCurrent.tasks.findIndex(x => x.uuid === beforeTaskUuid);
    kbCurrent.tasks.splice(i < 0 ? kbCurrent.tasks.length : i, 0, t);
  } else {
    kbCurrent.tasks.push(t);
  }
  kbSave();
  kbRenderBoard();
}

// ---- overlays ----
function kbOpenModal(id){
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById(id).hidden = false;
}
function kbCloseModals(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  ['kb-board-modal','kb-folder-modal','kb-task-modal','kb-md-modal','kb-confirm-modal']
    .forEach(id => document.getElementById(id).hidden = true);
  // Forget the opened-with snapshots so a future open starts fresh.
  kbBoardModalOpenedWith = null;
  kbTaskModalOpenedWith = null;
}

// ---- dirty-guarded dismissal (backdrop click / Esc) ----
// Cancel buttons always close (inline onclick="kbCloseModals()"). The two
// ACCIDENTAL dismiss paths below only close when the open modal is "clean":
// the user hasn't changed the value(s) it was opened with. Mirrors /chat's
// openModalDirty / dismissOpenModalIfClean.
//
// Opened-with snapshots, captured at the end of each open fn (empty string on
// a create modal, the existing value on an edit modal).
let kbBoardModalOpenedWith = null;  // {name, desc} while kb-board-modal is open
let kbTaskModalOpenedWith = null;   // {title, desc} while kb-task-modal is open
function kbModalDirty(){
  // Board modal: name or description differs from what it was opened with.
  if (!document.getElementById('kb-board-modal').hidden){
    const w = kbBoardModalOpenedWith || {name: '', desc: ''};
    return document.getElementById('kb-b-name').value !== w.name
        || document.getElementById('kb-b-desc').value !== w.desc;
  }
  // Task modal: title or description differs from what it was opened with.
  if (!document.getElementById('kb-task-modal').hidden){
    const w = kbTaskModalOpenedWith || {title: '', desc: ''};
    return document.getElementById('kb-t-title').value !== w.title
        || document.getElementById('kb-t-desc').value !== w.desc;
  }
  // Markdown modal (read-only view) and confirm modal (no data entry) are
  // never dirty.
  return false;
}
function kbDismissIfClean(){
  if (!kbModalDirty()) kbCloseModals();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') kbDismissIfClean(); });

// Board create/edit share one modal; kbEditingBoard picks the mode.
function kbNewBoard(){
  kbEditingBoard = false;
  document.getElementById('kb-board-modal-title').textContent = 'New board';
  document.getElementById('kb-b-name').value = '';
  document.getElementById('kb-b-desc').value = '';
  document.getElementById('kb-b-save').textContent = 'Create board';
  document.getElementById('kb-b-err').textContent = '';
  kbOpenModal('kb-board-modal');
  kbBoardModalOpenedWith = {name: '', desc: ''};  // create: empty = clean
  document.getElementById('kb-b-name').focus();
}
function kbEditBoard(){
  if (!kbCurrent) return;
  kbEditingBoard = true;
  document.getElementById('kb-board-modal-title').textContent = 'Edit board';
  document.getElementById('kb-b-name').value = kbCurrent.name;
  document.getElementById('kb-b-desc').value = kbCurrent.description || '';
  document.getElementById('kb-b-save').textContent = 'Save';
  document.getElementById('kb-b-err').textContent = '';
  kbOpenModal('kb-board-modal');
  kbBoardModalOpenedWith = {  // edit: snapshot the loaded values
    name: document.getElementById('kb-b-name').value,
    desc: document.getElementById('kb-b-desc').value,
  };
}
async function kbSaveBoardModal(){
  const name = document.getElementById('kb-b-name').value.trim();
  const desc = document.getElementById('kb-b-desc').value.trim();
  if (!name){
    document.getElementById('kb-b-err').textContent = 'Name is required.'; return;
  }
  if (kbEditingBoard){
    if (kbCurrent){ kbCurrent.name = name; kbCurrent.description = desc; kbSave(); }
    kbCloseModals();
    kbRender();
    return;
  }
  // Create server-side (the server makes the default columns + the version token).
  try {
    const r = await fetch('/kanban/api/boards', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, description: desc}),
    });
    const j = await r.json();
    if (!r.ok || !j.ok){
      document.getElementById('kb-b-err').textContent = (j && j.error) || 'Create failed.';
      return;
    }
    kbCloseModals();
    await kbFlushSave();  // leaving the old board: persist its pending edits
    kbSelected = j.board.uuid;
    kbCurrent = j.board;
    kbCurrent.pendingDeletes = 0;
    await kbLoadIndex();
    kbRender();
  } catch (e) {
    document.getElementById('kb-b-err').textContent = 'Network error.';
  }
}

// Generic confirm overlay — the in-page replacement for the native dialog.
function kbConfirm(title, text, onYes){
  document.getElementById('kb-confirm-title').textContent = title;
  document.getElementById('kb-confirm-text').textContent = text;
  const yes = document.getElementById('kb-confirm-yes');
  const fresh = yes.cloneNode(true);          // drop any previous click handler
  yes.parentNode.replaceChild(fresh, yes);
  fresh.addEventListener('click', () => { kbCloseModals(); onYes(); });
  kbOpenModal('kb-confirm-modal');
}

function kbConfirmDeleteBoard(uuid){
  // From the board header (no arg) or a tree kebab (uuid). Resolve to a board:
  // prefer the explicit uuid, else the loaded board.
  const board = uuid
    ? (kbBoards.find(b => b.uuid === uuid) || kbCurrent)
    : kbCurrent;
  if (!board) return;
  kbConfirm('Delete board?',
    '“' + board.name + '” and its ' + (board.tasks ? board.tasks.length : board.taskCount) + ' task(s) will be deleted.',
    async () => {
      kbCancelSave(board.uuid);  // a pending save would just race the DELETE
      const r = await fetch('/kanban/api/board/' + encodeURIComponent(board.uuid),
                            {method: 'DELETE'}).catch(() => null);
      if (!r || !r.ok){ kbToast('Delete failed.'); return; }
      kbSelected = null;
      kbCurrent = null;
      await kbLoadIndex();
      if (kbBoards.length) await kbSelectBoard(kbBoards[0].uuid);
      else kbRender();
    });
}

// ---- task modal (create + edit) ----
function kbFillTaskSelects(agentUuid, columnUuid){
  const agentSel = document.getElementById('kb-t-agent');
  agentSel.innerHTML = '';
  agentSel.appendChild(new Option('(unassigned)', ''));
  (window.KANBAN_AGENTS || []).forEach(a => agentSel.appendChild(new Option(a.name, a.uuid)));
  if (agentUuid && ![...agentSel.options].some(o => o.value === agentUuid)){
    // Keep a retired/unknown agent visible instead of silently dropping it.
    agentSel.appendChild(new Option(kbAgentName(agentUuid), agentUuid));
  }
  agentSel.value = agentUuid || '';
  const colSel = document.getElementById('kb-t-col');
  colSel.innerHTML = '';
  kbCurrent.columns.forEach(c => colSel.appendChild(new Option(c.name, c.uuid)));
  colSel.value = columnUuid;
}
function kbNewTask(columnUuid){
  if (!kbCurrent) return;
  kbEditingTask = null;
  kbModalColumn = columnUuid;
  document.getElementById('kb-task-modal-title').textContent = 'New task';
  document.getElementById('kb-t-title').value = '';
  document.getElementById('kb-t-desc').value = '';
  kbFillTaskSelects('', columnUuid);
  document.getElementById('kb-t-save').textContent = 'Create task';
  document.getElementById('kb-t-delete').hidden = true;
  document.getElementById('kb-t-run').hidden = true;
  document.getElementById('kb-t-uuid-row').hidden = true;
  document.getElementById('kb-t-events').hidden = true;
  document.getElementById('kb-t-err').textContent = '';
  kbOpenModal('kb-task-modal');
  kbTaskModalOpenedWith = {title: '', desc: ''};  // create: empty = clean
  document.getElementById('kb-t-title').focus();
}
function kbEditTask(uuid){
  const t = kbTask(uuid);
  if (!t || !kbCurrent) return;
  kbEditingTask = uuid;
  document.getElementById('kb-task-modal-title').textContent = 'Edit task';
  document.getElementById('kb-t-title').value = t.title;
  document.getElementById('kb-t-desc').value = t.description || '';
  kbFillTaskSelects(t.agentUuid || '', t.columnUuid);
  document.getElementById('kb-t-save').textContent = 'Save';
  document.getElementById('kb-t-delete').hidden = false;
  document.getElementById('kb-t-run').hidden = !t.agentUuid;  // needs an assignee
  document.getElementById('kb-t-uuid-row').hidden = false;
  document.getElementById('kb-t-uuid').textContent = t.uuid;
  // Lease state (read-only; only the agent claim operations write it).
  const claim = document.getElementById('kb-t-claim');
  if (t.claimedBy){
    const expired = t.claimExpiresAt && new Date(t.claimExpiresAt) < new Date();
    claim.textContent = 'Claimed by @' + kbAgentName(t.claimedBy) +
      (t.claimExpiresAt
        ? (expired ? ' — lease expired ' : ' — lease until ') + kbFmtDate(t.claimExpiresAt)
        : '');
    claim.hidden = false;
  } else {
    claim.hidden = true;
  }
  document.getElementById('kb-t-err').textContent = '';
  kbOpenModal('kb-task-modal');
  kbTaskModalOpenedWith = {  // edit: snapshot the loaded values
    title: document.getElementById('kb-t-title').value,
    desc: document.getElementById('kb-t-desc').value,
  };
  kbLoadTaskEvents(uuid);
}
// Copy a shareable link to the open task. Absolute (origin + path) so it works
// pasted anywhere; /kanban?id=<task> reopens this overlay.
function kbCopyTaskLink(btn){
  if (!kbEditingTask) return;
  const url = window.location.origin + '/kanban?id=' + kbEditingTask;
  navigator.clipboard.writeText(url).then(() => {
    const prev = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = prev; }, 1200);
  }).catch(() => kbToast('Could not copy to clipboard.'));
}
// Copy a board/folder uuid from its tree kebab — handy for addressing it in
// chat/assistant commands (e.g. "add a task to board <id>").
function kbCopyId(uuid, kind){
  navigator.clipboard.writeText(uuid).then(
    () => kbToast(kind + ' id copied: ' + uuid),
    () => kbToast('Could not copy to clipboard.'));
}
// The task's audit trail (kanban_task_event): created/claimed/moved/done/
// failed/notes, from UI saves and agent operations alike. Read-only here.
async function kbLoadTaskEvents(uuid){
  const box = document.getElementById('kb-t-events');
  box.hidden = false;
  box.innerHTML = '<span class="muted">loading history…</span>';
  let data = null;
  try {
    const r = await fetch('/kanban/api/tasks/' + encodeURIComponent(uuid) + '/events');
    data = await r.json();
  } catch (e) { /* fall through */ }
  if (kbEditingTask !== uuid) return;  // modal moved on; drop this response
  const events = (data && data.events) || [];
  if (!events.length){ box.innerHTML = '<span class="muted">no history yet</span>'; return; }
  box.innerHTML = '<div class="kb-events-title">History</div>' + events.map(e =>
    '<div class="kb-event"><span class="kb-event-kind">' + escapeHtml(e.kind) + '</span> ' +
    escapeHtml(e.detail || '') +
    '<span class="muted"> — ' + escapeHtml(kbAgentName(e.actor) || e.actor || '?') +
    ' · ' + kbFmtDate(e.created_at) + '</span></div>').join('');
}
function kbFmtDate(iso){
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const MON = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  const p2 = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + MON[d.getMonth()] + '-' + p2(d.getDate()) +
         ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
}
function kbSaveTaskModal(){
  const title = document.getElementById('kb-t-title').value.trim();
  if (!title){
    document.getElementById('kb-t-err').textContent = 'Title is required.'; return;
  }
  const desc = document.getElementById('kb-t-desc').value.trim();
  const agentUuid = document.getElementById('kb-t-agent').value || null;
  const columnUuid = document.getElementById('kb-t-col').value;
  if (kbEditingTask){
    const t = kbTask(kbEditingTask);
    if (t){ t.title = title; t.description = desc; t.agentUuid = agentUuid; t.columnUuid = columnUuid; }
  } else {
    kbCurrent.tasks.push({uuid: crypto.randomUUID(),
                          columnUuid: columnUuid || kbModalColumn,
                          title: title, description: desc, agentUuid: agentUuid});
  }
  kbCloseModals();
  kbSave();
  kbRenderBoard();
  kbRefreshIndexCounts();
}
// "Run": enqueue the assigned agent to execute this task (milestone 3 —
// enqueue-on-command). The agent's kanban adapter then claims, works, and
// completes/fails; the outcome shows up in the task's event trail.
async function kbEnqueueTask(){
  const t = kbTask(kbEditingTask);
  if (!t) return;
  await kbFlushSave();  // the agent reads server state; persist edits first
  let j = null;
  try {
    const r = await fetch('/kanban/api/tasks/' + encodeURIComponent(t.uuid) + '/enqueue',
                          {method: 'POST'});
    j = await r.json();
  } catch (e) { /* fall through */ }
  if (!j || !j.ok){
    kbToast('Run failed: ' + ((j && j.error) || 'network error'));
    return;
  }
  kbCloseModals();
  kbToast('Enqueued for @' + kbAgentName(t.agentUuid) + ' — outcome lands in the task history.');
}

function kbConfirmDeleteTask(){
  const t = kbTask(kbEditingTask);
  if (!t) return;
  kbCloseModals();
  kbConfirm('Delete task?', '“' + t.title + '” will be deleted.', () => {
    kbCurrent.tasks = kbCurrent.tasks.filter(x => x.uuid !== t.uuid);
    kbCurrent.pendingDeletes = (kbCurrent.pendingDeletes || 0) + 1;  // declare to the save's tripwire
    kbSave();
    kbRenderBoard();
    kbRefreshIndexCounts();
  });
}

// ---- LLM-facing serializations (served from the canonical DB state) ----
// Two formats, same board: 'markdown' (the canonical contract) and 'json'
// (columns→tasks nested, agent names resolved). Both viewable + copyable —
// from the board header (markdown) and the Developer sidebar (both).
const KB_SERIALIZATIONS = {
  markdown: {label: 'Markdown', path: '/markdown'},
  json: {label: 'JSON', path: '/json'},
};
async function kbFetchSerialization(kind){
  const s = KB_SERIALIZATIONS[kind];
  if (!s || !kbCurrent) return null;
  try {
    const r = await fetch('/kanban/api/board/' + encodeURIComponent(kbCurrent.uuid) + s.path);
    return r.ok ? await r.text() : null;
  } catch (e) { return null; }
}
async function kbShowSerialization(kind){
  if (!kbCurrent) return;
  document.getElementById('kb-md-title').textContent = KB_SERIALIZATIONS[kind].label;
  const pre = document.getElementById('kb-md-pre');
  pre.textContent = 'loading…';
  kbOpenModal('kb-md-modal');
  const text = await kbFetchSerialization(kind);
  pre.textContent = text !== null ? text : '(unavailable)';
}
function kbCopyShownSerialization(){
  kbCopyText(document.getElementById('kb-md-pre').textContent);
}
async function kbCopySerialization(kind){
  const text = await kbFetchSerialization(kind);
  if (text === null){ kbToast('Fetch failed.'); return; }
  kbCopyText(text);
}
function kbCopyText(text){
  navigator.clipboard.writeText(text).then(
    () => kbToast('Copied.'),
    () => kbToast('Copy failed — select the text manually.'));
}

// Developer sidebar: one row per LLM serialization with View/Copy actions.
function kbRenderSidebarDev(el){
  const h = document.createElement('h3');
  h.className = 'kb-sidebar-title';
  h.textContent = 'Developer';
  el.appendChild(h);
  const note = document.createElement('div');
  note.className = 'muted';
  note.style.marginBottom = '0.5em';
  note.textContent = 'Serialize this board for an LLM context.';
  el.appendChild(note);
  Object.entries(KB_SERIALIZATIONS).forEach(([kind, s]) => {
    const row = document.createElement('div');
    row.className = 'kb-dev-row';
    const label = document.createElement('span');
    label.className = 'kb-dev-label';
    label.textContent = s.label;
    const view = document.createElement('button');
    view.className = 'kb-secondary';
    view.textContent = 'View';
    view.addEventListener('click', () => kbShowSerialization(kind));
    const copy = document.createElement('button');
    copy.textContent = 'Copy';
    copy.addEventListener('click', () => kbCopySerialization(kind));
    row.appendChild(label);
    row.appendChild(view);
    row.appendChild(copy);
    el.appendChild(row);
  });
}

// Clicking the backdrop dismisses overlays, but only when clean — a stray
// click never destroys typed data inside the modal itself.
document.getElementById('ui-modal-backdrop').addEventListener('click', kbDismissIfClean);
// The static "All boards" root node selects the whole-tree view (no uuid).
document.getElementById('kb-all-boards').addEventListener('click', () => kbSelectFolder('all'));

// ---- init ----
(async function kbInit(){
  await kbLoadIndex();
  // Deep link: ?id=<uuid> selects that folder or board on load (mirrors /cron).
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && kbFolders.some(f => f.uuid === wantId)){
    kbSelectFolder(wantId);
  } else if (wantId && kbBoards.some(b => b.uuid === wantId)){
    await kbSelectBoard(wantId);
  } else if (wantId && await kbOpenTaskDeepLink(wantId)){
    // wantId was a task uuid: its board is selected and the overlay is open.
  } else {
    // Default to the top-most board in render order (depth-first, subfolders
    // before boards) — NOT kbBoards[0], which is global position order and can
    // sit below entire folders in the tree.
    const firstBoard = kbFlattenTree('all').find(n => n.kind === 'board');
    if (firstBoard) await kbSelectBoard(firstBoard.node.uuid);
    else kbRender();
  }
})();

// Deep link to a task: `?id=<task>` resolves the task's board (server lookup,
// since the index holds only boards), selects it, then pops the task overlay.
// Returns false when the id is not a task so the caller can fall through.
async function kbOpenTaskDeepLink(taskUuid){
  const r = await fetch('/kanban/api/tasks/' + encodeURIComponent(taskUuid))
    .then(x => x.ok ? x.json() : null).catch(() => null);
  if (!r || !r.ok || !r.task || !r.task.boardUuid) return false;
  await kbSelectBoard(r.task.boardUuid);
  if (!kbTask(taskUuid)) return false;  // board changed under us / not present
  kbEditTask(taskUuid);
  return true;
}

// ---- folder-contents detail pane + folder modals + tree drag-and-drop ----
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
    if (kind === 'folder'){
      const ic = document.createElement('span');
      ic.className = 'kb-ficon';
      ic.innerHTML = KB_ICON_FOLDER;  // folder icon; boards (leaves) get none
      nameWrap.appendChild(ic);
    }
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
// True if `rootId` is `candidateId` or lives anywhere in its subtree — used to
// refuse nesting a folder inside itself/its descendants (the DB validator
// enforces it again server-side).
function kbFolderInSubtree(candidateId, rootId){
  if (candidateId === rootId) return true;
  return kbChildFolders(rootId).some(c => kbFolderInSubtree(candidateId, c.uuid));
}
// Reorder `arr` so `movedId` sits just before `beforeId` (or at end if null),
// then renumber `position` within each parent group from the new array order.
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
