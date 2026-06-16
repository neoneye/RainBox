// /git page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/git_views.py; this file is served at /static/git.js with an mtime
// cache-buster. State hydrates from GET /git/api/tree and saves via debounced
// whole-tree PUTs (version-guarded). Mirrors static/cron.js.

// ---- helpers ----
function gitEscapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

// ---- state (browser-only, lost on refresh) ----
let gitFolders = [];           // {id, name, description, parentId, ...}
let gitRepos = [];             // {uuid, name, folderId, path, description, ...}
let gitSelectedFolder = null;  // folder id, or null for "All repositories" / root
let gitSelectedRepo = null;    // repo uuid when a repo is selected
let gitExpanded = {};          // folder id -> false when collapsed (default expanded)
let gitDrag = null;            // {type:'folder'|'repo', id} while a node is dragged

// ---- inlined Lucide icons (https://lucide.dev), self-contained ----
const GIT_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const GIT_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';
const GIT_ICON_REPO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>';

// ---- lookups ----
function gitFolderById(id){ return gitFolders.find(f => f.id === id) || null; }
function gitRepoByUuid(uuid){ return gitRepos.find(r => r.uuid === uuid) || null; }
function gitChildFolders(parentId){ return gitFolders.filter(f => (f.parentId || null) === (parentId || null)); }
function gitReposInFolder(id){ return gitRepos.filter(r => (r.folderId || null) === (id || null)); }
function gitIsExpanded(id){ return gitExpanded[id] !== false; }
// Optimistically stamp a node as just-modified; the server sets the
// authoritative updated_at on save and a reload reconciles.
function gitTouch(node){ if (node) node.updated_at = new Date().toISOString(); }

