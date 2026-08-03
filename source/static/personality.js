// /personality page logic (vanilla JS, no framework). The HTML shell + CSS
// live in webapp/personality_views.py; this file is served at
// /static/personality.js with an mtime cache-buster. State hydrates from
// GET /personality/api/tree and structural edits save via debounced PUTs.
// Per docs/ui-tree-persistence.md the PUT can only update rows that already
// exist: creating and deleting go to their own endpoints, so no payload of
// ours can destroy a personality or its history. Ported from static/prompt.js.

// ---- helpers ----
function personalityEscapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function personalityShortDate(iso){
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toISOString().slice(0, 16).replace('T', ' ');
}

// ---- state ----
let personalityFolders = [];          // {id, name, description, parentId, ...}
let personalityItems = [];            // {uuid, name, folderId, revisionCount, ...}
let personalitySelectedFolder = null; // folder id, or null for "All personalities" / root
let personalitySelectedItem = null;   // personality uuid when a personality is selected
let personalityExpanded = {};         // folder id -> false when collapsed (default expanded)
let personalityDrag = null;           // {type:'folder'|'item', id} while a node is dragged
const PERSONALITY_EXPAND_KEY = 'personality.expandedFolders';
try { personalityExpanded = JSON.parse(localStorage.getItem(PERSONALITY_EXPAND_KEY)) || {}; }
catch (e) { personalityExpanded = {}; }
function personalityPersistExpand(){
  try { localStorage.setItem(PERSONALITY_EXPAND_KEY, JSON.stringify(personalityExpanded)); }
  catch (e) { /* private mode etc. — expand state just won't survive reload */ }
}

// ---- inlined Lucide icons (https://lucide.dev), self-contained ----
const PERSONALITY_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const PERSONALITY_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

// ---- lookups ----
function personalityFolderById(id){ return personalityFolders.find(f => f.id === id) || null; }
function personalityByUuid(uuid){ return personalityItems.find(p => p.uuid === uuid) || null; }
function personalityChildFolders(parentId){ return personalityFolders.filter(f => (f.parentId || null) === (parentId || null)); }
function personalitiesInFolder(id){
  const target = id || null;
  if (target === null) {
    // Root level also surfaces a personality whose folderId names a folder
    // that isn't in personalityFolders (e.g. the folder was deleted via the
    // admin, orphaning the row). The server rejects it in every tree save,
    // so if it stayed invisible here the operator could never reach it to
    // move or delete it and every structural edit would 400 forever.
    return personalityItems.filter(p => {
      const fid = p.folderId || null;
      return fid === null || !personalityFolderById(fid);
    });
  }
  return personalityItems.filter(p => (p.folderId || null) === target);
}
function personalityIsExpanded(id){ return personalityExpanded[id] !== false; }
// Optimistically stamp a node as just-modified; the server sets the
// authoritative updated_at on save and a reload reconciles.
function personalityTouch(node){ if (node) node.updated_at = new Date().toISOString(); }

