// /persona page logic (vanilla JS, no framework). The HTML shell + CSS
// live in webapp/persona_views.py; this file is served at
// /static/persona.js with an mtime cache-buster. State hydrates from
// GET /persona/api/tree and structural edits save via debounced PUTs.
// Per notes/ui-tree-persistence.md the PUT can only update rows that already
// exist: creating and deleting go to their own endpoints, so no payload of
// ours can destroy a persona or its history. Ported from static/prompt.js.

// ---- helpers ----
function personaEscapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function personaShortDate(iso){
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toISOString().slice(0, 16).replace('T', ' ');
}

// ---- state ----
let personaFolders = [];          // {id, name, description, parentId, ...}
let personaItems = [];            // {uuid, name, folderId, revisionCount, ...}
let personaSelectedFolder = null; // folder id, or null for "All personas" / root
let personaSelectedItem = null;   // persona uuid when a persona is selected
let personaExpanded = {};         // folder id -> false when collapsed (default expanded)
let personaDrag = null;           // {type:'folder'|'item', id} while a node is dragged
const PERSONA_EXPAND_KEY = 'persona.expandedFolders';
try { personaExpanded = JSON.parse(localStorage.getItem(PERSONA_EXPAND_KEY)) || {}; }
catch (e) { personaExpanded = {}; }
function personaPersistExpand(){
  try { localStorage.setItem(PERSONA_EXPAND_KEY, JSON.stringify(personaExpanded)); }
  catch (e) { /* private mode etc. — expand state just won't survive reload */ }
}

// ---- inlined Lucide icons (https://lucide.dev), self-contained ----
const PERSONA_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const PERSONA_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

// ---- lookups ----
function personaFolderById(id){ return personaFolders.find(f => f.id === id) || null; }
function personaByUuid(uuid){ return personaItems.find(p => p.uuid === uuid) || null; }
function personaChildFolders(parentId){ return personaFolders.filter(f => (f.parentId || null) === (parentId || null)); }
function personasInFolder(id){
  const target = id || null;
  if (target === null) {
    // Root level also surfaces a persona whose folderId names a folder
    // that isn't in personaFolders (e.g. the folder was deleted via the
    // admin, orphaning the row). The server rejects it in every tree save,
    // so if it stayed invisible here the operator could never reach it to
    // move or delete it and every structural edit would 400 forever.
    return personaItems.filter(p => {
      const fid = p.folderId || null;
      return fid === null || !personaFolderById(fid);
    });
  }
  return personaItems.filter(p => (p.folderId || null) === target);
}
function personaIsExpanded(id){ return personaExpanded[id] !== false; }
// Optimistically stamp a node as just-modified; the server sets the
// authoritative updated_at on save and a reload reconciles.
function personaTouch(node){ if (node) node.updated_at = new Date().toISOString(); }

// ---- selection ----
function personaCurrentSelectionId(){
  if (personaSelectedItem) return personaSelectedItem;
  if (personaSelectedFolder) return personaSelectedFolder;
  return null;
}
function personaSyncUrl(){
  // Reflect the selection in ?id= so the URL is a shareable deep link.
  const url = new URL(window.location);
  const id = personaCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function personaSelectFolder(id){
  personaSelectedFolder = id;
  personaSelectedItem = null;
  personaCloseHistoryView();  // a folder has nothing to show History for
  personaRenderTree();
  personaRender();
}
function personaSelectItem(uuid){
  const p = personaByUuid(uuid);
  personaSelectedItem = uuid;
  personaSelectedFolder = p ? (p.folderId || null) : null;
  personaCloseHistoryView();  // a different persona has nothing to show History for
  personaRenderTree();
  personaRender();
}
function personaSelectNode(type, id){
  if (type === 'item') personaSelectItem(id); else personaSelectFolder(id);
}
function personaFolderClick(id){
  // First click selects; clicking the already-selected folder toggles expand.
  const wasSelected = (personaSelectedFolder === id) && !personaSelectedItem;
  if (wasSelected){
    personaExpanded[id] = !personaIsExpanded(id);
    personaPersistExpand();
    personaRenderTree();
    personaRender();
  } else {
    personaSelectFolder(id);
  }
}

// ---- right-pane render ----
function personaRender(){
  personaRenderRename();
  personaRenderFolderDesc();
  personaRenderContents();
  personaRenderEditor();
  personaSyncUrl();
}
// Depth-first list of everything under parentId (null = whole tree), in the
// same order as the left tree, each row tagged with its nesting `depth` — like
// /cron's cronFlattenTree (notes/ui-left-panel-tree.md §7).
function personaFlattenTree(parentId){
  parentId = parentId || null;
  const out = [];
  const walk = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    personaChildFolders(f.id).forEach(c => walk(c, depth + 1));
    personasInFolder(f.id).forEach(p => out.push({kind: 'item', node: p, depth: depth + 1}));
  };
  personaChildFolders(parentId).forEach(f => walk(f, 0));
  personasInFolder(parentId).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
  return out;
}
function personaRenderContents(){
  const wrap = document.getElementById('persona-table-wrap');
  const editorView = !!personaSelectedItem;
  wrap.hidden = editorView;
  if (editorView) return;
  const tb = document.getElementById('persona-rows');
  tb.innerHTML = '';
  // The selected folder's whole subtree (or the entire tree at the root),
  // depth-first and depth-indented, mirroring the left tree.
  const nodes = personaFlattenTree(personaSelectedFolder);
  if (!nodes.length){
    tb.innerHTML = '<tr><td colspan="4"><i>' +
      (personaSelectedFolder === null ? 'no personas yet' : 'empty folder') + '</i></td></tr>';
    return;
  }
  nodes.forEach(item => {
    const pad = 9 + item.depth * 20;  // indent the name cell by nesting depth, like the tree
    const tr = document.createElement('tr');
    if (item.kind === 'folder'){
      // Folder rows carry the tree's folder icon in the Name cell; that (plus
      // the empty Revisions/Updated cells) is what marks them as folders.
      const f = item.node;
      tr.innerHTML =
        '<td class="persona-name-cell" style="padding-left:' + pad + 'px">' +
        '<span class="persona-ficon">' + PERSONA_ICON_FOLDER + '</span>' + personaEscapeHtml(f.name) + '</td>' +
        '<td></td><td></td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); personaSelectFolder(f.id); });
    } else {
      const p = item.node;
      tr.innerHTML =
        '<td class="persona-name-cell" style="padding-left:' + pad + 'px">' + personaEscapeHtml(p.name) + '</td>' +
        '<td>' + (p.revisionCount || 0) + '</td>' +
        '<td>' + personaShortDate(p.updated_at) + '</td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); personaSelectItem(p.uuid); });
    }
    tb.appendChild(tr);
  });
}
// The selected folder's / persona's name, shown as a click-to-rename control.
// All editing happens in the rename modal (Cancel / Rename are the only ways
// out), so a half-typed name can never be silently lost — the failure mode of
// the old inline field + Rename button.
function personaRenderRename(){
  const el = document.getElementById('persona-node-rename');
  el.innerHTML = '';
  let node = null, type = null;
  if (personaSelectedItem){ node = personaByUuid(personaSelectedItem); type = 'item'; }
  else if (personaSelectedFolder !== null){ node = personaFolderById(personaSelectedFolder); type = 'folder'; }
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'persona-rename-display';
  btn.textContent = node.name;
  btn.title = 'Click to rename';
  btn.addEventListener('click', () => personaOpenRenameModal(type, node, node.name));
  el.appendChild(btn);
}