// ---- selection ----
function gitCurrentSelectionId(){
  if (gitSelectedRepo) return gitSelectedRepo;
  if (gitSelectedFolder) return gitSelectedFolder;
  return null;
}
function gitSyncUrl(){
  // Reflect the selection in ?id= so the URL is a shareable deep link.
  const url = new URL(window.location);
  const id = gitCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function gitSelectFolder(id){
  gitSelectedFolder = id;
  gitSelectedRepo = null;
  gitRenderTree();
  gitRender();
}
function gitSelectRepo(uuid){
  const r = gitRepoByUuid(uuid);
  gitSelectedRepo = uuid;
  gitSelectedFolder = r ? (r.folderId || null) : null;
  gitRenderTree();
  gitRender();
}
function gitSelectNode(type, id){
  if (type === 'repo') gitSelectRepo(id); else gitSelectFolder(id);
}
function gitFolderClick(id){
  // First click selects; clicking the already-selected folder toggles expand.
  const wasSelected = (gitSelectedFolder === id) && !gitSelectedRepo;
  gitSelectedRepo = null;
  if (wasSelected){ gitExpanded[id] = !gitIsExpanded(id); }
  else { gitSelectedFolder = id; }
  gitRenderTree();
  gitRender();
}

// ---- right-pane render ----
function gitRender(){
  gitRenderRename();
  gitRenderFolderDesc();
  gitRenderContents();
  gitRenderRepoDetail();
  gitSyncUrl();
  const paneTitle = document.getElementById('git-pane-title');
  if (gitSelectedRepo) paneTitle.textContent = 'Repository';
  else if (gitSelectedFolder !== null) paneTitle.textContent = 'Folder';
  else paneTitle.textContent = 'All repositories';
}
// The contents table: the DIRECT children (subfolders + repos) of the selected
// folder, or of the root when nothing/`All repositories` is selected. Hidden
// while a repo is selected (the repo detail pane shows instead).
function gitRenderContents(){
  const wrap = document.getElementById('git-table-wrap');
  const repoView = !!gitSelectedRepo;
  wrap.hidden = repoView;
  if (repoView) return;
  const tb = document.getElementById('git-rows');
  tb.innerHTML = '';
  const folders = gitChildFolders(gitSelectedFolder);
  const repos = gitReposInFolder(gitSelectedFolder);
  if (!folders.length && !repos.length){
    tb.innerHTML = '<tr><td colspan="5"><i>' +
      (gitSelectedFolder === null ? 'no repositories yet' : 'empty folder') + '</i></td></tr>';
    return;
  }
  folders.forEach(f => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="git-name-cell"><span class="git-ficon">' + GIT_ICON_FOLDER + '</span> ' + gitEscapeHtml(f.name) + '</td>' +
      '<td>Folder</td><td></td><td>' + gitEscapeHtml(f.description || '') + '</td>' +
      '<td><a href="#" class="row-open">Open</a></td>';
    tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); gitSelectFolder(f.id); });
    tb.appendChild(tr);
  });
  repos.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="git-name-cell"><span class="git-ficon">' + GIT_ICON_REPO + '</span> ' + gitEscapeHtml(r.name) + '</td>' +
      '<td>Repo</td><td><code>' + gitEscapeHtml(r.path) + '</code></td><td>' + gitEscapeHtml(r.description || '') + '</td>' +
      '<td><a href="#" class="row-open">Open</a></td>';
    tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); gitSelectRepo(r.uuid); });
    tb.appendChild(tr);
  });
}
// Repo detail: filesystem path + current branch header, then the root listing.
// Live data is fetched from /git/api/repos/<uuid>/detail (uuid-guarded so a
// stale response for a previously-selected repo is dropped).
function gitRenderRepoDetail(){
  const el = document.getElementById('git-repo-detail');
  const r = gitSelectedRepo ? gitRepoByUuid(gitSelectedRepo) : null;
  if (!r){ el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML =
    '<div class="git-repo-head">' +
      '<div><span class="muted">Path:</span> <code>' + gitEscapeHtml(r.path) + '</code></div>' +
      '<div><span class="muted">Branch:</span> <span id="git-repo-branch" class="muted">loading…</span></div>' +
    '</div>' +
    '<div id="git-repo-listing"><span class="muted">loading…</span></div>';
  gitLoadRepoDetail(r.uuid);
}
async function gitLoadRepoDetail(uuid){
  let d = null;
  try {
    const r = await fetch('/git/api/repos/' + encodeURIComponent(uuid) + '/detail');
    d = await r.json();
  } catch (e) { /* fall through to the unavailable message */ }
  if (gitSelectedRepo !== uuid) return;  // selection moved on; drop this response
  const branchEl = document.getElementById('git-repo-branch');
  const listEl = document.getElementById('git-repo-listing');
  if (!branchEl || !listEl) return;
  if (!d || !d.ok){ branchEl.textContent = '(unavailable)'; listEl.innerHTML = '<span class="muted">(repository unavailable)</span>'; return; }
  if (!d.exists){ branchEl.textContent = '(path not found)'; listEl.innerHTML = '<span class="muted">The path no longer exists on disk.</span>'; return; }
  if (!d.isRepo){ branchEl.textContent = '(not a git repo)'; listEl.innerHTML = '<span class="muted">This path is no longer a git repository.</span>'; return; }
  branchEl.textContent = d.branch || '(detached)';
  branchEl.classList.remove('muted');
  if (!d.entries.length){ listEl.innerHTML = '<span class="muted">(empty)</span>'; return; }
  listEl.innerHTML = '<ul class="git-flist">' + d.entries.map(e =>
    '<li><span class="git-ficon">' + (e.isDir ? GIT_ICON_FOLDER : '') + '</span>' +
    gitEscapeHtml(e.name) + (e.isDir ? '/' : '') + '</li>').join('') + '</ul>';
}
// Rename field for the selected folder or repo. Changes the display name only;
// for a repo it never touches the directory on disk.
function gitRenderRename(){
  const el = document.getElementById('git-node-rename');
  el.innerHTML = '';
  let node = null, kind = null;
  if (gitSelectedRepo){ node = gitRepoByUuid(gitSelectedRepo); kind = 'repo'; }
  else if (gitSelectedFolder !== null){ node = gitFolderById(gitSelectedFolder); kind = 'folder'; }
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const input = document.createElement('input');
  input.type = 'text'; input.id = 'git-rename-field'; input.value = node.name;
  const btn = document.createElement('button');
  btn.textContent = 'Rename';
  const doRename = () => {
    const v = input.value.trim();
    if (!v) return;
    node.name = v;
    gitTouch(node);
    gitRenderTree();
    gitRender();
    gitSave();
  };
  btn.addEventListener('click', doRename);
  input.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); doRename(); } });
  el.appendChild(input); el.appendChild(btn);
  void kind;  // kind is informational; behavior is identical for folder/repo
}
// Description (folder or repo): read-only value + Edit button (overlay edits).
function gitCurrentDescNode(){
  if (gitSelectedRepo) return gitRepoByUuid(gitSelectedRepo);
  if (gitSelectedFolder !== null) return gitFolderById(gitSelectedFolder);
  return null;
}
function gitFillDescValue(el, text){
  if (text){ el.textContent = text; el.classList.remove('muted'); }
  else { el.textContent = '(none)'; el.classList.add('muted'); }
}
function gitRenderFolderDesc(){
  const el = document.getElementById('git-folder-desc');
  el.innerHTML = '';
  const node = gitCurrentDescNode();
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const lbl = document.createElement('span'); lbl.className = 'muted'; lbl.textContent = 'Description:';
  const val = document.createElement('span'); gitFillDescValue(val, node.description);
  const btn = document.createElement('button'); btn.textContent = 'Edit description';
  btn.addEventListener('click', gitEditDescription);
  el.appendChild(lbl); el.appendChild(val); el.appendChild(btn);
}
let gitDescOrig = '';
function gitEditDescription(){
  const node = gitCurrentDescNode();
  if (!node) return;
  gitDescOrig = node.description || '';
  document.getElementById('git-desc-input').value = gitDescOrig;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('git-desc-modal').hidden = false;
  document.getElementById('git-desc-input').focus();
}
function gitCloseDescModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('git-desc-modal').hidden = true;
}
function gitSaveDescription(){
  const node = gitCurrentDescNode();
  if (node){ node.description = document.getElementById('git-desc-input').value; gitTouch(node); }
  gitCloseDescModal();
  gitRender();
  gitSave();
}

