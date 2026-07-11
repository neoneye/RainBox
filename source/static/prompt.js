// /prompt page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/prompt_views.py; this file is served at /static/prompt.js with an
// mtime cache-buster. Tree state hydrates from GET /prompt/api/tree and saves
// via debounced whole-tree PUTs (version-guarded); prompt content autosaves
// per-prompt via PUT /prompt/api/prompts/<uuid>. Mirrors static/git.js.

// ---- helpers ----
function promptEscapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function promptShortDate(iso){
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toISOString().slice(0, 16).replace('T', ' ');
}

// ---- state ----
let promptFolders = [];          // {id, name, description, parentId, ...}
let promptItems = [];            // {uuid, name, folderId, parentUuid, ...}
let promptSelectedFolder = null; // folder id, or null for "All prompts" / root
let promptSelectedItem = null;   // prompt uuid when a prompt is selected
let promptExpanded = {};         // folder id -> false when collapsed (default expanded)
let promptDrag = null;           // {type:'folder'|'item', id} while a node is dragged
const PROMPT_EXPAND_KEY = 'prompt.expandedFolders';
try { promptExpanded = JSON.parse(localStorage.getItem(PROMPT_EXPAND_KEY)) || {}; }
catch (e) { promptExpanded = {}; }
function promptPersistExpand(){
  try { localStorage.setItem(PROMPT_EXPAND_KEY, JSON.stringify(promptExpanded)); }
  catch (e) { /* private mode etc. — expand state just won't survive reload */ }
}

// ---- inlined Lucide icons (https://lucide.dev), self-contained ----
const PROMPT_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const PROMPT_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

// ---- lookups ----
function promptFolderById(id){ return promptFolders.find(f => f.id === id) || null; }
function promptByUuid(uuid){ return promptItems.find(p => p.uuid === uuid) || null; }
function promptChildFolders(parentId){ return promptFolders.filter(f => (f.parentId || null) === (parentId || null)); }
function promptsInFolder(id){ return promptItems.filter(p => (p.folderId || null) === (id || null)); }
function promptIsExpanded(id){ return promptExpanded[id] !== false; }
// Optimistically stamp a node as just-modified; the server sets the
// authoritative updated_at on save and a reload reconciles.
function promptTouch(node){ if (node) node.updated_at = new Date().toISOString(); }