// ---- rename modal ----
let personaRenameState = null;   // {type: 'item'|'folder', id, original}
function personaOpenRenameModal(type, node, seed){
  personaRenameState = {type: type, id: type === 'item' ? node.uuid : node.id,
                       original: node.name};
  document.getElementById('persona-rename-title').textContent =
    type === 'item' ? 'Rename persona' : 'Rename folder';
  const input = document.getElementById('persona-rename-input');
  input.value = seed;
  personaSyncRenameConfirm();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('persona-rename-modal').hidden = false;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function personaCloseRenameModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('persona-rename-modal').hidden = true;
  personaRenameState = null;
}
// Rename is enabled only for a non-empty name that actually differs.
function personaSyncRenameConfirm(){
  const v = document.getElementById('persona-rename-input').value.trim();
  document.getElementById('persona-rename-confirm').disabled =
    v === '' || !personaRenameState || v === personaRenameState.original;
}
function personaConfirmRenameModal(){
  if (!personaRenameState) return;
  const v = document.getElementById('persona-rename-input').value.trim();
  if (!v || v === personaRenameState.original) return;
  const node = personaRenameState.type === 'item'
    ? personaByUuid(personaRenameState.id) : personaFolderById(personaRenameState.id);
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('persona-rename-modal').hidden = true;
  personaRenameState = null;
  if (!node) return;
  node.name = v;
  personaTouch(node);
  personaRenderTree();
  personaRender();
  personaSave();
  personaToastMsg('Renamed to “' + v + '”');
}
// Description: folders only (personas have no description field).
function personaFillDescValue(el, text){
  if (text){ el.textContent = text; el.classList.remove('muted'); }
  else { el.textContent = '(none)'; el.classList.add('muted'); }
}
function personaRenderFolderDesc(){
  const el = document.getElementById('persona-folder-desc');
  el.innerHTML = '';
  const node = (!personaSelectedItem && personaSelectedFolder !== null)
    ? personaFolderById(personaSelectedFolder) : null;
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const lbl = document.createElement('span'); lbl.className = 'muted'; lbl.textContent = 'Description:';
  const val = document.createElement('span'); personaFillDescValue(val, node.description);
  const btn = document.createElement('button'); btn.textContent = 'Edit description';
  btn.addEventListener('click', personaEditDescription);
  el.appendChild(lbl); el.appendChild(val); el.appendChild(btn);
}
let personaDescOrig = '';
function personaEditDescription(){
  const node = personaSelectedFolder !== null ? personaFolderById(personaSelectedFolder) : null;
  if (!node) return;
  personaDescOrig = node.description || '';
  document.getElementById('persona-desc-input').value = personaDescOrig;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('persona-desc-modal').hidden = false;
  document.getElementById('persona-desc-input').focus();
}
function personaCloseDescModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('persona-desc-modal').hidden = true;
}
function personaSaveDescription(){
  const node = personaSelectedFolder !== null ? personaFolderById(personaSelectedFolder) : null;
  if (node){ node.description = document.getElementById('persona-desc-input').value; personaTouch(node); }
  personaCloseDescModal();
  personaRender();
  personaSave();
}

// ---- editor pane (selected persona: dates, toolbar, editor) ----
// The editor is CodeMirror (markdown highlighting, line numbers, soft wrap)
// over the hidden #persona-content textarea; these wrappers are the only place
// the rest of the page touches it.
let personaCM = null;
function personaInitEditor(){
  personaCM = CodeMirror.fromTextArea(document.getElementById('persona-content'), {
    mode: 'markdown',
    lineNumbers: true,
    lineWrapping: true,
    placeholder: 'Describe who the assistant is…',
  });
  personaEditorReadOnly(true);  // content is read-only until Edit is clicked
}
function personaEditorValue(){ return personaCM.getValue(); }
function personaEditorSet(value){ personaCM.setValue(value); }
function personaEditorReadOnly(ro){ personaCM.setOption('readOnly', ro ? 'nocursor' : false); }