// ---- left tree ----
function gitRenderTree(){
  document.getElementById('git-all').className =
    'git-node' + ((gitSelectedFolder === null && !gitSelectedRepo) ? ' sel' : '');
  const root = document.getElementById('git-tree-root');
  root.innerHTML = '';
  gitChildFolders(null).forEach(f => root.appendChild(gitFolderLi(f)));
  gitReposInFolder(null).forEach(r => {
    const li = document.createElement('li'); li.appendChild(gitRepoNode(r)); root.appendChild(li);
  });
}
function gitFolderLi(f){
  const li = document.createElement('li');
  const kids = gitChildFolders(f.id);
  const repos = gitReposInFolder(f.id);
  const hasKids = (kids.length + repos.length) > 0;
  const expanded = gitIsExpanded(f.id);
  const node = document.createElement('div');
  const selected = (gitSelectedFolder === f.id && !gitSelectedRepo);
  node.className = 'git-node' + (selected ? ' sel' : '');
  const icon = document.createElement('span');
  icon.className = 'git-ficon';
  icon.innerHTML = (expanded && hasKids) ? GIT_ICON_FOLDER_OPEN : GIT_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'git-folder-label';
  label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', () => gitFolderClick(f.id));
  gitMakeDraggable(node, 'folder', f.id);
  gitMakeFolderDrop(node, f.id);
  // The kebab (actions) only appears on the selected node, to keep the tree uncluttered.
  if (selected) gitMakeKebab(node, {
    onNewRepo: () => gitNewRepo(f.id),
    onNewSubfolder: () => gitNewSubfolder(f.id),
    onRename: () => gitKebabRename('folder', f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(gitFolderLi(c)));
    repos.forEach(r => { const rli = document.createElement('li'); rli.appendChild(gitRepoNode(r)); ul.appendChild(rli); });
    li.appendChild(ul);
  }
  return li;
}
function gitRepoNode(r){
  const n = document.createElement('div');
  const selected = (gitSelectedRepo === r.uuid);
  n.className = 'git-repo-node' + (selected ? ' sel' : '');
  n.title = r.path || r.name;
  // No repo icon in the tree — every leaf here is a git repo, so the icon is noise.
  const label = document.createElement('span'); label.className = 'git-repo-label'; label.textContent = r.name;
  n.appendChild(label);
  n.addEventListener('click', () => gitSelectRepo(r.uuid));
  gitMakeDraggable(n, 'repo', r.uuid);
  gitMakeRepoDrop(n, r.uuid);
  // The kebab (actions) only appears on the selected node, to keep the tree uncluttered.
  if (selected) gitMakeKebab(n, { onRename: () => gitKebabRename('repo', r.uuid) });
  return n;
}
// Kebab "Rename" selects the node and focuses the right-pane rename field.
function gitKebabRename(type, id){
  gitSelectNode(type, id);
  const field = document.getElementById('git-rename-field');
  if (field){ field.focus(); field.select(); }
}
// 3-dot overflow menu. opts: { onNewRepo?, onNewSubfolder?, onRename? }.
// Folders get all three; repos get rename only. No Delete (deferred).
function gitMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button'; kebab.className = 'git-kebab';
  kebab.setAttribute('aria-label', 'Item actions'); kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'git-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
  const items = [];
  if (opts.onNewRepo) items.push(['New repo', opts.onNewRepo]);
  if (opts.onNewSubfolder) items.push(['New subfolder', opts.onNewSubfolder]);
  if (opts.onRename) items.push(['Rename', opts.onRename]);
  items.forEach(spec => {
    const item = document.createElement('button');
    item.type = 'button'; item.className = 'item'; item.setAttribute('role', 'menuitem');
    item.textContent = spec[0];
    item.addEventListener('click', e => { e.stopPropagation(); menu.hidden = true; spec[1](); });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', e => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    document.querySelectorAll('.git-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      const r = kebab.getBoundingClientRect();
      menu.style.left = r.left + 'px';
      menu.style.top = (r.bottom + 4) + 'px';
      menu.hidden = false;
    }
  });
  node.appendChild(kebab); node.appendChild(menu);
}