// ---- selection ----
function personalityCurrentSelectionId(){
  if (personalitySelectedItem) return personalitySelectedItem;
  if (personalitySelectedFolder) return personalitySelectedFolder;
  return null;
}
function personalitySyncUrl(){
  // Reflect the selection in ?id= so the URL is a shareable deep link.
  const url = new URL(window.location);
  const id = personalityCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function personalitySelectFolder(id){
  personalitySelectedFolder = id;
  personalitySelectedItem = null;
  personalityCloseHistoryView();  // a folder has nothing to show History for
  personalityRenderTree();
  personalityRender();
}
function personalitySelectItem(uuid){
  const p = personalityByUuid(uuid);
  personalitySelectedItem = uuid;
  personalitySelectedFolder = p ? (p.folderId || null) : null;
  personalityCloseHistoryView();  // a different personality has nothing to show History for
  personalityRenderTree();
  personalityRender();
}
function personalitySelectNode(type, id){
  if (type === 'item') personalitySelectItem(id); else personalitySelectFolder(id);
}
function personalityFolderClick(id){
  // First click selects; clicking the already-selected folder toggles expand.
  const wasSelected = (personalitySelectedFolder === id) && !personalitySelectedItem;
  if (wasSelected){
    personalityExpanded[id] = !personalityIsExpanded(id);
    personalityPersistExpand();
    personalityRenderTree();
    personalityRender();
  } else {
    personalitySelectFolder(id);
  }
}

// ---- right-pane render ----
function personalityRender(){
  personalityRenderRename();
  personalityRenderFolderDesc();
  personalityRenderContents();
  personalityRenderEditor();
  personalitySyncUrl();
}
// Depth-first list of everything under parentId (null = whole tree), in the
// same order as the left tree, each row tagged with its nesting `depth` — like
// /cron's cronFlattenTree (docs/ui-left-panel-tree.md §7).
function personalityFlattenTree(parentId){
  parentId = parentId || null;
  const out = [];
  const walk = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    personalityChildFolders(f.id).forEach(c => walk(c, depth + 1));
    personalitiesInFolder(f.id).forEach(p => out.push({kind: 'item', node: p, depth: depth + 1}));
  };
  personalityChildFolders(parentId).forEach(f => walk(f, 0));
  personalitiesInFolder(parentId).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
  return out;
}
function personalityRenderContents(){
  const wrap = document.getElementById('personality-table-wrap');
  const editorView = !!personalitySelectedItem;
  wrap.hidden = editorView;
  if (editorView) return;
  const tb = document.getElementById('personality-rows');
  tb.innerHTML = '';
  // The selected folder's whole subtree (or the entire tree at the root),
  // depth-first and depth-indented, mirroring the left tree.
  const nodes = personalityFlattenTree(personalitySelectedFolder);
  if (!nodes.length){
    tb.innerHTML = '<tr><td colspan="4"><i>' +
      (personalitySelectedFolder === null ? 'no personalities yet' : 'empty folder') + '</i></td></tr>';
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
        '<td class="personality-name-cell" style="padding-left:' + pad + 'px">' +
        '<span class="personality-ficon">' + PERSONALITY_ICON_FOLDER + '</span>' + personalityEscapeHtml(f.name) + '</td>' +
        '<td></td><td></td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); personalitySelectFolder(f.id); });
    } else {
      const p = item.node;
      tr.innerHTML =
        '<td class="personality-name-cell" style="padding-left:' + pad + 'px">' + personalityEscapeHtml(p.name) + '</td>' +
        '<td>' + (p.revisionCount || 0) + '</td>' +
        '<td>' + personalityShortDate(p.updated_at) + '</td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); personalitySelectItem(p.uuid); });
    }
    tb.appendChild(tr);
  });
}
// The selected folder's / personality's name, shown as a click-to-rename control.
// All editing happens in the rename modal (Cancel / Rename are the only ways
// out), so a half-typed name can never be silently lost — the failure mode of
// the old inline field + Rename button.
function personalityRenderRename(){
  const el = document.getElementById('personality-node-rename');
  el.innerHTML = '';
  let node = null, type = null;
  if (personalitySelectedItem){ node = personalityByUuid(personalitySelectedItem); type = 'item'; }
  else if (personalitySelectedFolder !== null){ node = personalityFolderById(personalitySelectedFolder); type = 'folder'; }
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'personality-rename-display';
  btn.textContent = node.name;
  btn.title = 'Click to rename';
  btn.addEventListener('click', () => personalityOpenRenameModal(type, node, node.name));
  el.appendChild(btn);
}

// ---- rename modal ----
let personalityRenameState = null;   // {type: 'item'|'folder', id, original}
function personalityOpenRenameModal(type, node, seed){
  personalityRenameState = {type: type, id: type === 'item' ? node.uuid : node.id,
                       original: node.name};
  document.getElementById('personality-rename-title').textContent =
    type === 'item' ? 'Rename personality' : 'Rename folder';
  const input = document.getElementById('personality-rename-input');
  input.value = seed;
  personalitySyncRenameConfirm();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('personality-rename-modal').hidden = false;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function personalityCloseRenameModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('personality-rename-modal').hidden = true;
  personalityRenameState = null;
}
// Rename is enabled only for a non-empty name that actually differs.
function personalitySyncRenameConfirm(){
  const v = document.getElementById('personality-rename-input').value.trim();
  document.getElementById('personality-rename-confirm').disabled =
    v === '' || !personalityRenameState || v === personalityRenameState.original;
}
function personalityConfirmRenameModal(){
  if (!personalityRenameState) return;
  const v = document.getElementById('personality-rename-input').value.trim();
  if (!v || v === personalityRenameState.original) return;
  const node = personalityRenameState.type === 'item'
    ? personalityByUuid(personalityRenameState.id) : personalityFolderById(personalityRenameState.id);
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('personality-rename-modal').hidden = true;
  personalityRenameState = null;
  if (!node) return;
  node.name = v;
  personalityTouch(node);
  personalityRenderTree();
  personalityRender();
  personalitySave();
  personalityToastMsg('Renamed to “' + v + '”');
}
// Description: folders only (personalities have no description field).
function personalityFillDescValue(el, text){
  if (text){ el.textContent = text; el.classList.remove('muted'); }
  else { el.textContent = '(none)'; el.classList.add('muted'); }
}
function personalityRenderFolderDesc(){
  const el = document.getElementById('personality-folder-desc');
  el.innerHTML = '';
  const node = (!personalitySelectedItem && personalitySelectedFolder !== null)
    ? personalityFolderById(personalitySelectedFolder) : null;
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const lbl = document.createElement('span'); lbl.className = 'muted'; lbl.textContent = 'Description:';
  const val = document.createElement('span'); personalityFillDescValue(val, node.description);
  const btn = document.createElement('button'); btn.textContent = 'Edit description';
  btn.addEventListener('click', personalityEditDescription);
  el.appendChild(lbl); el.appendChild(val); el.appendChild(btn);
}
let personalityDescOrig = '';
function personalityEditDescription(){
  const node = personalitySelectedFolder !== null ? personalityFolderById(personalitySelectedFolder) : null;
  if (!node) return;
  personalityDescOrig = node.description || '';
  document.getElementById('personality-desc-input').value = personalityDescOrig;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('personality-desc-modal').hidden = false;
  document.getElementById('personality-desc-input').focus();
}
function personalityCloseDescModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('personality-desc-modal').hidden = true;
}
function personalitySaveDescription(){
  const node = personalitySelectedFolder !== null ? personalityFolderById(personalitySelectedFolder) : null;
  if (node){ node.description = document.getElementById('personality-desc-input').value; personalityTouch(node); }
  personalityCloseDescModal();
  personalityRender();
  personalitySave();
}