// ---- selection ----
function promptCurrentSelectionId(){
  if (promptSelectedItem) return promptSelectedItem;
  if (promptSelectedFolder) return promptSelectedFolder;
  return null;
}
function promptSyncUrl(){
  // Reflect the selection in ?id= so the URL is a shareable deep link.
  const url = new URL(window.location);
  const id = promptCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function promptSelectFolder(id){
  promptFlushContent();
  promptSelectedFolder = id;
  promptSelectedItem = null;
  promptRenderTree();
  promptRender();
}
function promptSelectItem(uuid){
  promptFlushContent();
  const p = promptByUuid(uuid);
  promptSelectedItem = uuid;
  promptSelectedFolder = p ? (p.folderId || null) : null;
  promptRenderTree();
  promptRender();
}
function promptSelectNode(type, id){
  if (type === 'item') promptSelectItem(id); else promptSelectFolder(id);
}
function promptFolderClick(id){
  // First click selects; clicking the already-selected folder toggles expand.
  const wasSelected = (promptSelectedFolder === id) && !promptSelectedItem;
  if (wasSelected){
    promptExpanded[id] = !promptIsExpanded(id);
    promptPersistExpand();
    promptRenderTree();
    promptRender();
  } else {
    promptSelectFolder(id);
  }
}

// ---- right-pane render ----
function promptRender(){
  promptRenderRename();
  promptRenderFolderDesc();
  promptRenderContents();
  promptRenderEditor();
  promptSyncUrl();
  const paneTitle = document.getElementById('prompt-pane-title');
  if (promptSelectedItem) paneTitle.textContent = 'Prompt';
  else if (promptSelectedFolder !== null) paneTitle.textContent = 'Folder';
  else paneTitle.textContent = 'All prompts';
}
// Depth-first list of everything under parentId (null = whole tree), in the
// same order as the left tree, each row tagged with its nesting `depth` — like
// /cron's cronFlattenTree (docs/ui-left-panel-tree.md §7).
function promptFlattenTree(parentId){
  parentId = parentId || null;
  const out = [];
  const walk = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    promptChildFolders(f.id).forEach(c => walk(c, depth + 1));
    promptsInFolder(f.id).forEach(p => out.push({kind: 'item', node: p, depth: depth + 1}));
  };
  promptChildFolders(parentId).forEach(f => walk(f, 0));
  promptsInFolder(parentId).forEach(p => out.push({kind: 'item', node: p, depth: 0}));
  return out;
}
function promptBasedOnLabel(p){
  if (!p.parentUuid) return '';
  const parent = promptByUuid(p.parentUuid);
  return parent ? parent.name : '(deleted)';
}
function promptRenderContents(){
  const wrap = document.getElementById('prompt-table-wrap');
  const editorView = !!promptSelectedItem;
  wrap.hidden = editorView;
  if (editorView) return;
  const tb = document.getElementById('prompt-rows');
  tb.innerHTML = '';
  // The selected folder's whole subtree (or the entire tree at the root),
  // depth-first and depth-indented, mirroring the left tree.
  const nodes = promptFlattenTree(promptSelectedFolder);
  if (!nodes.length){
    tb.innerHTML = '<tr><td colspan="4"><i>' +
      (promptSelectedFolder === null ? 'no prompts yet' : 'empty folder') + '</i></td></tr>';
    return;
  }
  nodes.forEach(item => {
    const pad = 9 + item.depth * 20;  // indent the name cell by nesting depth, like the tree
    const tr = document.createElement('tr');
    if (item.kind === 'folder'){
      // Folder rows carry the tree's folder icon in the Name cell; that (plus
      // the empty Based on/Updated cells) is what marks them as folders.
      const f = item.node;
      tr.innerHTML =
        '<td class="prompt-name-cell" style="padding-left:' + pad + 'px">' +
        '<span class="prompt-ficon">' + PROMPT_ICON_FOLDER + '</span>' + promptEscapeHtml(f.name) + '</td>' +
        '<td></td><td></td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); promptSelectFolder(f.id); });
    } else {
      const p = item.node;
      tr.innerHTML =
        '<td class="prompt-name-cell" style="padding-left:' + pad + 'px">' + promptEscapeHtml(p.name) + '</td>' +
        '<td>' + promptEscapeHtml(promptBasedOnLabel(p)) + '</td>' +
        '<td>' + promptShortDate(p.updated_at) + '</td>' +
        '<td><a href="#" class="row-open">Open</a></td>';
      tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); promptSelectItem(p.uuid); });
    }
    tb.appendChild(tr);
  });
}
// Rename field for the selected folder or prompt (the prompt's display name
// lives in the tree payload, so renaming goes through the tree save).
function promptRenderRename(){
  const el = document.getElementById('prompt-node-rename');
  el.innerHTML = '';
  let node = null;
  if (promptSelectedItem) node = promptByUuid(promptSelectedItem);
  else if (promptSelectedFolder !== null) node = promptFolderById(promptSelectedFolder);
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const input = document.createElement('input');
  input.type = 'text'; input.id = 'prompt-rename-field'; input.value = node.name;
  const btn = document.createElement('button');
  btn.textContent = 'Rename';
  const doRename = () => {
    const v = input.value.trim();
    if (!v) return;
    node.name = v;
    promptTouch(node);
    promptRenderTree();
    promptRender();
    promptSave();
  };
  btn.addEventListener('click', doRename);
  input.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); doRename(); } });
  el.appendChild(input); el.appendChild(btn);
}
// Description: folders only (prompts have no description field).
function promptFillDescValue(el, text){
  if (text){ el.textContent = text; el.classList.remove('muted'); }
  else { el.textContent = '(none)'; el.classList.add('muted'); }
}
function promptRenderFolderDesc(){
  const el = document.getElementById('prompt-folder-desc');
  el.innerHTML = '';
  const node = (!promptSelectedItem && promptSelectedFolder !== null)
    ? promptFolderById(promptSelectedFolder) : null;
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const lbl = document.createElement('span'); lbl.className = 'muted'; lbl.textContent = 'Description:';
  const val = document.createElement('span'); promptFillDescValue(val, node.description);
  const btn = document.createElement('button'); btn.textContent = 'Edit description';
  btn.addEventListener('click', promptEditDescription);
  el.appendChild(lbl); el.appendChild(val); el.appendChild(btn);
}
let promptDescOrig = '';
function promptEditDescription(){
  const node = promptSelectedFolder !== null ? promptFolderById(promptSelectedFolder) : null;
  if (!node) return;
  promptDescOrig = node.description || '';
  document.getElementById('prompt-desc-input').value = promptDescOrig;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('prompt-desc-modal').hidden = false;
  document.getElementById('prompt-desc-input').focus();
}
function promptCloseDescModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('prompt-desc-modal').hidden = true;
}
function promptSaveDescription(){
  const node = promptSelectedFolder !== null ? promptFolderById(promptSelectedFolder) : null;
  if (node){ node.description = document.getElementById('prompt-desc-input').value; promptTouch(node); }
  promptCloseDescModal();
  promptRender();
  promptSave();
}