// ---- add folder / add repo ----
let gitAddFolderAsSub = false;
function gitAddFolder(asSub){
  gitAddFolderAsSub = !!asSub;
  document.getElementById('git-folder-title').textContent = asSub ? 'New subfolder' : 'New folder';
  const input = document.getElementById('git-folder-input');
  input.value = '';
  document.getElementById('git-folder-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('git-folder-modal').hidden = false;
  input.focus();
}
function gitCloseFolderModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('git-folder-modal').hidden = true;
}
function gitAddFolderConfirm(){
  const name = document.getElementById('git-folder-input').value.trim();
  if (!name) return;
  const parentId = gitAddFolderAsSub ? gitSelectedFolder : null;
  const id = crypto.randomUUID();
  gitFolders.push({id: id, name: name, description: '', parentId: parentId});
  if (parentId) gitExpanded[parentId] = true;
  gitCloseFolderModal();
  gitSelectFolder(id);
  gitSave();
}
function gitNewSubfolder(folderId){ gitSelectFolder(folderId); gitAddFolder(true); }
function gitNewRepo(folderId){ gitSelectFolder(folderId); gitAddRepo(); }
function gitAddRepo(){
  document.getElementById('git-repo-name').value = '';
  document.getElementById('git-repo-path').value = '';
  document.getElementById('git-repo-err').textContent = '';
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('git-repo-modal').hidden = false;
  document.getElementById('git-repo-path').focus();
}
function gitCloseRepoModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('git-repo-modal').hidden = true;
}
// Validate the path is a real git repo (server-side) before creating the node.
// The repo is added into the currently-selected folder (null = root).
async function gitAddRepoConfirm(){
  const name = document.getElementById('git-repo-name').value.trim();
  const path = document.getElementById('git-repo-path').value.trim();
  const err = document.getElementById('git-repo-err');
  err.textContent = '';
  if (!path){ err.textContent = 'Path is required.'; return; }
  let res = null;
  try {
    const r = await fetch('/git/api/check-path', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path}),
    });
    res = await r.json();
  } catch (e) { err.textContent = 'Could not reach the server.'; return; }
  if (!res || !res.ok){ err.textContent = (res && res.error) || 'Not a git repository.'; return; }
  const uuid = crypto.randomUUID();
  const fallback = res.path.split('/').filter(Boolean).pop() || 'repo';
  gitRepos.push({uuid: uuid, name: name || fallback, folderId: gitSelectedFolder,
                 path: res.path, description: ''});
  gitCloseRepoModal();
  gitSelectRepo(uuid);
  gitSave();
}