// ---- editor pane (selected personality: dates, toolbar, editor) ----
// The editor is CodeMirror (markdown highlighting, line numbers, soft wrap)
// over the hidden #personality-content textarea; these wrappers are the only place
// the rest of the page touches it.
let personalityCM = null;
function personalityInitEditor(){
  personalityCM = CodeMirror.fromTextArea(document.getElementById('personality-content'), {
    mode: 'markdown',
    lineNumbers: true,
    lineWrapping: true,
    placeholder: 'Describe who the assistant is…',
  });
  personalityEditorReadOnly(true);  // content is read-only until Edit is clicked
}
function personalityEditorValue(){ return personalityCM.getValue(); }
function personalityEditorSet(value){ personalityCM.setValue(value); }
function personalityEditorReadOnly(ro){ personalityCM.setOption('readOnly', ro ? 'nocursor' : false); }

let personalityEditorUuid = null;   // uuid whose content the editor currently holds
function personalityRenderEditor(){
  const el = document.getElementById('personality-editor');
  const p = personalitySelectedItem ? personalityByUuid(personalitySelectedItem) : null;
  if (!p){
    el.hidden = true;
    personalityEditorUuid = null;
    return;
  }
  el.hidden = false;
  personalityCM.refresh();  // re-measure: the pane may have been display:none until now
  personalityRenderDates(p);
  const rev = document.getElementById('personality-revcount');
  const count = (p && p.revisionCount) || 0;
  rev.textContent = count === 1 ? '1 version' : count + ' versions';
  if (personalityEditorUuid !== p.uuid){
    if (personalityEditMode) personalityExitEdit();  // content is being replaced anyway
    personalityEditorUuid = p.uuid;
    personalityLoadContent(p.uuid);
  }
}
// A just-created personality has no timestamps until the server assigns them (the
// content fetch backfills below) — show nothing rather than bare labels.
function personalityRenderDates(p){
  const parts = [];
  if (p.created_at) parts.push('created ' + personalityShortDate(p.created_at));
  if (p.updated_at) parts.push('updated ' + personalityShortDate(p.updated_at));
  document.getElementById('personality-dates').textContent = parts.join(' · ');
}
async function personalityLoadContent(uuid){
  personalityContentLoading = true;   // Edit is refused until the content is in
  personalityEditorSet('');
  personalityEditorReadOnly(true);
  let d = null;
  try {
    const r = await fetch('/personality/api/personalities/' + encodeURIComponent(uuid));
    d = await r.json();
  } catch (e) { /* fall through to the unavailable message */ }
  if (personalityEditorUuid !== uuid) return;  // selection moved on; drop this response
  personalityContentLoading = false;
  if (!d || !d.ok){
    // A just-created personality may not have hit the DB yet (the tree save is
    // in flight); its content is empty by construction, so an empty editor
    // is correct either way. Stays read-only until Edit is clicked.
    return;
  }
  personalityEditorSet(d.content || '');
  // Backfill server-assigned timestamps onto the local tree row (a client-side
  // created row has none until now) and refresh the dates line.
  const local = personalityByUuid(uuid);
  if (local){
    if (d.created_at) local.created_at = d.created_at;
    if (d.updated_at) local.updated_at = d.updated_at;
    if (personalitySelectedItem === uuid) personalityRenderDates(local);
  }
}