let personaEditorUuid = null;   // uuid whose content the editor currently holds
function personaRenderEditor(){
  const el = document.getElementById('persona-editor');
  const p = personaSelectedItem ? personaByUuid(personaSelectedItem) : null;
  if (!p){
    el.hidden = true;
    personaEditorUuid = null;
    return;
  }
  el.hidden = false;
  personaCM.refresh();  // re-measure: the pane may have been display:none until now
  personaRenderDates(p);
  const rev = document.getElementById('persona-revcount');
  const count = (p && p.revisionCount) || 0;
  rev.textContent = count === 1 ? '1 version' : count + ' versions';
  if (personaEditorUuid !== p.uuid){
    if (personaEditMode) personaExitEdit();  // content is being replaced anyway
    personaEditorUuid = p.uuid;
    personaLoadContent(p.uuid);
  }
}
// A just-created persona has no timestamps until the server assigns them (the
// content fetch backfills below) — show nothing rather than bare labels.
function personaRenderDates(p){
  const parts = [];
  if (p.created_at) parts.push('created ' + personaShortDate(p.created_at));
  if (p.updated_at) parts.push('updated ' + personaShortDate(p.updated_at));
  document.getElementById('persona-dates').textContent = parts.join(' · ');
}
async function personaLoadContent(uuid){
  personaContentLoading = true;   // Edit is refused until the content is in
  personaEditorSet('');
  personaEditorReadOnly(true);
  let d = null;
  try {
    const r = await fetch('/persona/api/personas/' + encodeURIComponent(uuid));
    d = await r.json();
  } catch (e) { /* fall through to the unavailable message */ }
  if (personaEditorUuid !== uuid) return;  // selection moved on; drop this response
  personaContentLoading = false;
  if (!d || !d.ok){
    // A just-created persona may not have hit the DB yet (the tree save is
    // in flight); its content is empty by construction, so an empty editor
    // is correct either way. Stays read-only until Edit is clicked.
    return;
  }
  personaEditorSet(d.content || '');
  // Backfill server-assigned timestamps onto the local tree row (a client-side
  // created row has none until now) and refresh the dates line.
  const local = personaByUuid(uuid);
  if (local){
    if (d.created_at) local.created_at = d.created_at;
    if (d.updated_at) local.updated_at = d.updated_at;
    if (personaSelectedItem === uuid) personaRenderDates(local);
  }
}

// ---- explicit edit mode (content is read-only until Edit → Save/Cancel) ----
// No autosave: an accidental keystroke in a system persona must never persist
// on its own. Edit raises the editor above the shared modal backdrop, so the
// rest of the page is grayed out and non-clickable until the edit is resolved
// — Save PUTs the content, Cancel restores the snapshot. Backdrop-click / Esc
// follow the ui-modals.md dirty guard (dismiss = Cancel, only while unchanged).
let personaEditMode = false;
let personaEditOriginal = '';      // content snapshot at Edit time (Cancel / dirty check)
let personaContentLoading = false; // fetch in flight — its setValue would clobber an edit
function personaSyncEditButtons(){
  document.getElementById('persona-edit-btn').hidden = personaEditMode;
  document.getElementById('persona-save-btn').hidden = !personaEditMode;
  document.getElementById('persona-cancel-btn').hidden = !personaEditMode;
  document.getElementById('persona-history-btn').hidden = personaEditMode;
  document.getElementById('persona-editor').classList.toggle('editing', personaEditMode);
  document.getElementById('ui-modal-backdrop').hidden = !personaEditMode;
}
function personaStartEdit(){
  if (!personaEditorUuid || personaEditMode || personaContentLoading) return;
  personaCloseHistoryView();  // Edit takes over the pane; History has nothing left to show
  personaEditMode = true;
  personaEditOriginal = personaEditorValue();
  personaEditorReadOnly(false);
  personaSyncEditButtons();
  personaCM.focus();
}
function personaExitEdit(){
  personaEditMode = false;
  personaEditOriginal = '';
  personaEditorReadOnly(true);
  personaSyncEditButtons();
}
function personaCancelEdit(){
  if (!personaEditMode) return;
  personaEditorSet(personaEditOriginal);
  personaExitEdit();
}
async function personaSaveEdit(){
  if (!personaEditMode || !personaEditorUuid) return;
  const uuid = personaEditorUuid;
  const saveBtn = document.getElementById('persona-save-btn');
  saveBtn.disabled = true;
  let ok = false, data = null;
  try {
    const r = await fetch('/persona/api/personas/' + encodeURIComponent(uuid), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: personaEditorValue()}),
    });
    ok = r.ok;
    data = await r.json();
  } catch (e) { /* ok stays false */ }
  saveBtn.disabled = false;
  if (!ok){
    personaToastMsg((data && data.error) || 'Save failed — the persona is still in edit mode.');
    return;
  }
  personaTouch(personaByUuid(uuid));
  if (personaSelectedItem === uuid) personaRenderDates(personaByUuid(uuid));
  // The response tells us whether anything was actually recorded: a PUT of
  // identical text changes nothing and appends no revision.
  if (data.changed) {
    const row = personaByUuid(uuid);
    if (row) row.revisionCount = (row.revisionCount || 0) + 1;
    personaToastMsg('saved — version ' + ((row && row.revisionCount) || 1));
  } else {
    personaToastMsg('no changes');
  }
  personaExitEdit();
  personaRenderEditor();
}
// Unsaved edit-mode changes are lost on tab close — warn like any editor.
window.addEventListener('beforeunload', (e) => {
  if (personaEditMode && personaEditorValue() !== personaEditOriginal){
    e.preventDefault();
    e.returnValue = '';
  }
});

