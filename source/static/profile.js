// /profile page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/profile_views.py; this file is served at /static/profile.js with an
// mtime cache-buster. Tree state hydrates from GET /profile/api/tree and
// structural edits save via debounced PUTs (projected to structural keys with
// the read-only built-ins left out); creation and deletion are their own
// immediate requests (docs/ui-tree-persistence.md). Profile data autosaves
// through a separate per-profile PUT. Mirrors static/prompt.js.

// ---- helpers ----
function profileEscapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function profileShortDate(iso){
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toISOString().slice(0, 16).replace('T', ' ');
}

// ---- state ----
let profileFolders = [];          // {id, name, description, parentId, builtin?, ...}
let profileItems = [];            // {uuid, name, folderId, summary, builtin?, ...}
let profileSelectedFolder = null; // folder id, or null for "All profiles" / root
let profileSelectedItem = null;   // profile uuid when a profile is selected
let profileExpanded = {};         // folder id -> false when collapsed (default expanded)
let profileDrag = null;           // {type:'folder'|'item', id} while a node is dragged
const PROFILE_EXPAND_KEY = 'profile.expandedFolders';
try { profileExpanded = JSON.parse(localStorage.getItem(PROFILE_EXPAND_KEY)) || {}; }
catch (e) { profileExpanded = {}; }
function profilePersistExpand(){
  try { localStorage.setItem(PROFILE_EXPAND_KEY, JSON.stringify(profileExpanded)); }
  catch (e) { /* private mode etc. — expand state just won't survive reload */ }
}

// ---- inlined Lucide icons (https://lucide.dev), self-contained ----
const PROFILE_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const PROFILE_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

// ---- lookups ----
function profileFolderById(id){ return profileFolders.find(f => f.id === id) || null; }
function profileByUuid(uuid){ return profileItems.find(p => p.uuid === uuid) || null; }
function profileChildFolders(parentId){ return profileFolders.filter(f => (f.parentId || null) === (parentId || null)); }
function profileItemsInFolder(id){
  const target = id || null;
  if (target === null){
    // Root level also surfaces a profile whose folderId names a folder that
    // isn't in profileFolders (e.g. the folder was deleted via the admin,
    // orphaning the row). The server rejects it in every tree save, so if it
    // stayed invisible here the operator could never reach it to move or
    // delete it and every structural edit would 400 forever.
    return profileItems.filter(p => {
      const fid = p.folderId || null;
      return fid === null || !profileFolderById(fid);
    });
  }
  return profileItems.filter(p => (p.folderId || null) === target);
}
function profileIsExpanded(id){ return profileExpanded[id] !== false; }
// Optimistically stamp a node as just-modified; the server sets the
// authoritative updated_at on save and a reload reconciles.
function profileTouch(node){ if (node) node.updated_at = new Date().toISOString(); }

// ---- selection ----
function profileCurrentSelectionId(){
  if (profileSelectedItem) return profileSelectedItem;
  if (profileSelectedFolder) return profileSelectedFolder;
  return null;
}
function profileSyncUrl(){
  // Reflect the selection in ?id= so the URL is a shareable deep link.
  const url = new URL(window.location);
  const id = profileCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function profileSelectFolder(id){
  profileSelectedFolder = id;
  profileSelectedItem = null;
  profileRenderTree();
  profileRender();
}
function profileSelectItem(uuid){
  const p = profileByUuid(uuid);
  profileSelectedItem = uuid;
  profileSelectedFolder = p ? (p.folderId || null) : null;
  profileRenderTree();
  profileRender();
}
function profileSelectNode(type, id){
  if (type === 'item') profileSelectItem(id); else profileSelectFolder(id);
}
function profileFolderClick(id){
  // First click selects; clicking the already-selected folder toggles expand.
  const wasSelected = (profileSelectedFolder === id) && !profileSelectedItem;
  if (wasSelected){
    profileExpanded[id] = !profileIsExpanded(id);
    profilePersistExpand();
    profileRenderTree();
    profileRender();
  } else {
    profileSelectFolder(id);
  }
}

// ---- right-pane render ----
function profileRender(){
  profileRenderRename();
  profileRenderFolderDesc();
  profileRenderContents();
  profileRenderForm();
  profileSyncUrl();
}
// Depth-first list of everything under parentId (null = whole tree), in the
// same order as the left tree, each row tagged with its nesting `depth` — like
// /cron's cronFlattenTree (docs/ui-left-panel-tree.md §7). At the root the
// user's own content comes first and the built-in Templates folder last,
// matching the tree render.
function profileFlattenTree(parentId){
  parentId = parentId || null;
  const out = [];
  const walk = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    profileChildFolders(f.id).forEach(c => walk(c, depth + 1));
    profileItemsInFolder(f.id).forEach(p => out.push({kind: 'item', node: p, depth: depth + 1}));
  };
  if (parentId === null){
    profileChildFolders(null).filter(f => !f.builtin).forEach(f => walk(f, 0));
    profileItemsInFolder(null).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
    profileChildFolders(null).filter(f => f.builtin).forEach(f => walk(f, 0));
  } else {
    profileChildFolders(parentId).forEach(f => walk(f, 0));
    profileItemsInFolder(parentId).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
  }
  return out;
}
function profileRenderContents(){
  const wrap = document.getElementById('profile-table-wrap');
  const formView = !!profileSelectedItem;
  wrap.hidden = formView;
  if (formView) return;
  const tb = document.getElementById('profile-rows');
  tb.innerHTML = '';
  // The selected folder's whole subtree (or the entire tree at the root),
  // depth-first and depth-indented, mirroring the left tree.
  const nodes = profileFlattenTree(profileSelectedFolder);
  if (!nodes.length){
    tb.innerHTML = '<tr><td colspan="6"><i>' +
      (profileSelectedFolder === null ? 'no profiles yet' : 'empty folder') + '</i></td></tr>';
    return;
  }
  nodes.forEach(item => {
    const pad = 9 + item.depth * 20;  // indent the name cell by nesting depth, like the tree
    const tr = document.createElement('tr');
    if (item.kind === 'folder'){
      // Folder rows carry the tree's folder icon in the Name cell; that (plus
      // the empty person/locale cells) is what marks them as folders.
      const f = item.node;
      tr.innerHTML =
        '<td class="profile-name-cell" style="padding-left:' + pad + 'px">' +
        '<span class="profile-ficon">' + PROFILE_ICON_FOLDER + '</span>' + profileEscapeHtml(f.name) + '</td>' +
        '<td></td><td></td><td></td><td></td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); profileSelectFolder(f.id); });
    } else {
      const p = item.node;
      const s = p.summary || {};
      tr.innerHTML =
        '<td class="profile-name-cell" style="padding-left:' + pad + 'px">' + profileEscapeHtml(p.name) + '</td>' +
        '<td>' + profileEscapeHtml(s.full_name) + '</td>' +
        '<td>' + profileEscapeHtml(s.language) + '</td>' +
        '<td>' + profileEscapeHtml(s.time_format) + '</td>' +
        '<td>' + profileEscapeHtml(s.country) + '</td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); profileSelectItem(p.uuid); });
    }
    tb.appendChild(tr);
  });
}
// The selected folder's / profile's name, shown as a click-to-rename control
// (docs/ui-modal-rename.md). Built-ins are unrenamable, so they get a plain
// heading with no rename affordance.
function profileRenderRename(){
  const el = document.getElementById('profile-node-rename');
  el.innerHTML = '';
  let node = null, type = null;
  if (profileSelectedItem){ node = profileByUuid(profileSelectedItem); type = 'item'; }
  else if (profileSelectedFolder !== null){ node = profileFolderById(profileSelectedFolder); type = 'folder'; }
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  if (node.builtin){
    const span = document.createElement('span');
    span.className = 'profile-heading';
    span.textContent = node.name;
    el.appendChild(span);
    return;
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'profile-rename-display';
  btn.textContent = node.name;
  btn.title = 'Click to rename';
  btn.addEventListener('click', () => profileOpenRenameModal(type, node, node.name));
  el.appendChild(btn);
}

// ---- rename modal ----
let profileRenameState = null;   // {type: 'item'|'folder', id, original}
function profileOpenRenameModal(type, node, seed){
  profileRenameState = {type: type, id: type === 'item' ? node.uuid : node.id,
                        original: node.name};
  document.getElementById('profile-rename-title').textContent =
    type === 'item' ? 'Rename profile' : 'Rename folder';
  const input = document.getElementById('profile-rename-input');
  input.value = seed;
  profileSyncRenameConfirm();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('profile-rename-modal').hidden = false;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function profileCloseRenameModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-rename-modal').hidden = true;
  profileRenameState = null;
}
// Rename is enabled only for a non-empty name that actually differs.
function profileSyncRenameConfirm(){
  const v = document.getElementById('profile-rename-input').value.trim();
  document.getElementById('profile-rename-confirm').disabled =
    v === '' || !profileRenameState || v === profileRenameState.original;
}
function profileConfirmRenameModal(){
  if (!profileRenameState) return;
  const v = document.getElementById('profile-rename-input').value.trim();
  if (!v || v === profileRenameState.original) return;
  const node = profileRenameState.type === 'item'
    ? profileByUuid(profileRenameState.id) : profileFolderById(profileRenameState.id);
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-rename-modal').hidden = true;
  profileRenameState = null;
  if (!node) return;
  node.name = v;
  profileTouch(node);
  profileRenderTree();
  profileRender();
  profileSave();
  profileToastMsg('Renamed to “' + v + '”');
}
// Description: folders only (profiles have no description field). Built-in
// folder shows its shipped description read-only.
function profileFillDescValue(el, text){
  if (text){ el.textContent = text; el.classList.remove('muted'); }
  else { el.textContent = '(none)'; el.classList.add('muted'); }
}
function profileRenderFolderDesc(){
  const el = document.getElementById('profile-folder-desc');
  el.innerHTML = '';
  const node = (!profileSelectedItem && profileSelectedFolder !== null)
    ? profileFolderById(profileSelectedFolder) : null;
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const lbl = document.createElement('span'); lbl.className = 'muted'; lbl.textContent = 'Description:';
  const val = document.createElement('span'); profileFillDescValue(val, node.description);
  el.appendChild(lbl); el.appendChild(val);
  if (!node.builtin){
    const btn = document.createElement('button'); btn.textContent = 'Edit description';
    btn.addEventListener('click', profileEditDescription);
    el.appendChild(btn);
  }
}
let profileDescOrig = '';
function profileEditDescription(){
  const node = profileSelectedFolder !== null ? profileFolderById(profileSelectedFolder) : null;
  if (!node || node.builtin) return;
  profileDescOrig = node.description || '';
  document.getElementById('profile-desc-input').value = profileDescOrig;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('profile-desc-modal').hidden = false;
  document.getElementById('profile-desc-input').focus();
}
function profileCloseDescModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-desc-modal').hidden = true;
}
function profileSaveDescription(){
  const node = profileSelectedFolder !== null ? profileFolderById(profileSelectedFolder) : null;
  if (node && !node.builtin){ node.description = document.getElementById('profile-desc-input').value; profileTouch(node); }
  profileCloseDescModal();
  profileRender();
  profileSave();
}