// ---- explicit edit mode (content is read-only until Edit → Save/Cancel) ----
// No autosave: an accidental keystroke in a system personality must never persist
// on its own. Edit raises the editor above the shared modal backdrop, so the
// rest of the page is grayed out and non-clickable until the edit is resolved
// — Save PUTs the content, Cancel restores the snapshot. Backdrop-click / Esc
// follow the ui-modals.md dirty guard (dismiss = Cancel, only while unchanged).
let personalityEditMode = false;
let personalityEditOriginal = '';      // content snapshot at Edit time (Cancel / dirty check)
let personalityContentLoading = false; // fetch in flight — its setValue would clobber an edit
function personalitySyncEditButtons(){
  document.getElementById('personality-edit-btn').hidden = personalityEditMode;
  document.getElementById('personality-save-btn').hidden = !personalityEditMode;
  document.getElementById('personality-cancel-btn').hidden = !personalityEditMode;
  document.getElementById('personality-history-btn').hidden = personalityEditMode;
  document.getElementById('personality-editor').classList.toggle('editing', personalityEditMode);
  document.getElementById('ui-modal-backdrop').hidden = !personalityEditMode;
}
function personalityStartEdit(){
  if (!personalityEditorUuid || personalityEditMode || personalityContentLoading) return;
  personalityCloseHistoryView();  // Edit takes over the pane; History has nothing left to show
  personalityEditMode = true;
  personalityEditOriginal = personalityEditorValue();
  personalityEditorReadOnly(false);
  personalitySyncEditButtons();
  personalityCM.focus();
}
function personalityExitEdit(){
  personalityEditMode = false;
  personalityEditOriginal = '';
  personalityEditorReadOnly(true);
  personalitySyncEditButtons();
}
function personalityCancelEdit(){
  if (!personalityEditMode) return;
  personalityEditorSet(personalityEditOriginal);
  personalityExitEdit();
}
async function personalitySaveEdit(){
  if (!personalityEditMode || !personalityEditorUuid) return;
  const uuid = personalityEditorUuid;
  const saveBtn = document.getElementById('personality-save-btn');
  saveBtn.disabled = true;
  let ok = false, data = null;
  try {
    const r = await fetch('/personality/api/personalities/' + encodeURIComponent(uuid), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: personalityEditorValue()}),
    });
    ok = r.ok;
    data = await r.json();
  } catch (e) { /* ok stays false */ }
  saveBtn.disabled = false;
  if (!ok){
    personalityToastMsg((data && data.error) || 'Save failed — the personality is still in edit mode.');
    return;
  }
  personalityTouch(personalityByUuid(uuid));
  if (personalitySelectedItem === uuid) personalityRenderDates(personalityByUuid(uuid));
  // The response tells us whether anything was actually recorded: a PUT of
  // identical text changes nothing and appends no revision.
  if (data.changed) {
    const row = personalityByUuid(uuid);
    if (row) row.revisionCount = (row.revisionCount || 0) + 1;
    personalityToastMsg('saved — version ' + ((row && row.revisionCount) || 1));
  } else {
    personalityToastMsg('no changes');
  }
  personalityExitEdit();
  personalityRenderEditor();
}
// Unsaved edit-mode changes are lost on tab close — warn like any editor.
window.addEventListener('beforeunload', (e) => {
  if (personalityEditMode && personalityEditorValue() !== personalityEditOriginal){
    e.preventDefault();
    e.returnValue = '';
  }
});

// ---- left tree ----
function personalityRenderTree(){
  document.getElementById('personality-all').className =
    'personality-node' + ((personalitySelectedFolder === null && !personalitySelectedItem) ? ' sel' : '');
  const root = document.getElementById('personality-tree-root');
  root.innerHTML = '';
  personalityChildFolders(null).forEach(f => root.appendChild(personalityFolderLi(f)));
  personalitiesInFolder(null).forEach(p => {
    const li = document.createElement('li'); li.appendChild(personalityItemNode(p)); root.appendChild(li);
  });
}
function personalityFolderLi(f){
  const li = document.createElement('li');
  const kids = personalityChildFolders(f.id);
  const leaves = personalitiesInFolder(f.id);
  const hasKids = (kids.length + leaves.length) > 0;
  const expanded = personalityIsExpanded(f.id);
  // A real anchor so CMD/Ctrl/middle click opens the folder view in a new
  // tab; a plain click is intercepted below and selects/toggles in-page.
  const node = document.createElement('a');
  const selected = (personalitySelectedFolder === f.id && !personalitySelectedItem);
  node.className = 'personality-node' + (selected ? ' sel' : '');
  node.href = '/personality?id=' + encodeURIComponent(f.id);
  const icon = document.createElement('span');
  icon.className = 'personality-ficon';
  icon.innerHTML = (expanded && hasKids) ? PERSONALITY_ICON_FOLDER_OPEN : PERSONALITY_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'personality-folder-label';
  label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    personalityFolderClick(f.id);
  });
  personalityMakeDraggable(node, 'folder', f.id);
  personalityMakeFolderDrop(node, f.id);
  // Kebab is rendered on every row but only shown (via CSS) on the selected one,
  // so row heights stay consistent — matches /cron. Add a personality/subfolder via
  // the "+ Personality"/"+ Folder" buttons.
  personalityMakeKebab(node, {
    onRename: () => personalityKebabRename('folder', f.id),
    onDelete: () => personalityConfirmDeleteFolder(f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(personalityFolderLi(c)));
    leaves.forEach(p => { const pli = document.createElement('li'); pli.appendChild(personalityItemNode(p)); ul.appendChild(pli); });
    li.appendChild(ul);
  }
  return li;
}
function personalityItemNode(p){
  // A real anchor so CMD/Ctrl/middle click opens the personality in a new tab; a
  // plain click is intercepted below and selects the personality in-page instead.
  const n = document.createElement('a');
  const selected = (personalitySelectedItem === p.uuid);
  n.className = 'personality-item-node' + (selected ? ' sel' : '');
  n.href = '/personality?id=' + encodeURIComponent(p.uuid);
  n.title = p.name;
  // No leaf icon in the tree — every leaf here is a personality, so an icon is noise.
  const label = document.createElement('span'); label.className = 'personality-item-label'; label.textContent = p.name;
  n.appendChild(label);
  n.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    personalitySelectItem(p.uuid);
  });
  personalityMakeDraggable(n, 'item', p.uuid);
  personalityMakeItemDrop(n, p.uuid);
  // Kebab on every row, shown (via CSS) only on the selected one — matches /cron.
  personalityMakeKebab(n, {
    onRename: () => personalityKebabRename('item', p.uuid),
    onDelete: () => personalityConfirmDeleteItem(p.uuid),
  });
  return n;
}
// Kebab "Rename" selects the node and opens the rename modal on it.
function personalityKebabRename(type, id){
  personalitySelectNode(type, id);
  const node = type === 'item' ? personalityByUuid(id) : personalityFolderById(id);
  if (node) personalityOpenRenameModal(type, node, node.name);
}
// Position a fixed kebab menu near its anchor, clamped inside the viewport:
// below the anchor when it fits, flipped above when it would overflow the
// bottom edge (nodes at the bottom of a long tree). Unhides the menu first so
// its offsetWidth/Height are measurable.
function personalityPlaceMenu(menu, anchorRect){
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
function personalityMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button'; kebab.className = 'personality-kebab';
  kebab.setAttribute('aria-label', 'Item actions'); kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'personality-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
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
    document.querySelectorAll('.personality-menu').forEach(m => { m.hidden = true; });
    if (willOpen) personalityPlaceMenu(menu, kebab.getBoundingClientRect());
  });
  node.appendChild(kebab); node.appendChild(menu);
}

