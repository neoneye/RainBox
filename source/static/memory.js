/* /memory review page logic.
 *
 * Hydrates from GET /memory/api/claims and mutates via
 * POST /memory/api/claims/<uuid>/<action>. The left panel groups claims by
 * STATUS facets (no folders, no drag-drop, no whole-tree save — see
 * docs/ui-left-panel-tree.md for what we deliberately drop, and the design
 * spec). Chrome (selection, kebab, detail pane, modals, toast) mirrors /cron.
 *
 * The grouping is a single swappable seam (`groupClaims`) so a future
 * user-created folder tree can be added as another grouping mode without
 * touching selection/detail/actions.
 */

// Lucide folder SVGs — copied verbatim from /chat & /cron (the folder icon IS
// the expand indicator; it flips open/closed on expanded-and-non-empty).
const MEM_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const MEM_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';

const STATUS_ORDER = ['active', 'candidate', 'superseded', 'rejected', 'expired'];
const STATUS_LABEL = {
  active: 'Active', candidate: 'Candidate', superseded: 'Superseded',
  rejected: 'Rejected', expired: 'Expired',
};
const EXPAND_KEY = 'memory.expandedGroups';

let claims = [];                 // [{uuid, text, status, ...}] from the API
let selectedGroup = null;        // status string, 'all', or null
let currentClaimUuid = null;     // open claim in the detail pane
let expanded = {};               // status -> false when collapsed (default expanded)
let toastTimer = null;
let modalState = {};             // per-modal scratch (uuid, updated_at, ...)