// ---- left tree ----
function profileRenderTree(){
  document.getElementById('profile-all').className =
    'profile-node' + ((profileSelectedFolder === null && !profileSelectedItem) ? ' sel' : '');
  const root = document.getElementById('profile-tree-root');
  root.innerHTML = '';
  // User content first; the virtual Templates folder renders after it.
  profileChildFolders(null).filter(f => !f.builtin).forEach(f => root.appendChild(profileFolderLi(f)));
  profileItemsInFolder(null).forEach(p => {
    const li = document.createElement('li'); li.appendChild(profileItemNode(p)); root.appendChild(li);
  });
  profileChildFolders(null).filter(f => f.builtin).forEach(f => root.appendChild(profileFolderLi(f)));
}
function profileMakeBuiltinTag(){
  const tag = document.createElement('span');
  tag.className = 'profile-builtin-tag';
  tag.textContent = 'built-in';
  return tag;
}
function profileFolderLi(f){
  const li = document.createElement('li');
  const kids = profileChildFolders(f.id);
  const leaves = profileItemsInFolder(f.id);
  const hasKids = (kids.length + leaves.length) > 0;
  const expanded = profileIsExpanded(f.id);
  // A real anchor so CMD/Ctrl/middle click opens the folder view in a new
  // tab; a plain click is intercepted below and selects/toggles in-page.
  const node = document.createElement('a');
  const selected = (profileSelectedFolder === f.id && !profileSelectedItem);
  node.className = 'profile-node' + (selected ? ' sel' : '');
  node.href = '/profile?id=' + encodeURIComponent(f.id);
  const icon = document.createElement('span');
  icon.className = 'profile-ficon';
  icon.innerHTML = (expanded && hasKids) ? PROFILE_ICON_FOLDER_OPEN : PROFILE_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'profile-folder-label';
  label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  if (f.builtin) node.appendChild(profileMakeBuiltinTag());
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    profileFolderClick(f.id);
  });
  if (!f.builtin){
    profileMakeDraggable(node, 'folder', f.id);
  } else {
    node.draggable = false;  // anchors are natively draggable — switch that off too
  }
  profileMakeFolderDrop(node, f.id);
  // Kebab is rendered on every row but only shown (via CSS) on the selected
  // one, so row heights stay consistent — matches /cron. No Rename item: a
  // selected folder's pane heading is already the click-to-rename control.
  // The built-in Templates folder has no actions, so its kebab stays
  // permanently hidden.
  profileMakeKebab(node, f.builtin ? {} : {
    onDelete: () => profileConfirmDeleteFolder(f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(profileFolderLi(c)));
    leaves.forEach(p => { const pli = document.createElement('li'); pli.appendChild(profileItemNode(p)); ul.appendChild(pli); });
    li.appendChild(ul);
  }
  return li;
}
function profileItemNode(p){
  // A real anchor so CMD/Ctrl/middle click opens the profile in a new tab; a
  // plain click is intercepted below and selects the profile in-page instead.
  const n = document.createElement('a');
  const selected = (profileSelectedItem === p.uuid);
  n.className = 'profile-item-node' + (selected ? ' sel' : '');
  n.href = '/profile?id=' + encodeURIComponent(p.uuid);
  n.title = p.name;
  // No leaf icon in the tree — every leaf here is a profile, so an icon is
  // noise. Built-in leaves carry no tag either: the Templates folder above
  // them already says built-in once, tagging all 20 rows repeats it.
  const label = document.createElement('span'); label.className = 'profile-item-label'; label.textContent = p.name;
  n.appendChild(label);
  n.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    profileSelectItem(p.uuid);
  });
  if (!p.builtin){
    profileMakeDraggable(n, 'item', p.uuid);
    profileMakeItemDrop(n, p.uuid);
  } else {
    n.draggable = false;  // anchors are natively draggable — switch that off too
  }
  // Kebab on every row, shown (via CSS) only on the selected one — matches
  // /cron. No Rename item: a selected profile's pane heading is already the
  // click-to-rename control. Built-ins are read-only: Duplicate only.
  // Export is offered on built-ins too: a template's blocks are exactly what
  // you want to read before adopting it.
  profileMakeKebab(n, p.builtin ? {
    onExport: () => profileOpenExportModal(p.uuid),
    onDuplicate: () => profileDuplicateUuid(p.uuid),
  } : {
    onExport: () => profileOpenExportModal(p.uuid),
    onDuplicate: () => profileDuplicateUuid(p.uuid),
    onDelete: () => profileConfirmDeleteItem(p.uuid),
  });
  return n;
}
// Position a fixed kebab menu near its anchor, clamped inside the viewport:
// below the anchor when it fits, flipped above when it would overflow the
// bottom edge (nodes at the bottom of a long tree). Unhides the menu first so
// its offsetWidth/Height are measurable.
function profilePlaceMenu(menu, anchorRect){
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
// 3-dot overflow menu. opts: { onDuplicate?, onDelete? } — renaming lives on
// the pane heading (click-to-rename), not here. With no actions at all the
// kebab element is still rendered (constant row height) but stays
// permanently invisible.
function profileMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button'; kebab.className = 'profile-kebab';
  kebab.setAttribute('aria-label', 'Item actions'); kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'profile-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
  const items = [];
  if (opts.onExport) items.push(['Export', opts.onExport, '']);
  if (opts.onDuplicate) items.push(['Duplicate', opts.onDuplicate, '']);
  if (opts.onDelete) items.push(['Delete', opts.onDelete, 'danger']);
  if (!items.length) kebab.classList.add('profile-kebab-none');
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
    document.querySelectorAll('.profile-menu').forEach(m => { m.hidden = true; });
    if (willOpen) profilePlaceMenu(menu, kebab.getBoundingClientRect());
  });
  node.appendChild(kebab); node.appendChild(menu);
}

// ---- export ----
// Reads the serialization from the server rather than assembling it here: the
// point of the dialog is to show what the assistant's own block builders
// produce, and a JS reimplementation would be free to disagree with them.
let profileExportUuid = null;
function profileOpenExportModal(uuid){
  profileExportUuid = uuid;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('profile-export-modal').hidden = false;
  profileExportRefresh();
}
function profileCloseExportModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-export-modal').hidden = true;
  profileExportUuid = null;
}
function profileExportSections(){
  return Array.from(document.querySelectorAll('.profile-export-section'))
    .filter(cb => cb.checked).map(cb => cb.value);
}
// UTF-8 bytes for every format side by side, the current one highlighted —
// a format's overhead only means something next to the alternatives. Built
// from DOM nodes rather than innerHTML: the numbers are ours, but a size line
// is not worth a second HTML-injection surface.
function profileExportRenderSizes(sizes, active){
  const el = document.getElementById('profile-export-size');
  el.textContent = '';
  if (!sizes) return;
  Object.keys(sizes).forEach((fmt, i) => {
    if (i) el.appendChild(document.createTextNode('  ·  '));
    const span = document.createElement('span');
    if (fmt === active) span.className = 'sel';
    span.textContent = fmt.toUpperCase() + ' ' + sizes[fmt].toLocaleString() + ' B';
    el.appendChild(span);
  });
}
async function profileExportRefresh(){
  if (!profileExportUuid) return;
  const out = document.getElementById('profile-export-output');
  const sections = profileExportSections();
  if (!sections.length){
    out.value = '';
    profileExportRenderSizes(null);
    return;
  }
  const fmt = document.getElementById('profile-export-format').value;
  const url = '/profile/api/profiles/' + encodeURIComponent(profileExportUuid)
    + '/export?format=' + encodeURIComponent(fmt)
    + '&sections=' + encodeURIComponent(sections.join(','));
  const requested = profileExportUuid;
  let body;
  try {
    const r = await fetch(url);
    body = await r.json();
  } catch (_) {
    out.value = '(export failed — could not reach the server)';
    profileExportRenderSizes(null);
    return;
  }
  if (requested !== profileExportUuid) return;   // closed or switched while loading
  out.value = body && body.ok ? body.text
    : '(export failed: ' + ((body && body.error) || 'unknown error') + ')';
  profileExportRenderSizes(body && body.ok ? body.sizes : null, fmt);
}
async function profileExportCopy(){
  const text = document.getElementById('profile-export-output').value;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    profileToastMsg('Copied');
  } catch (_) {
    profileToastMsg('Copy failed');
  }
}

// ---- add folder / add profile ----
let profileAddFolderAsSub = false;
function profileAddFolder(asSub){
  profileAddFolderAsSub = !!asSub;
  document.getElementById('profile-folder-title').textContent = asSub ? 'New subfolder' : 'New folder';
  const input = document.getElementById('profile-folder-input');
  input.value = '';
  document.getElementById('profile-folder-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('profile-folder-modal').hidden = false;
  input.focus();
}
function profileCloseFolderModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-folder-modal').hidden = true;
}
async function profileAddFolderConfirm(){
  const name = document.getElementById('profile-folder-input').value.trim();
  if (!name) return;
  let parentId = profileAddFolderAsSub ? profileSelectedFolder : null;
  const parent = parentId ? profileFolderById(parentId) : null;
  if (parent && parent.builtin) parentId = null;  // the Templates folder can't hold user rows
  try {
    await profileFlushPendingSave();
    const r = await fetch('/profile/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, parentId: parentId}),
    });
    const data = await r.json();
    if (!r.ok){ profileToastMsg(data.error || 'could not create folder'); return; }
    profileFolders.push(data.folder);
    profileTreeVersion = data.version;
    profileCloseFolderModal();
    if (parentId){ profileExpanded[parentId] = true; profilePersistExpand(); }
    profileSelectFolder(data.folder.id);
  } catch (e) {
    profileToastMsg('could not create folder');
  }
}
function profileAddProfile(){
  const input = document.getElementById('profile-new-input');
  input.value = '';
  document.getElementById('profile-new-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('profile-new-modal').hidden = false;
  input.focus();
}
function profileCloseNewModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-new-modal').hidden = true;
}
// A new profile starts with empty data. It lands in the currently-selected
// folder — or at the root when the selection is the read-only Templates
// folder. Created by its own endpoint — the tree save can never make a row
// (docs/ui-tree-persistence.md).
async function profileAddProfileConfirm(){
  const name = document.getElementById('profile-new-input').value.trim();
  if (!name) return;
  let folderId = profileSelectedFolder;
  const folder = folderId ? profileFolderById(folderId) : null;
  if (folder && folder.builtin) folderId = null;
  try {
    await profileFlushPendingSave();
    const r = await fetch('/profile/api/profiles', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, folderId: folderId}),
    });
    const data = await r.json();
    if (!r.ok){ profileToastMsg(data.error || 'could not create'); return; }
    profileItems.push(data.profile);
    profileTreeVersion = data.version;
    profileCloseNewModal();
    profileSelectItem(data.profile.uuid);
  } catch (e) {
    profileToastMsg('could not create');
  }
}