// ---- add folder / add personality ----
let personalityAddFolderAsSub = false;
function personalityAddFolder(asSub){
  personalityAddFolderAsSub = !!asSub;
  document.getElementById('personality-folder-title').textContent = asSub ? 'New subfolder' : 'New folder';
  const input = document.getElementById('personality-folder-input');
  input.value = '';
  document.getElementById('personality-folder-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('personality-folder-modal').hidden = false;
  input.focus();
}
function personalityCloseFolderModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('personality-folder-modal').hidden = true;
}
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
function personalityAddPersonality(){
  const input = document.getElementById('personality-new-input');
  input.value = '';
  document.getElementById('personality-new-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('personality-new-modal').hidden = false;
  input.focus();
}
function personalityCloseNewModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('personality-new-modal').hidden = true;
}
// A new personality starts with empty content and no history. It lands in
// the currently-selected folder.
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

// ---- drag & drop (one node at a time) ----
function personalityFolderInSubtree(candidateId, rootId){
  let cur = personalityFolderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? personalityFolderById(cur.parentId) : null;
  }
  return false;
}
function personalityMoveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && personalityFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = personalityFolderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  personalityFolders = personalityFolders.filter(x => x.id !== folderId);
  if (atStart){
    const i = personalityFolders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) personalityFolders.push(f); else personalityFolders.splice(i, 0, f);
  } else {
    let at = personalityFolders.length;
    for (let i = personalityFolders.length - 1; i >= 0; i--){
      if ((personalityFolders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    personalityFolders.splice(at, 0, f);
  }
  personalitySave();
}
function personalityMoveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = personalityFolderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && personalityFolderInSubtree(newParent, folderId)) return;  // no cycles
  const f = personalityFolderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  personalityFolders = personalityFolders.filter(x => x.id !== folderId);
  const ti = personalityFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) personalityFolders.push(f);
  else personalityFolders.splice(after ? ti + 1 : ti, 0, f);
  personalitySave();
}
function personalityMoveItem(itemUuid, targetFolderId, beforeItemUuid){
  targetFolderId = targetFolderId || null;
  const idx = personalityItems.findIndex(p => p.uuid === itemUuid);
  if (idx < 0) return;
  const item = personalityItems.splice(idx, 1)[0];
  item.folderId = targetFolderId;
  let insertAt = beforeItemUuid ? personalityItems.findIndex(p => p.uuid === beforeItemUuid) : -1;
  if (insertAt < 0){
    insertAt = personalityItems.length;
    for (let i = personalityItems.length - 1; i >= 0; i--){
      if ((personalityItems[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  personalityItems.splice(insertAt, 0, item);
  personalitySave();
}
function personalityMakeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    personalityDrag = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('personality-dragging');
    document.getElementById('personality-tree').classList.add('personality-dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    personalityDrag = null;
    document.getElementById('personality-tree').classList.remove('personality-dragging-on');
    personalityRenderTree();
  });
}
function personalityDropInto(folderId, atStart){
  if (!personalityDrag) return;
  const dragged = personalityDrag;
  if (dragged.type === 'item'){
    let beforeUuid = null;
    if (atStart){
      const first = personalityItems.find(p => (p.folderId || null) === (folderId || null) && p.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    personalityMoveItem(dragged.id, folderId, beforeUuid);
  } else {
    personalityMoveFolder(dragged.id, folderId, atStart);
  }
  if (folderId){ personalityExpanded[folderId] = true; personalityPersistExpand(); }
  personalityDrag = null;
  personalitySelectNode(dragged.type, dragged.id);  // select the moved node (also renders)
}
function personalityMakeFolderDrop(node, folderId){
  // Three zones on a folder: top third = reorder before, bottom third = after
  // (sibling), middle = nest into. Personality items always go "into".
  const zoneOf = e => {
    if (personalityDrag && personalityDrag.type === 'item') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!personalityDrag) return false;
    if (personalityDrag.type === 'item') return z === 'into';
    if (folderId === personalityDrag.id) return false;
    if (z === 'into') return !personalityFolderInSubtree(folderId, personalityDrag.id);
    const t = personalityFolderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && personalityFolderInSubtree(np, personalityDrag.id));
  };
  const clear = () => node.classList.remove('personality-drop-before', 'personality-drop-after', 'personality-drop-target');
  node.addEventListener('dragover', e => {
    if (!personalityDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('personality-drop-before', z === 'before');
    node.classList.toggle('personality-drop-after', z === 'after');
    node.classList.toggle('personality-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!personalityDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      personalityDropInto(folderId, false);
    } else {
      const draggedId = personalityDrag.id;
      personalityMoveFolderBeside(personalityDrag.id, folderId, z === 'after');
      personalityDrag = null;
      personalitySelectNode('folder', draggedId);
    }
  });
}
function personalityMakeItemDrop(node, itemUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!personalityDrag) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('personality-drop-after', after);
    node.classList.toggle('personality-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('personality-drop-before', 'personality-drop-after'));
  node.addEventListener('drop', e => {
    if (!personalityDrag) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('personality-drop-before', 'personality-drop-after');
    personalityDropOnItem(itemUuid, after);
  });
}
function personalityDropOnItem(targetUuid, after){
  if (!personalityDrag) return;
  if (personalityDrag.type === 'item' && personalityDrag.id === targetUuid) return;
  const dragged = personalityDrag;
  const target = personalityByUuid(targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'item'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = personalityItems.findIndex(p => p.uuid === targetUuid);
      beforeUuid = (ti + 1 < personalityItems.length) ? personalityItems[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    personalityMoveItem(dragged.id, targetFolder, beforeUuid);
  } else {
    personalityMoveFolder(dragged.id, targetFolder);
  }
  personalityDrag = null;
  personalitySelectNode(dragged.type, dragged.id);
}
function personalityWireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (personalityDrag){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (personalityDrag){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); personalityDropInto(null, atStart); }
  });
}
function personalityInitTreeDnD(){
  const root = document.getElementById('personality-tree-root');
  root.addEventListener('dragover', e => {
    if (personalityDrag){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (personalityDrag){ e.preventDefault(); personalityDropInto(null, false); }  // empty space → end of root
  });
  personalityWireRootDrop(document.getElementById('personality-root-drop'), false);
  document.getElementById('personality-all').addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    personalitySelectFolder(null);
  });
  // Dismiss any open kebab menu on an outside click or Escape.
  document.addEventListener('click', () => {
    document.querySelectorAll('.personality-menu').forEach(m => { m.hidden = true; });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.personality-menu').forEach(m => { m.hidden = true; });
  });
}

// ---- delete confirmation. The tree PUT can only update existing rows, so
// removal always goes through the dedicated DELETE endpoints below, never
// through the tree save. ----
let personalityDeleteOnConfirm = null;
let personalityDeleteRequireName = null;
function personalityOpenDeleteModal(opts){
  personalityDeleteOnConfirm = opts.onConfirm;
  personalityDeleteRequireName = opts.requireName || null;
  document.getElementById('personality-delete-title').textContent = opts.title || 'Delete';
  document.getElementById('personality-delete-msg').textContent = opts.message;
  const nameRow = document.getElementById('personality-delete-name-row');
  const input = document.getElementById('personality-delete-input');
  const btn = document.getElementById('personality-delete-confirm');
  if (personalityDeleteRequireName){
    nameRow.hidden = false;
    document.getElementById('personality-delete-name').textContent = personalityDeleteRequireName;
    input.value = ''; btn.disabled = true;
  } else {
    nameRow.hidden = true; btn.disabled = false;
  }
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('personality-delete-modal').hidden = false;
  if (personalityDeleteRequireName) input.focus();
}
function personalityCloseDeleteModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('personality-delete-modal').hidden = true;
  personalityDeleteOnConfirm = null;
  personalityDeleteRequireName = null;
}
function personalityDeleteUpdateState(){
  const input = document.getElementById('personality-delete-input');
  document.getElementById('personality-delete-confirm').disabled =
    personalityDeleteRequireName ? (input.value.trim() !== personalityDeleteRequireName) : false;
}
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
function personalityConfirmDeleteFolder(id){
  const f = personalityFolderById(id);
  if (!f) return;
  const sub = personalityFlattenTree(f.id);
  const folderCount = sub.filter(n => n.kind === 'folder').length;
  const itemCount = sub.filter(n => n.kind === 'item').length;
  if (folderCount + itemCount === 0){
    personalityOpenDeleteModal({
      title: 'Delete folder',
      message: 'Delete empty folder "' + f.name + '"?',
      onConfirm: () => personalityDeleteFolderById(f.id),
    });
    return;
  }
  const parts = [];
  if (folderCount) parts.push(folderCount + (folderCount === 1 ? ' subfolder' : ' subfolders'));
  if (itemCount) parts.push(itemCount + (itemCount === 1 ? ' personality' : ' personalities'));
  personalityOpenDeleteModal({
    title: 'Delete folder',
    message: 'Are you sure you want to delete folder "' + f.name + '" containing ' +
      parts.join(' and ') + '? The personalities inside are deleted too. This cannot be undone.',
    requireName: f.name,
    onConfirm: () => personalityDeleteFolderById(f.id),
  });
}
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
  const doomedFolder = personalityFolderById(id);  // captured before removal, for parent fallback below
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
    // Land on the deleted folder's parent, not the root, so the operator stays in context.
    if (doomedFolders.has(personalitySelectedFolder)) {
      personalitySelectedFolder = (doomedFolder && doomedFolder.parentId) || null;
    }
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
document.getElementById('personality-delete-input').addEventListener('input', personalityDeleteUpdateState);
document.getElementById('personality-delete-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('personality-delete-confirm').disabled){
    e.preventDefault();
    document.getElementById('personality-delete-confirm').click();
  }
});
document.getElementById('personality-delete-confirm').addEventListener('click', () => {
  const fn = personalityDeleteOnConfirm;
  personalityCloseDeleteModal();
  if (fn) fn();
});

