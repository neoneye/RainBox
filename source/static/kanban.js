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
let kbIndex = [];          // sidebar: [{uuid, name, taskCount}]
let kbCurrent = null;      // the loaded board payload (see shape above)
let kbSelected = null;     // selected board uuid
let kbEditingTask = null;  // task uuid while the task modal edits (null = create)
let kbModalColumn = null;  // column uuid the task modal creates into
let kbEditingBoard = false;// board modal mode: false = create, true = edit selected
let kbDrag = null;         // task uuid while a card is dragged
// Deletions since the last successful save are tracked PER BOARD (a
// `pendingDeletes` counter on the board payload object), so a board switch
// can't misattribute them.

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
  try {
    const r = await fetch('/kanban/api/boards');
    const data = await r.json();
    kbIndex = (data && data.boards) || [];
  } catch (e) { kbIndex = []; }
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
// Keep the sidebar entry for `board` in step without a full refetch.
function kbRefreshIndexCounts(board){
  if (!board) return;
  const entry = kbIndex.find(b => b.uuid === board.uuid);
  if (entry){
    entry.name = board.name;
    entry.taskCount = board.tasks.length;
    kbRenderBoardList();
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
    if (willOpen){
      const r = kebab.getBoundingClientRect();
      menu.style.left = r.left + 'px';
      menu.style.top = (r.bottom + 4) + 'px';
      menu.hidden = false;
    }
  });
  node.appendChild(kebab);
  node.appendChild(menu);
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

function kbRenderBoard(){
  const board = kbCurrent;
  document.getElementById('kb-empty').hidden = !!board;
  document.getElementById('kb-board').hidden = !board;
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
  kbRenderBoardList();
  kbRenderBoard();
  kbSyncUrl();
}

// Deep link: ?board=<uuid> selects a board; selection mirrors into the URL.
function kbSyncUrl(){
  const url = new URL(window.location);
  if (kbSelected) url.searchParams.set('board', kbSelected);
  else url.searchParams.delete('board');
  history.replaceState(null, '', url);
}

// ---- board operations ----
async function kbSelectBoard(uuid){
  kbFlushSave();  // an edit inside the debounce window must not be dropped
  kbSelected = uuid;
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
  ['kb-board-modal','kb-task-modal','kb-md-modal','kb-confirm-modal'].forEach(id =>
    document.getElementById(id).hidden = true);
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

function kbConfirmDeleteBoard(){
  if (!kbCurrent) return;
  const board = kbCurrent;
  kbConfirm('Delete board?',
    '“' + board.name + '” and its ' + board.tasks.length + ' task(s) will be deleted.',
    async () => {
      kbCancelSave(board.uuid);  // a pending save would just race the DELETE
      const r = await fetch('/kanban/api/board/' + encodeURIComponent(board.uuid),
                            {method: 'DELETE'}).catch(() => null);
      if (!r || !r.ok){ kbToast('Delete failed.'); return; }
      kbSelected = null;
      kbCurrent = null;
      await kbLoadIndex();
      if (kbIndex.length) await kbSelectBoard(kbIndex[0].uuid);
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

// ---- init ----
(async function kbInit(){
  await kbLoadIndex();
  const want = new URLSearchParams(window.location.search).get('board');
  const first = (want && kbIndex.some(b => b.uuid === want)) ? want
              : (kbIndex.length ? kbIndex[0].uuid : null);
  if (first) await kbSelectBoard(first);
  else kbRender();
})();