// ---- drag & drop (one node at a time; built-ins are not draggable and the
// Templates folder accepts no drops) ----
function profileFolderInSubtree(candidateId, rootId){
  let cur = profileFolderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? profileFolderById(cur.parentId) : null;
  }
  return false;
}
function profileMoveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && profileFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = profileFolderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  profileFolders = profileFolders.filter(x => x.id !== folderId);
  if (atStart){
    const i = profileFolders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) profileFolders.push(f); else profileFolders.splice(i, 0, f);
  } else {
    let at = profileFolders.length;
    for (let i = profileFolders.length - 1; i >= 0; i--){
      if ((profileFolders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    profileFolders.splice(at, 0, f);
  }
  profileSave();
}
function profileMoveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = profileFolderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && profileFolderInSubtree(newParent, folderId)) return;  // no cycles
  const f = profileFolderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  profileFolders = profileFolders.filter(x => x.id !== folderId);
  const ti = profileFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) profileFolders.push(f);
  else profileFolders.splice(after ? ti + 1 : ti, 0, f);
  profileSave();
}
function profileMoveItem(itemUuid, targetFolderId, beforeItemUuid){
  targetFolderId = targetFolderId || null;
  const idx = profileItems.findIndex(p => p.uuid === itemUuid);
  if (idx < 0) return;
  const item = profileItems.splice(idx, 1)[0];
  item.folderId = targetFolderId;
  let insertAt = beforeItemUuid ? profileItems.findIndex(p => p.uuid === beforeItemUuid) : -1;
  if (insertAt < 0){
    insertAt = profileItems.length;
    for (let i = profileItems.length - 1; i >= 0; i--){
      if ((profileItems[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  profileItems.splice(insertAt, 0, item);
  profileSave();
}
function profileMakeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    profileDrag = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('profile-dragging');
    document.getElementById('profile-tree').classList.add('profile-dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    profileDrag = null;
    document.getElementById('profile-tree').classList.remove('profile-dragging-on');
    profileRenderTree();
  });
}
function profileDropInto(folderId, atStart){
  if (!profileDrag) return;
  const dragged = profileDrag;
  if (dragged.type === 'item'){
    let beforeUuid = null;
    if (atStart){
      const first = profileItems.find(p => (p.folderId || null) === (folderId || null) && p.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    profileMoveItem(dragged.id, folderId, beforeUuid);
  } else {
    profileMoveFolder(dragged.id, folderId, atStart);
  }
  if (folderId){ profileExpanded[folderId] = true; profilePersistExpand(); }
  profileDrag = null;
  profileSelectNode(dragged.type, dragged.id);  // select the moved node (also renders)
}
function profileMakeFolderDrop(node, folderId){
  // Three zones on a folder: top third = reorder before, bottom third = after
  // (sibling), middle = nest into. Profile items always go "into".
  const zoneOf = e => {
    if (profileDrag && profileDrag.type === 'item') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!profileDrag) return false;
    const target = profileFolderById(folderId);
    if (target && target.builtin) return false;  // the Templates folder accepts no drops
    if (profileDrag.type === 'item') return z === 'into';
    if (folderId === profileDrag.id) return false;
    if (z === 'into') return !profileFolderInSubtree(folderId, profileDrag.id);
    const t = profileFolderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && profileFolderInSubtree(np, profileDrag.id));
  };
  const clear = () => node.classList.remove('profile-drop-before', 'profile-drop-after', 'profile-drop-target');
  node.addEventListener('dragover', e => {
    if (!profileDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('profile-drop-before', z === 'before');
    node.classList.toggle('profile-drop-after', z === 'after');
    node.classList.toggle('profile-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!profileDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      profileDropInto(folderId, false);
    } else {
      const draggedId = profileDrag.id;
      profileMoveFolderBeside(profileDrag.id, folderId, z === 'after');
      profileDrag = null;
      profileSelectNode('folder', draggedId);
    }
  });
}
function profileMakeItemDrop(node, itemUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!profileDrag) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('profile-drop-after', after);
    node.classList.toggle('profile-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('profile-drop-before', 'profile-drop-after'));
  node.addEventListener('drop', e => {
    if (!profileDrag) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('profile-drop-before', 'profile-drop-after');
    profileDropOnItem(itemUuid, after);
  });
}
function profileDropOnItem(targetUuid, after){
  if (!profileDrag) return;
  if (profileDrag.type === 'item' && profileDrag.id === targetUuid) return;
  const dragged = profileDrag;
  const target = profileByUuid(targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'item'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = profileItems.findIndex(p => p.uuid === targetUuid);
      beforeUuid = (ti + 1 < profileItems.length) ? profileItems[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    profileMoveItem(dragged.id, targetFolder, beforeUuid);
  } else {
    profileMoveFolder(dragged.id, targetFolder);
  }
  profileDrag = null;
  profileSelectNode(dragged.type, dragged.id);
}
function profileWireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (profileDrag){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (profileDrag){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); profileDropInto(null, atStart); }
  });
}
function profileInitTreeDnD(){
  const root = document.getElementById('profile-tree-root');
  root.addEventListener('dragover', e => {
    if (profileDrag){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (profileDrag){ e.preventDefault(); profileDropInto(null, false); }  // empty space → end of root
  });
  profileWireRootDrop(document.getElementById('profile-root-drop'), false);
  document.getElementById('profile-all').addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    profileSelectFolder(null);
  });
  // Dismiss any open kebab menu on an outside click or Escape.
  document.addEventListener('click', () => {
    document.querySelectorAll('.profile-menu').forEach(m => { m.hidden = true; });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.profile-menu').forEach(m => { m.hidden = true; });
  });
}

// ---- delete. The tree PUT can only update existing rows, so removal always
// goes through the dedicated DELETE endpoints below, never through the tree
// save. Built-ins are virtual and have no row to delete. ----
let profileDeleteOnConfirm = null;
let profileDeleteRequireName = null;
function profileOpenDeleteModal(opts){
  profileDeleteOnConfirm = opts.onConfirm;
  profileDeleteRequireName = opts.requireName || null;
  document.getElementById('profile-delete-title').textContent = opts.title || 'Delete';
  document.getElementById('profile-delete-msg').textContent = opts.message;
  const nameRow = document.getElementById('profile-delete-name-row');
  const input = document.getElementById('profile-delete-input');
  const btn = document.getElementById('profile-delete-confirm');
  if (profileDeleteRequireName){
    nameRow.hidden = false;
    document.getElementById('profile-delete-name').textContent = profileDeleteRequireName;
    input.value = ''; btn.disabled = true;
  } else {
    nameRow.hidden = true; btn.disabled = false;
  }
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('profile-delete-modal').hidden = false;
  if (profileDeleteRequireName) input.focus();
}
function profileCloseDeleteModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('profile-delete-modal').hidden = true;
  profileDeleteOnConfirm = null;
  profileDeleteRequireName = null;
}
function profileDeleteUpdateState(){
  const input = document.getElementById('profile-delete-input');
  document.getElementById('profile-delete-confirm').disabled =
    profileDeleteRequireName ? (input.value.trim() !== profileDeleteRequireName) : false;
}
function profileConfirmDeleteItem(uuid){
  const p = profileByUuid(uuid);
  if (!p || p.builtin) return;
  profileOpenDeleteModal({
    title: 'Delete profile',
    message: 'Delete the profile "' + p.name + '"? Its person data is deleted too. This cannot be undone.',
    requireName: p.name,
    onConfirm: () => profileDeleteItem(uuid),
  });
}
function profileConfirmDeleteFolder(id){
  const f = profileFolderById(id);
  if (!f || f.builtin) return;
  const sub = profileFlattenTree(f.id);
  const folderCount = sub.filter(n => n.kind === 'folder').length;
  const itemCount = sub.filter(n => n.kind === 'item').length;
  if (folderCount + itemCount === 0){
    profileOpenDeleteModal({
      title: 'Delete folder',
      message: 'Delete empty folder "' + f.name + '"?',
      onConfirm: () => profileDeleteFolderById(f.id),
    });
    return;
  }
  const parts = [];
  if (folderCount) parts.push(folderCount + (folderCount === 1 ? ' subfolder' : ' subfolders'));
  if (itemCount) parts.push(itemCount + (itemCount === 1 ? ' profile' : ' profiles'));
  profileOpenDeleteModal({
    title: 'Delete folder',
    message: 'Are you sure you want to delete folder "' + f.name + '" containing ' +
      parts.join(' and ') + '? The profiles inside are deleted too. This cannot be undone.',
    requireName: f.name,
    onConfirm: () => profileDeleteFolderById(f.id),
  });
}
async function profileDeleteItem(uuid){
  try {
    await profileFlushPendingSave();
    const r = await fetch('/profile/api/profiles/' + uuid, {method: 'DELETE'});
    const data = await r.json();
    if (!r.ok){ profileToastMsg(data.error || 'could not delete'); return; }
    profileItems = profileItems.filter(p => p.uuid !== uuid);
    profileTreeVersion = data.version;
    if (profileSelectedItem === uuid) profileSelectedItem = null;
    profileRenderTree();
    profileRender();
    profileToastMsg('deleted');
  } catch (e) {
    profileToastMsg('could not delete');
  }
}
async function profileDeleteFolderById(id){
  const doomedFolder = profileFolderById(id);  // captured before removal, for the parent fallback below
  try {
    await profileFlushPendingSave();
    const r = await fetch('/profile/api/folders/' + id, {method: 'DELETE'});
    const data = await r.json();
    if (!r.ok){ profileToastMsg(data.error || 'could not delete'); return; }
    // The server cascaded the subtree; mirror that locally instead of re-fetching.
    const folderIds = new Set([id]);
    let grew = true;
    while (grew){
      grew = false;
      profileFolders.forEach(c => {
        if (c.parentId && folderIds.has(c.parentId) && !folderIds.has(c.id)){
          folderIds.add(c.id); grew = true;
        }
      });
    }
    profileFolders = profileFolders.filter(x => !folderIds.has(x.id));
    profileItems = profileItems.filter(p => !folderIds.has(p.folderId));
    profileTreeVersion = data.version;
    if (profileSelectedItem && !profileByUuid(profileSelectedItem)) profileSelectedItem = null;
    // Land on the deleted folder's parent, not the root, so the operator stays in context.
    if (folderIds.has(profileSelectedFolder)){
      profileSelectedFolder = (doomedFolder && doomedFolder.parentId) || null;
    }
    profileRenderTree();
    profileRender();
    profileToastMsg('deleted');
  } catch (e) {
    profileToastMsg('could not delete');
  }
}
document.getElementById('profile-delete-input').addEventListener('input', profileDeleteUpdateState);
document.getElementById('profile-delete-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('profile-delete-confirm').disabled){
    e.preventDefault();
    document.getElementById('profile-delete-confirm').click();
  }
});
document.getElementById('profile-delete-confirm').addEventListener('click', () => {
  const fn = profileDeleteOnConfirm;
  profileCloseDeleteModal();
  if (fn) fn();
});