// ---- persistence ----
async function personalityLoadTree(){
  try {
    const r = await fetch('/personality/api/tree');
    const data = await r.json();
    personalityFolders = (data && data.folders) || [];
    personalityItems = (data && data.personalities) || [];
    personalityTreeVersion = (data && data.version) || null;
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    personalityFolders = []; personalityItems = []; personalityTreeVersion = null;
  }
}
let personalityToastTimer = null;
function personalityToastMsg(text){
  const el = document.getElementById('personality-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(personalityToastTimer);
  personalityToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let personalitySaveTimer = null;
let personalityTreeVersion = null;    // token from hydrate; PUTs echo it (stale → 409)
let personalitySaveInFlight = false;
let personalitySaveQueued = false;
function personalitySave(){
  clearTimeout(personalitySaveTimer);
  personalitySaveTimer = setTimeout(personalitySavePush, 250);  // coalesce bursts into one PUT
}
// After a re-hydrate, the fresh data may no longer contain the selected
// folder/personality (e.g. the rejected edit was the move that put it there).
// Clear whichever selection no longer resolves so render doesn't point at a
// row that isn't in the tree anymore.
function personalityReconcileSelectionAfterReload(){
  if (personalitySelectedItem && !personalityByUuid(personalitySelectedItem)) {
    personalitySelectedItem = null;
  }
  if (personalitySelectedFolder && !personalityFolderById(personalitySelectedFolder)) {
    personalitySelectedFolder = null;
  }
}
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
      // Another tab/editor changed the tree; their version wins — re-hydrate
      // and repaint so the screen matches what the server just accepted.
      await personalityLoadTree();
      personalityReconcileSelectionAfterReload();
      personalityRenderTree();
      personalityRender();
      personalityToastMsg('tree changed elsewhere — reloaded');
      return;
    }
    if (!resp.ok) {
      // A 400 here means our payload disagreed with the server about which
      // rows exist — re-hydrate rather than retry the same bad shape, and
      // repaint so the rejected edit doesn't linger on screen.
      await personalityLoadTree();
      personalityReconcileSelectionAfterReload();
      personalityRenderTree();
      personalityRender();
      personalityToastMsg(data.error || 'save failed — reloaded');
      return;
    }
    personalityTreeVersion = data.version;
  } catch (e) {
    // Network error: we can't tell whether the server applied the change, so
    // re-hydrate and repaint rather than leave the client's guess on screen.
    await personalityLoadTree();
    personalityReconcileSelectionAfterReload();
    personalityRenderTree();
    personalityRender();
    personalityToastMsg('save failed — reloaded');
  } finally {
    personalitySaveInFlight = false;
    if (personalitySaveQueued) { personalitySaveQueued = false; personalitySave(); }
  }
}