// ---- left tree ----
function personaRenderTree(){
  document.getElementById('persona-all').className =
    'persona-node' + ((personaSelectedFolder === null && !personaSelectedItem) ? ' sel' : '');
  const root = document.getElementById('persona-tree-root');
  root.innerHTML = '';
  personaChildFolders(null).forEach(f => root.appendChild(personaFolderLi(f)));
  personasInFolder(null).forEach(p => {
    const li = document.createElement('li'); li.appendChild(personaItemNode(p)); root.appendChild(li);
  });
}
function personaFolderLi(f){
  const li = document.createElement('li');
  const kids = personaChildFolders(f.id);
  const leaves = personasInFolder(f.id);
  const hasKids = (kids.length + leaves.length) > 0;
  const expanded = personaIsExpanded(f.id);
  // A real anchor so CMD/Ctrl/middle click opens the folder view in a new
  // tab; a plain click is intercepted below and selects/toggles in-page.
  const node = document.createElement('a');
  const selected = (personaSelectedFolder === f.id && !personaSelectedItem);
  node.className = 'persona-node' + (selected ? ' sel' : '');
  node.href = '/persona?id=' + encodeURIComponent(f.id);
  const icon = document.createElement('span');
  icon.className = 'persona-ficon';
  icon.innerHTML = (expanded && hasKids) ? PERSONA_ICON_FOLDER_OPEN : PERSONA_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'persona-folder-label';
  label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    personaFolderClick(f.id);
  });
  personaMakeDraggable(node, 'folder', f.id);
  personaMakeFolderDrop(node, f.id);
  // Kebab is rendered on every row but only shown (via CSS) on the selected one,
  // so row heights stay consistent — matches /cron. Add a persona/subfolder via
  // the "+ Persona"/"+ Folder" buttons.
  personaMakeKebab(node, {
    onRename: () => personaKebabRename('folder', f.id),
    onDelete: () => personaConfirmDeleteFolder(f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(personaFolderLi(c)));
    leaves.forEach(p => { const pli = document.createElement('li'); pli.appendChild(personaItemNode(p)); ul.appendChild(pli); });
    li.appendChild(ul);
  }
  return li;
}
function personaItemNode(p){
  // A real anchor so CMD/Ctrl/middle click opens the persona in a new tab; a
  // plain click is intercepted below and selects the persona in-page instead.
  const n = document.createElement('a');
  const selected = (personaSelectedItem === p.uuid);
  n.className = 'persona-item-node' + (selected ? ' sel' : '');
  n.href = '/persona?id=' + encodeURIComponent(p.uuid);
  n.title = p.name;
  // No leaf icon in the tree — every leaf here is a persona, so an icon is noise.
  const label = document.createElement('span'); label.className = 'persona-item-label'; label.textContent = p.name;
  n.appendChild(label);
  n.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    personaSelectItem(p.uuid);
  });
  personaMakeDraggable(n, 'item', p.uuid);
  personaMakeItemDrop(n, p.uuid);
  // Kebab on every row, shown (via CSS) only on the selected one — matches /cron.
  personaMakeKebab(n, {
    onRename: () => personaKebabRename('item', p.uuid),
    onDelete: () => personaConfirmDeleteItem(p.uuid),
  });
  return n;
}
// Kebab "Rename" selects the node and opens the rename modal on it.
function personaKebabRename(type, id){
  personaSelectNode(type, id);
  const node = type === 'item' ? personaByUuid(id) : personaFolderById(id);
  if (node) personaOpenRenameModal(type, node, node.name);
}
// Position a fixed kebab menu near its anchor, clamped inside the viewport:
// below the anchor when it fits, flipped above when it would overflow the
// bottom edge (nodes at the bottom of a long tree). Unhides the menu first so
// its offsetWidth/Height are measurable.
function personaPlaceMenu(menu, anchorRect){
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
// 3-dot overflow menu. opts: { onRename?, onDelete? }.
function personaMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button'; kebab.className = 'persona-kebab';
  kebab.setAttribute('aria-label', 'Item actions'); kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'persona-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
  const items = [];
  if (opts.onRename) items.push(['Rename', opts.onRename, '']);
  if (opts.onDelete) items.push(['Delete', opts.onDelete, 'danger']);
  items.forEach(spec => {
    const item = document.createElement('button');
    item.type = 'button'; item.className = 'item' + (spec[2] ? ' ' + spec[2] : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = spec[0];
    // preventDefault: the menu sits inside the row's anchor — never follow it.
    item.addEventListener('click', e => { e.stopPropagation(); e.preventDefault(); menu.hidden = true; spec[1](); });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', e => {
    e.stopPropagation();
    e.preventDefault();  // the kebab sits inside the row's anchor — never follow it
    const willOpen = menu.hidden;
    document.querySelectorAll('.persona-menu').forEach(m => { m.hidden = true; });
    if (willOpen) personaPlaceMenu(menu, kebab.getBoundingClientRect());
  });
  node.appendChild(kebab); node.appendChild(menu);
}

// ---- add folder / add persona ----
let personaAddFolderAsSub = false;
function personaAddFolder(asSub){
  personaAddFolderAsSub = !!asSub;
  document.getElementById('persona-folder-title').textContent = asSub ? 'New subfolder' : 'New folder';
  const input = document.getElementById('persona-folder-input');
  input.value = '';
  document.getElementById('persona-folder-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('persona-folder-modal').hidden = false;
  input.focus();
}
function personaCloseFolderModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('persona-folder-modal').hidden = true;
}
async function personaAddFolderConfirm(){
  const input = document.getElementById('persona-folder-input');
  const name = (input.value || '').trim();
  if (!name) return;
  const parentId = personaAddFolderAsSub ? personaSelectedFolder : null;
  try {
    await personaFlushPendingSave();
    const resp = await fetch('/persona/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, parentId}),
    });
    const data = await resp.json();
    if (!resp.ok) { personaToastMsg(data.error || 'could not create folder'); return; }
    personaFolders.push(data.folder);
    personaTreeVersion = data.version;
    personaCloseFolderModal();
    personaExpanded[data.folder.id] = true;
    personaSelectFolder(data.folder.id);
  } catch (e) {
    personaToastMsg('could not create folder');
  }
}
function personaAddPersona(){
  const input = document.getElementById('persona-new-input');
  input.value = '';
  document.getElementById('persona-new-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('persona-new-modal').hidden = false;
  input.focus();
}
function personaCloseNewModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('persona-new-modal').hidden = true;
}
// A new persona starts with empty content and no history. It lands in
// the currently-selected folder.
async function personaAddPersonaConfirm(){
  const input = document.getElementById('persona-new-input');
  const name = (input.value || '').trim();
  if (!name) return;
  try {
    await personaFlushPendingSave();
    const resp = await fetch('/persona/api/personas', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, folderId: personaSelectedFolder}),
    });
    const data = await resp.json();
    if (!resp.ok) { personaToastMsg(data.error || 'could not create'); return; }
    personaItems.push(data.persona);
    personaTreeVersion = data.version;
    personaCloseNewModal();
    personaSelectItem(data.persona.uuid);
  } catch (e) {
    personaToastMsg('could not create');
  }
}