// ---- editor pane (selected prompt: based-on line, toolbar, textarea, diff) ----
let promptEditorUuid = null;   // uuid whose content the textarea currently holds
let promptDiffOpen = false;
function promptRenderEditor(){
  const el = document.getElementById('prompt-editor');
  const p = promptSelectedItem ? promptByUuid(promptSelectedItem) : null;
  if (!p){
    el.hidden = true;
    promptEditorUuid = null;
    promptDiffOpen = false;
    return;
  }
  el.hidden = false;
  // Based-on line: link to the parent version (in-page selection), or a muted
  // "(none)" / "(deleted)" when there is no navigable parent.
  const basedOn = document.getElementById('prompt-based-on');
  basedOn.innerHTML = 'Based on: ';
  if (!p.parentUuid){
    basedOn.appendChild(document.createTextNode('(none — this is an original)'));
  } else if (promptByUuid(p.parentUuid)){
    const a = document.createElement('a');
    a.href = '/prompt?id=' + encodeURIComponent(p.parentUuid);
    a.textContent = promptByUuid(p.parentUuid).name;
    a.addEventListener('click', e => { e.preventDefault(); promptSelectItem(p.parentUuid); });
    basedOn.appendChild(a);
  } else {
    basedOn.appendChild(document.createTextNode('(deleted)'));
  }
  promptRenderDates(p);
  document.getElementById('prompt-diff-btn').disabled = !p.parentUuid;
  document.getElementById('prompt-diff-btn').title =
    p.parentUuid ? '' : 'This prompt was not cloned from another, so there is nothing to diff against.';
  // Close any open diff when the selection changes; reopening refetches.
  if (promptEditorUuid !== p.uuid) promptDiffOpen = false;
  promptApplyDiffVisibility();
  if (promptEditorUuid !== p.uuid){
    promptEditorUuid = p.uuid;
    promptLoadContent(p.uuid);
  }
}
// A just-created prompt has no timestamps until the server assigns them (the
// content fetch backfills below) — show nothing rather than bare labels.
function promptRenderDates(p){
  const parts = [];
  if (p.created_at) parts.push('created ' + promptShortDate(p.created_at));
  if (p.updated_at) parts.push('updated ' + promptShortDate(p.updated_at));
  document.getElementById('prompt-dates').textContent = parts.join(' · ');
}
function promptApplyDiffVisibility(){
  document.getElementById('prompt-content').hidden = promptDiffOpen;
  document.getElementById('prompt-diff').hidden = !promptDiffOpen;
  document.getElementById('prompt-diff-against').hidden = !promptDiffOpen;
  document.getElementById('prompt-diff-btn').textContent =
    promptDiffOpen ? 'Back to editor' : 'Diff against parent';
}
async function promptLoadContent(uuid){
  const ta = document.getElementById('prompt-content');
  ta.value = '';
  ta.disabled = true;
  let d = null;
  try {
    const r = await fetch('/prompt/api/prompts/' + encodeURIComponent(uuid));
    d = await r.json();
  } catch (e) { /* fall through to the unavailable message */ }
  if (promptEditorUuid !== uuid) return;  // selection moved on; drop this response
  if (!d || !d.ok){
    // A just-created prompt may not have hit the DB yet (the tree save is
    // in flight); its content is empty by construction, so an empty editor
    // is correct either way.
    ta.disabled = false;
    return;
  }
  ta.value = d.content || '';
  ta.disabled = false;
  document.getElementById('prompt-save-state').textContent = '';
  // Backfill server-assigned timestamps onto the local tree row (a client-side
  // created row has none until now) and refresh the dates line.
  const local = promptByUuid(uuid);
  if (local){
    if (d.created_at) local.created_at = d.created_at;
    if (d.updated_at) local.updated_at = d.updated_at;
    if (promptSelectedItem === uuid) promptRenderDates(local);
  }
}

// ---- content autosave (debounced per-prompt PUT; last write wins) ----
let promptContentTimer = null;
let promptContentPending = null;   // {uuid, content} not yet PUT
function promptSetSaveState(text){
  document.getElementById('prompt-save-state').textContent = text;
}
function promptContentEdited(){
  if (!promptEditorUuid) return;
  promptContentPending = {uuid: promptEditorUuid,
                          content: document.getElementById('prompt-content').value};
  promptSetSaveState('Saving…');
  clearTimeout(promptContentTimer);
  promptContentTimer = setTimeout(promptContentPush, 600);
}
async function promptContentPush(){
  clearTimeout(promptContentTimer);
  const pending = promptContentPending;
  promptContentPending = null;
  if (!pending) return;
  try {
    const r = await fetch('/prompt/api/prompts/' + encodeURIComponent(pending.uuid), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: pending.content}),
    });
    if (r.ok){
      promptTouch(promptByUuid(pending.uuid));
      if (promptEditorUuid === pending.uuid && !promptContentPending) promptSetSaveState('Saved');
    } else {
      promptSetSaveState('Save failed');
    }
  } catch (e) {
    promptSetSaveState('Save failed — offline?');
  }
}
// Called before the selection moves away (and on page hide): push any pending
// content edit immediately so switching prompts never drops keystrokes.
function promptFlushContent(){
  if (promptContentPending) promptContentPush();
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden' && promptContentPending){
    // keepalive lets the PUT outlive the page during unload.
    const pending = promptContentPending;
    promptContentPending = null;
    clearTimeout(promptContentTimer);
    fetch('/prompt/api/prompts/' + encodeURIComponent(pending.uuid), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: pending.content}), keepalive: true,
    }).catch(() => {});
  }
});

// ---- clone (the only way to make a new version) ----
async function promptCloneCurrent(){
  if (!promptSelectedItem) return;
  await promptCloneUuid(promptSelectedItem);
}
async function promptCloneUuid(uuid){
  promptFlushContent();
  // Flush any pending structural edits first: the clone bumps the server-side
  // tree version, which would 409 a queued stale PUT.
  clearTimeout(promptSaveTimer);
  await promptSavePush();
  let d = null;
  try {
    const r = await fetch('/prompt/api/prompts/' + encodeURIComponent(uuid) + '/clone',
                          {method: 'POST'});
    d = await r.json();
  } catch (e) { /* handled below */ }
  if (!d || !d.ok){
    promptToastMsg('Clone failed: ' + ((d && d.error) || 'server unreachable'));
    return;
  }
  await promptLoadTree();
  promptSelectItem(d.prompt.uuid);
}

