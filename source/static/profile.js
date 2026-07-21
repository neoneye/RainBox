// /profile page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/profile_views.py; this file is served at /static/profile.js with an
// mtime cache-buster. Tree state hydrates from GET /profile/api/tree and saves
// via debounced whole-tree PUTs (version-guarded, projected to structural keys
// with the read-only built-ins left out); profile data autosaves through a
// separate per-profile PUT. Mirrors static/prompt.js.

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
function profileItemsInFolder(id){ return profileItems.filter(p => (p.folderId || null) === (id || null)); }
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
  profileMakeKebab(n, p.builtin ? {
    onDuplicate: () => profileDuplicateUuid(p.uuid),
  } : {
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
function profileAddFolderConfirm(){
  const name = document.getElementById('profile-folder-input').value.trim();
  if (!name) return;
  let parentId = profileAddFolderAsSub ? profileSelectedFolder : null;
  const parent = parentId ? profileFolderById(parentId) : null;
  if (parent && parent.builtin) parentId = null;  // the Templates folder can't hold user rows
  const id = crypto.randomUUID();
  profileFolders.push({id: id, name: name, description: '', parentId: parentId});
  if (parentId){ profileExpanded[parentId] = true; profilePersistExpand(); }
  profileCloseFolderModal();
  profileSelectFolder(id);
  profileSave();
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
// A new profile starts with empty data (created server-side by the tree
// save). It lands in the currently-selected folder — or at the root when the
// selection is the read-only Templates folder. The tree save is flushed
// immediately so the form's data fetch finds the row.
async function profileAddProfileConfirm(){
  const name = document.getElementById('profile-new-input').value.trim();
  if (!name) return;
  let folderId = profileSelectedFolder;
  const folder = folderId ? profileFolderById(folderId) : null;
  if (folder && folder.builtin) folderId = null;
  const uuid = crypto.randomUUID();
  profileItems.push({uuid: uuid, name: name, folderId: folderId, summary: {}});
  profileCloseNewModal();
  clearTimeout(profileSaveTimer);
  await profileSavePush();
  profileSelectItem(uuid);
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

// ---- delete. Uses the same whole-tree save + declared-deletes tripwire as
// /cron: removed rows are absent from the next PUT, and profilePendingDeletes
// tells the server how many deletions to expect. ----
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
function profileDeleteItem(uuid){
  const before = profileItems.length;
  profileItems = profileItems.filter(p => p.uuid !== uuid);
  profilePendingDeletes += before - profileItems.length;  // declare to the save's tripwire
  if (profileSelectedItem === uuid) profileSelectedItem = null;
  profileRenderTree();
  profileRender();
  profileSave();
}
function profileDeleteFolderById(id){
  const f = profileFolderById(id);
  if (!f) return;
  // Cascade: this folder + every descendant folder + every profile inside any of them.
  const folderIds = new Set([f.id]);
  let grew = true;
  while (grew){
    grew = false;
    profileFolders.forEach(c => {
      if (folderIds.has(c.parentId) && !folderIds.has(c.id)){ folderIds.add(c.id); grew = true; }
    });
  }
  const beforeF = profileFolders.length, beforeP = profileItems.length;
  profileFolders = profileFolders.filter(x => !folderIds.has(x.id));
  profileItems = profileItems.filter(p => !folderIds.has(p.folderId));
  profilePendingDeletes += (beforeF - profileFolders.length) + (beforeP - profileItems.length);
  if (profileSelectedItem && !profileByUuid(profileSelectedItem)) profileSelectedItem = null;
  if (folderIds.has(profileSelectedFolder)) profileSelectedFolder = f.parentId || null;
  profileRenderTree();
  profileRender();
  profileSave();
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
let profilePendingDeletes = 0;    // deletions since the last save (declared to the server)
let profileSaveInFlight = false;
let profileSaveQueued = false;
let profileTreeSaveOk = true;     // last structural PUT outcome (duplicate aborts on false)
function profileSave(){
  clearTimeout(profileSaveTimer);
  profileSaveTimer = setTimeout(profileSavePush, 250);  // coalesce bursts into one PUT
}
async function profileSavePush(){
  if (profileSaveInFlight){ profileSaveQueued = true; return; }  // serialize PUTs
  profileSaveInFlight = true;
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
        version: profileTreeVersion, deletes: profilePendingDeletes}),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      // Another tab/editor changed the tree; their version wins — re-hydrate.
      await profileLoadTree();
      profilePendingDeletes = 0;
      profileTreeSaveOk = false;
      if (profileSelectedItem && !profileByUuid(profileSelectedItem)) profileSelectedItem = null;
      if (profileSelectedFolder && !profileFolderById(profileSelectedFolder)) profileSelectedFolder = null;
      profileRenderTree();
      profileRender();
      profileToastMsg('Profile tree was changed elsewhere — reloaded. Your last edit was not saved.');
    } else if (!r.ok){
      profileTreeSaveOk = false;
      profileToastMsg('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      profileTreeVersion = (j && j.version) || profileTreeVersion;
      profilePendingDeletes = 0;
      profileTreeSaveOk = true;
    }
  } catch (e) {
    // Network error: keep local state + version; the next edit retries.
    profileTreeSaveOk = false;
  } finally {
    profileSaveInFlight = false;
    if (profileSaveQueued){ profileSaveQueued = false; profileSavePush(); }
  }
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
    profileCalOnSelect(p);
  } else {
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
function profileCheckLanguage(v){
  try { Intl.getCanonicalLocales(v); return null; }
  catch (e) { return 'Not a valid language tag — e.g. da, en-US, zh-Hans.'; }
}
function profileCheckCurrency(v){
  return /^[A-Za-z]{3}$/.test(v) ? null : 'Currency codes are three letters — e.g. DKK, USD, EUR.';
}
const PROFILE_SOFT_CHECKS = {
  timezone: profileCheckTimezone,
  language: profileCheckLanguage,
  language_2: profileCheckLanguage,
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
  return flat || cal;
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
  // would 409 on.
  clearTimeout(profileSaveTimer);
  await profileSavePush();
  if (!profileTreeSaveOk){
    profileToastMsg('Duplicate aborted — the tree could not be saved.');
    return;
  }
  const p = profileByUuid(uuid);
  if (p && !p.builtin){
    // An edit followed immediately by Duplicate must be part of the copy —
    // the flat fields and the calibration rows both flush first.
    const flushed = await profileFlushData(uuid);
    const calFlushed = await profileCalFlush(uuid);
    if (!flushed || !calFlushed){
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
}
function profileDismissIfClean(){ if (!profileOpenModalDirty()) profileCloseOpenModal(); }

// ---- wiring + initial paint ----
profileInitTreeDnD();
profileInitDatalists();
document.querySelectorAll('#profile-form [data-key]').forEach(el => {
  el.addEventListener('input', profileFieldEdited);
  el.addEventListener('change', profileFieldEdited);
});
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