// ---- drag & drop (one node at a time) ----
function personaFolderInSubtree(candidateId, rootId){
  let cur = personaFolderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? personaFolderById(cur.parentId) : null;
  }
  return false;
}
function personaMoveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && personaFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = personaFolderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  personaFolders = personaFolders.filter(x => x.id !== folderId);
  if (atStart){
    const i = personaFolders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) personaFolders.push(f); else personaFolders.splice(i, 0, f);
  } else {
    let at = personaFolders.length;
    for (let i = personaFolders.length - 1; i >= 0; i--){
      if ((personaFolders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    personaFolders.splice(at, 0, f);
  }
  personaSave();
}
function personaMoveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = personaFolderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && personaFolderInSubtree(newParent, folderId)) return;  // no cycles
  const f = personaFolderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  personaFolders = personaFolders.filter(x => x.id !== folderId);
  const ti = personaFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) personaFolders.push(f);
  else personaFolders.splice(after ? ti + 1 : ti, 0, f);
  personaSave();
}
function personaMoveItem(itemUuid, targetFolderId, beforeItemUuid){
  targetFolderId = targetFolderId || null;
  const idx = personaItems.findIndex(p => p.uuid === itemUuid);
  if (idx < 0) return;
  const item = personaItems.splice(idx, 1)[0];
  item.folderId = targetFolderId;
  let insertAt = beforeItemUuid ? personaItems.findIndex(p => p.uuid === beforeItemUuid) : -1;
  if (insertAt < 0){
    insertAt = personaItems.length;
    for (let i = personaItems.length - 1; i >= 0; i--){
      if ((personaItems[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  personaItems.splice(insertAt, 0, item);
  personaSave();
}
function personaMakeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    personaDrag = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('persona-dragging');
    document.getElementById('persona-tree').classList.add('persona-dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    personaDrag = null;
    document.getElementById('persona-tree').classList.remove('persona-dragging-on');
    personaRenderTree();
  });
}
function personaDropInto(folderId, atStart){
  if (!personaDrag) return;
  const dragged = personaDrag;
  if (dragged.type === 'item'){
    let beforeUuid = null;
    if (atStart){
      const first = personaItems.find(p => (p.folderId || null) === (folderId || null) && p.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    personaMoveItem(dragged.id, folderId, beforeUuid);
  } else {
    personaMoveFolder(dragged.id, folderId, atStart);
  }
  if (folderId){ personaExpanded[folderId] = true; personaPersistExpand(); }
  personaDrag = null;
  personaSelectNode(dragged.type, dragged.id);  // select the moved node (also renders)
}
function personaMakeFolderDrop(node, folderId){
  // Three zones on a folder: top third = reorder before, bottom third = after
  // (sibling), middle = nest into. Persona items always go "into".
  const zoneOf = e => {
    if (personaDrag && personaDrag.type === 'item') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!personaDrag) return false;
    if (personaDrag.type === 'item') return z === 'into';
    if (folderId === personaDrag.id) return false;
    if (z === 'into') return !personaFolderInSubtree(folderId, personaDrag.id);
    const t = personaFolderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && personaFolderInSubtree(np, personaDrag.id));
  };
  const clear = () => node.classList.remove('persona-drop-before', 'persona-drop-after', 'persona-drop-target');
  node.addEventListener('dragover', e => {
    if (!personaDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('persona-drop-before', z === 'before');
    node.classList.toggle('persona-drop-after', z === 'after');
    node.classList.toggle('persona-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!personaDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      personaDropInto(folderId, false);
    } else {
      const draggedId = personaDrag.id;
      personaMoveFolderBeside(personaDrag.id, folderId, z === 'after');
      personaDrag = null;
      personaSelectNode('folder', draggedId);
    }
  });
}
function personaMakeItemDrop(node, itemUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!personaDrag) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('persona-drop-after', after);
    node.classList.toggle('persona-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('persona-drop-before', 'persona-drop-after'));
  node.addEventListener('drop', e => {
    if (!personaDrag) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('persona-drop-before', 'persona-drop-after');
    personaDropOnItem(itemUuid, after);
  });
}
function personaDropOnItem(targetUuid, after){
  if (!personaDrag) return;
  if (personaDrag.type === 'item' && personaDrag.id === targetUuid) return;
  const dragged = personaDrag;
  const target = personaByUuid(targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'item'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = personaItems.findIndex(p => p.uuid === targetUuid);
      beforeUuid = (ti + 1 < personaItems.length) ? personaItems[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    personaMoveItem(dragged.id, targetFolder, beforeUuid);
  } else {
    personaMoveFolder(dragged.id, targetFolder);
  }
  personaDrag = null;
  personaSelectNode(dragged.type, dragged.id);
}
function personaWireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (personaDrag){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (personaDrag){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); personaDropInto(null, atStart); }
  });
}
function personaInitTreeDnD(){
  const root = document.getElementById('persona-tree-root');
  root.addEventListener('dragover', e => {
    if (personaDrag){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (personaDrag){ e.preventDefault(); personaDropInto(null, false); }  // empty space → end of root
  });
  personaWireRootDrop(document.getElementById('persona-root-drop'), false);
  document.getElementById('persona-all').addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    personaSelectFolder(null);
  });
  // Dismiss any open kebab menu on an outside click or Escape.
  document.addEventListener('click', () => {
    document.querySelectorAll('.persona-menu').forEach(m => { m.hidden = true; });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.persona-menu').forEach(m => { m.hidden = true; });
  });
}

// ---- delete confirmation. The tree PUT can only update existing rows, so
// removal always goes through the dedicated DELETE endpoints below, never
// through the tree save. ----
let personaDeleteOnConfirm = null;
let personaDeleteRequireName = null;
function personaOpenDeleteModal(opts){
  personaDeleteOnConfirm = opts.onConfirm;
  personaDeleteRequireName = opts.requireName || null;
  document.getElementById('persona-delete-title').textContent = opts.title || 'Delete';
  document.getElementById('persona-delete-msg').textContent = opts.message;
  const nameRow = document.getElementById('persona-delete-name-row');
  const input = document.getElementById('persona-delete-input');
  const btn = document.getElementById('persona-delete-confirm');
  if (personaDeleteRequireName){
    nameRow.hidden = false;
    document.getElementById('persona-delete-name').textContent = personaDeleteRequireName;
    input.value = ''; btn.disabled = true;
  } else {
    nameRow.hidden = true; btn.disabled = false;
  }
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('persona-delete-modal').hidden = false;
  if (personaDeleteRequireName) input.focus();
}
function personaCloseDeleteModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('persona-delete-modal').hidden = true;
  personaDeleteOnConfirm = null;
  personaDeleteRequireName = null;
}
function personaDeleteUpdateState(){
  const input = document.getElementById('persona-delete-input');
  document.getElementById('persona-delete-confirm').disabled =
    personaDeleteRequireName ? (input.value.trim() !== personaDeleteRequireName) : false;
}
function personaConfirmDeleteItem(uuid){
  const p = personaByUuid(uuid);
  if (!p) return;
  const revisions = p.revisionCount || 0;
  personaOpenDeleteModal({
    title: 'Delete persona',
    message: revisions
      ? `Delete "${p.name}" and its ${revisions} saved version` +
        `${revisions === 1 ? '' : 's'}? This cannot be undone.`
      : `Delete "${p.name}"?`,
    requireName: revisions ? p.name : null,
    onConfirm: () => personaDeleteItem(uuid),
  });
}
function personaConfirmDeleteFolder(id){
  const f = personaFolderById(id);
  if (!f) return;
  const sub = personaFlattenTree(f.id);
  const folderCount = sub.filter(n => n.kind === 'folder').length;
  const itemCount = sub.filter(n => n.kind === 'item').length;
  if (folderCount + itemCount === 0){
    personaOpenDeleteModal({
      title: 'Delete folder',
      message: 'Delete empty folder "' + f.name + '"?',
      onConfirm: () => personaDeleteFolderById(f.id),
    });
    return;
  }
  const parts = [];
  if (folderCount) parts.push(folderCount + (folderCount === 1 ? ' subfolder' : ' subfolders'));
  if (itemCount) parts.push(itemCount + (itemCount === 1 ? ' persona' : ' personas'));
  personaOpenDeleteModal({
    title: 'Delete folder',
    message: 'Are you sure you want to delete folder "' + f.name + '" containing ' +
      parts.join(' and ') + '? The personas inside are deleted too. This cannot be undone.',
    requireName: f.name,
    onConfirm: () => personaDeleteFolderById(f.id),
  });
}
async function personaDeleteItem(uuid){
  try {
    await personaFlushPendingSave();
    const resp = await fetch('/persona/api/personas/' + uuid,
                             {method: 'DELETE'});
    const data = await resp.json();
    if (!resp.ok) { personaToastMsg(data.error || 'could not delete'); return; }
    personaItems = personaItems.filter(p => p.uuid !== uuid);
    personaTreeVersion = data.version;
    if (personaSelectedItem === uuid) personaSelectedItem = null;
    personaRenderTree();
    personaRender();
    personaSyncUrl();
    personaToastMsg('deleted');
  } catch (e) {
    personaToastMsg('could not delete');
  }
}