// ---- new chat (a direct /chat room linked to this prompt version) ----
async function promptNewChat(){
  const p = promptSelectedItem ? promptByUuid(promptSelectedItem) : null;
  if (!p) return;
  promptFlushContent();  // the room resolves content server-side; store the newest text first
  const btn = document.getElementById('prompt-newchat-btn');
  btn.disabled = true;
  try {
    const created = await fetch('/chat/api/rooms', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: p.name || 'direct chat', room_type: 'direct'}),
    });
    const room = await created.json();
    if (!created.ok || !room.uuid) throw new Error((room && room.description) || 'room create failed');
    const linked = await fetch('/chat/api/rooms/' + encodeURIComponent(room.uuid) + '/settings', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt_uuid: p.uuid}),
    });
    if (!linked.ok) throw new Error('room created but linking the prompt failed');
    window.location.href = '/chat?id=' + encodeURIComponent(room.uuid);
  } catch (e) {
    promptToastMsg('New chat failed: ' + e.message);
    btn.disabled = false;
  }
}

// ---- diff view ----
async function promptToggleDiff(){
  if (!promptSelectedItem) return;
  if (promptDiffOpen){
    promptDiffOpen = false;
    promptApplyDiffVisibility();
    return;
  }
  promptDiffOpen = true;
  promptApplyDiffVisibility();
  await promptLoadDiff(promptSelectedItem, null);
}
function promptDiffAgainstChanged(){
  const sel = document.getElementById('prompt-diff-against');
  if (promptSelectedItem && sel.value) promptLoadDiff(promptSelectedItem, sel.value);
}
async function promptLoadDiff(uuid, againstUuid){
  promptFlushContent();
  const box = document.getElementById('prompt-diff');
  box.innerHTML = '<div class="prompt-diff-line muted">loading…</div>';
  let d = null;
  try {
    let url = '/prompt/api/prompts/' + encodeURIComponent(uuid) + '/diff';
    if (againstUuid) url += '?against=' + encodeURIComponent(againstUuid);
    const r = await fetch(url);
    d = await r.json();
  } catch (e) { /* handled below */ }
  if (promptSelectedItem !== uuid || !promptDiffOpen) return;  // moved on
  if (!d || !d.ok){
    box.innerHTML = '<div class="prompt-diff-line muted">' +
      promptEscapeHtml((d && d.error) || 'diff unavailable') + '</div>';
    return;
  }
  // Ancestor picker: parent first, oldest last; marks the one being shown.
  const sel = document.getElementById('prompt-diff-against');
  sel.innerHTML = '';
  d.ancestors.forEach((a, i) => {
    const opt = document.createElement('option');
    opt.value = a.uuid;
    opt.textContent = (i === 0 ? 'parent: ' : '') + (a.name || a.uuid.slice(0, 8)) +
      (a.created_at ? ' (' + promptShortDate(a.created_at) + ')' : '');
    if (a.uuid === d.against.uuid) opt.selected = true;
    sel.appendChild(opt);
  });
  box.innerHTML = '';
  if (!d.lines.length){
    box.innerHTML = '<div class="prompt-diff-line muted">(no differences)</div>';
    return;
  }
  d.lines.forEach(line => {
    const div = document.createElement('div');
    let cls = 'ctx';
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'hdr';
    else if (line.startsWith('@@')) cls = 'hunk';
    else if (line.startsWith('+')) cls = 'add';
    else if (line.startsWith('-')) cls = 'del';
    div.className = 'prompt-diff-line ' + cls;
    div.textContent = line;
    box.appendChild(div);
  });
}

