// /bridges page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/bridges_views.py; this file is served at /static/bridges.js with an
// mtime cache-buster. State hydrates from GET /bridges/api/tree and
// structural edits (names, placement, order) save via debounced PUTs;
// creation, deletion, enabled flags, launch mode, and policies are their own
// immediate requests (notes/ui-tree-persistence.md). Ported rule-for-rule
// from static/git.js with a third node kind: connectors are the roots,
// folders nest under one connector, bindings are the leaves.

// ---- helpers ----
function brEscapeHtml(s){
  return (s == null ? '' : String(s)).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
// POSIX single-quote so a value can never become shell syntax.
function brShellQuote(s){ return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

// ---- state (in-memory; hydrated from GET /bridges/api/tree, saved via PUT) ----
let brConnectors = [];   // {uuid, name, platform, base_url, identity, token_env, launch_mode, enabled, policy, ...}
let brFolders = [];      // {id, connectorId, parentId, name, enabled, policy, ...}
let brBindings = [];     // {uuid, connectorId, folderId, roomUuid, roomName, address, addressKey, enabled, policy, ...}
let brPlatforms = {};    // platform -> {label, available, address_fields, policy_keys, state_file_env, directory, argv, ...}
let brSel = null;        // {kind: 'connector'|'folder'|'binding', id} or null for "All bridges"
let brExpanded = {};     // connector uuid / folder id -> false when collapsed (default expanded)
let brDrag = null;       // {type:'connector'|'folder'|'binding', id, connectorId} while a node is dragged
let brStatus = null;     // last GET /services/api/status payload
let brRooms = null;      // chatrooms for the binding modal, fetched on demand
let brCoreUrl = 'http://127.0.0.1:5000';   // what this core listens on (from the tree GET)

// ---- inlined Lucide icons (https://lucide.dev), self-contained ----
const BR_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const BR_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';
const BR_ICON_PLUG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>';

// ---- lookups ----
function brConnectorByUuid(uuid){ return brConnectors.find(c => c.uuid === uuid) || null; }
function brFolderById(id){ return brFolders.find(f => f.id === id) || null; }
function brBindingByUuid(uuid){ return brBindings.find(b => b.uuid === uuid) || null; }
function brChildFolders(connectorId, parentId){
  return brFolders.filter(f => f.connectorId === connectorId && (f.parentId || null) === (parentId || null));
}
function brBindingsIn(connectorId, folderId){
  const target = folderId || null;
  if (target === null){
    // The connector root also surfaces a binding whose folderId names a
    // folder that no longer exists, so the operator can still reach it
    // (the server rejects every tree save while it dangles).
    return brBindings.filter(b => b.connectorId === connectorId &&
      ((b.folderId || null) === null || !brFolderById(b.folderId)));
  }
  return brBindings.filter(b => b.connectorId === connectorId && (b.folderId || null) === target);
}
function brIsExpanded(id){ return brExpanded[id] !== false; }
function brPlatformOf(connectorId){
  const c = brConnectorByUuid(connectorId);
  return (c && brPlatforms[c.platform]) || {address_fields: [], policy_keys: [], label: c ? c.platform : '?'};
}
// The node behind a selection: {kind, node, connectorId} or null.
function brNodeOf(kind, id){
  if (kind === 'connector'){ const c = brConnectorByUuid(id); return c ? {kind, node: c, connectorId: c.uuid} : null; }
  if (kind === 'folder'){ const f = brFolderById(id); return f ? {kind, node: f, connectorId: f.connectorId} : null; }
  if (kind === 'binding'){ const b = brBindingByUuid(id); return b ? {kind, node: b, connectorId: b.connectorId} : null; }
  return null;
}
function brSelected(){ return brSel ? brNodeOf(brSel.kind, brSel.id) : null; }
// Folder chain root -> leaf for a folder id (empty for the connector root).
function brFolderChain(folderId){
  const chain = [];
  let cur = folderId ? brFolderById(folderId) : null;
  const seen = new Set();
  while (cur && !seen.has(cur.id)){ seen.add(cur.id); chain.unshift(cur); cur = cur.parentId ? brFolderById(cur.parentId) : null; }
  return chain;
}
// effective_enabled = connector.enabled AND every ancestor folder AND the
// node's own flag (design: "Policy resolution and enabled gates").
function brEffectiveEnabled(kind, id){
  const sel = brNodeOf(kind, id);
  if (!sel) return false;
  const c = brConnectorByUuid(sel.connectorId);
  if (!c || !c.enabled) return false;
  const folderId = kind === 'folder' ? sel.node.id : (kind === 'binding' ? sel.node.folderId : null);
  if (folderId && !brFolderById(folderId)) return false;  // dangling placement fails closed
  if (brFolderChain(folderId).some(f => !f.enabled)) return false;
  return kind === 'connector' ? true : !!sel.node.enabled;
}
// Nearest-wins policy resolution over connector -> folders -> binding.
// Returns {effective: {key: value}, sources: {key: label}}.
function brResolvePolicy(kind, id){
  const sel = brNodeOf(kind, id);
  const out = {effective: {}, sources: {}};
  if (!sel) return out;
  const plat = brPlatformOf(sel.connectorId);
  (plat.policy_keys || []).forEach(k => { out.effective[k.name] = k.default; out.sources[k.name] = 'default'; });
  const layers = [];
  const c = brConnectorByUuid(sel.connectorId);
  if (c) layers.push(['connector', c.policy || {}]);
  const folderId = kind === 'folder' ? sel.node.id : (kind === 'binding' ? sel.node.folderId : null);
  brFolderChain(folderId).forEach(f => layers.push(['folder "' + f.name + '"', f.policy || {}]));
  if (kind === 'binding') layers.push(['this binding', sel.node.policy || {}]);
  if (kind === 'folder') layers[layers.length - 1][0] = 'this folder';
  if (kind === 'connector') layers[0][0] = 'this connector';
  layers.forEach(([label, policy]) => {
    Object.keys(policy || {}).forEach(name => {
      if (policy[name] !== null && policy[name] !== undefined && name in out.effective){
        out.effective[name] = policy[name]; out.sources[name] = label;
      }
    });
  });
  return out;
}
function brFormatValue(v){
  if (v === null || v === undefined) return '(inherit)';
  if (Array.isArray(v)) return v.length ? v.join(', ') : '(empty list)';
  return String(v);
}

// ---- selection ----
function brCurrentSelectionId(){ return brSel ? brSel.id : null; }
function brSyncUrl(){
  // Reflect the selection in ?id= so the URL is a shareable deep link.
  const url = new URL(window.location);
  const id = brCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function brSelectNode(kind, id){
  brSel = (kind && id) ? {kind: kind, id: id} : null;
  brRenderTree();
  brRender();
}
function brContainerClick(kind, id){
  // First click selects; clicking the already-selected container toggles expand.
  const wasSelected = brSel && brSel.kind === kind && brSel.id === id;
  if (wasSelected){ brExpanded[id] = !brIsExpanded(id); }
  else { brSel = {kind: kind, id: id}; }
  brRenderTree();
  brRender();
}

// ---- right-pane render ----
function brRender(){
  brRenderRename();
  brRenderContents();
  brRenderDetail();
  brSyncUrl();
}
// Depth-first list of everything under a container (null = whole tree), in
// the same order as the left tree, each row tagged with its nesting depth.
function brFlattenTree(kind, id){
  const out = [];
  const walkFolder = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    brChildFolders(f.connectorId, f.id).forEach(c => walkFolder(c, depth + 1));
    brBindingsIn(f.connectorId, f.id).forEach(b => out.push({kind: 'binding', node: b, depth: depth + 1}));
  };
  const walkConnector = (c, depth) => {
    out.push({kind: 'connector', node: c, depth: depth});
    brChildFolders(c.uuid, null).forEach(f => walkFolder(f, depth + 1));
    brBindingsIn(c.uuid, null).forEach(b => out.push({kind: 'binding', node: b, depth: depth + 1}));
  };
  if (!kind) brConnectors.forEach(c => walkConnector(c, 0));
  else if (kind === 'connector'){ const c = brConnectorByUuid(id); if (c){
    brChildFolders(c.uuid, null).forEach(f => walkFolder(f, 0));
    brBindingsIn(c.uuid, null).forEach(b => out.push({kind: 'binding', node: b, depth: 0}));
  } }
  else if (kind === 'folder'){ const f = brFolderById(id); if (f){
    brChildFolders(f.connectorId, f.id).forEach(c => walkFolder(c, 0));
    brBindingsIn(f.connectorId, f.id).forEach(b => out.push({kind: 'binding', node: b, depth: 0}));
  } }
  return out;
}
function brNodeLabel(kind, node){
  if (kind === 'binding') return (node.roomName || '(room missing)') + ' ↔ ' + (node.addressKey || '');
  return node.name;
}
function brEnabledCell(kind, node){
  const id = kind === 'folder' ? node.id : node.uuid;
  const local = kind === 'connector' ? node.enabled : node.enabled;
  const eff = brEffectiveEnabled(kind, id);
  return (local ? 'on' : 'off') + (local && !eff ? ' <span class="br-warn">(blocked above)</span>' : (eff ? '' : ''));
}
function brRenderContents(){
  const wrap = document.getElementById('br-table-wrap');
  const leafView = brSel && brSel.kind === 'binding';
  wrap.hidden = !!leafView;
  document.getElementById('br-intro').hidden = !!brSel;
  document.getElementById('br-contents-head').hidden = !brSel;
  if (leafView) return;
  const tb = document.getElementById('br-rows');
  tb.innerHTML = '';
  const nodes = brFlattenTree(brSel ? brSel.kind : null, brSel ? brSel.id : null);
  if (!nodes.length){
    tb.innerHTML = '<tr><td colspan="5"><i>' +
      (brSel ? 'nothing inside yet' : 'no connectors yet — add one to begin') + '</i></td></tr>';
    return;
  }
  nodes.forEach(item => {
    const pad = 9 + item.depth * 20;
    const tr = document.createElement('tr');
    const n = item.node;
    let type = '', details = '', id = '';
    if (item.kind === 'connector'){ type = 'Connector'; details = brEscapeHtml(brPlatformOf(n.uuid).label) + ' · <code>' + brEscapeHtml(n.token_env) + '</code>' + (n.launch_mode === 'manual' ? ' · manual' : ''); id = n.uuid; }
    else if (item.kind === 'folder'){ type = 'Folder'; id = n.id; }
    else { type = 'Binding'; details = 'room <b>' + brEscapeHtml(n.roomName || '(missing)') + '</b> · <code>' + brEscapeHtml(n.addressKey) + '</code>'; id = n.uuid; }
    tr.innerHTML =
      '<td class="br-name-cell" style="padding-left:' + pad + 'px">' + brEscapeHtml(brNodeLabel(item.kind, n)) + '</td>' +
      '<td>' + type + '</td><td>' + details + '</td><td>' + brEnabledCell(item.kind, n) + '</td>' +
      '<td><a href="#" class="row-open">Open</a></td>';
    tr.querySelector('.row-open').addEventListener('click', e => { e.preventDefault(); brSelectNode(item.kind, id); });
    tb.appendChild(tr);
  });
}
// The selected node's name as a click-to-rename control doubling as the pane
// heading (notes/ui-modal-rename.md). Bindings have no name of their own:
// their heading is the room and address, not renameable.
function brRenderRename(){
  const el = document.getElementById('br-node-rename');
  el.innerHTML = '';
  const sel = brSelected();
  if (!sel){ el.hidden = true; return; }
  el.hidden = false;
  if (sel.kind === 'binding'){
    const h = document.createElement('span'); h.className = 'br-heading';
    h.textContent = brNodeLabel('binding', sel.node);
    el.appendChild(h);
    return;
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'br-rename-display';
  btn.textContent = sel.node.name;
  btn.title = 'Click to rename';
  btn.addEventListener('click', () => brOpenRenameModal(sel.kind, sel.node));
  el.appendChild(btn);
}

// ---- detail pane ----
function brStatusFor(connectorId){
  const key = 'bridge:' + connectorId;
  const d = brStatus;
  if (!d) return {managed: false, rec: {state: 'unknown'}, stateDir: null, coreOnly: false};
  return {managed: !!d.managed, rec: (d.services || {})[key] || {state: 'unknown'},
          stateDir: d.launcher ? d.launcher.state_dir : null, coreOnly: !!(d.launcher && d.launcher.core_only)};
}
function brLocalStateDir(){ try { return localStorage.getItem('bridges.stateDir') || ''; } catch (e) { return ''; } }
function brSetLocalStateDir(v){ try { localStorage.setItem('bridges.stateDir', v); } catch (e) { /* private mode */ } }
// The copyable manual launch command (design: "Credentials and process
// identity"): working directory, deployment variables, the platform's
// state-file variable pointing at <state-dir>/bridge-<uuid>.json, and the
// credential named beside it — never an empty assignment that would clobber it.
function brLaunchCommand(c, stateDir){
  const plat = brPlatforms[c.platform] || {};
  if (!plat.directory || !plat.argv || !stateDir) return null;
  const stateFile = stateDir.replace(/\/+$/, '') + '/bridge-' + c.uuid + '.json';
  return 'cd source/' + plat.directory + '/\n' +
    'RAINBOX_URL=' + brShellQuote(brCoreUrl) + ' BRIDGE_CONNECTOR=' + brShellQuote(c.uuid) + ' ' +
    plat.state_file_env + '=' + brShellQuote(stateFile) + ' ' + plat.argv.join(' ') + '\n' +
    '# ' + c.token_env + ' must already be set in the launch environment (a manual run gets no value from the database); ' +
    'RAINBOX_URL is where this core listens — change it for a bridge on another host';
}
function brPolicyTable(kind, id, node){
  const sel = brNodeOf(kind, id);
  const plat = brPlatformOf(sel.connectorId);
  const res = brResolvePolicy(kind, id);
  const local = node.policy || {};
  let html = '<table class="br-policy"><thead><tr><th>Key</th><th>Here</th><th>Effective</th><th></th></tr></thead><tbody>';
  (plat.policy_keys || []).forEach(k => {
    const has = Object.prototype.hasOwnProperty.call(local, k.name) && local[k.name] !== null;
    html += '<tr><td><code>' + brEscapeHtml(k.name) + '</code><div class="br-src">' + brEscapeHtml(k.description || '') + '</div></td>' +
      '<td>' + (k.supported ? brEscapeHtml(has ? brFormatValue(local[k.name]) : '(inherit)') : '<span class="muted">not supported by ' + brEscapeHtml(plat.label) + '</span>') + '</td>' +
      '<td>' + brEscapeHtml(brFormatValue(res.effective[k.name])) + ' <span class="br-src">from ' + brEscapeHtml(res.sources[k.name]) + '</span></td>' +
      '<td>' + (k.supported ? '<button class="br-btn" data-policy="' + brEscapeHtml(k.name) + '">Edit</button>' : '') + '</td></tr>';
  });
  return html + '</tbody></table>';
}
function brEnabledRow(kind, id, node){
  const eff = brEffectiveEnabled(kind, id);
  let note = '';
  if (node.enabled && !eff) note = ' <span class="br-warn">off in effect: a level above is disabled</span>';
  else if (kind !== 'connector' && eff) note = ' <span class="muted">on in effect</span>';
  return '<label class="br-inline"><input type="checkbox" id="br-enabled" ' + (node.enabled ? 'checked' : '') + '> Enabled' + note + '</label>';
}
function brRenderDetail(){
  const el = document.getElementById('br-detail');
  const sel = brSelected();
  if (!sel){ el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  const n = sel.node;
  const plat = brPlatformOf(sel.connectorId);
  let html = '';
  if (sel.kind === 'connector'){
    const st = brStatusFor(n.uuid);
    const stateDir = st.managed && st.stateDir ? st.stateDir : brLocalStateDir();
    const cmd = brLaunchCommand(n, stateDir);
    let stateText = st.rec.state;
    if (st.rec.pid) stateText += ' (pid ' + st.rec.pid + ')';
    const stateClass = String(st.rec.state || 'unknown').replace(/\s+/g, '-');
    html +=
      '<dl class="br-kv">' +
      '<dt>Platform</dt><dd>' + brEscapeHtml(plat.label) + (plat.available === false ? ' <span class="br-warn">(no bridge implementation yet)</span>' : '') + '</dd>' +
      '<dt>Credential variable</dt><dd><code>' + brEscapeHtml(n.token_env) + '</code> <span class="muted">what the bridge process reads; the launcher fills it from the sealed value below</span></dd>' +
      '<dt>Token</dt><dd id="br-cred"><span class="muted">checking…</span></dd>' +
      (n.base_url ? '<dt>Realm URL</dt><dd><code>' + brEscapeHtml(n.base_url) + '</code></dd>' : '') +
      (n.identity ? '<dt>Identity</dt><dd>' + brEscapeHtml(n.identity) + '</dd>' : '') +
      '<dt>Connector id</dt><dd><code>' + brEscapeHtml(n.uuid) + '</code> <button class="br-btn" id="br-copy-id">Copy ID</button></dd>' +
      '</dl>' +
      '<div class="br-section"><h4>Desired state</h4>' + brEnabledRow('connector', n.uuid, n) +
      '<div class="br-inline" style="margin-top:6px"><span>Launch mode</span> <select id="br-launch-mode">' +
      '<option value="launcher"' + (n.launch_mode === 'launcher' ? ' selected' : '') + '>launcher (started by main.py)</option>' +
      '<option value="manual"' + (n.launch_mode === 'manual' ? ' selected' : '') + '>manual (you run the process)</option></select></div>' +
      (n.enabled && n.launch_mode === 'launcher' && brStatus && brStatus.managed === false ? '<div class="br-warn" style="margin-top:6px">This core is not running under the launcher, so nothing starts this bridge.</div>' : '') +
      '</div>' +
      '<div class="br-section"><h4>Process</h4><div class="br-inline">' +
      '<span class="br-state ' + brEscapeHtml(stateClass) + '">' + brEscapeHtml(stateText) + '</span>' +
      (st.rec.message ? '<span class="muted">' + brEscapeHtml(st.rec.message) + '</span>' : '') +
      (st.rec.credential_source ? '<span class="muted">credential from ' + brEscapeHtml(st.rec.credential_source) + '</span>' : '') +
      (st.coreOnly ? '<span class="br-warn">launcher started with --core-only: bridges are suppressed</span>' : '') +
      (n.launch_mode === 'manual'
        ? '<span class="muted">manual mode: restart your own process; the launcher does not own it</span>'
        : '<button class="br-btn" id="br-restart"' + (st.managed ? '' : ' disabled title="no launcher is attached to this core"') + '>Restart</button>') +
      '</div><div class="muted" style="margin-top:4px">Restart does not enable a connector. “running” means a process exists; it does not prove fresh config or successful forwarding.</div></div>' +
      '<div class="br-section"><h4>Manual launch command</h4>' +
      (st.managed && st.stateDir
        ? '<div class="muted">State directory reported by the launcher: <code>' + brEscapeHtml(st.stateDir) + '</code>, so a manual run shares the supervised run’s state file and lock. Stop the launcher-owned process first.</div>'
        : '<div class="br-inline"><span class="muted">State directory on the bridge host</span> <input type="text" id="br-state-dir" value="' + brEscapeHtml(brLocalStateDir()) + '" placeholder="/absolute/path/to/var/services" style="min-width:320px"></div>') +
      (cmd ? '<pre class="br-cmd" id="br-cmd">' + brEscapeHtml(cmd) + '</pre><div class="br-inline" style="margin-top:6px"><button class="br-btn" id="br-copy-cmd">Copy command</button></div>'
           : '<div class="muted">Choose a state directory to get a runnable command; it is not guessed from this checkout.</div>') +
      '</div>' +
      '<div class="br-section"><h4>Policy (connector level)</h4>' + brPolicyTable('connector', n.uuid, n) + '</div>';
  } else if (sel.kind === 'folder'){
    const c = brConnectorByUuid(n.connectorId);
    html +=
      '<dl class="br-kv"><dt>Connector</dt><dd><a href="/bridges?id=' + brEscapeHtml(n.connectorId) + '" class="br-link" data-open-connector="' + brEscapeHtml(n.connectorId) + '">' + brEscapeHtml(c ? c.name : n.connectorId) + '</a></dd>' +
      '<dt>Folder id</dt><dd><code>' + brEscapeHtml(n.id) + '</code> <button class="br-btn" id="br-copy-id">Copy ID</button></dd></dl>' +
      '<div class="br-section"><h4>Desired state</h4>' + brEnabledRow('folder', n.id, n) +
      '<div class="muted" style="margin-top:4px">Folders are organisation, not an access boundary: a folder policy can broaden or narrow what its bindings inherit.</div></div>' +
      '<div class="br-section"><h4>Policy (folder level)</h4>' + brPolicyTable('folder', n.id, n) + '</div>';
  } else {
    const c = brConnectorByUuid(n.connectorId);
    const chain = brFolderChain(n.folderId);
    const addr = n.address || {};
    html +=
      '<dl class="br-kv">' +
      '<dt>Connector</dt><dd><a href="/bridges?id=' + brEscapeHtml(n.connectorId) + '" data-open-connector="' + brEscapeHtml(n.connectorId) + '">' + brEscapeHtml(c ? c.name : n.connectorId) + '</a>' +
        (chain.length ? ' <span class="muted">/ ' + chain.map(f => brEscapeHtml(f.name)).join(' / ') + '</span>' : '') + '</dd>' +
      '<dt>Chatroom</dt><dd>' + (n.roomName ? '<a href="/chat?id=' + brEscapeHtml(n.roomUuid) + '">' + brEscapeHtml(n.roomName) + '</a>' : '<span class="br-err">room missing</span>') + ' <code class="muted">' + brEscapeHtml(n.roomUuid) + '</code></dd>' +
      '<dt>Remote address</dt><dd>' + Object.keys(addr).map(k => '<code>' + brEscapeHtml(k) + '=' + brEscapeHtml(addr[k]) + '</code>').join(' ') + ' <span class="muted">fixed after creation; delete and re-create to change it</span></dd>' +
      '<dt>Binding id</dt><dd><code>' + brEscapeHtml(n.uuid) + '</code> <button class="br-btn" id="br-copy-id">Copy ID</button></dd>' +
      '</dl>' +
      '<div class="br-section"><h4>Desired state</h4>' + brEnabledRow('binding', n.uuid, n) +
      '<div class="muted" style="margin-top:4px">Traffic flows only while the connector, every folder above, and this binding are enabled. Moving the binding to another folder changes what it inherits.</div></div>' +
      '<div class="br-section"><h4>Policy (binding level)</h4>' + brPolicyTable('binding', n.uuid, n) + '</div>';
  }
  el.innerHTML = html;
  // Wiring.
  const enabled = el.querySelector('#br-enabled');
  if (enabled) enabled.addEventListener('change', () => brSetEnabled(sel.kind, sel.kind === 'folder' ? n.id : n.uuid, enabled.checked));
  const lm = el.querySelector('#br-launch-mode');
  if (lm) lm.addEventListener('change', () => brSetLaunchMode(n.uuid, lm.value));
  const restart = el.querySelector('#br-restart');
  if (restart) restart.addEventListener('click', () => brRestart(n.uuid, restart));
  const copyId = el.querySelector('#br-copy-id');
  if (copyId) copyId.addEventListener('click', () => brCopyIdToast(sel.kind === 'folder' ? n.id : n.uuid, sel.kind));
  const copyCmd = el.querySelector('#br-copy-cmd');
  if (copyCmd) copyCmd.addEventListener('click', () => brCopyText(document.getElementById('br-cmd').textContent, 'Launch command copied'));
  const sd = el.querySelector('#br-state-dir');
  if (sd) sd.addEventListener('change', () => { brSetLocalStateDir(sd.value.trim()); brRenderDetail(); });
  el.querySelectorAll('[data-open-connector]').forEach(a => a.addEventListener('click', e => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault(); brSelectNode('connector', a.getAttribute('data-open-connector'));
  }));
  el.querySelectorAll('[data-policy]').forEach(b => b.addEventListener('click', () => brOpenPolicyModal(sel.kind, sel.kind === 'folder' ? n.id : n.uuid, b.getAttribute('data-policy'))));
  if (sel.kind === 'connector'){ brRefreshStatus(); brLoadCredential(n.uuid); }
}

// ---- credential (write-only; the page only ever learns "set" or "not set") ----
let brCredential = {};   // connector uuid -> {set, updated_at, key_configured}
async function brLoadCredential(uuid){
  try {
    const r = await fetch('/bridges/api/connectors/' + encodeURIComponent(uuid));
    const d = await r.json();
    if (r.ok) brCredential[uuid] = d.credential || {};
  } catch (e) { /* rendered as unknown */ }
  brRenderCredential(uuid);
}
function brRenderCredential(uuid){
  const el = document.getElementById('br-cred');
  const sel = brSelected();
  if (!el || !sel || sel.kind !== 'connector' || sel.node.uuid !== uuid) return;
  const c = brCredential[uuid];
  if (!c){ el.innerHTML = '<span class="muted">unknown</span>'; return; }
  let html = c.set
    ? '<span class="br-state running">set</span> <span class="muted">saved ' + brEscapeHtml((c.updated_at || '').replace('T', ' ').slice(0, 19)) + '</span> '
    : '<span class="br-state">not set</span> ';
  html += '<button class="br-btn" id="br-cred-set">' + (c.set ? 'Replace token…' : 'Set token…') + '</button>';
  if (c.set) html += ' <button class="br-btn" id="br-cred-clear">Clear</button>';
  if (c.key_configured === false){
    html += '<div class="br-warn" style="margin-top:4px">RAINBOX_CREDENTIAL_KEY is not set in the core’s environment (repo-root .env), so nothing can be sealed' +
      (c.set ? ' or opened: the launcher cannot use the stored token until the key is back' : '') +
      '. Generate one with <code>python3 -c "import secrets; print(secrets.token_urlsafe(48))"</code>, add the line, restart the core.</div>';
  }
  el.innerHTML = html;
  const setBtn = el.querySelector('#br-cred-set');
  if (setBtn) setBtn.addEventListener('click', () => brOpenCredentialModal(uuid));
  const clearBtn = el.querySelector('#br-cred-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => brConfirmClearCredential(uuid));
}
let brCredentialUuid = null;
function brOpenCredentialModal(uuid){
  const c = brConnectorByUuid(uuid);
  if (!c) return;
  brCredentialUuid = uuid;
  document.getElementById('br-credential-title').textContent = (brCredential[uuid] && brCredential[uuid].set ? 'Replace token for ' : 'Set token for ') + c.name;
  document.getElementById('br-credential-desc').textContent = 'The bridge process will read it as ' + c.token_env + '.';
  const input = document.getElementById('br-credential-input');
  input.value = '';
  document.getElementById('br-credential-err').textContent = '';
  document.getElementById('br-credential-save').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-credential-modal').hidden = false;
  input.focus();
}
function brCloseCredentialModal(){
  document.getElementById('br-credential-input').value = '';   // never keep it around
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-credential-modal').hidden = true;
  brCredentialUuid = null;
}
async function brSaveCredential(){
  const uuid = brCredentialUuid;
  const value = document.getElementById('br-credential-input').value.trim();
  const err = document.getElementById('br-credential-err');
  if (!uuid || !value) return;
  err.textContent = '';
  try {
    const r = await fetch('/bridges/api/connectors/' + encodeURIComponent(uuid) + '/credential',
      {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({value: value})});
    const d = await r.json();
    if (!r.ok){ err.textContent = d.error || 'Could not save.'; return; }
    brCredential[uuid] = d.credential || {};
    brCloseCredentialModal();
    brRenderCredential(uuid);
    const c = brConnectorByUuid(uuid);
    brToast(c && c.enabled && c.launch_mode === 'launcher' ? 'Token saved; the launcher restarts the connector with it' : 'Token saved');
    setTimeout(brRefreshStatus, 800);
  } catch (e) { err.textContent = 'Could not reach the server.'; }
}
function brConfirmClearCredential(uuid){
  const c = brConnectorByUuid(uuid);
  if (!c) return;
  brOpenDeleteModal({title: 'Clear token',
    message: 'Forget the stored token for "' + c.name + '"? A running process keeps going until its next restart; after that the launcher reports "credential missing" unless ' + c.token_env + ' is in its environment.',
    onConfirm: async () => {
      try {
        const r = await fetch('/bridges/api/connectors/' + encodeURIComponent(uuid) + '/credential', {method: 'DELETE'});
        const d = await r.json();
        if (!r.ok){ brToast(d.error || 'Could not clear.'); return; }
        brCredential[uuid] = d.credential || {};
        brRenderCredential(uuid);
        brToast('Token cleared');
      } catch (e) { brToast('Could not clear.'); }
    }});
}

// ---- per-item content writes (their own PUTs; the tree token is untouched) ----
function brItemUrl(kind, id){
  return '/bridges/api/' + (kind === 'connector' ? 'connectors' : kind === 'folder' ? 'folders' : 'bindings') + '/' + encodeURIComponent(id);
}
async function brPutItem(kind, id, body){
  const r = await fetch(brItemUrl(kind, id), {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
  const row = data.connector || data.folder || data.binding;
  const sel = brNodeOf(kind, id);
  if (sel && row) Object.assign(sel.node, row);
  return row;
}
async function brSetEnabled(kind, id, value){
  try {
    await brPutItem(kind, id, {enabled: !!value});
    brRenderTree(); brRender();
    brToast(value ? 'Enabled' : 'Disabled');
  } catch (e) { brToast('Could not save: ' + e.message); brRender(); }
}
async function brSetLaunchMode(uuid, mode){
  try {
    await brPutItem('connector', uuid, {launch_mode: mode});
    brRender();
    brToast(mode === 'manual' ? 'Manual mode: the launcher stops its process; run yours after it exits' : 'Launcher mode: stop your manual process first');
  } catch (e) { brToast('Could not save: ' + e.message); brRender(); }
}
async function brRestart(uuid, btn){
  btn.disabled = true;
  try {
    const r = await fetch('/services/api/restart/bridge:' + encodeURIComponent(uuid), {method: 'POST'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    brToast('Restart requested');
  } catch (e) { brToast('Could not request a restart: ' + e.message); }
  finally { btn.disabled = false; setTimeout(brRefreshStatus, 400); }
}
// Observed process state from GET /services/api/status, refreshed while a
// connector is selected. Never a reason to change a toggle.
let brStatusTimer = null;
async function brRefreshStatus(){
  clearTimeout(brStatusTimer);
  let d = null;
  try { const r = await fetch('/services/api/status'); d = await r.json(); } catch (e) { d = null; }
  const before = JSON.stringify(brStatus);
  brStatus = d;
  const sel = brSelected();
  if (sel && sel.kind === 'connector'){
    if (JSON.stringify(d) !== before) brRenderDetail();  // repaint only on change; keeps typed state-dir input intact
    brStatusTimer = setTimeout(brRefreshStatus, 10000);
  }
}

// ---- policy modal ----
let brPolicyState = null;   // {kind, id, key, spec}
function brOpenPolicyModal(kind, id, key){
  const sel = brNodeOf(kind, id);
  if (!sel) return;
  const spec = (brPlatformOf(sel.connectorId).policy_keys || []).find(k => k.name === key);
  if (!spec) return;
  brPolicyState = {kind, id, key, spec};
  const local = (sel.node.policy || {});
  const has = Object.prototype.hasOwnProperty.call(local, key) && local[key] !== null;
  document.getElementById('br-policy-title').textContent = key + ' at ' + (kind === 'connector' ? 'connector' : kind) + ' level';
  document.getElementById('br-policy-desc').textContent = spec.description || '';
  const res = brResolvePolicy(kind, id);
  document.getElementById('br-policy-effective').textContent =
    'Effective now: ' + brFormatValue(res.effective[key]) + ' (from ' + res.sources[key] + '); default: ' + brFormatValue(spec.default);
  document.getElementById('br-policy-err').textContent = '';
  const inherit = document.getElementById('br-policy-inherit');
  inherit.checked = !has;
  const box = document.getElementById('br-policy-control');
  const cur = has ? local[key] : spec.default;
  if (spec.type === 'bool'){
    box.innerHTML = '<label>Value<select id="br-policy-value"><option value="true">true</option><option value="false">false</option></select></label>';
    box.querySelector('select').value = cur ? 'true' : 'false';
  } else if (spec.type === 'choice'){
    box.innerHTML = '<label>Value<select id="br-policy-value">' + spec.choices.map(c => '<option value="' + brEscapeHtml(c) + '">' + brEscapeHtml(c) + '</option>').join('') + '</select></label>';
    box.querySelector('select').value = cur;
  } else if (spec.type === 'number'){
    box.innerHTML = '<label>Value<input type="number" id="br-policy-value" step="any"' +
      (spec.minimum != null ? ' min="' + spec.minimum + '"' : '') + (spec.maximum != null ? ' max="' + spec.maximum + '"' : '') + '></label>';
    box.querySelector('input').value = cur;
  } else {
    if (spec.choices && spec.choices.length){
      box.innerHTML = '<label>Values</label>' + spec.choices.map(c =>
        '<label class="br-check"><input type="checkbox" data-choice="' + brEscapeHtml(c) + '"' + ((cur || []).includes(c) ? ' checked' : '') + '> ' + brEscapeHtml(c) + '</label>').join('');
    } else {
      box.innerHTML = '<label>Values, one per line<textarea id="br-policy-value" placeholder="one id per line; leave empty for an empty list"></textarea></label>';
      box.querySelector('textarea').value = (cur || []).join('\n');
    }
  }
  brSyncPolicyControl();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-policy-modal').hidden = false;
}
function brSyncPolicyControl(){
  const inherit = document.getElementById('br-policy-inherit').checked;
  document.querySelectorAll('#br-policy-control input, #br-policy-control select, #br-policy-control textarea').forEach(el => { el.disabled = inherit; });
}
function brClosePolicyModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-policy-modal').hidden = true;
  brPolicyState = null;
}
function brReadPolicyControl(spec){
  if (spec.type === 'bool') return document.getElementById('br-policy-value').value === 'true';
  if (spec.type === 'choice') return document.getElementById('br-policy-value').value;
  if (spec.type === 'number'){
    const v = Number(document.getElementById('br-policy-value').value);
    if (!isFinite(v) || document.getElementById('br-policy-value').value.trim() === '') throw new Error('enter a number');
    return v;
  }
  if (spec.choices && spec.choices.length){
    return Array.from(document.querySelectorAll('#br-policy-control [data-choice]')).filter(cb => cb.checked).map(cb => cb.getAttribute('data-choice'));
  }
  return document.getElementById('br-policy-value').value.split('\n').map(s => s.trim()).filter(Boolean);
}
async function brSavePolicy(){
  if (!brPolicyState) return;
  const {kind, id, key, spec} = brPolicyState;
  const sel = brNodeOf(kind, id);
  if (!sel) { brClosePolicyModal(); return; }
  const policy = Object.assign({}, sel.node.policy || {});
  const err = document.getElementById('br-policy-err');
  try {
    if (document.getElementById('br-policy-inherit').checked) delete policy[key];
    else policy[key] = brReadPolicyControl(spec);
  } catch (e) { err.textContent = e.message; return; }
  try {
    await brPutItem(kind, id, {policy: policy});
  } catch (e) { err.textContent = e.message; return; }
  brClosePolicyModal();
  brRenderTree(); brRender();
  brToast('Policy saved');
}

// ---- rename modal (notes/ui-modal-rename.md) ----
let brRenameState = null;   // {kind: 'connector'|'folder', id, original}
function brOpenRenameModal(kind, node){
  brRenameState = {kind: kind, id: kind === 'folder' ? node.id : node.uuid, original: node.name};
  document.getElementById('br-rename-title').textContent = kind === 'connector' ? 'Rename connector' : 'Rename folder';
  const input = document.getElementById('br-rename-input');
  input.value = node.name;
  brSyncRenameConfirm();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-rename-modal').hidden = false;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}
function brCloseRenameModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-rename-modal').hidden = true;
  brRenameState = null;
}
function brSyncRenameConfirm(){
  const v = document.getElementById('br-rename-input').value.trim();
  document.getElementById('br-rename-confirm').disabled = v === '' || !brRenameState || v === brRenameState.original;
}
function brConfirmRenameModal(){
  if (!brRenameState) return;
  const v = document.getElementById('br-rename-input').value.trim();
  if (!v || v === brRenameState.original) return;
  const sel = brNodeOf(brRenameState.kind, brRenameState.id);
  brCloseRenameModal();
  if (!sel) return;
  sel.node.name = v;
  brRenderTree();
  brRender();
  brSave();
  brToast('Renamed to “' + v + '”');
}

// ---- left tree ----
function brRenderTree(){
  document.getElementById('br-all').className = 'br-node' + (brSel ? '' : ' sel');
  const root = document.getElementById('br-tree-root');
  root.innerHTML = '';
  brConnectors.forEach(c => root.appendChild(brConnectorLi(c)));
  // "+ Folder" / "+ Binding" need a connector context.
  const ctx = brSelected();
  document.getElementById('br-add-folder-btn').disabled = !ctx;
  document.getElementById('br-add-binding-btn').disabled = !ctx;
}
function brIsSel(kind, id){ return !!(brSel && brSel.kind === kind && brSel.id === id); }
function brConnectorLi(c){
  const li = document.createElement('li');
  const kids = brChildFolders(c.uuid, null);
  const leaves = brBindingsIn(c.uuid, null);
  const hasKids = (kids.length + leaves.length) > 0;
  const expanded = brIsExpanded(c.uuid);
  const node = document.createElement('a');
  node.className = 'br-node br-connector' + (brIsSel('connector', c.uuid) ? ' sel' : '') + (c.enabled ? '' : ' br-off');
  node.href = '/bridges?id=' + encodeURIComponent(c.uuid);
  node.title = brPlatformOf(c.uuid).label + ' · ' + c.token_env;
  const icon = document.createElement('span'); icon.className = 'br-ficon'; icon.innerHTML = BR_ICON_PLUG;
  const label = document.createElement('span'); label.className = 'br-folder-label'; label.textContent = c.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    brContainerClick('connector', c.uuid);
  });
  brMakeDraggable(node, 'connector', c.uuid, c.uuid);
  brMakeConnectorDrop(node, c.uuid);
  brMakeKebab(node, {
    onRename: () => brKebabRename('connector', c.uuid),
    onCopyId: () => brCopyIdToast(c.uuid, 'connector'),
    onDelete: () => brConfirmDeleteConnector(c.uuid),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(f => ul.appendChild(brFolderLi(f)));
    leaves.forEach(b => { const bli = document.createElement('li'); bli.appendChild(brBindingNode(b)); ul.appendChild(bli); });
    li.appendChild(ul);
  }
  return li;
}
function brFolderLi(f){
  const li = document.createElement('li');
  const kids = brChildFolders(f.connectorId, f.id);
  const leaves = brBindingsIn(f.connectorId, f.id);
  const hasKids = (kids.length + leaves.length) > 0;
  const expanded = brIsExpanded(f.id);
  const node = document.createElement('a');
  node.className = 'br-node' + (brIsSel('folder', f.id) ? ' sel' : '') + (brEffectiveEnabled('folder', f.id) ? '' : ' br-off');
  node.href = '/bridges?id=' + encodeURIComponent(f.id);
  const icon = document.createElement('span'); icon.className = 'br-ficon';
  icon.innerHTML = (expanded && hasKids) ? BR_ICON_FOLDER_OPEN : BR_ICON_FOLDER;
  const label = document.createElement('span'); label.className = 'br-folder-label'; label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    brContainerClick('folder', f.id);
  });
  brMakeDraggable(node, 'folder', f.id, f.connectorId);
  brMakeFolderDrop(node, f.id);
  brMakeKebab(node, {
    onRename: () => brKebabRename('folder', f.id),
    onCopyId: () => brCopyIdToast(f.id, 'folder'),
    onDelete: () => brConfirmDeleteFolder(f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(brFolderLi(c)));
    leaves.forEach(b => { const bli = document.createElement('li'); bli.appendChild(brBindingNode(b)); ul.appendChild(bli); });
    li.appendChild(ul);
  }
  return li;
}
function brBindingNode(b){
  const n = document.createElement('a');
  n.className = 'br-leaf-node' + (brIsSel('binding', b.uuid) ? ' sel' : '') + (brEffectiveEnabled('binding', b.uuid) ? '' : ' br-off');
  n.href = '/bridges?id=' + encodeURIComponent(b.uuid);
  n.title = (b.roomName || '(room missing)') + ' ↔ ' + b.addressKey;
  const label = document.createElement('span'); label.className = 'br-leaf-label'; label.textContent = b.roomName || '(room missing)';
  const addr = document.createElement('span'); addr.className = 'br-leaf-addr'; addr.textContent = b.addressKey;
  n.appendChild(label); n.appendChild(addr);
  n.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    brSelectNode('binding', b.uuid);
  });
  brMakeDraggable(n, 'binding', b.uuid, b.connectorId);
  brMakeBindingDrop(n, b.uuid);
  brMakeKebab(n, {
    onCopyId: () => brCopyIdToast(b.uuid, 'binding'),
    onDelete: () => brConfirmDeleteBinding(b.uuid),
  });
  return n;
}
function brKebabRename(kind, id){
  brSelectNode(kind, id);
  const sel = brNodeOf(kind, id);
  if (sel) brOpenRenameModal(kind, sel.node);
}
// 3-dot overflow menu. opts: { onRename?, onCopyId?, onDelete? }.
function brMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button'; kebab.className = 'br-kebab';
  kebab.setAttribute('aria-label', 'Item actions'); kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'br-menu'; menu.setAttribute('role', 'menu'); menu.hidden = true;
  const items = [];
  if (opts.onRename) items.push(['Rename', opts.onRename, '']);
  if (opts.onCopyId) items.push(['Copy ID', opts.onCopyId, '']);
  if (opts.onDelete) items.push(['Delete', opts.onDelete, 'danger']);
  items.forEach(spec => {
    const item = document.createElement('button');
    item.type = 'button'; item.className = 'item' + (spec[2] ? ' ' + spec[2] : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = spec[0];
    item.addEventListener('click', e => { e.stopPropagation(); e.preventDefault(); menu.hidden = true; spec[1](); });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', e => {
    e.stopPropagation();
    e.preventDefault();  // the kebab sits inside the row's anchor — never follow it
    const willOpen = menu.hidden;
    document.querySelectorAll('.br-menu').forEach(m => { m.hidden = true; });
    if (willOpen) brPlaceMenu(menu, kebab.getBoundingClientRect());
  });
  node.appendChild(kebab); node.appendChild(menu);
}
function brPlaceMenu(menu, anchorRect){
  menu.hidden = false;
  const margin = 6;
  const left = Math.max(margin, Math.min(anchorRect.left, window.innerWidth - menu.offsetWidth - margin));
  let top = anchorRect.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - margin) top = anchorRect.top - menu.offsetHeight - 4;
  menu.style.left = left + 'px';
  menu.style.top = Math.max(margin, top) + 'px';
}

// ---- clipboard (verified copy, as /chat does it) ----
function brVerifiedCopy(text){
  if (!document.execCommand) return 'unavailable';
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', '');
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta);
  const previous = document.activeElement;
  ta.select();
  let copied = false;
  const witness = () => { copied = true; };
  document.addEventListener('copy', witness, true);
  let claimed = false;
  try { claimed = document.execCommand('copy'); } catch (e) { claimed = false; }
  document.removeEventListener('copy', witness, true);
  document.body.removeChild(ta);
  if (previous && previous.focus) previous.focus();
  if (claimed && copied) return 'ok';
  return claimed ? 'blocked' : 'unavailable';
}
function brCopyText(text, message){
  const report = ok => brToast(ok ? (message || 'Copied to clipboard') : 'Could not copy — select the text and copy it manually');
  const status = brVerifiedCopy(text);
  if (status !== 'unavailable'){ report(status === 'ok'); return; }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => report(true), () => report(false));
  else report(false);
}
function brCopyIdToast(uuid, kind){ brCopyText(uuid, kind + ' id copied: ' + uuid); }