// ---- persistence ----
async function profileLoadTree(){
  try {
    const r = await fetch('/profile/api/tree');
    const data = await r.json();
    profileFolders = (data && data.folders) || [];
    profileItems = (data && data.profiles) || [];
    profileTreeVersion = (data && data.version) || null;
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    profileFolders = []; profileItems = []; profileTreeVersion = null;
  }
}
let profileToastTimer = null;
function profileToastMsg(text){
  const el = document.getElementById('profile-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(profileToastTimer);
  profileToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let profileSaveTimer = null;
let profileTreeVersion = null;    // token from hydrate; PUTs echo it (stale → 409)
let profileSaveInFlight = false;
let profileSaveQueued = false;
let profileSaveChain = null;      // promise for the active PUT (+ any queued follow-up), or null when idle
let profileTreeSaveOk = true;     // last structural PUT outcome (duplicate aborts on false)
function profileSave(){
  clearTimeout(profileSaveTimer);
  profileSaveTimer = setTimeout(profileSavePush, 250);  // coalesce bursts into one PUT
}
// Per docs/ui-tree-persistence.md: "Flush or await a pending tree PUT before
// issuing a create or delete." Nothing else orders a tree PUT against a
// create/delete, and the two responses race — if the older PUT's response
// lands after the create/delete's fresher token, it overwrites that token with
// a stale one and the next save 409s for no reason the operator can see.
// Cancels a pending debounce timer and runs that save immediately, or awaits a
// save already in flight (including one queued behind it). A no-op when
// nothing is pending.
function profileFlushPendingSave(){
  if (profileSaveTimer){
    clearTimeout(profileSaveTimer);
    profileSaveTimer = null;
    return profileSavePush();
  }
  return profileSaveChain || Promise.resolve();
}
// After a re-hydrate the fresh data may no longer contain the selected
// folder/profile (e.g. the rejected edit was the move that put it there).
// Clear whichever selection no longer resolves so render doesn't point at a
// row that isn't in the tree anymore.
async function profileReloadAndRepaint(message){
  await profileLoadTree();
  if (profileSelectedItem && !profileByUuid(profileSelectedItem)) profileSelectedItem = null;
  if (profileSelectedFolder && !profileFolderById(profileSelectedFolder)) profileSelectedFolder = null;
  profileRenderTree();
  profileRender();
  profileToastMsg(message);
}
// Returns the promise for this save (or, if one was already in flight, the
// promise for that one — which folds in this call via profileSaveQueued once
// it settles). profileFlushPendingSave relies on that: awaiting the returned/
// chained promise always means "no tree PUT is outstanding anymore".
function profileSavePush(){
  if (profileSaveInFlight){ profileSaveQueued = true; return profileSaveChain; }  // serialize PUTs
  profileSaveInFlight = true;
  const run = (async () => {
    try {
      // Project the mixed GET state back to structural keys only: built-in rows
      // and the derived summary never ride a save (the server rejects both).
      const r = await fetch('/profile/api/tree', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          folders: profileFolders.filter(f => !f.builtin).map(f => ({
            id: f.id, name: f.name, description: f.description || '',
            parentId: f.parentId || null})),
          profiles: profileItems.filter(p => !p.builtin).map(p => ({
            uuid: p.uuid, name: p.name, folderId: p.folderId || null})),
          version: profileTreeVersion}),
      });
      const j = await r.json().catch(() => null);
      if (r.status === 409){
        // Another tab/editor changed the tree; their version wins — re-hydrate
        // and repaint so the screen matches what the server just accepted.
        profileTreeSaveOk = false;
        await profileReloadAndRepaint(
          'Profile tree was changed elsewhere — reloaded. Your last edit was not saved.');
        return;
      }
      if (!r.ok){
        // A 400 here means our payload disagreed with the server about which
        // rows exist — re-hydrate rather than retry the same bad shape, and
        // repaint so the rejected edit doesn't linger on screen.
        profileTreeSaveOk = false;
        await profileReloadAndRepaint(
          'Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)) + ' — reloaded.');
        return;
      }
      profileTreeVersion = j.version;
      profileTreeSaveOk = true;
    } catch (e) {
      // Network error: we can't tell whether the server applied the change, so
      // re-hydrate and repaint rather than leave the client's guess on screen.
      profileTreeSaveOk = false;
      await profileReloadAndRepaint('Save failed — reloaded.');
    } finally {
      profileSaveInFlight = false;
      // A save requested while we were in flight gets its own immediate push
      // here, not a fresh 250ms debounce — and this run doesn't resolve until
      // that one does too, so anything awaiting it (profileFlushPendingSave)
      // sees the whole chain settle before the token is treated as final.
      if (profileSaveQueued){
        profileSaveQueued = false;
        profileSaveChain = profileSavePush();
        await profileSaveChain;
      } else {
        profileSaveChain = null;
      }
    }
  })();
  profileSaveChain = run;
  return run;
}

// ---- datalists (static arrays; timezones from the runtime — no list to maintain) ----
const PROFILE_DL_LANG = ['da','de','en','en-AU','en-CA','en-GB','en-IN','en-SG','en-US','es','es-MX','fr','fr-CA','he','it','ja','ko','nb','nl','pl','pt-BR','sv','te','zh','zh-Hans','zh-Hant'];
const PROFILE_DL_CURRENCY = ['AUD','BRL','CAD','CHF','CNY','DKK','EUR','GBP','ILS','INR','JPY','KRW','MXN','NOK','PLN','SEK','SGD','USD'];
const PROFILE_DL_COUNTRY = ['Australia','Brazil','Canada','China','Denmark','France','Germany','India','Israel','Italy','Japan','Mexico','Netherlands','Norway','Poland','Singapore','South Korea','Spain','Sweden','UK','US'];
function profileFillDatalist(id, values){
  const dl = document.getElementById(id);
  dl.innerHTML = '';
  values.forEach(v => { const o = document.createElement('option'); o.value = v; dl.appendChild(o); });
}
function profileInitDatalists(){
  profileFillDatalist('profile-dl-lang', PROFILE_DL_LANG);
  profileFillDatalist('profile-dl-currency', PROFILE_DL_CURRENCY);
  profileFillDatalist('profile-dl-country', PROFILE_DL_COUNTRY);
  profileFillDatalist('profile-dl-topic', PROFILE_DL_TOPIC);
  let zones = [];
  // Without Intl.supportedValuesOf the timezone input stays free text over an
  // empty list — never blocked, just unassisted.
  try { if (Intl.supportedValuesOf) zones = Intl.supportedValuesOf('timeZone'); } catch (e) {}
  profileFillDatalist('profile-dl-tz', zones);
}

// ---- form pane ----
const PROFILE_FIELD_KEYS = Array.from(
  document.querySelectorAll('#profile-form [data-key]')).map(el => el.dataset.key);
function profileFieldEl(key){
  return document.querySelector('#profile-form [data-key="' + key + '"]');
}
let profileFormUuid = null;   // uuid whose data the form currently holds

function profileRenderForm(){
  const el = document.getElementById('profile-form');
  const p = profileSelectedItem ? profileByUuid(profileSelectedItem) : null;
  if (!p){ el.hidden = true; profileFormUuid = null; return; }
  el.hidden = false;
  document.getElementById('profile-builtin-hint').hidden = !p.builtin;
  if (profileFormUuid !== p.uuid){
    profileFormUuid = p.uuid;
    profileFillForm({});
    profileSetFormDisabled(true);   // until the data arrives (built-ins stay disabled)
    profileRenderDynamic(null);
    profileLoadData(p.uuid);
    profileLangOnSelect(p);
    profileCalOnSelect(p);
  } else {
    profileLangRenderStatus();
    profileCalRenderStatus();
  }
  profileRenderStatus();
}
async function profileLoadData(uuid){
  let d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid));
    d = await r.json();
  } catch (e) { /* handled below */ }
  // A late GET is discarded unless its uuid is still the selected profile.
  if (profileFormUuid !== uuid || profileSelectedItem !== uuid) return;
  const st = profileFormState[uuid];
  if (st && st.snapshot && (st.dirty || st.inFlight || st.failed)){
    // A pending local edit outranks the fetched snapshot — show what the
    // autosave is about to push, not what the server last acknowledged.
    profileFillForm(st.snapshot);
    profileSetFormDisabled(false);
    return;
  }
  if (!d || !d.ok){
    // A just-created profile may not be saved yet (the tree save is in
    // flight); its data is {} by construction, so the blank form is correct.
    profileSetFormDisabled(false);
    return;
  }
  profileFillForm(d.data || {});
  profileSetFormDisabled(!!d.builtin);
  profileRenderDynamic((d.data && d.data.dynamic) || null);
}
function profileFillForm(data){
  PROFILE_FIELD_KEYS.forEach(k => {
    profileFieldEl(k).value = (data && data[k] != null) ? data[k] : '';
  });
  profileUpdatePreview();
  profileUpdateWarnings();
}
function profileReadForm(){
  // Complete editable snapshot; blanks stay off (the server canonicalizes
  // "" away regardless, this just keeps the payload sparse like the storage).
  const out = {};
  PROFILE_FIELD_KEYS.forEach(k => {
    const v = profileFieldEl(k).value;
    if (v !== '') out[k] = v;
  });
  return out;
}
function profileSetFormDisabled(dis){
  PROFILE_FIELD_KEYS.forEach(k => { profileFieldEl(k).disabled = dis; });
  document.getElementById('profile-tz-mine').disabled = dis;
}