async function personaDeleteFolderById(id){
  const doomedFolder = personaFolderById(id);  // captured before removal, for parent fallback below
  try {
    await personaFlushPendingSave();
    const resp = await fetch('/persona/api/folders/' + id, {method: 'DELETE'});
    const data = await resp.json();
    if (!resp.ok) { personaToastMsg(data.error || 'could not delete'); return; }
    // The server cascaded the subtree; mirror that locally instead of re-fetching.
    const doomedFolders = new Set([id]);
    let grew = true;
    while (grew) {
      grew = false;
      personaFolders.forEach(f => {
        if (f.parentId && doomedFolders.has(f.parentId) && !doomedFolders.has(f.id)) {
          doomedFolders.add(f.id); grew = true;
        }
      });
    }
    personaItems = personaItems.filter(p => !doomedFolders.has(p.folderId));
    personaFolders = personaFolders.filter(f => !doomedFolders.has(f.id));
    personaTreeVersion = data.version;
    // Land on the deleted folder's parent, not the root, so the operator stays in context.
    if (doomedFolders.has(personaSelectedFolder)) {
      personaSelectedFolder = (doomedFolder && doomedFolder.parentId) || null;
    }
    if (personaSelectedItem && !personaByUuid(personaSelectedItem)) {
      personaSelectedItem = null;
    }
    personaRenderTree();
    personaRender();
    personaSyncUrl();
    personaToastMsg('deleted');
  } catch (e) {
    personaToastMsg('could not delete');
  }
}
document.getElementById('persona-delete-input').addEventListener('input', personaDeleteUpdateState);
document.getElementById('persona-delete-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('persona-delete-confirm').disabled){
    e.preventDefault();
    document.getElementById('persona-delete-confirm').click();
  }
});
document.getElementById('persona-delete-confirm').addEventListener('click', () => {
  const fn = personaDeleteOnConfirm;
  personaCloseDeleteModal();
  if (fn) fn();
});