// ---- add connector / folder / binding ----
function brAddConnector(){
  const sel = document.getElementById('br-conn-platform');
  sel.innerHTML = Object.keys(brPlatforms).map(p => {
    const meta = brPlatforms[p];
    return '<option value="' + brEscapeHtml(p) + '"' + (meta.available ? '' : ' disabled') + '>' + brEscapeHtml(meta.label) + (meta.available ? '' : ' (no bridge yet)') + '</option>';
  }).join('');
  document.getElementById('br-conn-name').value = '';
  document.getElementById('br-conn-token-env').value = '';
  document.getElementById('br-conn-base-url').value = '';
  document.getElementById('br-conn-identity').value = '';
  document.getElementById('br-conn-err').textContent = '';
  brSyncConnectorFields();
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-connector-modal').hidden = false;
  document.getElementById('br-conn-name').focus();
}
function brSyncConnectorFields(){
  const meta = brPlatforms[document.getElementById('br-conn-platform').value] || {};
  document.getElementById('br-conn-base-url-row').hidden = !meta.requires_base_url;
  document.getElementById('br-conn-identity-row').hidden = !meta.requires_identity;
}
function brCloseConnectorModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-connector-modal').hidden = true;
}
async function brAddConnectorConfirm(){
  const err = document.getElementById('br-conn-err');
  err.textContent = '';
  const body = {
    name: document.getElementById('br-conn-name').value.trim(),
    platform: document.getElementById('br-conn-platform').value,
    token_env: document.getElementById('br-conn-token-env').value.trim(),
  };
  if (!body.name){ err.textContent = 'Name is required.'; return; }
  if (!body.token_env){ err.textContent = 'The credential variable name is required.'; return; }
  const meta = brPlatforms[body.platform] || {};
  if (meta.requires_base_url) body.base_url = document.getElementById('br-conn-base-url').value.trim();
  if (meta.requires_identity) body.identity = document.getElementById('br-conn-identity').value.trim();
  try {
    await brFlushPendingSave();
    const r = await fetch('/bridges/api/connectors', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await r.json();
    if (!r.ok){ err.textContent = data.error || 'Could not create the connector.'; return; }
    brConnectors.push(data.connector);
    brTreeVersion = data.version;
    brCloseConnectorModal();
    brSelectNode('connector', data.connector.uuid);
  } catch (e) { err.textContent = 'Could not reach the server.'; }
}
// A folder lives under the selected connector, inside the selected folder if
// one is selected (a selected binding's folder counts).
function brPlacement(){
  const sel = brSelected();
  if (!sel) return null;
  const folderId = sel.kind === 'folder' ? sel.node.id : (sel.kind === 'binding' ? (sel.node.folderId || null) : null);
  return {connectorId: sel.connectorId, folderId: folderId && brFolderById(folderId) ? folderId : null};
}
function brPlacementText(p){
  const c = brConnectorByUuid(p.connectorId);
  const chain = brFolderChain(p.folderId).map(f => f.name);
  return 'In ' + (c ? c.name : p.connectorId) + (chain.length ? ' / ' + chain.join(' / ') : '');
}
function brAddFolder(){
  const p = brPlacement();
  if (!p){ brToast('Select a connector first'); return; }
  document.getElementById('br-folder-title').textContent = p.folderId ? 'New subfolder' : 'New folder';
  document.getElementById('br-folder-where').textContent = brPlacementText(p);
  const input = document.getElementById('br-folder-input');
  input.value = '';
  document.getElementById('br-folder-create').disabled = true;
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-folder-modal').hidden = false;
  input.focus();
}
function brCloseFolderModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-folder-modal').hidden = true;
}
async function brAddFolderConfirm(){
  const name = document.getElementById('br-folder-input').value.trim();
  const p = brPlacement();
  if (!name || !p) return;
  try {
    await brFlushPendingSave();
    const r = await fetch('/bridges/api/folders', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({connectorId: p.connectorId, parentId: p.folderId, name: name})});
    const data = await r.json();
    if (!r.ok){ brToast(data.error || 'Could not create folder.'); return; }
    brFolders.push(data.folder);
    brTreeVersion = data.version;
    brCloseFolderModal();
    brExpanded[p.folderId || p.connectorId] = true;
    brSelectNode('folder', data.folder.id);
  } catch (e) { brToast('Could not create folder.'); }
}
async function brAddBinding(){
  const p = brPlacement();
  if (!p){ brToast('Select a connector first'); return; }
  const err = document.getElementById('br-binding-err');
  err.textContent = '';
  document.getElementById('br-binding-where').textContent = brPlacementText(p);
  const plat = brPlatformOf(p.connectorId);
  document.getElementById('br-binding-fields').innerHTML = (plat.address_fields || []).map(f =>
    '<label>' + brEscapeHtml(f.name) + (f.required ? '' : ' <span class="hint">(optional)</span>') +
    '<input type="text" data-address="' + brEscapeHtml(f.name) + '" autocomplete="off" placeholder="' +
    (f.kind === 'snowflake' ? 'numeric id' : f.kind === 'signed_int' ? 'integer id' : 'text') + '"></label>').join('');
  const roomSel = document.getElementById('br-binding-room');
  roomSel.innerHTML = '<option value="">loading rooms…</option>';
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-binding-modal').hidden = false;
  try {
    const r = await fetch('/chat/api/rooms');
    brRooms = await r.json();
  } catch (e) { brRooms = []; }
  roomSel.innerHTML = (brRooms || []).map(rm => '<option value="' + brEscapeHtml(rm.uuid) + '">' + brEscapeHtml(rm.name) + '</option>').join('')
    || '<option value="">no chatrooms yet</option>';
}
function brCloseBindingModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-binding-modal').hidden = true;
}
async function brAddBindingConfirm(){
  const err = document.getElementById('br-binding-err');
  err.textContent = '';
  const p = brPlacement();
  if (!p) return;
  const roomUuid = document.getElementById('br-binding-room').value;
  if (!roomUuid){ err.textContent = 'Pick a chatroom.'; return; }
  const address = {};
  document.querySelectorAll('#br-binding-fields [data-address]').forEach(inp => {
    const v = inp.value.trim();
    if (v) address[inp.getAttribute('data-address')] = v;
  });
  try {
    await brFlushPendingSave();
    const r = await fetch('/bridges/api/bindings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({connectorId: p.connectorId, folderId: p.folderId, roomUuid: roomUuid, address: address})});
    const data = await r.json();
    if (!r.ok){ err.textContent = data.error || 'Could not create the binding.'; return; }
    const room = (brRooms || []).find(rm => rm.uuid === roomUuid);
    if (room && !data.binding.roomName) data.binding.roomName = room.name;
    brBindings.push(data.binding);
    brTreeVersion = data.version;
    brCloseBindingModal();
    brExpanded[p.folderId || p.connectorId] = true;
    brSelectNode('binding', data.binding.uuid);
  } catch (e) { err.textContent = 'Could not reach the server.'; }
}

// ---- drag & drop (one node at a time; folders and bindings never leave their connector) ----
function brFolderInSubtree(candidateId, rootId){
  let cur = brFolderById(candidateId);
  const seen = new Set();
  while (cur && !seen.has(cur.id)){
    if (cur.id === rootId) return true;
    seen.add(cur.id);
    cur = cur.parentId ? brFolderById(cur.parentId) : null;
  }
  return false;
}
function brMoveConnectorBeside(uuid, targetUuid, after){
  if (uuid === targetUuid) return;
  const c = brConnectorByUuid(uuid);
  if (!c) return;
  brConnectors = brConnectors.filter(x => x.uuid !== uuid);
  const ti = brConnectors.findIndex(x => x.uuid === targetUuid);
  if (ti < 0) brConnectors.push(c); else brConnectors.splice(after ? ti + 1 : ti, 0, c);
  brSave();
}
function brMoveFolder(folderId, connectorId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && brFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = brFolderById(folderId);
  if (!f || f.connectorId !== connectorId) return;
  f.parentId = targetParentId;
  brFolders = brFolders.filter(x => x.id !== folderId);
  const sib = x => x.connectorId === connectorId && (x.parentId || null) === targetParentId;
  if (atStart){
    const i = brFolders.findIndex(sib);
    if (i < 0) brFolders.push(f); else brFolders.splice(i, 0, f);
  } else {
    let at = brFolders.length;
    for (let i = brFolders.length - 1; i >= 0; i--){ if (sib(brFolders[i])){ at = i + 1; break; } }
    brFolders.splice(at, 0, f);
  }
  brSave();
}
function brMoveFolderBeside(folderId, targetFolderId, after){
  if (folderId === targetFolderId) return;
  const target = brFolderById(targetFolderId);
  const f = brFolderById(folderId);
  if (!target || !f || target.connectorId !== f.connectorId) return;
  const newParent = target.parentId || null;
  if (newParent && brFolderInSubtree(newParent, folderId)) return;
  f.parentId = newParent;
  brFolders = brFolders.filter(x => x.id !== folderId);
  const ti = brFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) brFolders.push(f); else brFolders.splice(after ? ti + 1 : ti, 0, f);
  brSave();
}
function brMoveBinding(uuid, connectorId, targetFolderId, beforeUuid){
  targetFolderId = targetFolderId || null;
  const idx = brBindings.findIndex(b => b.uuid === uuid);
  if (idx < 0 || brBindings[idx].connectorId !== connectorId) return;
  const b = brBindings.splice(idx, 1)[0];
  b.folderId = targetFolderId;
  let insertAt = beforeUuid ? brBindings.findIndex(x => x.uuid === beforeUuid) : -1;
  if (insertAt < 0){
    insertAt = brBindings.length;
    for (let i = brBindings.length - 1; i >= 0; i--){
      if (brBindings[i].connectorId === connectorId && (brBindings[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  brBindings.splice(insertAt, 0, b);
  brSave();
}
function brMakeDraggable(el, type, id, connectorId){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    brDrag = {type: type, id: id, connectorId: connectorId};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('br-dragging');
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => { brDrag = null; brRenderTree(); });
}
// Drop a folder/binding into a container (connector root when folderId is null).
function brDropInto(connectorId, folderId, atStart){
  if (!brDrag || brDrag.type === 'connector' || brDrag.connectorId !== connectorId) return;
  const dragged = brDrag;
  if (dragged.type === 'binding'){
    let beforeUuid = null;
    if (atStart){
      const first = brBindings.find(b => b.connectorId === connectorId && (b.folderId || null) === (folderId || null) && b.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    brMoveBinding(dragged.id, connectorId, folderId, beforeUuid);
  } else {
    brMoveFolder(dragged.id, connectorId, folderId, atStart);
  }
  brExpanded[folderId || connectorId] = true;
  brDrag = null;
  brSelectNode(dragged.type, dragged.id);
}
function brMakeConnectorDrop(node, connectorId){
  // Connector drags reorder (top/bottom halves); a folder/binding of this
  // connector dropped anywhere on the row moves to the connector root.
  const zoneOf = e => {
    if (brDrag && brDrag.type !== 'connector') return 'into';
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2 ? 'after' : 'before';
  };
  const okFor = z => {
    if (!brDrag) return false;
    if (brDrag.type === 'connector') return brDrag.id !== connectorId;
    return brDrag.connectorId === connectorId;
  };
  const clear = () => node.classList.remove('br-drop-before', 'br-drop-after', 'br-drop-target');
  node.addEventListener('dragover', e => {
    if (!brDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('br-drop-before', z === 'before');
    node.classList.toggle('br-drop-after', z === 'after');
    node.classList.toggle('br-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!brDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into') brDropInto(connectorId, null, false);
    else {
      const draggedId = brDrag.id;
      brMoveConnectorBeside(draggedId, connectorId, z === 'after');
      brDrag = null;
      brSelectNode('connector', draggedId);
    }
  });
}
function brMakeFolderDrop(node, folderId){
  // Three zones on a folder: top third = reorder before, bottom third = after
  // (sibling), middle = nest into. Bindings always go "into".
  const f = brFolderById(folderId);
  const zoneOf = e => {
    if (brDrag && brDrag.type === 'binding') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!brDrag || !f || brDrag.type === 'connector' || brDrag.connectorId !== f.connectorId) return false;
    if (brDrag.type === 'binding') return z === 'into';
    if (folderId === brDrag.id) return false;
    if (z === 'into') return !brFolderInSubtree(folderId, brDrag.id);
    const np = f.parentId || null;
    return !(np && brFolderInSubtree(np, brDrag.id));
  };
  const clear = () => node.classList.remove('br-drop-before', 'br-drop-after', 'br-drop-target');
  node.addEventListener('dragover', e => {
    if (!brDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('br-drop-before', z === 'before');
    node.classList.toggle('br-drop-after', z === 'after');
    node.classList.toggle('br-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!brDrag) return;
    e.stopPropagation();
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into') brDropInto(f.connectorId, folderId, false);
    else {
      const draggedId = brDrag.id;
      brMoveFolderBeside(draggedId, folderId, z === 'after');
      brDrag = null;
      brSelectNode('folder', draggedId);
    }
  });
}
function brMakeBindingDrop(node, bindingUuid){
  const isAfter = e => { const r = node.getBoundingClientRect(); return (e.clientY - r.top) > r.height / 2; };
  const ok = () => { const t = brBindingByUuid(bindingUuid); return !!(brDrag && t && brDrag.type !== 'connector' && brDrag.connectorId === t.connectorId); };
  node.addEventListener('dragover', e => {
    if (!ok()) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('br-drop-after', after);
    node.classList.toggle('br-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('br-drop-before', 'br-drop-after'));
  node.addEventListener('drop', e => {
    if (!ok()) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('br-drop-before', 'br-drop-after');
    brDropOnBinding(bindingUuid, after);
  });
}
function brDropOnBinding(targetUuid, after){
  if (!brDrag) return;
  if (brDrag.type === 'binding' && brDrag.id === targetUuid) return;
  const dragged = brDrag;
  const target = brBindingByUuid(targetUuid);
  if (!target) return;
  const targetFolder = target.folderId || null;
  if (dragged.type === 'binding'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = brBindings.findIndex(b => b.uuid === targetUuid);
      beforeUuid = (ti + 1 < brBindings.length) ? brBindings[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    brMoveBinding(dragged.id, target.connectorId, targetFolder, beforeUuid);
  } else {
    brMoveFolder(dragged.id, target.connectorId, targetFolder);
  }
  brDrag = null;
  brSelectNode(dragged.type, dragged.id);
}
function brInitTreeDnD(){
  const root = document.getElementById('br-tree-root');
  root.addEventListener('dragover', e => {
    if (brDrag && brDrag.type === 'connector'){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (brDrag && brDrag.type === 'connector'){  // empty space → end of the connector list
      e.preventDefault();
      const last = brConnectors[brConnectors.length - 1];
      if (last && last.uuid !== brDrag.id){ const id = brDrag.id; brMoveConnectorBeside(id, last.uuid, true); brDrag = null; brSelectNode('connector', id); }
    }
  });
  document.getElementById('br-all').addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    brSelectNode(null, null);
  });
  document.addEventListener('click', () => { document.querySelectorAll('.br-menu').forEach(m => { m.hidden = true; }); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.br-menu').forEach(m => { m.hidden = true; });
  });
}

// ---- delete (dedicated DELETE endpoints; the tree PUT never removes rows) ----
let brDeleteOnConfirm = null;
let brDeleteRequireName = null;
function brOpenDeleteModal(opts){
  brDeleteOnConfirm = opts.onConfirm;
  brDeleteRequireName = opts.requireName || null;
  document.getElementById('br-delete-title').textContent = opts.title || 'Delete';
  document.getElementById('br-delete-msg').textContent = opts.message;
  const nameRow = document.getElementById('br-delete-name-row');
  const input = document.getElementById('br-delete-input');
  const btn = document.getElementById('br-delete-confirm');
  if (brDeleteRequireName){
    nameRow.hidden = false;
    document.getElementById('br-delete-name').textContent = brDeleteRequireName;
    input.value = ''; btn.disabled = true;
  } else {
    nameRow.hidden = true; btn.disabled = !!opts.blocked;  // blocked: nothing to confirm, only Cancel
  }
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('br-delete-modal').hidden = false;
  if (brDeleteRequireName) input.focus();
}
function brCloseDeleteModal(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('br-delete-modal').hidden = true;
  brDeleteOnConfirm = null;
  brDeleteRequireName = null;
}
function brDeleteUpdateState(){
  const input = document.getElementById('br-delete-input');
  document.getElementById('br-delete-confirm').disabled = brDeleteRequireName ? (input.value.trim() !== brDeleteRequireName) : false;
}
function brConfirmDeleteConnector(uuid){
  const c = brConnectorByUuid(uuid);
  if (!c) return;
  const folders = brFolders.filter(f => f.connectorId === uuid).length;
  const bindings = brBindings.filter(b => b.connectorId === uuid).length;
  if (folders + bindings){
    brOpenDeleteModal({title: 'Delete connector', blocked: true,
      message: '"' + c.name + '" still owns ' + folders + ' folder(s) and ' + bindings + ' binding(s). Delete those first; nothing is removed for you.'});
    return;
  }
  brOpenDeleteModal({title: 'Delete connector', requireName: c.name,
    message: 'Delete connector "' + c.name + '"? The launcher stops its process. The credential variable itself is untouched. This cannot be undone.',
    onConfirm: () => brDeleteNode('connector', uuid)});
}
function brConfirmDeleteFolder(id){
  const f = brFolderById(id);
  if (!f) return;
  const sub = brFlattenTree('folder', id);
  if (sub.length){
    brOpenDeleteModal({title: 'Delete folder', blocked: true,
      message: 'Folder "' + f.name + '" is not empty (' + sub.length + ' item(s) inside). Move or delete them first.'});
    return;
  }
  brOpenDeleteModal({title: 'Delete folder', message: 'Delete empty folder "' + f.name + '"?', onConfirm: () => brDeleteNode('folder', id)});
}
function brConfirmDeleteBinding(uuid){
  const b = brBindingByUuid(uuid);
  if (!b) return;
  brOpenDeleteModal({title: 'Delete binding',
    message: 'Delete the binding between "' + (b.roomName || b.roomUuid) + '" and ' + b.addressKey + '? A running bridge retires it and removes its mirrored progress bubbles best-effort; the chatroom stays.',
    onConfirm: () => brDeleteNode('binding', uuid)});
}
async function brDeleteNode(kind, id){
  const sel = brNodeOf(kind, id);
  try {
    await brFlushPendingSave();
    const r = await fetch(brItemUrl(kind, id), {method: 'DELETE'});
    const data = await r.json();
    if (!r.ok){ brToast(data.error || 'Could not delete.'); return; }
    if (kind === 'connector') brConnectors = brConnectors.filter(x => x.uuid !== id);
    else if (kind === 'folder') brFolders = brFolders.filter(x => x.id !== id);
    else brBindings = brBindings.filter(x => x.uuid !== id);
    brTreeVersion = data.version;
    if (brSel && brSel.id === id){
      // Land on the parent, not the root, so the operator stays in context.
      if (kind === 'connector') brSel = null;
      else if (kind === 'folder') brSel = sel.node.parentId && brFolderById(sel.node.parentId) ? {kind: 'folder', id: sel.node.parentId} : {kind: 'connector', id: sel.connectorId};
      else brSel = sel.node.folderId && brFolderById(sel.node.folderId) ? {kind: 'folder', id: sel.node.folderId} : {kind: 'connector', id: sel.connectorId};
    }
    brRenderTree();
    brRender();
    brToast('Deleted');
  } catch (e) { brToast('Could not delete.'); }
}
document.getElementById('br-delete-input').addEventListener('input', brDeleteUpdateState);
document.getElementById('br-delete-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('br-delete-confirm').disabled){
    e.preventDefault(); document.getElementById('br-delete-confirm').click();
  }
});
document.getElementById('br-delete-confirm').addEventListener('click', () => {
  const fn = brDeleteOnConfirm;
  brCloseDeleteModal();
  if (fn) fn();
});

// ---- persistence ----
async function brLoadTree(){
  try {
    const r = await fetch('/bridges/api/tree');
    const data = await r.json();
    brConnectors = (data && data.connectors) || [];
    brFolders = (data && data.folders) || [];
    brBindings = (data && data.bindings) || [];
    brPlatforms = (data && data.platforms) || {};
    if (data && data.core_url) brCoreUrl = data.core_url;
    brTreeVersion = (data && data.version) || null;
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    brConnectors = []; brFolders = []; brBindings = []; brTreeVersion = null;
  }
}
let brToastTimer = null;
function brToast(text){
  const el = document.getElementById('br-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(brToastTimer);
  brToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let brSaveTimer = null;
let brTreeVersion = null;     // token from hydrate; PUTs echo it (stale → 409)
let brSaveInFlight = false;
let brSaveQueued = false;
let brSaveChain = null;
function brSave(){
  clearTimeout(brSaveTimer);
  brSaveTimer = setTimeout(brSavePush, 250);  // coalesce bursts into one PUT
}
// Flush or await a pending tree PUT before a create or delete
// (notes/ui-tree-persistence.md), so an older PUT's response cannot overwrite
// the fresher token a create/delete just handed back.
function brFlushPendingSave(){
  if (brSaveTimer){
    clearTimeout(brSaveTimer);
    brSaveTimer = null;
    return brSavePush();
  }
  return brSaveChain || Promise.resolve();
}
function brReconcileSelectionAfterReload(){
  if (brSel && !brNodeOf(brSel.kind, brSel.id)) brSel = null;
}
async function brReloadAndRepaint(message){
  await brLoadTree();
  brReconcileSelectionAfterReload();
  brRenderTree();
  brRender();
  brToast(message);
}
function brSavePush(){
  if (brSaveInFlight){ brSaveQueued = true; return brSaveChain; }  // serialize PUTs
  brSaveInFlight = true;
  const run = (async () => {
    try {
      const r = await fetch('/bridges/api/tree', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          connectors: brConnectors.map(c => ({uuid: c.uuid, name: c.name})),
          folders: brFolders.map(f => ({id: f.id, connectorId: f.connectorId, parentId: f.parentId || null, name: f.name})),
          bindings: brBindings.map(b => ({uuid: b.uuid, connectorId: b.connectorId, folderId: b.folderId || null})),
          version: brTreeVersion,
        }),
      });
      const j = await r.json().catch(() => null);
      if (r.status === 409){
        await brReloadAndRepaint('Bridge tree was changed elsewhere — reloaded. Your last edit was not saved.');
        return;
      }
      if (!r.ok){
        await brReloadAndRepaint('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)) + ' — reloaded.');
        return;
      }
      brTreeVersion = j.version;
    } catch (e) {
      await brReloadAndRepaint('Save failed — reloaded.');
    } finally {
      brSaveInFlight = false;
      if (brSaveQueued){
        brSaveQueued = false;
        brSaveChain = brSavePush();
        await brSaveChain;
      } else {
        brSaveChain = null;
      }
    }
  })();
  brSaveChain = run;
  return run;
}

// ---- dirty-guarded dismissal (clicking backdrop / Esc) ----
function brOpenModalDirty(){
  const v = id => document.getElementById(id).value.trim();
  if (!document.getElementById('br-connector-modal').hidden) return v('br-conn-name') !== '' || v('br-conn-token-env') !== '';
  if (!document.getElementById('br-folder-modal').hidden) return v('br-folder-input') !== '';
  if (!document.getElementById('br-credential-modal').hidden) return v('br-credential-input') !== '';
  if (!document.getElementById('br-binding-modal').hidden)
    return Array.from(document.querySelectorAll('#br-binding-fields [data-address]')).some(i => i.value.trim() !== '');
  if (!document.getElementById('br-policy-modal').hidden) return true;  // explicit Save/Cancel only
  if (!document.getElementById('br-delete-modal').hidden) return brDeleteRequireName ? v('br-delete-input') !== '' : false;
  if (!document.getElementById('br-rename-modal').hidden)
    return document.getElementById('br-rename-input').value !== ((brRenameState && brRenameState.original) || '');
  return false;
}
function brCloseOpenModal(){
  if (!document.getElementById('br-connector-modal').hidden){ brCloseConnectorModal(); return; }
  if (!document.getElementById('br-folder-modal').hidden){ brCloseFolderModal(); return; }
  if (!document.getElementById('br-credential-modal').hidden){ brCloseCredentialModal(); return; }
  if (!document.getElementById('br-binding-modal').hidden){ brCloseBindingModal(); return; }
  if (!document.getElementById('br-policy-modal').hidden){ brClosePolicyModal(); return; }
  if (!document.getElementById('br-delete-modal').hidden){ brCloseDeleteModal(); return; }
  if (!document.getElementById('br-rename-modal').hidden){ brCloseRenameModal(); return; }
}
function brDismissIfClean(){ if (!brOpenModalDirty()) brCloseOpenModal(); }

// ---- wiring + initial paint ----
brInitTreeDnD();
document.getElementById('br-rename-input').addEventListener('input', brSyncRenameConfirm);
document.getElementById('br-rename-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('br-rename-confirm').disabled){ e.preventDefault(); brConfirmRenameModal(); }
});
document.getElementById('br-folder-input').addEventListener('input', () => {
  document.getElementById('br-folder-create').disabled = document.getElementById('br-folder-input').value.trim() === '';
});
document.getElementById('br-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('br-folder-create').disabled){ e.preventDefault(); brAddFolderConfirm(); }
});
document.getElementById('br-conn-platform').addEventListener('change', brSyncConnectorFields);
document.getElementById('br-credential-input').addEventListener('input', () => {
  document.getElementById('br-credential-save').disabled = document.getElementById('br-credential-input').value.trim() === '';
});
document.getElementById('br-credential-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('br-credential-save').disabled){ e.preventDefault(); brSaveCredential(); }
});
document.getElementById('br-conn-token-env').addEventListener('keydown', e => {
  if (e.key === 'Enter'){ e.preventDefault(); brAddConnectorConfirm(); }
});
document.getElementById('br-policy-inherit').addEventListener('change', brSyncPolicyControl);
document.getElementById('ui-modal-backdrop').addEventListener('click', brDismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') brDismissIfClean(); });
brLoadTree().then(() => {
  // Deep link: ?id=<uuid> selects that connector, folder, or binding on load.
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && brConnectorByUuid(wantId)) brSelectNode('connector', wantId);
  else if (wantId && brFolderById(wantId)) brSelectNode('folder', wantId);
  else if (wantId && brBindingByUuid(wantId)) brSelectNode('binding', wantId);
  else { brRenderTree(); brRender(); }
});