// ---- advisory validation (never blocks a save — the server stays soft so an
// uncommon-yet-valid value is never rejected; these warn only when the typed
// value is PROVABLY invalid, so a non-developer isn't left saving junk silently) ----
function profileCheckTimezone(v){
  try { new Intl.DateTimeFormat('en', {timeZone: v}); return null; }
  catch (e) { return 'Not a known timezone — pick one from the list, e.g. Europe/Copenhagen.'; }
}
function profileCheckCurrency(v){
  return /^[A-Za-z]{3}$/.test(v) ? null : 'Currency codes are three letters — e.g. DKK, USD, EUR.';
}
const PROFILE_SOFT_CHECKS = {
  timezone: profileCheckTimezone,
  currency: profileCheckCurrency,
  currency_2: profileCheckCurrency,
};
function profileUpdateWarnings(){
  Object.keys(PROFILE_SOFT_CHECKS).forEach(k => {
    const el = document.getElementById('pf-warn-' + k);
    if (!el) return;
    const v = profileFieldEl(k).value.trim();
    const warn = v ? PROFILE_SOFT_CHECKS[k](v) : null;
    el.textContent = warn || '';
    el.hidden = !warn;
  });
}
// Connector-written observations under data.dynamic: a read-only "Last seen"
// group, rendered only when present. Humans never edit these; the PUT
// preserves them server-side.
function profileRenderDynamic(dyn){
  const fs = document.getElementById('profile-dynamic');
  const box = document.getElementById('profile-dynamic-rows');
  box.innerHTML = '';
  const keys = (dyn && typeof dyn === 'object') ? Object.keys(dyn) : [];
  fs.hidden = !keys.length;
  keys.forEach(k => {
    const e = dyn[k] || {};
    const div = document.createElement('div');
    div.className = 'profile-dynamic-row muted';
    const val = (e.value != null) ? String(e.value) : JSON.stringify(e);
    div.textContent = k + ': ' + val + (e.seen_at ? ' — seen ' + profileShortDate(e.seen_at) : '');
    box.appendChild(div);
  });
}

// ---- datetime preview (the preview is the documentation for the enums) ----
function profileFormatDateParts(parts, fmt){
  switch (fmt){
    case 'DD/MM/YYYY': return parts.day + '/' + parts.month + '/' + parts.year;
    case 'MM/DD/YYYY': return parts.month + '/' + parts.day + '/' + parts.year;
    case 'DD.MM.YYYY': return parts.day + '.' + parts.month + '.' + parts.year;
    case 'DD-MM-YYYY': return parts.day + '-' + parts.month + '-' + parts.year;
    default: return parts.year + '-' + parts.month + '-' + parts.day;   // YYYY-MM-DD
  }
}
function profileUpdatePreview(){
  const el = document.getElementById('profile-preview');
  const tz = profileFieldEl('timezone').value.trim();
  const dateFmt = profileFieldEl('date_format').value || 'YYYY-MM-DD';
  const hour12 = (profileFieldEl('time_format').value || '24h') === '12h';
  // The number_format enum's stored value IS its own preview: every choice
  // renders the same sample (1234567.89) differing only in separators.
  const numberFmt = profileFieldEl('number_format').value;
  try {
    // The timezone's only job here is validation: an invalid or half-typed
    // zone throws and must never break the rest of the form.
    if (tz) new Intl.DateTimeFormat('en', {timeZone: tz});
    // Fixed sample values, chosen to be unambiguous: 31 can only be a day
    // (so DD/MM vs MM/DD is readable) and 23:59 can only be a 24h clock.
    const parts = {year: String(new Date().getFullYear()), month: '12', day: '31'};
    const time = hour12 ? '11:59 pm' : '23:59';
    el.textContent = 'Preview: ' + profileFormatDateParts(parts, dateFmt) + ' · ' + time
      + (numberFmt ? ' · ' + numberFmt : '');
  } catch (e) {
    el.textContent = 'Preview unavailable — timezone not recognized';
  }
}

// ---- autosave (debounced 400 ms per profile; one in-flight PUT per profile;
// a queued re-send carries the newest snapshot; failures retain the dirty
// snapshot and retry with capped backoff for as long as the page is open) ----
const PROFILE_SAVE_DEBOUNCE_MS = 400;
const PROFILE_RETRY_MAX_MS = 30000;
let profileFormState = {};   // uuid -> {timer, retryTimer, retryDelay, inFlight, dirty, failed, snapshot}
function profileFormStateFor(uuid){
  if (!profileFormState[uuid]){
    profileFormState[uuid] = {timer: null, retryTimer: null, retryDelay: 1000,
                              inFlight: false, dirty: false, failed: false, snapshot: null};
  }
  return profileFormState[uuid];
}
function profileFieldEdited(){
  const p = profileFormUuid ? profileByUuid(profileFormUuid) : null;
  if (!p || p.builtin) return;
  const uuid = profileFormUuid;
  const st = profileFormStateFor(uuid);
  st.snapshot = profileReadForm();
  st.dirty = true;
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }  // an edit retries a failure immediately
  clearTimeout(st.timer);
  st.timer = setTimeout(() => { st.timer = null; profileDataPush(uuid); },
                        PROFILE_SAVE_DEBOUNCE_MS);
  profileUpdatePreview();
  profileUpdateWarnings();
  profileRenderStatus();
}
async function profileDataPush(uuid){
  const st = profileFormStateFor(uuid);
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.inFlight || !st.dirty || !st.snapshot) return;  // the ack handler re-sends queued edits
  st.inFlight = true;
  st.dirty = false;   // a new edit mid-flight re-marks it; failure below restores it
  const snapshot = st.snapshot;
  profileRenderStatus();
  let ok = false, d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: snapshot}),
    });
    d = await r.json().catch(() => null);
    ok = r.ok;
  } catch (e) { /* ok stays false */ }
  st.inFlight = false;
  if (ok){
    st.failed = false;
    st.retryDelay = 1000;
    // Refresh the row's local summary from the canonical snapshot so a folder
    // table opened later shows the saved values without reloading the tree.
    const row = profileByUuid(uuid);
    if (row && d && d.summary){ row.summary = d.summary; profileTouch(row); }
    if (st.dirty) profileDataPush(uuid);   // queued re-send with the newest snapshot
  } else {
    st.dirty = true;    // retain the dirty snapshot
    st.failed = true;
    st.retryTimer = setTimeout(() => { st.retryTimer = null; profileDataPush(uuid); },
                               st.retryDelay);
    st.retryDelay = Math.min(st.retryDelay * 2, PROFILE_RETRY_MAX_MS);  // capped; keeps retrying while the page is open
  }
  profileRenderStatus();
}
function profileRenderStatus(){
  const el = document.getElementById('profile-save-status');
  const st = profileFormUuid ? profileFormState[profileFormUuid] : null;
  if (!st){ el.textContent = ''; return; }
  if (st.failed) el.textContent = 'Save failed — retrying';
  else if (st.inFlight || st.dirty || st.timer) el.textContent = 'Saving…';
  else if (st.snapshot) el.textContent = 'Saved ✓';
  else el.textContent = '';
}
function profileAnySavePending(){
  const flat = Object.keys(profileFormState).some(u => {
    const st = profileFormState[u];
    return st && (st.dirty || st.inFlight || st.failed || st.timer);
  });
  // Calibration participates too: pending, failed-validation, and
  // incomplete-row (topicless but touched) states must hold the unload
  // guard until acknowledged or resolved.
  const cal = Object.keys(profileCalState).some(u => {
    const st = profileCalState[u];
    return profileCalPending(st) || (st && st.invalid)
      || profileCalHasIncomplete(st);
  });
  const languages = Object.keys(profileLangState).some(u => {
    const st = profileLangState[u];
    return profileLangPending(st) || (st && st.invalid)
      || profileLangHasIncomplete(st);
  });
  return flat || languages || cal;
}
// The unload guard is active only while a save is pending or failed; it is
// gone the moment the latest snapshot is acknowledged. Confirming the dialog
// deliberately abandons the pending edit — the browser wording says so.
window.addEventListener('beforeunload', (e) => {
  if (profileAnySavePending()){ e.preventDefault(); e.returnValue = ''; }
});
window.addEventListener('online', () => {
  Object.keys(profileFormState).forEach(u => {
    const st = profileFormState[u];
    if (st && st.failed && !st.inFlight){
      if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
      profileDataPush(u);
    }
  });
  Object.keys(profileLangState).forEach(u => {
    const st = profileLangState[u];
    if (st && st.failed && !st.inFlight){
      if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
      profileLangPush(u);
    }
  });
});
// Cancel the debounce and await the newest data PUT; false if it can't be saved.
async function profileFlushData(uuid){
  const st = profileFormState[uuid];
  if (!st) return true;
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
  while (st.dirty || st.inFlight){
    if (st.inFlight){
      await new Promise(res => setTimeout(res, 50));
    } else {
      await profileDataPush(uuid);
      if (st.failed) return false;
    }
  }
  return !st.failed;
}