// ---- persistence ----
async function personaLoadTree(){
  try {
    const r = await fetch('/persona/api/tree');
    const data = await r.json();
    personaFolders = (data && data.folders) || [];
    personaItems = (data && data.personas) || [];
    personaTreeVersion = (data && data.version) || null;
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    personaFolders = []; personaItems = []; personaTreeVersion = null;
  }
}
let personaToastTimer = null;
function personaToastMsg(text){
  const el = document.getElementById('persona-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(personaToastTimer);
  personaToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let personaSaveTimer = null;
let personaTreeVersion = null;    // token from hydrate; PUTs echo it (stale → 409)
let personaSaveInFlight = false;
let personaSaveQueued = false;
let personaSaveChain = null;      // promise for the active PUT (+ any queued follow-up), or null when idle
function personaSave(){
  clearTimeout(personaSaveTimer);
  personaSaveTimer = setTimeout(personaSavePush, 250);  // coalesce bursts into one PUT
}
// Per notes/ui-tree-persistence.md: "Flush or await a pending tree PUT before
// issuing a create or delete." Nothing else orders a tree PUT against a
// create/delete, and the two responses race — if the older PUT's response
// lands after the create/delete's fresher token, it overwrites that token
// with a stale one and the next save 409s for no reason the operator can see.
// Resolves once no tree PUT is outstanding and personaTreeVersion holds
// the newest token: cancels a pending debounce timer and runs that save
// immediately, or awaits a save already in flight (including one queued
// behind it — personaSaveChain covers that via personaSavePush's
// own re-invocation, see below). A no-op when nothing is pending.
function personaFlushPendingSave(){
  if (personaSaveTimer){
    clearTimeout(personaSaveTimer);
    personaSaveTimer = null;
    return personaSavePush();
  }
  return personaSaveChain || Promise.resolve();
}
// After a re-hydrate, the fresh data may no longer contain the selected
// folder/persona (e.g. the rejected edit was the move that put it there).
// Clear whichever selection no longer resolves so render doesn't point at a
// row that isn't in the tree anymore.
function personaReconcileSelectionAfterReload(){
  if (personaSelectedItem && !personaByUuid(personaSelectedItem)) {
    personaSelectedItem = null;
  }
  if (personaSelectedFolder && !personaFolderById(personaSelectedFolder)) {
    personaSelectedFolder = null;
  }
}
// Returns the promise for this save (or, if one was already in flight, the
// promise for that one — which folds in this call via personaSaveQueued
// once it settles). personaFlushPendingSave relies on that: awaiting the
// returned/chained promise always means "no tree PUT is outstanding anymore".
function personaSavePush(){
  if (personaSaveInFlight) { personaSaveQueued = true; return personaSaveChain; }
  personaSaveInFlight = true;
  const run = (async () => {
    try {
      const resp = await fetch('/persona/api/tree', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          folders: personaFolders.map(f => ({
            id: f.id, name: f.name, description: f.description || '',
            parentId: f.parentId || null})),
          personas: personaItems.map(p => ({
            uuid: p.uuid, name: p.name, folderId: p.folderId || null})),
          version: personaTreeVersion,
        }),
      });
      const data = await resp.json();
      if (resp.status === 409) {
        // Another tab/editor changed the tree; their version wins — re-hydrate
        // and repaint so the screen matches what the server just accepted.
        await personaLoadTree();
        personaReconcileSelectionAfterReload();
        personaRenderTree();
        personaRender();
        personaToastMsg('tree changed elsewhere — reloaded');
        return;
      }
      if (!resp.ok) {
        // A 400 here means our payload disagreed with the server about which
        // rows exist — re-hydrate rather than retry the same bad shape, and
        // repaint so the rejected edit doesn't linger on screen.
        await personaLoadTree();
        personaReconcileSelectionAfterReload();
        personaRenderTree();
        personaRender();
        personaToastMsg(data.error || 'save failed — reloaded');
        return;
      }
      personaTreeVersion = data.version;
    } catch (e) {
      // Network error: we can't tell whether the server applied the change, so
      // re-hydrate and repaint rather than leave the client's guess on screen.
      await personaLoadTree();
      personaReconcileSelectionAfterReload();
      personaRenderTree();
      personaRender();
      personaToastMsg('save failed — reloaded');
    } finally {
      personaSaveInFlight = false;
      // A save requested while we were in flight (personaSave() re-invoked
      // us in the middle of this run) gets its own immediate push here, not a
      // fresh 250ms debounce — and this run doesn't resolve until that one
      // does too, so anything awaiting it (personaFlushPendingSave) sees
      // the whole chain settle before the token is treated as final.
      if (personaSaveQueued) {
        personaSaveQueued = false;
        personaSaveChain = personaSavePush();
        await personaSaveChain;
      } else {
        personaSaveChain = null;
      }
    }
  })();
  personaSaveChain = run;
  return run;
}

// ---- dirty-guarded dismissal (clicking backdrop / Esc) ----
function personaOpenModalDirty(){
  if (!document.getElementById('persona-folder-modal').hidden){
    return document.getElementById('persona-folder-input').value.trim() !== '';
  }
  if (!document.getElementById('persona-new-modal').hidden){
    return document.getElementById('persona-new-input').value.trim() !== '';
  }
  if (!document.getElementById('persona-desc-modal').hidden){
    return document.getElementById('persona-desc-input').value !== personaDescOrig;
  }
  // Rename: dirty once the typed name differs from the stored one — only the
  // explicit Rename/Cancel buttons close it then.
  if (!document.getElementById('persona-rename-modal').hidden){
    return document.getElementById('persona-rename-input').value
      !== ((personaRenameState && personaRenameState.original) || '');
  }
  // Delete: dirty only when the type-to-confirm box is in use and non-empty;
  // a plain yes/no delete is never dirty.
  if (!document.getElementById('persona-delete-modal').hidden){
    return personaDeleteRequireName
      ? document.getElementById('persona-delete-input').value.trim() !== '' : false;
  }
  // Restore confirm: a plain yes/no like a no-name-required delete — never dirty.
  if (!document.getElementById('persona-restore-modal').hidden){
    return false;
  }
  // Content edit mode behaves like an open modal: dirty once the text differs
  // from the snapshot — then only Save / Cancel end it.
  if (personaEditMode){
    return personaEditorValue() !== personaEditOriginal;
  }
  return false;
}
function personaCloseOpenModal(){
  if (!document.getElementById('persona-folder-modal').hidden){ personaCloseFolderModal(); return; }
  if (!document.getElementById('persona-new-modal').hidden){ personaCloseNewModal(); return; }
  if (!document.getElementById('persona-desc-modal').hidden){ personaCloseDescModal(); return; }
  if (!document.getElementById('persona-rename-modal').hidden){ personaCloseRenameModal(); return; }
  if (!document.getElementById('persona-delete-modal').hidden){ personaCloseDeleteModal(); return; }
  if (!document.getElementById('persona-restore-modal').hidden){ personaCloseRestoreModal(); return; }
  if (personaEditMode){ personaCancelEdit(); return; }
}
function personaDismissIfClean(){ if (!personaOpenModalDirty()) personaCloseOpenModal(); }

// ---- history view (append-only revisions; restore appends, never rewinds) ----
let personaHistoryOpen = false;
let personaHistoryRows = [];   // [{uuid, created_at, bytes, lines, preview, current}]
let personaRestoreUuid = null; // revision awaiting confirmation

function personaHistoryVisible(show){
  document.getElementById('persona-history').hidden = !show;
  document.getElementById('persona-editor')
          .querySelector('.CodeMirror').style.display = show ? 'none' : '';
  if (!show) personaCM.refresh();  // re-measure: it was display:none while History was open
}
// Shared by a selection change and by entering edit mode: either way the
// editor pane takes over and History has nothing left to show. Closing here
// is unconditional (not a toggle), so it's a no-op when History is already shut.
function personaCloseHistoryView(){
  if (!personaHistoryOpen) return;
  personaHistoryOpen = false;
  document.getElementById('persona-history-btn').textContent = 'History';
  personaHistoryVisible(false);
}