// ---- dirty-guarded dismissal (clicking backdrop / Esc) ----
function personalityOpenModalDirty(){
  if (!document.getElementById('personality-folder-modal').hidden){
    return document.getElementById('personality-folder-input').value.trim() !== '';
  }
  if (!document.getElementById('personality-new-modal').hidden){
    return document.getElementById('personality-new-input').value.trim() !== '';
  }
  if (!document.getElementById('personality-desc-modal').hidden){
    return document.getElementById('personality-desc-input').value !== personalityDescOrig;
  }
  // Rename: dirty once the typed name differs from the stored one — only the
  // explicit Rename/Cancel buttons close it then.
  if (!document.getElementById('personality-rename-modal').hidden){
    return document.getElementById('personality-rename-input').value
      !== ((personalityRenameState && personalityRenameState.original) || '');
  }
  // Delete: dirty only when the type-to-confirm box is in use and non-empty;
  // a plain yes/no delete is never dirty.
  if (!document.getElementById('personality-delete-modal').hidden){
    return personalityDeleteRequireName
      ? document.getElementById('personality-delete-input').value.trim() !== '' : false;
  }
  // Restore confirm: a plain yes/no like a no-name-required delete — never dirty.
  if (!document.getElementById('personality-restore-modal').hidden){
    return false;
  }
  // Content edit mode behaves like an open modal: dirty once the text differs
  // from the snapshot — then only Save / Cancel end it.
  if (personalityEditMode){
    return personalityEditorValue() !== personalityEditOriginal;
  }
  return false;
}
function personalityCloseOpenModal(){
  if (!document.getElementById('personality-folder-modal').hidden){ personalityCloseFolderModal(); return; }
  if (!document.getElementById('personality-new-modal').hidden){ personalityCloseNewModal(); return; }
  if (!document.getElementById('personality-desc-modal').hidden){ personalityCloseDescModal(); return; }
  if (!document.getElementById('personality-rename-modal').hidden){ personalityCloseRenameModal(); return; }
  if (!document.getElementById('personality-delete-modal').hidden){ personalityCloseDeleteModal(); return; }
  if (!document.getElementById('personality-restore-modal').hidden){ personalityCloseRestoreModal(); return; }
  if (personalityEditMode){ personalityCancelEdit(); return; }
}
function personalityDismissIfClean(){ if (!personalityOpenModalDirty()) personalityCloseOpenModal(); }