// ---- languages (own validated subtree + autosave state) --------------------
const PROFILE_LANG_DEBOUNCE_MS = 400;
const PROFILE_LANG_RETRY_MAX_MS = 30000;
const PROFILE_LANG_LEVELS = ['native', 'fluent', 'intermediate', 'beginner'];
const PROFILE_LANG_STANCES = ['prefer', 'neutral', 'avoid'];
// uuid -> {rows, loaded, loadFailed, builtin, timer, retryTimer, retryDelay,
//          inFlight, dirty, failed, invalid, error}
let profileLangState = {};
function profileLangStateFor(uuid){
  if (!profileLangState[uuid]){
    profileLangState[uuid] = {rows: [], loaded: false, loadFailed: false,
                              builtin: false,
                              timer: null, retryTimer: null, retryDelay: 1000,
                              inFlight: false, dirty: false, failed: false,
                              invalid: false, error: ''};
  }
  return profileLangState[uuid];
}
function profileLangPending(st){
  return st && (st.dirty || st.inFlight || st.failed || st.timer);
}
// A newly-added row with only its seeded defaults is a harmless local draft.
// Once any other content is entered, a missing tag is unsaved information and
// participates in the unload guard.
function profileLangIncompleteRow(row){
  if ((row.tag || '').trim() !== '') return false;
  if (row.id) return true;
  return (row.level || '') !== 'intermediate'
    || (row.stance || '') !== 'neutral'
    || (row.note || '').trim() !== '';
}
function profileLangHasIncomplete(st){
  return !!st && st.loaded && st.rows.some(profileLangIncompleteRow);
}
function profileLangOnSelect(profile){
  const st = profileLangStateFor(profile.uuid);
  st.builtin = !!profile.builtin;
  profileLangRender();
  if (!st.loaded && !profileLangPending(st)) profileLangLoad(profile.uuid);
}
async function profileLangLoad(uuid){
  let data = null;
  try {
    const response = await fetch(
      '/profile/api/profiles/' + encodeURIComponent(uuid) + '/languages');
    data = await response.json();
  } catch (e) { /* load failure is rendered below */ }
  if (profileFormUuid !== uuid) return;
  const st = profileLangStateFor(uuid);
  if (st.loaded && profileLangPending(st)) return;
  if (data && data.ok){
    st.rows = data.rows || [];
    st.builtin = !!data.builtin;
    st.loaded = true;
    st.loadFailed = false;
  } else {
    st.loadFailed = true;
  }
  profileLangRender();
}
function profileLangSelect(options, value){
  const select = document.createElement('select');
  options.forEach(optionValue => {
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = optionValue;
    select.appendChild(option);
  });
  select.value = options.includes(value) ? value : options[0];
  return select;
}
function profileLangRender(){
  const box = document.getElementById('profile-lang-rows');
  const add = document.getElementById('profile-lang-add');
  const builtinHint = document.getElementById('profile-lang-builtin-hint');
  box.innerHTML = '';
  const uuid = profileFormUuid;
  const st = uuid ? profileLangState[uuid] : null;
  if (!uuid || !st){
    add.hidden = true;
    builtinHint.hidden = true;
    profileLangRenderStatus();
    return;
  }
  const builtin = st.builtin;
  add.hidden = builtin || !st.loaded;
  builtinHint.hidden = !builtin;
  if (st.rows.length){
    const head = document.createElement('div');
    head.className = 'profile-lang-head';
    ['Language tag', 'Level', 'Stance'].forEach(text => {
      const span = document.createElement('span');
      span.textContent = text;
      head.appendChild(span);
    });
    box.appendChild(head);
  }
  st.rows.forEach((row, index) => {
    const wrap = document.createElement('div');
    wrap.className = 'profile-lang-row';
    const main = document.createElement('div');
    main.className = 'profile-lang-main';

    const tag = document.createElement('input');
    tag.type = 'text';
    tag.value = row.tag || '';
    tag.placeholder = 'BCP-47, e.g. da, en-US, zh-Hans';
    tag.setAttribute('list', 'profile-dl-lang');
    tag.addEventListener('input', () => {
      row.tag = tag.value;
      profileLangEdited(uuid);
    });
    const level = profileLangSelect(PROFILE_LANG_LEVELS, row.level);
    level.addEventListener('change', () => {
      row.level = level.value;
      profileLangEdited(uuid);
    });
    const stance = profileLangSelect(PROFILE_LANG_STANCES, row.stance);
    stance.addEventListener('change', () => {
      row.stance = stance.value;
      if (stance.value === 'prefer'){
        st.rows.forEach((other, otherIndex) => {
          if (otherIndex !== index && other.stance === 'prefer'){
            other.stance = 'neutral';
          }
        });
        profileLangRender();
      }
      profileLangEdited(uuid);
    });
    main.appendChild(tag);
    main.appendChild(level);
    main.appendChild(stance);
    wrap.appendChild(main);

    const note = document.createElement('input');
    note.type = 'text';
    note.className = 'profile-lang-note';
    note.placeholder = 'Note (optional, e.g. primary response language)';
    note.value = row.note || '';
    note.addEventListener('input', () => {
      row.note = note.value;
      profileLangEdited(uuid);
    });
    wrap.appendChild(note);

    const meta = document.createElement('div');
    meta.className = 'profile-lang-meta';
    const age = document.createElement('span');
    age.className = 'profile-lang-age';
    age.textContent = builtin ? '' : profileCalAge(row.updated_at);
    meta.appendChild(age);
    if (!builtin){
      const up = document.createElement('button');
      up.type = 'button'; up.textContent = '↑'; up.title = 'Move up';
      up.disabled = index === 0;
      up.addEventListener('click', () => profileLangMove(uuid, index, -1));
      const down = document.createElement('button');
      down.type = 'button'; down.textContent = '↓'; down.title = 'Move down';
      down.disabled = index === st.rows.length - 1;
      down.addEventListener('click', () => profileLangMove(uuid, index, 1));
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'danger';
      remove.textContent = 'Remove';
      remove.addEventListener('click', () => profileLangRemove(uuid, index));
      meta.appendChild(up);
      meta.appendChild(down);
      meta.appendChild(remove);
    }
    wrap.appendChild(meta);
    [tag, level, stance, note].forEach(input => { input.disabled = builtin; });
    box.appendChild(wrap);
  });
  profileLangRenderStatus();
}
function profileLangMove(uuid, index, delta){
  const st = profileLangStateFor(uuid);
  const target = index + delta;
  if (target < 0 || target >= st.rows.length) return;
  const row = st.rows[index];
  st.rows[index] = st.rows[target];
  st.rows[target] = row;
  profileLangRender();
  profileLangEdited(uuid);
}
function profileLangRemove(uuid, index){
  profileLangStateFor(uuid).rows.splice(index, 1);
  profileLangRender();
  profileLangEdited(uuid);
}
function profileLangAdd(){
  const uuid = profileFormUuid;
  if (!uuid) return;
  const st = profileLangStateFor(uuid);
  if (st.builtin || !st.loaded) return;
  st.rows.push({tag: '', level: 'intermediate', stance: 'neutral'});
  profileLangRender();
  const inputs = document.querySelectorAll('#profile-lang-rows input[list]');
  if (inputs.length) inputs[inputs.length - 1].focus();
}
function profileLangEdited(uuid){
  const st = profileLangStateFor(uuid);
  if (st.builtin || !st.loaded) return;
  st.dirty = true;
  st.invalid = false;
  st.error = '';
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
  clearTimeout(st.timer);
  st.timer = setTimeout(() => {
    st.timer = null;
    profileLangPush(uuid);
  }, PROFILE_LANG_DEBOUNCE_MS);
  profileLangRenderStatus();
}
function profileLangPayload(st){
  const rows = [];
  const sent = [];
  st.rows.forEach(row => {
    if (!(row.id || (row.tag || '').trim() !== ''
          || (row.note || '').trim() !== '')) return;
    const out = {
      tag: row.tag || '',
      level: row.level || '',
      stance: row.stance || '',
    };
    if (row.id) out.id = row.id;
    if (row.note) out.note = row.note;
    rows.push(out);
    const keeps = ['tag', 'level', 'stance', 'note']
      .some(key => (out[key] || '').trim() !== '');
    sent.push(keeps ? row : null);   // null = server drops an all-blank row
  });
  return {rows: rows, sent: sent};
}
async function profileLangPush(uuid){
  const st = profileLangStateFor(uuid);
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.inFlight || !st.dirty) return;
  st.inFlight = true;
  st.dirty = false;
  profileLangRenderStatus();
  const payload = profileLangPayload(st);
  let status = 0;
  let data = null;
  try {
    const response = await fetch(
      '/profile/api/profiles/' + encodeURIComponent(uuid) + '/languages',
      {method: 'PUT', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({rows: payload.rows})});
    status = response.status;
    data = await response.json().catch(() => null);
  } catch (e) { /* network class below */ }
  st.inFlight = false;
  if (status === 200 && data && data.ok){
    st.failed = false;
    st.invalid = false;
    st.error = '';
    st.retryDelay = 1000;
    st.loaded = true;
    (data.rows || []).forEach((canonical, index) => {
      const ref = payload.sent[index];
      if (!ref) return;
      if (!ref.id) ref.id = canonical.id;
      ref.updated_at = canonical.updated_at;
    });
    const treeRow = profileByUuid(uuid);
    if (treeRow){
      const preferred = (data.rows || []).find(row => row.stance === 'prefer');
      const first = preferred || (data.rows || [])[0] || {};
      treeRow.summary = treeRow.summary || {};
      treeRow.summary.language = first.tag || '';
    }
    if (st.dirty){
      profileLangPush(uuid);
    } else {
      const active = document.activeElement;
      const box = document.getElementById('profile-lang-rows');
      if (profileFormUuid === uuid && (!active || !box.contains(active))){
        const drafts = st.rows.filter(
          row => !row.id && (row.tag || '').trim() === '');
        st.rows = (data.rows || []).concat(drafts);
        profileLangRender();
      }
    }
  } else if (status === 400){
    st.failed = false;
    st.invalid = true;
    st.dirty = true;
    st.error = (data && data.error) || 'validation failed';
  } else {
    st.dirty = true;
    st.failed = true;
    st.retryTimer = setTimeout(() => {
      st.retryTimer = null;
      profileLangPush(uuid);
    }, st.retryDelay);
    st.retryDelay = Math.min(
      st.retryDelay * 2, PROFILE_LANG_RETRY_MAX_MS);
  }
  profileLangRenderStatus();
}
function profileLangRenderStatus(){
  const status = document.getElementById('profile-lang-status');
  const error = document.getElementById('profile-lang-error');
  const st = profileFormUuid ? profileLangState[profileFormUuid] : null;
  status.innerHTML = '';
  if (!st || st.builtin){ error.textContent = ''; return; }
  error.textContent = st.invalid ? st.error : '';
  if (!st.loaded){
    if (st.loadFailed){
      status.textContent = 'Could not load languages — ';
      const retry = document.createElement('a');
      retry.href = '#';
      retry.textContent = 'retry';
      retry.addEventListener('click', event => {
        event.preventDefault();
        st.loadFailed = false;
        profileLangRenderStatus();
        profileLangLoad(profileFormUuid);
      });
      status.appendChild(retry);
    } else {
      status.textContent = 'Loading…';
    }
    return;
  }
  if (st.failed) status.textContent = 'Save failed — retrying';
  else if (st.invalid) status.textContent = 'Not saved';
  else if (st.inFlight || st.dirty || st.timer) status.textContent = 'Saving…';
  else if (profileLangHasIncomplete(st)){
    status.textContent = 'Not saved — a row needs a language tag';
  } else {
    status.textContent = st.rows.length ? 'Saved ✓' : '';
  }
}
async function profileLangFlush(uuid){
  const st = profileLangState[uuid];
  if (!st) return true;
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
  while (st.dirty || st.inFlight){
    if (st.inFlight){
      await new Promise(resolve => setTimeout(resolve, 50));
    } else {
      await profileLangPush(uuid);
      if (st.failed || st.invalid) return false;
    }
  }
  return !st.failed && !st.invalid && !profileLangHasIncomplete(st);
}