// ---- left tree ----
function promptRenderTree(){
  document.getElementById('prompt-all').className =
    'prompt-node' + ((promptSelectedFolder === null && !promptSelectedItem) ? ' sel' : '');
  const root = document.getElementById('prompt-tree-root');
  root.innerHTML = '';
  promptChildFolders(null).forEach(f => root.appendChild(promptFolderLi(f)));
  promptsInFolder(null).forEach(p => {
    const li = document.createElement('li'); li.appendChild(promptItemNode(p)); root.appendChild(li);
  });
}
function promptFolderLi(f){
  const li = document.createElement('li');
  const kids = promptChildFolders(f.id);
  const leaves = promptsInFolder(f.id);
  const hasKids = (kids.length + leaves.length) > 0;
  const expanded = promptIsExpanded(f.id);
  const node = document.createElement('div');
  const selected = (promptSelectedFolder === f.id && !promptSelectedItem);
  node.className = 'prompt-node' + (selected ? ' sel' : '');
  const icon = document.createElement('span');
  icon.className = 'prompt-ficon';
  icon.innerHTML = (expanded && hasKids) ? PROMPT_ICON_FOLDER_OPEN : PROMPT_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'prompt-folder-label';
  label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', () => promptFolderClick(f.id));
  promptMakeDraggable(node, 'folder', f.id);
  promptMakeFolderDrop(node, f.id);
  // Kebab is rendered on every row but only shown (via CSS) on the selected one,
  // so row heights stay consistent — matches /cron. Add a prompt/subfolder via
  // the "+ Prompt"/"+ Folder" buttons.
  promptMakeKebab(node, {
    onRename: () => promptKebabRename('folder', f.id),
    onDelete: () => promptConfirmDeleteFolder(f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(promptFolderLi(c)));
    leaves.forEach(p => { const pli = document.createElement('li'); pli.appendChild(promptItemNode(p)); ul.appendChild(pli); });
    li.appendChild(ul);
  }
  return li;
}
function promptItemNode(p){
  const n = document.createElement('div');
  const selected = (promptSelectedItem === p.uuid);
  n.className = 'prompt-item-node' + (selected ? ' sel' : '');
  n.title = p.name;
  // No leaf icon in the tree — every leaf here is a prompt, so an icon is noise.
  const label = document.createElement('span'); label.className = 'prompt-item-label'; label.textContent = p.name;
  n.appendChild(label);
  n.addEventListener('click', () => promptSelectItem(p.uuid));
  promptMakeDraggable(n, 'item', p.uuid);
  promptMakeItemDrop(n, p.uuid);
  // Kebab on every row, shown (via CSS) only on the selected one — matches /cron.
  promptMakeKebab(n, {
    onRename: () => promptKebabRename('item', p.uuid),
    onClone: () => promptCloneUuid(p.uuid),
    onDelete: () => promptConfirmDeleteItem(p.uuid),
  });
  return n;
}
// Kebab "Rename" selects the node and focuses the right-pane rename field.
function promptKebabRename(type, id){
  promptSelectNode(type, id);
  const field = document.getElementById('prompt-rename-field');
  if (field){ field.focus(); field.select(); }
}
// 3-dot overflow menu. opts: { onRename?, onClone?, onDelete? }.
function promptMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button'; kebab.className = 'prompt-kebab';
  kebab.setAttribute('aria-label', 'Item actions'); kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'prompt-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
  const items = [];
  if (opts.onRename) items.push(['Rename', opts.onRename, '']);
  if (opts.onClone) items.push(['Clone', opts.onClone, '']);
  if (opts.onDelete) items.push(['Delete', opts.onDelete, 'danger']);
  items.forEach(spec => {
    const item = document.createElement('button');
    item.type = 'button'; item.className = 'item' + (spec[2] ? ' ' + spec[2] : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = spec[0];
    item.addEventListener('click', e => { e.stopPropagation(); menu.hidden = true; spec[1](); });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', e => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    document.querySelectorAll('.prompt-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      const r = kebab.getBoundingClientRect();
      menu.style.left = r.left + 'px';
      menu.style.top = (r.bottom + 4) + 'px';
      menu.hidden = false;
    }
  });
  node.appendChild(kebab); node.appendChild(menu);
}

// ---- add folder / add prompt ----
let promptAddFolderAsSub = false;
function promptAddFolder(asSub){
  promptAddFolderAsSub = !!asSub;
  document.getElementById('prompt-folder-title').textContent = asSub ? 'New subfolder' : 'New folder';
  const input = document.getElementById('prompt-folder-input');
  input.value = '';
  document.getElementById('prompt-folder-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('prompt-folder-modal').hidden = false;
  input.focus();
}
function promptCloseFolderModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('prompt-folder-modal').hidden = true;
}
function promptAddFolderConfirm(){
  const name = document.getElementById('prompt-folder-input').value.trim();
  if (!name) return;
  const parentId = promptAddFolderAsSub ? promptSelectedFolder : null;
  const id = crypto.randomUUID();
  promptFolders.push({id: id, name: name, description: '', parentId: parentId});
  if (parentId){ promptExpanded[parentId] = true; promptPersistExpand(); }
  promptCloseFolderModal();
  promptSelectFolder(id);
  promptSave();
}
function promptAddPrompt(){
  const input = document.getElementById('prompt-new-input');
  input.value = '';
  document.getElementById('prompt-new-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('prompt-new-modal').hidden = false;
  input.focus();
}
function promptCloseNewModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('prompt-new-modal').hidden = true;
}
// A new prompt is a lineage root: empty content (created server-side by the
// tree save), no parentUuid. It lands in the currently-selected folder. The
// tree save is flushed immediately so the editor's content fetch finds the row.
async function promptAddPromptConfirm(){
  const name = document.getElementById('prompt-new-input').value.trim();
  if (!name) return;
  const uuid = crypto.randomUUID();
  promptItems.push({uuid: uuid, name: name, folderId: promptSelectedFolder, parentUuid: null});
  promptCloseNewModal();
  clearTimeout(promptSaveTimer);
  await promptSavePush();
  promptSelectItem(uuid);
}