// ---- history view (append-only revisions; restore appends, never rewinds) ----
let personalityHistoryOpen = false;
let personalityHistoryRows = [];   // [{uuid, created_at, bytes, lines, preview, current}]
let personalityRestoreUuid = null; // revision awaiting confirmation

function personalityHistoryVisible(show){
  document.getElementById('personality-history').hidden = !show;
  document.getElementById('personality-editor')
          .querySelector('.CodeMirror').style.display = show ? 'none' : '';
  if (!show) personalityCM.refresh();  // re-measure: it was display:none while History was open
}
// Shared by a selection change and by entering edit mode: either way the
// editor pane takes over and History has nothing left to show. Closing here
// is unconditional (not a toggle), so it's a no-op when History is already shut.
function personalityCloseHistoryView(){
  if (!personalityHistoryOpen) return;
  personalityHistoryOpen = false;
  document.getElementById('personality-history-btn').textContent = 'History';
  personalityHistoryVisible(false);
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

// ---- wiring + initial paint ----
personalityInitTreeDnD();
personalityInitEditor();
document.getElementById('personality-folder-input').addEventListener('input', () => {
  document.getElementById('personality-folder-create').disabled =
    document.getElementById('personality-folder-input').value.trim() === '';
});
document.getElementById('personality-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('personality-folder-create').disabled){
    e.preventDefault(); personalityAddFolderConfirm();
  }
});
document.getElementById('personality-new-input').addEventListener('input', () => {
  document.getElementById('personality-new-create').disabled =
    document.getElementById('personality-new-input').value.trim() === '';
});
document.getElementById('personality-new-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('personality-new-create').disabled){
    e.preventDefault(); personalityAddPersonalityConfirm();
  }
});
document.getElementById('personality-rename-input').addEventListener('input', personalitySyncRenameConfirm);
document.getElementById('personality-rename-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('personality-rename-confirm').disabled){
    e.preventDefault(); personalityConfirmRenameModal();
  }
});
document.getElementById('ui-modal-backdrop').addEventListener('click', personalityDismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') personalityDismissIfClean(); });
personalityLoadTree().then(() => {
  // Deep link: ?id=<uuid> selects that folder or personality on load.
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && personalityFolderById(wantId)){
    personalitySelectFolder(wantId);
  } else if (wantId && personalityByUuid(wantId)){
    personalitySelectItem(wantId);
  } else {
    personalityRenderTree();
    personalityRender();
  }
});