// ---- knowledge calibration (own fieldset, own autosave state — mirrors the
// flat form's debounce/in-flight/backoff pattern; no conflict dialogs). ----
const PROFILE_CAL_DEBOUNCE_MS = 400;
const PROFILE_CAL_RETRY_MAX_MS = 30000;
const PROFILE_CAL_LEVELS = ['expert', 'intermediate', 'beginner', 'none'];
const PROFILE_CAL_STANCES = ['prefer', 'neutral', 'avoid'];
const PROFILE_CAL_DEPTHS = ['concise', 'standard', 'teach'];
// Broad technical and non-technical topic suggestions; the input stays free text.
const PROFILE_DL_TOPIC = ['Accounting', 'Carpentry', 'Cooking', 'Databases',
  'DevOps', 'Electronics', 'Finance', 'Gardening', 'Git', 'Graphic design',
  'History', 'JavaScript', 'Law', 'Linux', 'Machine learning', 'Mathematics',
  'Music theory', 'Networking', 'Photography', 'PostgreSQL', 'Python', 'Rust',
  'SQL', 'Statistics', 'Writing'];
// uuid -> {rows, loaded, loadFailed, builtin, timer, retryTimer, retryDelay,
//          inFlight, dirty, failed, invalid, error}
let profileCalState = {};
function profileCalStateFor(uuid){
  if (!profileCalState[uuid]){
    profileCalState[uuid] = {rows: [], loaded: false, loadFailed: false,
                             builtin: false,
                             timer: null, retryTimer: null, retryDelay: 1000,
                             inFlight: false, dirty: false, failed: false,
                             invalid: false, error: ''};
  }
  return profileCalState[uuid];
}
function profileCalPending(st){
  return st && (st.dirty || st.inFlight || st.failed || st.timer);
}
// A topicless row carrying operator-entered content cannot be sent (the
// server requires a topic) but must never be acknowledged as saved either:
// it holds the "Not saved" state and the unload guard until a topic exists.
// A fresh add-row (only the seeded default level) carries no information and
// stays a silent local draft.
function profileCalIncompleteRow(r){
  if ((r.topic || '').trim() !== '') return false;
  return (r.stance || '') !== '' || (r.depth || '') !== ''
    || (r.note || '').trim() !== ''
    || ((r.level || '') !== '' && r.level !== 'intermediate');
}
function profileCalHasIncomplete(st){
  return !!st && st.loaded && st.rows.some(profileCalIncompleteRow);
}
function profileCalOnSelect(p){
  const st = profileCalStateFor(p.uuid);
  st.builtin = !!p.builtin;
  profileCalRender();
  if (!st.loaded && !profileCalPending(st)) profileCalLoad(p.uuid);
}
async function profileCalLoad(uuid){
  let d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid) + '/calibration');
    d = await r.json();
  } catch (e) { /* loadFailed below — editing stays gated, Retry re-fetches */ }
  // Late GETs are keyed by uuid and never populate the wrong pane; a pending
  // local edit outranks the fetched snapshot (only possible on a re-load —
  // editing is disabled until the FIRST load succeeds, so autosave can never
  // send an incomplete list as a complete snapshot and delete unseen rows).
  if (profileFormUuid !== uuid) return;
  const st = profileCalStateFor(uuid);
  if (st.loaded && profileCalPending(st)) return;
  if (d && d.ok){
    st.rows = d.topics || [];
    st.builtin = !!d.builtin;
    st.loaded = true;
    st.loadFailed = false;
  } else {
    st.loadFailed = true;
  }
  profileCalRender();
}
function profileCalAge(iso){
  if (!iso) return '';
  const then = new Date(iso);
  if (isNaN(then)) return '';
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days < 1) return 'today';
  if (days < 31) return days + 'd ago';
  const months = Math.floor(days / 30);
  if (months < 12) return months + 'mo ago';
  return Math.floor(months / 12) + 'y ago';
}
function profileCalSelect(cls, options, value, blankLabel){
  // The first option is the explicit unset state: "Unspecified" for the
  // optional axes (absent stance/depth is a valid declaration), "Choose…"
  // for required level. The column headers above the rows name the axes,
  // so the blank label no longer doubles as a field name.
  const sel = document.createElement('select');
  sel.className = cls;
  const blank = document.createElement('option');
  blank.value = ''; blank.textContent = blankLabel || 'Unspecified';
  sel.appendChild(blank);
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o; opt.textContent = o;
    sel.appendChild(opt);
  });
  sel.value = options.includes(value) ? value : '';
  return sel;
}
// Rebuild the row DOM from state. Structural ops re-render; plain typing only
// updates state (no re-render, so focus is never stolen mid-word).
function profileCalRender(){
  const box = document.getElementById('profile-cal-rows');
  const add = document.getElementById('profile-cal-add');
  box.innerHTML = '';
  const uuid = profileFormUuid;
  const st = uuid ? profileCalState[uuid] : null;
  if (!uuid || !st){ add.hidden = true; profileCalRenderStatus(); return; }
  const builtin = st.builtin;
  // Editing is gated on a successful initial load: rows only exist in state
  // after the snapshot arrived, and the add button stays hidden until then.
  add.hidden = builtin || !st.loaded;
  if (st.rows.length){
    // One column-header row naming the axes, aligned to the row grid.
    const head = document.createElement('div');
    head.className = 'profile-cal-head';
    ['Topic', 'Level', 'Stance', 'Depth'].forEach(t => {
      const s = document.createElement('span');
      s.textContent = t;
      head.appendChild(s);
    });
    box.appendChild(head);
  }
  st.rows.forEach((row, i) => {
    const wrap = document.createElement('div');
    wrap.className = 'profile-cal-row';
    const main = document.createElement('div');
    main.className = 'profile-cal-main';
    const topic = document.createElement('input');
    topic.type = 'text'; topic.value = row.topic || '';
    topic.placeholder = 'Topic'; topic.setAttribute('list', 'profile-dl-topic');
    topic.addEventListener('input', () => { row.topic = topic.value; profileCalEdited(uuid); });
    const level = profileCalSelect('cal-level', PROFILE_CAL_LEVELS, row.level, 'Choose…');
    level.addEventListener('change', () => { row.level = level.value; profileCalEdited(uuid); });
    const stance = profileCalSelect('cal-stance', PROFILE_CAL_STANCES, row.stance, 'Unspecified');
    stance.addEventListener('change', () => { row.stance = stance.value; profileCalEdited(uuid); });
    const depth = profileCalSelect('cal-depth', PROFILE_CAL_DEPTHS, row.depth, 'Unspecified');
    depth.addEventListener('change', () => { row.depth = depth.value; profileCalEdited(uuid); });
    main.appendChild(topic); main.appendChild(level);
    main.appendChild(stance); main.appendChild(depth);
    wrap.appendChild(main);
    const note = document.createElement('input');
    note.type = 'text'; note.className = 'profile-cal-note';
    note.placeholder = 'Note (optional nuance, e.g. "rusty since 2014")';
    note.value = row.note || '';
    note.addEventListener('input', () => { row.note = note.value; profileCalEdited(uuid); });
    wrap.appendChild(note);
    const meta = document.createElement('div');
    meta.className = 'profile-cal-meta';
    const age = document.createElement('span');
    age.className = 'profile-cal-age';
    // Built-in fixture rows carry a shipped stamp for schema consistency
    // only; their age is meaningless and stays hidden.
    age.textContent = builtin ? '' : profileCalAge(row.updated_at);
    meta.appendChild(age);
    if (!builtin){
      const up = document.createElement('button');
      up.type = 'button'; up.textContent = '↑'; up.title = 'Move up';
      up.disabled = i === 0;
      up.addEventListener('click', () => profileCalMove(uuid, i, -1));
      const down = document.createElement('button');
      down.type = 'button'; down.textContent = '↓'; down.title = 'Move down';
      down.disabled = i === st.rows.length - 1;
      down.addEventListener('click', () => profileCalMove(uuid, i, 1));
      const rm = document.createElement('button');
      rm.type = 'button'; rm.className = 'danger'; rm.textContent = 'Remove';
      rm.addEventListener('click', () => profileCalRemove(uuid, i));
      meta.appendChild(up); meta.appendChild(down); meta.appendChild(rm);
    }
    wrap.appendChild(meta);
    [topic, level, stance, depth, note].forEach(el => { el.disabled = builtin; });
    box.appendChild(wrap);
  });
  profileCalRenderStatus();
}
function profileCalMove(uuid, i, delta){
  const st = profileCalStateFor(uuid);
  const j = i + delta;
  if (j < 0 || j >= st.rows.length) return;
  const tmp = st.rows[i]; st.rows[i] = st.rows[j]; st.rows[j] = tmp;
  profileCalRender();
  profileCalEdited(uuid);
}
function profileCalRemove(uuid, i){
  profileCalStateFor(uuid).rows.splice(i, 1);
  profileCalRender();
  profileCalEdited(uuid);
}
function profileCalAdd(){
  const uuid = profileFormUuid;
  if (!uuid) return;
  const st = profileCalStateFor(uuid);
  if (st.builtin || !st.loaded) return;
  // level defaults so the row turns valid the moment a topic is typed; a row
  // with no topic stays a local draft (excluded from the payload below).
  st.rows.push({topic: '', level: 'intermediate'});
  profileCalRender();
  const inputs = document.querySelectorAll('#profile-cal-rows input[list]');
  if (inputs.length) inputs[inputs.length - 1].focus();
}
function profileCalEdited(uuid){
  const st = profileCalStateFor(uuid);
  if (st.builtin || !st.loaded) return;
  st.dirty = true;
  st.invalid = false;
  st.error = '';
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
  clearTimeout(st.timer);
  st.timer = setTimeout(() => { st.timer = null; profileCalPush(uuid); },
                        PROFILE_CAL_DEBOUNCE_MS);
  profileCalRenderStatus();
}
function profileCalPayload(st){
  // Complete snapshot: existing rows carry their id, new rows omit it,
  // updated_at is server-owned and never sent. Topicless drafts stay local.
  // `sent` keeps the local row-object references the canonical response rows
  // will correspond to (the server drops all-blank rows, mirrored here), so
  // a success can write server-assigned ids back onto the very objects the
  // operator may have kept editing.
  const topics = [];
  const sent = [];
  st.rows.forEach(r => {
    if (!(r.id || (r.topic || '').trim() !== '' || (r.note || '').trim() !== '')) return;
    const out = {topic: r.topic || '', level: r.level || ''};
    if (r.id) out.id = r.id;
    if (r.stance) out.stance = r.stance;
    if (r.depth) out.depth = r.depth;
    if (r.note) out.note = r.note;
    topics.push(out);
    const keeps = ['topic', 'level', 'stance', 'depth', 'note']
      .some(k => (out[k] || '').trim() !== '');
    sent.push(keeps ? r : null);   // null = server will drop it as all-blank
  });
  return {topics: topics, sent: sent};
}
async function profileCalPush(uuid){
  const st = profileCalStateFor(uuid);
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.inFlight || !st.dirty) return;
  st.inFlight = true;
  st.dirty = false;      // a new edit mid-flight re-marks it
  profileCalRenderStatus();
  const payload = profileCalPayload(st);
  let status = 0, d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid) + '/calibration', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({topics: payload.topics}),
    });
    status = r.status;
    d = await r.json().catch(() => null);
  } catch (e) { /* status stays 0 → network class */ }
  st.inFlight = false;
  if (status === 200 && d && d.ok){
    st.failed = false; st.invalid = false; st.error = '';
    st.retryDelay = 1000;
    st.loaded = true;
    // Write server identity back onto the row objects that were sent, BEFORE
    // any queued resend: a newly created row adopts its id/stamp even while
    // the operator keeps typing, so the follow-up snapshot updates that row
    // in place instead of deleting and recreating it under a fresh uuid.
    const keptRefs = payload.sent.filter(r => r !== null);
    (d.topics || []).forEach((canon, i) => {
      const ref = keptRefs[i];
      if (!ref) return;
      if (!ref.id) ref.id = canon.id;
      ref.updated_at = canon.updated_at;
    });
    if (st.dirty){
      profileCalPush(uuid);      // a newer local edit wins; resend immediately
    } else {
      // Adopt the canonical snapshot ONLY when it is safe to re-render.
      // While focus is inside the fieldset the live row objects must stay
      // in state: the input listeners write into those objects, so swapping
      // in the server's copies here would silently detach every keystroke
      // typed after the ack (the Note field "forgetting" bug). The ids and
      // stamps were already merged onto the live objects above, so keeping
      // them loses nothing.
      const active = document.activeElement;
      const boxEl = document.getElementById('profile-cal-rows');
      if (profileFormUuid === uuid && (!active || !boxEl.contains(active))){
        const drafts = st.rows.filter(r => !r.id && (r.topic || '').trim() === '');
        st.rows = (d.topics || []).concat(drafts);
        profileCalRender();
      }
    }
  } else if (status === 400){
    // Server validation: show the message and wait for the next edit — an
    // unchanged invalid snapshot is never retried forever.
    st.failed = false; st.invalid = true; st.dirty = true;
    st.error = (d && d.error) || 'validation failed';
  } else {
    // Network error or 5xx: retain the draft and retry with capped backoff.
    st.dirty = true; st.failed = true;
    st.retryTimer = setTimeout(() => { st.retryTimer = null; profileCalPush(uuid); },
                               st.retryDelay);
    st.retryDelay = Math.min(st.retryDelay * 2, PROFILE_CAL_RETRY_MAX_MS);
  }
  profileCalRenderStatus();
}
function profileCalRenderStatus(){
  const el = document.getElementById('profile-cal-status');
  const err = document.getElementById('profile-cal-error');
  const st = profileFormUuid ? profileCalState[profileFormUuid] : null;
  el.innerHTML = '';
  if (!st || st.builtin){ err.textContent = ''; return; }
  err.textContent = st.invalid ? st.error : '';
  if (!st.loaded){
    // Editing is gated until this load succeeds; a failed load offers Retry.
    if (st.loadFailed){
      el.textContent = 'Could not load calibration — ';
      const retry = document.createElement('a');
      retry.href = '#'; retry.textContent = 'retry';
      retry.addEventListener('click', e => {
        e.preventDefault();
        st.loadFailed = false;
        profileCalRenderStatus();
        profileCalLoad(profileFormUuid);
      });
      el.appendChild(retry);
    } else {
      el.textContent = 'Loading…';
    }
    return;
  }
  if (st.failed) el.textContent = 'Save failed — retrying';
  else if (st.invalid) el.textContent = 'Not saved';
  else if (st.inFlight || st.dirty || st.timer) el.textContent = 'Saving…';
  else if (profileCalHasIncomplete(st)) el.textContent = 'Not saved — a row needs a topic';
  else el.textContent = st.rows.length ? 'Saved ✓' : '';
}
// Cancel the debounce and await the newest calibration PUT; false if it
// can't be saved (validation failure or the server is unreachable).
async function profileCalFlush(uuid){
  const st = profileCalState[uuid];
  if (!st) return true;
  if (st.timer){ clearTimeout(st.timer); st.timer = null; }
  if (st.retryTimer){ clearTimeout(st.retryTimer); st.retryTimer = null; }
  while (st.dirty || st.inFlight){
    if (st.inFlight){
      await new Promise(res => setTimeout(res, 50));
    } else {
      await profileCalPush(uuid);
      if (st.failed || st.invalid) return false;
    }
  }
  // A touched-but-topicless row cannot ride the flush; the caller (e.g.
  // Duplicate) must not proceed as if everything was captured.
  return !st.failed && !st.invalid && !profileCalHasIncomplete(st);
}