// ---- drag & drop (one node at a time) ----
function promptFolderInSubtree(candidateId, rootId){
  let cur = promptFolderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? promptFolderById(cur.parentId) : null;
  }
  return false;
}
function promptMoveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && promptFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = promptFolderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  promptFolders = promptFolders.filter(x => x.id !== folderId);
  if (atStart){
    const i = promptFolders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) promptFolders.push(f); else promptFolders.splice(i, 0, f);
  } else {
    let at = promptFolders.length;
    for (let i = promptFolders.length - 1; i >= 0; i--){
      if ((promptFolders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    promptFolders.splice(at, 0, f);
  }
  promptSave();
}
function promptMoveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = promptFolderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && promptFolderInSubtree(newParent, folderId)) return;  // no cycles
  const f = promptFolderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  promptFolders = promptFolders.filter(x => x.id !== folderId);
  const ti = promptFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) promptFolders.push(f);
  else promptFolders.splice(after ? ti + 1 : ti, 0, f);
  promptSave();
}
function promptMoveItem(itemUuid, targetFolderId, beforeItemUuid){
  targetFolderId = targetFolderId || null;
  const idx = promptItems.findIndex(p => p.uuid === itemUuid);
  if (idx < 0) return;
  const item = promptItems.splice(idx, 1)[0];
  item.folderId = targetFolderId;
  let insertAt = beforeItemUuid ? promptItems.findIndex(p => p.uuid === beforeItemUuid) : -1;
  if (insertAt < 0){
    insertAt = promptItems.length;
    for (let i = promptItems.length - 1; i >= 0; i--){
      if ((promptItems[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  promptItems.splice(insertAt, 0, item);
  promptSave();
}
function promptMakeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    promptDrag = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('prompt-dragging');
    document.getElementById('prompt-tree').classList.add('prompt-dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    promptDrag = null;
    document.getElementById('prompt-tree').classList.remove('prompt-dragging-on');
    promptRenderTree();
  });
}
function promptDropInto(folderId, atStart){
  if (!promptDrag) return;
  const dragged = promptDrag;
  if (dragged.type === 'item'){
    let beforeUuid = null;
    if (atStart){
      const first = promptItems.find(p => (p.folderId || null) === (folderId || null) && p.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    promptMoveItem(dragged.id, folderId, beforeUuid);
  } else {
    promptMoveFolder(dragged.id, folderId, atStart);
  }
  if (folderId){ promptExpanded[folderId] = true; promptPersistExpand(); }
  promptDrag = null;
  promptSelectNode(dragged.type, dragged.id);  // select the moved node (also renders)
}
function promptMakeFolderDrop(node, folderId){
  // Three zones on a folder: top third = reorder before, bottom third = after
  // (sibling), middle = nest into. Prompt items always go "into".
  const zoneOf = e => {
    if (promptDrag && promptDrag.type === 'item') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!promptDrag) return false;
    if (promptDrag.type === 'item') return z === 'into';
    if (folderId === promptDrag.id) return false;
    if (z === 'into') return !promptFolderInSubtree(folderId, promptDrag.id);
    const t = promptFolderById(folderId);
    const np = t ? (t.parentId || null) : null;
    return !(np && promptFolderInSubtree(np, promptDrag.id));
  };
  const clear = () => node.classList.remove('prompt-drop-before', 'prompt-drop-after', 'prompt-drop-target');
  node.addEventListener('dragover', e => {
    if (!promptDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('prompt-drop-before', z === 'before');
    node.classList.toggle('prompt-drop-after', z === 'after');
    node.classList.toggle('prompt-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!promptDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      promptDropInto(folderId, false);
    } else {
      const draggedId = promptDrag.id;
      promptMoveFolderBeside(promptDrag.id, folderId, z === 'after');
      promptDrag = null;
      promptSelectNode('folder', draggedId);
    }
  });
}
function promptMakeItemDrop(node, itemUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!promptDrag) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('prompt-drop-after', after);
    node.classList.toggle('prompt-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('prompt-drop-before', 'prompt-drop-after'));
  node.addEventListener('drop', e => {
    if (!promptDrag) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('prompt-drop-before', 'prompt-drop-after');
    promptDropOnItem(itemUuid, after);
  });
}
function promptDropOnItem(targetUuid, after){
  if (!promptDrag) return;
  if (promptDrag.type === 'item' && promptDrag.id === targetUuid) return;
  const dragged = promptDrag;
  const target = promptByUuid(targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'item'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = promptItems.findIndex(p => p.uuid === targetUuid);
      beforeUuid = (ti + 1 < promptItems.length) ? promptItems[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    promptMoveItem(dragged.id, targetFolder, beforeUuid);
  } else {
    promptMoveFolder(dragged.id, targetFolder);
  }
  promptDrag = null;
  promptSelectNode(dragged.type, dragged.id);
}
function promptWireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (promptDrag){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (promptDrag){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); promptDropInto(null, atStart); }
  });
}
function promptInitTreeDnD(){
  const root = document.getElementById('prompt-tree-root');
  root.addEventListener('dragover', e => {
    if (promptDrag){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (promptDrag){ e.preventDefault(); promptDropInto(null, false); }  // empty space → end of root
  });
  promptWireRootDrop(document.getElementById('prompt-root-drop'), false);
  document.getElementById('prompt-all').addEventListener('click', () => promptSelectFolder(null));
  // Dismiss any open kebab menu on an outside click or Escape.
  document.addEventListener('click', () => {
    document.querySelectorAll('.prompt-menu').forEach(m => { m.hidden = true; });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.prompt-menu').forEach(m => { m.hidden = true; });
  });
}

// ---- delete. Uses the same whole-tree save + declared-deletes tripwire as
// /cron: removed rows are absent from the next PUT, and promptPendingDeletes
// tells the server how many deletions to expect. Deleting a version leaves its
// clones' parentUuid dangling (they show "(deleted)"). ----
let promptDeleteOnConfirm = null;
let promptDeleteRequireName = null;
function promptOpenDeleteModal(opts){
  promptDeleteOnConfirm = opts.onConfirm;
  promptDeleteRequireName = opts.requireName || null;
  document.getElementById('prompt-delete-title').textContent = opts.title || 'Delete';
  document.getElementById('prompt-delete-msg').textContent = opts.message;
  const nameRow = document.getElementById('prompt-delete-name-row');
  const input = document.getElementById('prompt-delete-input');
  const btn = document.getElementById('prompt-delete-confirm');
  if (promptDeleteRequireName){
    nameRow.hidden = false;
    document.getElementById('prompt-delete-name').textContent = promptDeleteRequireName;
    input.value = ''; btn.disabled = true;
  } else {
    nameRow.hidden = true; btn.disabled = false;
  }
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('prompt-delete-modal').hidden = false;
  if (promptDeleteRequireName) input.focus();
}
function promptCloseDeleteModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('prompt-delete-modal').hidden = true;
  promptDeleteOnConfirm = null;
  promptDeleteRequireName = null;
}
function promptDeleteUpdateState(){
  const input = document.getElementById('prompt-delete-input');
  document.getElementById('prompt-delete-confirm').disabled =
    promptDeleteRequireName ? (input.value.trim() !== promptDeleteRequireName) : false;
}
function promptConfirmDeleteItem(uuid){
  const p = promptByUuid(uuid);
  if (!p) return;
  const kids = promptItems.filter(x => x.parentUuid === uuid).length;
  const suffix = kids
    ? ' ' + kids + (kids === 1 ? ' prompt is' : ' prompts are') +
      ' based on this version; they will keep working but lose their diff link.'
    : '';
  promptOpenDeleteModal({
    title: 'Delete prompt',
    message: 'Delete the prompt version "' + p.name + '"? This cannot be undone.' + suffix,
    onConfirm: () => promptDeleteItem(uuid),
  });
}
function promptConfirmDeleteFolder(id){
  const f = promptFolderById(id);
  if (!f) return;
  const sub = promptFlattenTree(f.id);
  const folderCount = sub.filter(n => n.kind === 'folder').length;
  const itemCount = sub.filter(n => n.kind === 'item').length;
  if (folderCount + itemCount === 0){
    promptOpenDeleteModal({
      title: 'Delete folder',
      message: 'Delete empty folder "' + f.name + '"?',
      onConfirm: () => promptDeleteFolderById(f.id),
    });
    return;
  }
  const parts = [];
  if (folderCount) parts.push(folderCount + (folderCount === 1 ? ' subfolder' : ' subfolders'));
  if (itemCount) parts.push(itemCount + (itemCount === 1 ? ' prompt' : ' prompts'));
  promptOpenDeleteModal({
    title: 'Delete folder',
    message: 'Are you sure you want to delete folder "' + f.name + '" containing ' +
      parts.join(' and ') + '? The prompts inside are deleted too. This cannot be undone.',
    requireName: f.name,
    onConfirm: () => promptDeleteFolderById(f.id),
  });
}
function promptDeleteItem(uuid){
  const before = promptItems.length;
  promptItems = promptItems.filter(p => p.uuid !== uuid);
  promptPendingDeletes += before - promptItems.length;  // declare to the save's tripwire
  if (promptSelectedItem === uuid) promptSelectedItem = null;
  promptRenderTree();
  promptRender();
  promptSave();
}
function promptDeleteFolderById(id){
  const f = promptFolderById(id);
  if (!f) return;
  // Cascade: this folder + every descendant folder + every prompt inside any of them.
  const folderIds = new Set([f.id]);
  let grew = true;
  while (grew){
    grew = false;
    promptFolders.forEach(c => {
      if (folderIds.has(c.parentId) && !folderIds.has(c.id)){ folderIds.add(c.id); grew = true; }
    });
  }
  const beforeF = promptFolders.length, beforeP = promptItems.length;
  promptFolders = promptFolders.filter(x => !folderIds.has(x.id));
  promptItems = promptItems.filter(p => !folderIds.has(p.folderId));
  promptPendingDeletes += (beforeF - promptFolders.length) + (beforeP - promptItems.length);
  if (promptSelectedItem && !promptByUuid(promptSelectedItem)) promptSelectedItem = null;
  if (folderIds.has(promptSelectedFolder)) promptSelectedFolder = f.parentId || null;
  promptRenderTree();
  promptRender();
  promptSave();
}
document.getElementById('prompt-delete-input').addEventListener('input', promptDeleteUpdateState);
document.getElementById('prompt-delete-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('prompt-delete-confirm').disabled){
    e.preventDefault();
    document.getElementById('prompt-delete-confirm').click();
  }
});
document.getElementById('prompt-delete-confirm').addEventListener('click', () => {
  const fn = promptDeleteOnConfirm;
  promptCloseDeleteModal();
  if (fn) fn();
});

// ---- persistence ----
async function promptLoadTree(){
  try {
    const r = await fetch('/prompt/api/tree');
    const data = await r.json();
    promptFolders = (data && data.folders) || [];
    promptItems = (data && data.prompts) || [];
    promptTreeVersion = (data && data.version) || null;
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    promptFolders = []; promptItems = []; promptTreeVersion = null;
  }
}
let promptToastTimer = null;
function promptToastMsg(text){
  const el = document.getElementById('prompt-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(promptToastTimer);
  promptToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let promptSaveTimer = null;
let promptTreeVersion = null;    // token from hydrate; PUTs echo it (stale → 409)
let promptPendingDeletes = 0;    // deletions since the last save (declared to the server)
let promptSaveInFlight = false;
let promptSaveQueued = false;
function promptSave(){
  clearTimeout(promptSaveTimer);
  promptSaveTimer = setTimeout(promptSavePush, 250);  // coalesce bursts into one PUT
}
async function promptSavePush(){
  if (promptSaveInFlight){ promptSaveQueued = true; return; }  // serialize PUTs
  promptSaveInFlight = true;
  try {
    const r = await fetch('/prompt/api/tree', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({folders: promptFolders, prompts: promptItems,
                            version: promptTreeVersion, deletes: promptPendingDeletes}),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      // Another tab/editor changed the tree; their version wins — re-hydrate.
      await promptLoadTree();
      promptPendingDeletes = 0;
      if (promptSelectedItem && !promptByUuid(promptSelectedItem)) promptSelectedItem = null;
      if (promptSelectedFolder && !promptFolderById(promptSelectedFolder)) promptSelectedFolder = null;
      promptRenderTree();
      promptRender();
      promptToastMsg('Prompt tree was changed elsewhere — reloaded. Your last edit was not saved.');
    } else if (!r.ok){
      promptToastMsg('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      promptTreeVersion = (j && j.version) || promptTreeVersion;
      promptPendingDeletes = 0;
    }
  } catch (e) {
    // Network error: keep local state + version; the next edit retries.
  } finally {
    promptSaveInFlight = false;
    if (promptSaveQueued){ promptSaveQueued = false; promptSavePush(); }
  }
}

// ---- dirty-guarded dismissal (clicking backdrop / Esc) ----
function promptOpenModalDirty(){
  if (!document.getElementById('prompt-folder-modal').hidden){
    return document.getElementById('prompt-folder-input').value.trim() !== '';
  }
  if (!document.getElementById('prompt-new-modal').hidden){
    return document.getElementById('prompt-new-input').value.trim() !== '';
  }
  if (!document.getElementById('prompt-desc-modal').hidden){
    return document.getElementById('prompt-desc-input').value !== promptDescOrig;
  }
  // Delete: dirty only when the type-to-confirm box is in use and non-empty;
  // a plain yes/no delete is never dirty.
  if (!document.getElementById('prompt-delete-modal').hidden){
    return promptDeleteRequireName
      ? document.getElementById('prompt-delete-input').value.trim() !== '' : false;
  }
  return false;
}
function promptCloseOpenModal(){
  if (!document.getElementById('prompt-folder-modal').hidden){ promptCloseFolderModal(); return; }
  if (!document.getElementById('prompt-new-modal').hidden){ promptCloseNewModal(); return; }
  if (!document.getElementById('prompt-desc-modal').hidden){ promptCloseDescModal(); return; }
  if (!document.getElementById('prompt-delete-modal').hidden){ promptCloseDeleteModal(); return; }
}
function promptDismissIfClean(){ if (!promptOpenModalDirty()) promptCloseOpenModal(); }

// ---- wiring + initial paint ----
promptInitTreeDnD();
document.getElementById('prompt-content').addEventListener('input', promptContentEdited);
document.getElementById('prompt-folder-input').addEventListener('input', () => {
  document.getElementById('prompt-folder-create').disabled =
    document.getElementById('prompt-folder-input').value.trim() === '';
});
document.getElementById('prompt-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('prompt-folder-create').disabled){
    e.preventDefault(); promptAddFolderConfirm();
  }
});
document.getElementById('prompt-new-input').addEventListener('input', () => {
  document.getElementById('prompt-new-create').disabled =
    document.getElementById('prompt-new-input').value.trim() === '';
});
document.getElementById('prompt-new-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('prompt-new-create').disabled){
    e.preventDefault(); promptAddPromptConfirm();
  }
});
document.getElementById('ui-modal-backdrop').addEventListener('click', promptDismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') promptDismissIfClean(); });
promptLoadTree().then(() => {
  // Deep link: ?id=<uuid> selects that folder or prompt on load.
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && promptFolderById(wantId)){
    promptSelectFolder(wantId);
  } else if (wantId && promptByUuid(wantId)){
    promptSelectItem(wantId);
  } else {
    promptRenderTree();
    promptRender();
  }
});