async function personaToggleHistory(){
  personaHistoryOpen = !personaHistoryOpen;
  document.getElementById('persona-history-btn').textContent =
    personaHistoryOpen ? 'Editor' : 'History';
  personaHistoryVisible(personaHistoryOpen);
  if (personaHistoryOpen) await personaLoadHistory(personaSelectedItem);
}

async function personaLoadHistory(uuid){
  const box = document.getElementById('persona-history-rows');
  const diff = document.getElementById('persona-history-diff');
  diff.hidden = true;
  box.innerHTML = '';
  if (!uuid) return;
  let data;
  try {
    const resp = await fetch('/persona/api/personas/' + uuid + '/revisions');
    data = await resp.json();
    if (!resp.ok) { personaToastMsg(data.error || 'could not load history'); return; }
  } catch (e) {
    personaToastMsg('could not load history');
    return;
  }
  personaHistoryRows = data.revisions;
  if (!personaHistoryRows.length) {
    box.innerHTML = '<tr><td colspan="4" class="muted">' +
      'No versions yet — the first save records one.</td></tr>';
    return;
  }
  personaHistoryRows.forEach(r => {
    const tr = document.createElement('tr');
    const when = personaShortDate(r.created_at) + (r.current ? ' (current)' : '');
    tr.innerHTML =
      '<td class="persona-name-cell">' + personaEscapeHtml(when) + '</td>' +
      '<td>' + r.bytes + ' B</td>' +
      '<td>' + personaEscapeHtml(r.preview) + '</td>' +
      '<td></td>';
    const actions = tr.lastElementChild;
    const diffBtn = document.createElement('button');
    diffBtn.textContent = 'Diff';
    diffBtn.onclick = () => personaShowRevisionDiff(r.uuid);
    actions.appendChild(diffBtn);
    if (!r.current) {
      const restoreBtn = document.createElement('button');
      restoreBtn.textContent = 'Restore';
      restoreBtn.onclick = () => personaConfirmRestore(r.uuid);
      actions.appendChild(restoreBtn);
    }
    box.appendChild(tr);
  });
}

async function personaShowRevisionDiff(revisionUuid){
  const uuid = personaSelectedItem;
  const box = document.getElementById('persona-history-diff');
  box.hidden = false;
  box.textContent = 'Loading…';
  let data;
  try {
    const resp = await fetch('/persona/api/personas/' + uuid +
                             '/revisions/' + revisionUuid + '/diff');
    data = await resp.json();
    if (!resp.ok) { box.textContent = data.error || 'could not diff'; return; }
  } catch (e) {
    box.textContent = 'could not diff';
    return;
  }
  box.innerHTML = '';
  if (!data.lines.length) {
    box.innerHTML = '<div class="persona-diff-line ctx">' +
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
    div.className = 'persona-diff-line ' + cls;
    div.textContent = line;
    box.appendChild(div);
  });
}

function personaConfirmRestore(revisionUuid){
  const row = personaHistoryRows.find(r => r.uuid === revisionUuid);
  personaRestoreUuid = revisionUuid;
  document.getElementById('persona-restore-msg').textContent =
    'Restore the version saved ' + personaShortDate(row && row.created_at) + '?';
  document.getElementById('persona-restore-confirm').onclick =
    personaRestoreConfirmed;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('persona-restore-modal').hidden = false;
}

function personaCloseRestoreModal(){
  personaRestoreUuid = null;
  document.getElementById('persona-restore-modal').hidden = true;
  document.getElementById('ui-modal-backdrop').hidden = true;
}

async function personaRestoreConfirmed(){
  const uuid = personaSelectedItem;
  const revisionUuid = personaRestoreUuid;
  personaCloseRestoreModal();
  if (!uuid || !revisionUuid) return;
  let data;
  try {
    const resp = await fetch('/persona/api/personas/' + uuid +
                             '/revisions/' + revisionUuid + '/restore',
                             {method: 'POST'});
    data = await resp.json();
    if (!resp.ok) { personaToastMsg(data.error || 'could not restore'); return; }
  } catch (e) {
    personaToastMsg('could not restore');
    return;
  }
  personaEditorSet(data.content);
  if (data.changed) {
    const row = personaByUuid(uuid);
    if (row) row.revisionCount = (row.revisionCount || 0) + 1;
    personaToastMsg('restored as a new version');
  } else {
    personaToastMsg('already the current text');
  }
  await personaLoadHistory(uuid);
  personaRenderEditor();
}

// ---- wiring + initial paint ----
personaInitTreeDnD();
personaInitEditor();
document.getElementById('persona-folder-input').addEventListener('input', () => {
  document.getElementById('persona-folder-create').disabled =
    document.getElementById('persona-folder-input').value.trim() === '';
});
document.getElementById('persona-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('persona-folder-create').disabled){
    e.preventDefault(); personaAddFolderConfirm();
  }
});
document.getElementById('persona-new-input').addEventListener('input', () => {
  document.getElementById('persona-new-create').disabled =
    document.getElementById('persona-new-input').value.trim() === '';
});
document.getElementById('persona-new-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('persona-new-create').disabled){
    e.preventDefault(); personaAddPersonaConfirm();
  }
});
document.getElementById('persona-rename-input').addEventListener('input', personaSyncRenameConfirm);
document.getElementById('persona-rename-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('persona-rename-confirm').disabled){
    e.preventDefault(); personaConfirmRenameModal();
  }
});
document.getElementById('ui-modal-backdrop').addEventListener('click', personaDismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') personaDismissIfClean(); });
personaLoadTree().then(() => {
  // Deep link: ?id=<uuid> selects that folder or persona on load.
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && personaFolderById(wantId)){
    personaSelectFolder(wantId);
  } else if (wantId && personaByUuid(wantId)){
    personaSelectItem(wantId);
  } else {
    personaRenderTree();
    personaRender();
  }
});