// ---- duplicate (kebab) — the one-action way to mint a profile from an
// archetype. No version lineage: duplication is a convenience, not ancestry. ----
async function profileDuplicateUuid(uuid){
  // Flush pending structural edits first: the source row must exist
  // server-side, and the new row bumps the version a queued stale tree PUT
  // would 409 on (docs/ui-tree-persistence.md).
  await profileFlushPendingSave();
  if (!profileTreeSaveOk){
    profileToastMsg('Duplicate aborted — the tree could not be saved.');
    return;
  }
  const p = profileByUuid(uuid);
  if (p && !p.builtin){
    // An edit followed immediately by Duplicate must be part of the copy —
    // flat fields and both nested editors flush first.
    const flushed = await profileFlushData(uuid);
    const languagesFlushed = await profileLangFlush(uuid);
    const calFlushed = await profileCalFlush(uuid);
    if (!flushed || !languagesFlushed || !calFlushed){
      profileToastMsg('Duplicate aborted — the latest edits could not be saved.');
      return;
    }
  }
  let d = null;
  try {
    const r = await fetch('/profile/api/profiles/' + encodeURIComponent(uuid) + '/duplicate',
                          {method: 'POST'});
    d = await r.json();
  } catch (e) { /* handled below */ }
  if (!d || !d.ok){
    profileToastMsg('Duplicate failed: ' + ((d && d.error) || 'server unreachable'));
    return;
  }
  await profileLoadTree();
  profileSelectItem(d.profile.uuid);
}

// ---- dirty-guarded dismissal (clicking backdrop / Esc) ----
function profileOpenModalDirty(){
  if (!document.getElementById('profile-folder-modal').hidden){
    return document.getElementById('profile-folder-input').value.trim() !== '';
  }
  if (!document.getElementById('profile-new-modal').hidden){
    return document.getElementById('profile-new-input').value.trim() !== '';
  }
  if (!document.getElementById('profile-desc-modal').hidden){
    return document.getElementById('profile-desc-input').value !== profileDescOrig;
  }
  // Rename: dirty once the typed name differs from the stored one — only the
  // explicit Rename/Cancel buttons close it then.
  if (!document.getElementById('profile-rename-modal').hidden){
    return document.getElementById('profile-rename-input').value
      !== ((profileRenameState && profileRenameState.original) || '');
  }
  // Delete: dirty only when the type-to-confirm box is in use and non-empty;
  // a plain yes/no delete is never dirty.
  if (!document.getElementById('profile-delete-modal').hidden){
    return profileDeleteRequireName
      ? document.getElementById('profile-delete-input').value.trim() !== '' : false;
  }
  return false;
}
function profileCloseOpenModal(){
  if (!document.getElementById('profile-folder-modal').hidden){ profileCloseFolderModal(); return; }
  if (!document.getElementById('profile-new-modal').hidden){ profileCloseNewModal(); return; }
  if (!document.getElementById('profile-desc-modal').hidden){ profileCloseDescModal(); return; }
  if (!document.getElementById('profile-rename-modal').hidden){ profileCloseRenameModal(); return; }
  if (!document.getElementById('profile-delete-modal').hidden){ profileCloseDeleteModal(); return; }
  // Export is read-only, so it is never dirty and Escape always closes it.
  if (!document.getElementById('profile-export-modal').hidden){ profileCloseExportModal(); return; }
}
function profileDismissIfClean(){ if (!profileOpenModalDirty()) profileCloseOpenModal(); }

// ---- wiring + initial paint ----
profileInitTreeDnD();
profileInitDatalists();
document.querySelectorAll('#profile-form [data-key]').forEach(el => {
  el.addEventListener('input', profileFieldEdited);
  el.addEventListener('change', profileFieldEdited);
});
document.getElementById('profile-lang-add').addEventListener('click', profileLangAdd);
document.getElementById('profile-cal-add').addEventListener('click', profileCalAdd);
document.getElementById('profile-tz-mine').addEventListener('click', () => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  if (!zone) return;
  profileFieldEl('timezone').value = zone;
  profileFieldEdited();
});
document.getElementById('profile-folder-input').addEventListener('input', () => {
  document.getElementById('profile-folder-create').disabled =
    document.getElementById('profile-folder-input').value.trim() === '';
});
document.getElementById('profile-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('profile-folder-create').disabled){
    e.preventDefault(); profileAddFolderConfirm();
  }
});
document.getElementById('profile-new-input').addEventListener('input', () => {
  document.getElementById('profile-new-create').disabled =
    document.getElementById('profile-new-input').value.trim() === '';
});
document.getElementById('profile-new-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('profile-new-create').disabled){
    e.preventDefault(); profileAddProfileConfirm();
  }
});
document.getElementById('profile-rename-input').addEventListener('input', profileSyncRenameConfirm);
document.getElementById('profile-rename-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('profile-rename-confirm').disabled){
    e.preventDefault(); profileConfirmRenameModal();
  }
});
document.getElementById('ui-modal-backdrop').addEventListener('click', profileDismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') profileDismissIfClean(); });
profileLoadTree().then(() => {
  // Deep link: ?id=<uuid> selects that folder or profile on load.
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && profileFolderById(wantId)){
    profileSelectFolder(wantId);
  } else if (wantId && profileByUuid(wantId)){
    profileSelectItem(wantId);
  } else {
    profileRenderTree();
    profileRender();
  }
});