try { expanded = JSON.parse(localStorage.getItem(EXPAND_KEY) || '{}'); } catch (_) { expanded = {}; }
const isExpanded = (s) => expanded[s] !== false;
function setExpanded(s, v) { expanded[s] = v; try { localStorage.setItem(EXPAND_KEY, JSON.stringify(expanded)); } catch (_) {} }

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function memToast(text) {
  const el = document.getElementById('mem-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
const claimByUuid = (u) => claims.find(c => c.uuid === u) || null;

// --- hydrate ---------------------------------------------------------------
async function hydrate() {
  const r = await fetch('/memory/api/claims');
  const data = await r.json();
  claims = data.claims || [];
  renderTree();
}

function filterPredicate() {
  const text = document.getElementById('mem-filter-text').value.trim().toLowerCase();
  const scope = document.getElementById('mem-filter-scope').value;
  const kind = document.getElementById('mem-filter-kind').value;
  const sens = document.getElementById('mem-filter-sens').value;
  return (c) => {
    if (scope && c.scope !== scope) return false;
    if (kind && c.kind !== kind) return false;
    if (sens && c.sensitivity !== sens) return false;
    if (text) {
      // secret rows are masked server-side; still filter on whatever text we have
      const hay = (c.text + ' ' + (c.room_name || '')).toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  };
}
const filteredClaims = () => claims.filter(filterPredicate());

// The swappable grouping seam: status today, folders later.
function groupClaims(list) {
  const groups = {};
  STATUS_ORDER.forEach(s => { groups[s] = []; });
  list.forEach(c => { (groups[c.status] = groups[c.status] || []).push(c); });
  return groups;
}

// --- render: tree ----------------------------------------------------------
function renderTree() {
  const root = document.getElementById('mem-tree-root');
  const groups = groupClaims(filteredClaims());
  const lis = STATUS_ORDER.map(status => groupLi(status, groups[status] || []));
  root.replaceChildren(...lis);
  // reflect selection highlight
  document.getElementById('mem-all').classList.toggle('sel', selectedGroup === 'all');
}

function groupLi(status, list) {
  const li = document.createElement('li');
  const node = document.createElement('div');
  node.className = 'mem-node' + (selectedGroup === status ? ' sel' : '');
  const open = isExpanded(status) && list.length > 0;
  node.innerHTML = '<span class="mem-ficon">' + (open ? MEM_ICON_FOLDER_OPEN : MEM_ICON_FOLDER) + '</span>' +
    '<span>' + escapeHtml(STATUS_LABEL[status] || status) + '</span>' +
    '<span class="mem-group-count">' + list.length + '</span>';
  node.addEventListener('click', () => groupClick(status, list.length));
  li.appendChild(node);
  if (open) {
    const sub = document.createElement('ul');
    list.forEach(c => sub.appendChild(claimLi(c)));
    li.appendChild(sub);
  }
  return li;
}

function claimLi(c) {
  const li = document.createElement('li');
  const node = document.createElement('div');
  node.className = 'mem-claim-node' + (currentClaimUuid === c.uuid ? ' sel' : '');
  const label = c.text + (c.stale ? '  (stale)' : '');
  node.innerHTML = '<span class="mem-claim-label" title="' + escapeHtml(c.text) + '">' +
    escapeHtml(label) + '</span>' +
    '<button class="mem-kebab" title="Actions"></button>';
  node.addEventListener('click', (e) => {
    if (e.target.closest('.mem-kebab')) { e.stopPropagation(); openClaimMenu(c, e.target.closest('.mem-kebab')); return; }
    openClaim(c.uuid);
  });
  li.appendChild(node);
  return li;
}

// --- selection -------------------------------------------------------------
function groupClick(status, count) {
  if (selectedGroup === status) { setExpanded(status, !isExpanded(status)); renderTree(); return; }
  selectedGroup = status;
  currentClaimUuid = null;
  setExpanded(status, true);
  syncUrl();
  renderTree();
  renderTable(STATUS_LABEL[status] || status, filteredClaims().filter(c => c.status === status));
}

function selectAll() {
  selectedGroup = 'all';
  currentClaimUuid = null;
  syncUrl();
  renderTree();
  renderTable('All memories', filteredClaims());
}

async function openClaim(uuid) {
  selectedGroup = null;
  currentClaimUuid = uuid;
  syncUrl();
  renderTree();
  const r = await fetch('/memory/api/claims/' + uuid);
  if (r.status === 404) { memToast('memory not found — reloaded'); currentClaimUuid = null; await hydrate(); return; }
  renderDetail(await r.json());
}

function showDetail() {
  document.getElementById('mem-table-wrap').hidden = true;
  document.getElementById('mem-pane-title').hidden = true;
  document.getElementById('mem-detail').hidden = false;
}
function showTable() {
  document.getElementById('mem-detail').hidden = true;
  document.getElementById('mem-table-wrap').hidden = false;
  document.getElementById('mem-pane-title').hidden = false;
}

// --- render: flat table (group / all) --------------------------------------
function renderTable(title, list) {
  showTable();
  document.getElementById('mem-pane-title').textContent = title + ' (' + list.length + ')';
  const tbody = document.getElementById('mem-rows');
  tbody.replaceChildren();
  list.forEach(c => {
    const tr = document.createElement('tr');
    if (c.secret) tr.className = 'row-secret';
    tr.innerHTML =
      '<td>' + escapeHtml(c.status) + (c.stale ? ' <span class="mem-badge stale">stale</span>' : '') + '</td>' +
      '<td class="mem-text-cell">' + escapeHtml(c.text) + '</td>' +
      '<td>' + escapeHtml(c.scope) + (c.room_name ? ' / ' + escapeHtml(c.room_name) : '') + '</td>' +
      '<td>' + escapeHtml(c.kind) + '</td>' +
      '<td>' + escapeHtml(c.sensitivity) + '</td>' +
      '<td>' + (c.used_recently ? '✓' : '') + '</td>' +
      '<td>' + escapeHtml(c.embedding_state) + '</td>' +
      '<td><a class="row-details">Details</a></td>';
    tr.querySelector('.row-details').addEventListener('click', () => openClaim(c.uuid));
    tbody.appendChild(tr);
  });
}

// --- render: detail pane ---------------------------------------------------
function renderDetail(d) {
  showDetail();
  const el = document.getElementById('mem-detail');
  const masked = d.secret;
  const textHtml = '<div class="mem-detail-text" id="mem-dtext">' +
    (masked ? '•••••• (secret)' : escapeHtml(d.text)) +
    (masked ? '<button class="mem-reveal" onclick="memReveal()">Reveal</button>' : '') + '</div>';
  const badge = (cls, t) => '<span class="mem-badge ' + cls + '">' + escapeHtml(t) + '</span>';
  let badges = '<div class="mem-badges">' +
    badge('status-' + d.status, d.status) +
    badge('', d.scope + (d.room_name ? ' · ' + d.room_name : '')) +
    badge('', d.kind) +
    badge(d.sensitivity === 'secret' ? 'sens-secret' : '', d.sensitivity) +
    badge('', 'conf ' + d.confidence) +
    (d.stale ? badge('stale', 'stale (expired)') : '') + '</div>';

  const ts = '<div class="mem-section"><div class="mem-section-label">Timestamps</div>' +
    '<div class="muted">created ' + fmt(d.created_at) + ' · updated ' + fmt(d.updated_at) +
    ' · expires ' + (d.expires_at ? fmt(d.expires_at) : '—') + '</div></div>';

  let lineage = '';
  if (d.supersedes || d.superseded_by) {
    lineage = '<div class="mem-section mem-lineage"><div class="mem-section-label">Lineage</div>';
    if (d.supersedes) lineage += '<div>supersedes → <a onclick="openClaim(\'' + d.supersedes.uuid + '\')">' + escapeHtml(d.supersedes.text) + '</a></div>';
    if (d.superseded_by) lineage += '<div>superseded by → <a onclick="openClaim(\'' + d.superseded_by.uuid + '\')">' + escapeHtml(d.superseded_by.text) + '</a></div>';
    lineage += '</div>';
  }

  const emb = '<div class="mem-section"><div class="mem-section-label">Embedding</div>' +
    '<span class="mem-emb-' + d.embedding_state + '">' + d.embedding_state + '</span></div>';

  let evi = '<div class="mem-section"><div class="mem-section-label">Evidence</div>';
  if (!d.evidence.length) evi += '<div class="muted">no evidence</div>';
  d.evidence.forEach(e => {
    evi += '<div class="mem-evi-row"><b>' + escapeHtml(e.provenance) + '</b> · ' + escapeHtml(e.source_type) +
      (e.source_id ? ' · ' + escapeHtml(e.source_id) : '') + ' · <span class="muted">' + fmt(e.created_at) + '</span>' +
      (e.excerpt ? '<div class="muted">' + escapeHtml(e.excerpt) + '</div>' : '') + '</div>';
  });
  evi += '</div>';

  let ret = '<div class="mem-section"><div class="mem-section-label">Retrieval</div>';
  if (!d.retrieval.length) ret += '<div class="muted">no retrieval recorded</div>';
  d.retrieval.forEach(r => {
    ret += '<div class="mem-ret-row"><b>' + escapeHtml(r.stage) + '</b>' +
      (r.source ? ' · ' + escapeHtml(r.source) : '') +
      (r.query ? ' · <span class="muted">' + escapeHtml(r.query) + '</span>' : '') +
      ' · <span class="muted">' + fmt(r.created_at) + '</span></div>';
  });
  ret += '</div>';

  el.innerHTML = textHtml + badges + actionsHtml(d) + ts + lineage + emb + evi + ret;
}

function actionsHtml(d) {
  const btns = [];
  const act = (label, cls, fn) => '<button class="' + cls + '" onclick="' + fn + '">' + label + '</button>';
  if (d.status === 'candidate') {
    btns.push(act('Activate', '', 'memActivate(\'' + d.uuid + '\')'));
    btns.push(act('Correct…', 'secondary', 'memOpenCorrect(\'' + d.uuid + '\')'));
    btns.push(act('Reject', 'danger', 'memOpenReject(\'' + d.uuid + '\')'));
  } else if (d.status === 'active') {
    btns.push(act('Correct…', 'secondary', 'memOpenCorrect(\'' + d.uuid + '\')'));
    btns.push(act('Forget', 'danger', 'memOpenReject(\'' + d.uuid + '\')'));
  } else if (d.status === 'rejected' || d.status === 'expired') {
    btns.push(act('Reactivate', '', 'memReactivate(\'' + d.uuid + '\')'));
  }
  if (d.status === 'active' || d.status === 'candidate') {
    btns.push(act('Sensitivity…', 'secondary', 'memOpenSens(\'' + d.uuid + '\')'));
    btns.push(act('Expiry…', 'secondary', 'memOpenExpiry(\'' + d.uuid + '\')'));
  }
  return '<div class="mem-section"><div class="mem-section-label">Actions</div><div class="mem-actions">' +
    (btns.join('') || '<span class="muted">read-only</span>') + '</div></div>';
}

function fmt(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString(); } catch (_) { return iso; } }
function memCopyId(uuid) {
  navigator.clipboard.writeText(uuid).then(
    () => memToast('Memory id copied: ' + uuid),
    () => memToast('Could not copy to clipboard.'));
}
function memReveal() { const c = claimByUuid(currentClaimUuid); /* detail already holds text */ const el = document.getElementById('mem-dtext'); fetch('/memory/api/claims/' + currentClaimUuid).then(r => r.json()).then(d => { el.textContent = d.text; }); }

// --- kebab menu (tree leaf) ------------------------------------------------
let openMenuEl = null;
function closeMenu() { if (openMenuEl) { openMenuEl.remove(); openMenuEl = null; } }
function openClaimMenu(c, anchor) {
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'mem-menu';
  const items = [];
  const it = (label, danger, fn) => '<button class="item' + (danger ? ' danger' : '') + '" data-fn="' + fn + '">' + label + '</button>';
  if (c.status === 'candidate') { items.push(it('Activate', false, 'activate')); items.push(it('Correct…', false, 'correct')); items.push(it('Reject', true, 'reject')); }
  else if (c.status === 'active') { items.push(it('Correct…', false, 'correct')); items.push(it('Forget', true, 'reject')); }
  else if (c.status === 'rejected' || c.status === 'expired') { items.push(it('Reactivate', false, 'reactivate')); }
  items.push(it('Copy memory id', false, 'copyid'));  // always available
  menu.innerHTML = items.join('');
  document.body.appendChild(menu);
  const rect = anchor.getBoundingClientRect();
  menu.style.left = Math.min(rect.left, window.innerWidth - 180) + 'px';
  menu.style.top = (rect.bottom + 4) + 'px';
  menu.querySelectorAll('.item').forEach(btn => btn.addEventListener('click', () => {
    const fn = btn.getAttribute('data-fn'); closeMenu();
    if (fn === 'activate') memActivate(c.uuid);
    else if (fn === 'reactivate') memReactivate(c.uuid);
    else if (fn === 'reject') memOpenReject(c.uuid);
    else if (fn === 'correct') memOpenCorrect(c.uuid);
    else if (fn === 'copyid') memCopyId(c.uuid);
  }));
  openMenuEl = menu;
}

// --- actions (POST) --------------------------------------------------------
function expectedFor(uuid) {
  const c = claimByUuid(uuid);
  return c ? c.updated_at : null;
}
async function doAction(uuid, action, body) {
  body = body || {};
  if (!('expected_updated_at' in body)) body.expected_updated_at = expectedFor(uuid);
  const r = await fetch('/memory/api/claims/' + uuid + '/' + action, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (r.status === 409) { memToast('memory changed elsewhere — reloaded'); await afterMutation(uuid); return null; }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { memToast(data.error || ('action failed (' + r.status + ')')); return null; }
  await afterMutation(data.new_uuid || uuid);
  return data;
}
async function afterMutation(focusUuid) {
  await hydrate();
  // Keep context: if a claim was open, re-open it (or its successor); else stay on the group/table.
  if (currentClaimUuid || focusUuid) {
    const target = claimByUuid(focusUuid) ? focusUuid : currentClaimUuid;
    if (target && claimByUuid(target)) { openClaim(target); return; }
  }
  if (selectedGroup === 'all') selectAll();
  else if (selectedGroup) groupClick(selectedGroup, 0);
}

function memActivate(uuid) { doAction(uuid, 'activate', {}); }
function memReactivate(uuid) { doAction(uuid, 'reactivate', {}); }

// --- modals: correct / sensitivity / expiry / reject -----------------------
function openBackdrop() { document.getElementById('ui-modal-backdrop').hidden = false; }
function closeBackdrop() { document.getElementById('ui-modal-backdrop').hidden = true; }

async function memOpenCorrect(uuid) {
  const inp = document.getElementById('mem-correct-input');
  document.getElementById('mem-correct-err').textContent = '';
  // Prefill with the CURRENT text so a correction is a tweak, not a blind
  // retype. Pull it from the detail endpoint, which reveals secret text too
  // (the list row would be masked); fall back to the cached row otherwise.
  let text = '';
  try { const r = await fetch('/memory/api/claims/' + uuid); if (r.ok) text = (await r.json()).text || ''; } catch (_) {}
  if (!text) { const c = claimByUuid(uuid); if (c && !c.secret) text = c.text; }
  modalState = {uuid, initial: text};
  inp.value = text;
  memCorrectSyncSave();  // starts disabled: prefilled text is unchanged
  openBackdrop(); document.getElementById('mem-correct-modal').hidden = false;
  inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length);
}
function memCloseCorrect() { document.getElementById('mem-correct-modal').hidden = true; closeBackdrop(); modalState = {}; }
// Save is enabled only once the text both is non-empty AND differs from the
// original — correcting to the same text is a no-op supersede, so block it.
function memCorrectSyncSave() {
  const v = document.getElementById('mem-correct-input').value;
  document.getElementById('mem-correct-save').disabled = !v.trim() || v === (modalState.initial || '');
}
async function memConfirmCorrect() {
  const text = document.getElementById('mem-correct-input').value.trim();
  if (!text) return;
  const res = await doAction(modalState.uuid, 'correct', {new_text: text});
  if (res) memCloseCorrect();
  else document.getElementById('mem-correct-err').textContent = 'could not save';
}

function memOpenSens(uuid) {
  const c = claimByUuid(uuid);
  modalState = {uuid};
  document.getElementById('mem-sens-input').value = (c && c.sensitivity) || 'private';
  openBackdrop(); document.getElementById('mem-sens-modal').hidden = false;
}
function memCloseSens() { document.getElementById('mem-sens-modal').hidden = true; closeBackdrop(); modalState = {}; }
async function memConfirmSens() {
  const v = document.getElementById('mem-sens-input').value;
  const res = await doAction(modalState.uuid, 'sensitivity', {sensitivity: v});
  if (res) memCloseSens();
}

function memOpenExpiry(uuid) {
  modalState = {uuid};
  document.getElementById('mem-expiry-input').value = '';
  openBackdrop(); document.getElementById('mem-expiry-modal').hidden = false;
}
function memCloseExpiry() { document.getElementById('mem-expiry-modal').hidden = true; closeBackdrop(); modalState = {}; }
async function memConfirmExpiry(clear) {
  let when = null;
  if (!clear) {
    const v = document.getElementById('mem-expiry-input').value;
    if (!v) { memToast('pick a date or use Clear'); return; }
    when = new Date(v).toISOString();
  }
  const res = await doAction(modalState.uuid, 'expiry', {expires_at: when});
  if (res) memCloseExpiry();
}

function memOpenReject(uuid) {
  const c = claimByUuid(uuid);
  modalState = {uuid};
  document.getElementById('mem-reject-msg').textContent = c ? c.text : '';
  openBackdrop(); document.getElementById('mem-reject-modal').hidden = false;
}
function memCloseReject() { document.getElementById('mem-reject-modal').hidden = true; closeBackdrop(); modalState = {}; }
async function memConfirmReject() {
  const res = await doAction(modalState.uuid, 'reject', {});
  if (res) memCloseReject();
}

// --- modal dismissal (guarded: backdrop/Esc only when clean) ---------------
function closeOpenModal() {
  if (!document.getElementById('mem-correct-modal').hidden) memCloseCorrect();
  if (!document.getElementById('mem-sens-modal').hidden) memCloseSens();
  if (!document.getElementById('mem-expiry-modal').hidden) memCloseExpiry();
  if (!document.getElementById('mem-reject-modal').hidden) memCloseReject();
}
function openModalDirty() {
  if (!document.getElementById('mem-correct-modal').hidden) return document.getElementById('mem-correct-input').value !== (modalState.initial || '');
  if (!document.getElementById('mem-expiry-modal').hidden) return document.getElementById('mem-expiry-input').value !== '';
  return false;  // sensitivity (a select) and reject (confirm-only) are never "dirty"
}
function dismissIfClean() { if (!openModalDirty()) closeOpenModal(); }

// --- url deep-link ---------------------------------------------------------
function syncUrl() {
  const u = new URL(window.location);
  if (currentClaimUuid) u.searchParams.set('id', currentClaimUuid);
  else u.searchParams.delete('id');
  history.replaceState(null, '', u);
}
function restoreFromUrl() {
  const id = new URL(window.location).searchParams.get('id');
  if (id && claimByUuid(id)) { openClaim(id); return true; }
  return false;
}

// --- init ------------------------------------------------------------------
document.getElementById('mem-all').addEventListener('click', selectAll);
['mem-filter-text', 'mem-filter-scope', 'mem-filter-kind', 'mem-filter-sens'].forEach(id =>
  document.getElementById(id).addEventListener('input', () => {
    renderTree();
    if (selectedGroup === 'all') selectAll();
    else if (selectedGroup) renderTable(STATUS_LABEL[selectedGroup] || selectedGroup,
      filteredClaims().filter(c => c.status === selectedGroup));
  }));
document.getElementById('mem-correct-input').addEventListener('input', memCorrectSyncSave);
document.getElementById('ui-modal-backdrop').addEventListener('click', dismissIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') { dismissIfClean(); closeMenu(); } });
document.addEventListener('click', e => { if (openMenuEl && !e.target.closest('.mem-menu') && !e.target.closest('.mem-kebab')) closeMenu(); });

hydrate().then(() => { if (!restoreFromUrl()) selectAll(); });