// ---- drag & drop (one node at a time) ----
function gitFolderInSubtree(candidateId, rootId){
  let cur = gitFolderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? gitFolderById(cur.parentId) : null;
  }
  return false;
}
function gitMoveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && gitFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = gitFolderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  gitFolders = gitFolders.filter(x => x.id !== folderId);
  if (atStart){
    const i = gitFolders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) gitFolders.push(f); else gitFolders.splice(i, 0, f);
  } else {
    let at = gitFolders.length;
    for (let i = gitFolders.length - 1; i >= 0; i--){
      if ((gitFolders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    gitFolders.splice(at, 0, f);
  }
  gitSave();
}
function gitMoveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = gitFolderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && gitFolderInSubtree(newParent, folderId)) return;  // no cycles
  const f = gitFolderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  gitFolders = gitFolders.filter(x => x.id !== folderId);
  const ti = gitFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) gitFolders.push(f);
  else gitFolders.splice(after ? ti + 1 : ti, 0, f);
  gitSave();
}
function gitMoveRepo(repoUuid, targetFolderId, beforeRepoUuid){
  targetFolderId = targetFolderId || null;
  const idx = gitRepos.findIndex(r => r.uuid === repoUuid);
  if (idx < 0) return;
  const repo = gitRepos.splice(idx, 1)[0];
  repo.folderId = targetFolderId;
  let insertAt = beforeRepoUuid ? gitRepos.findIndex(r => r.uuid === beforeRepoUuid) : -1;
  if (insertAt < 0){
    insertAt = gitRepos.length;
    for (let i = gitRepos.length - 1; i >= 0; i--){
      if ((gitRepos[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  gitRepos.splice(insertAt, 0, repo);
  gitSave();
}
function gitMakeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    gitDrag = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('git-dragging');
    document.getElementById('git-tree').classList.add('git-dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    gitDrag = null;
    document.getElementById('git-tree').classList.remove('git-dragging-on');
    gitRenderTree();
  });
}
function gitDropInto(folderId, atStart){
  if (!gitDrag) return;
  const dragged = gitDrag;
  if (dragged.type === 'repo'){
    let beforeUuid = null;
    if (atStart){
      const first = gitRepos.find(r => (r.folderId || null) === (folderId || null) && r.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    gitMoveRepo(dragged.id, folderId, beforeUuid);
  } else {
    gitMoveFolder(dragged.id, folderId, atStart);
  }
  if (folderId) gitExpanded[folderId] = true;
  gitDrag = null;
  gitSelectNode(dragged.type, dragged.id);  // select the moved node (also renders)
}
function gitMakeFolderDrop(node, folderId){
  // Three zones on a folder: top third = reorder before, bottom third = after
  // (sibling), middle = nest into. Repos always go "into".
  const zoneOf = e => {
    if (gitDrag && gitDrag.type === 'repo') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!gitDrag) return false;
    if (gitDrag.type === 'repo') return z === 'into';
    if (folderId === gitDrag.id) return false;
    if (z === 'into') return !gitFolderInSubtree(folderId, gitDrag.id);
    const t = gitFolderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && gitFolderInSubtree(np, gitDrag.id));
  };
  const clear = () => node.classList.remove('git-drop-before', 'git-drop-after', 'git-drop-target');
  node.addEventListener('dragover', e => {
    if (!gitDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('git-drop-before', z === 'before');
    node.classList.toggle('git-drop-after', z === 'after');
    node.classList.toggle('git-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!gitDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      gitDropInto(folderId, false);
    } else {
      const draggedId = gitDrag.id;
      gitMoveFolderBeside(gitDrag.id, folderId, z === 'after');
      gitDrag = null;
      gitSelectNode('folder', draggedId);
    }
  });
}
function gitMakeRepoDrop(node, repoUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!gitDrag) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('git-drop-after', after);
    node.classList.toggle('git-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('git-drop-before', 'git-drop-after'));
  node.addEventListener('drop', e => {
    if (!gitDrag) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('git-drop-before', 'git-drop-after');
    gitDropOnRepo(repoUuid, after);
  });
}
function gitDropOnRepo(targetUuid, after){
  if (!gitDrag) return;
  if (gitDrag.type === 'repo' && gitDrag.id === targetUuid) return;
  const dragged = gitDrag;
  const target = gitRepoByUuid(targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'repo'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = gitRepos.findIndex(r => r.uuid === targetUuid);
      beforeUuid = (ti + 1 < gitRepos.length) ? gitRepos[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    gitMoveRepo(dragged.id, targetFolder, beforeUuid);
  } else {
    gitMoveFolder(dragged.id, targetFolder);
  }
  gitDrag = null;
  gitSelectNode(dragged.type, dragged.id);
}
function gitWireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (gitDrag){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (gitDrag){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); gitDropInto(null, atStart); }
  });
}
function gitInitTreeDnD(){
  const root = document.getElementById('git-tree-root');
  root.addEventListener('dragover', e => {
    if (gitDrag){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (gitDrag){ e.preventDefault(); gitDropInto(null, false); }  // empty space → end of root
  });
  gitWireRootDrop(document.getElementById('git-root-drop'), false);
  document.getElementById('git-all').addEventListener('click', () => gitSelectFolder(null));
  // Dismiss any open kebab menu on an outside click or Escape.
  document.addEventListener('click', () => {
    document.querySelectorAll('.git-menu').forEach(m => { m.hidden = true; });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.git-menu').forEach(m => { m.hidden = true; });
  });
}

// ---- persistence ----
async function gitLoadTree(){
  try {
    const r = await fetch('/git/api/tree');
    const data = await r.json();
    gitFolders = (data && data.folders) || [];
    gitRepos = (data && data.repos) || [];
    gitTreeVersion = (data && data.version) || null;
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    gitFolders = []; gitRepos = []; gitTreeVersion = null;
  }
}
let gitToastTimer = null;
function gitToast(text){
  const el = document.getElementById('git-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(gitToastTimer);
  gitToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let gitSaveTimer = null;
let gitTreeVersion = null;     // token from hydrate; PUTs echo it (stale → 409)
let gitPendingDeletes = 0;     // deletions since the last save (declared to the server)
let gitSaveInFlight = false;
let gitSaveQueued = false;
function gitSave(){
  clearTimeout(gitSaveTimer);
  gitSaveTimer = setTimeout(gitSavePush, 250);  // coalesce bursts into one PUT
}
async function gitSavePush(){
  if (gitSaveInFlight){ gitSaveQueued = true; return; }  // serialize PUTs
  gitSaveInFlight = true;
  try {
    const r = await fetch('/git/api/tree', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({folders: gitFolders, repos: gitRepos,
                            version: gitTreeVersion, deletes: gitPendingDeletes}),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      // Another tab/editor changed the tree; their version wins — re-hydrate.
      await gitLoadTree();
      gitPendingDeletes = 0;
      if (gitSelectedRepo && !gitRepoByUuid(gitSelectedRepo)) gitSelectedRepo = null;
      if (gitSelectedFolder && !gitFolderById(gitSelectedFolder)) gitSelectedFolder = null;
      gitRenderTree();
      gitRender();
      gitToast('Git tree was changed elsewhere — reloaded. Your last edit was not saved.');
    } else if (!r.ok){
      gitToast('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      gitTreeVersion = (j && j.version) || gitTreeVersion;
      gitPendingDeletes = 0;
    }
  } catch (e) {
    // Network error: keep local state + version; the next edit retries.
  } finally {
    gitSaveInFlight = false;
    if (gitSaveQueued){ gitSaveQueued = false; gitSavePush(); }
  }
}

// ---- dirty-guarded dismissal (clicking backdrop / Esc) ----
function gitOpenModalDirty(){
  if (!document.getElementById('git-folder-modal').hidden){
    return document.getElementById('git-folder-input').value.trim() !== '';
  }
  if (!document.getElementById('git-repo-modal').hidden){
    return document.getElementById('git-repo-name').value.trim() !== ''
      || document.getElementById('git-repo-path').value.trim() !== '';
  }
  if (!document.getElementById('git-desc-modal').hidden){
    return document.getElementById('git-desc-input').value !== gitDescOrig;
  }
  return false;
}
function gitCloseOpenModal(){
  if (!document.getElementById('git-folder-modal').hidden){ gitCloseFolderModal(); return; }
  if (!document.getElementById('git-repo-modal').hidden){ gitCloseRepoModal(); return; }
  if (!document.getElementById('git-desc-modal').hidden){ gitCloseDescModal(); return; }
}
function gitDismissIfClean(){ if (!gitOpenModalDirty()) gitCloseOpenModal(); }

// ---- wiring + initial paint ----
gitInitTreeDnD();
document.getElementById('git-folder-input').addEventListener('input', () => {
  document.getElementById('git-folder-create').disabled =
    document.getElementById('git-folder-input').value.trim() === '';
});
document.getElementById('git-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('git-folder-create').disabled){
    e.preventDefault(); gitAddFolderConfirm();
  }
});
document.getElementById('git-repo-path').addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); gitAddRepoConfirm(); }
});
document.getElementById('ui-modal-backdrop').addEventListener('click', gitDismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') gitDismissIfClean(); });
gitLoadTree().then(() => {
  // Deep link: ?id=<uuid> selects that folder or repo on load.
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && gitFolderById(wantId)){
    gitSelectFolder(wantId);
  } else if (wantId && gitRepoByUuid(wantId)){
    gitSelectRepo(wantId);
  } else {
    gitRenderTree();
    gitRender();
  }
});
