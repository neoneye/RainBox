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
let tombstoneHits = [];          // [{uuid, claim_text, hit_count, ...}] from the API
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
  tombstoneHits = data.tombstone_hits || [];
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
  // A real anchor so CMD/Ctrl/middle click opens the group view in a new
  // tab (?id=<status>); a plain click is intercepted below and
  // selects/toggles in-page.
  const node = document.createElement('a');
  node.className = 'mem-node' + (selectedGroup === status ? ' sel' : '');
  node.href = '/memory?id=' + encodeURIComponent(status);
  const open = isExpanded(status) && list.length > 0;
  node.innerHTML = '<span class="mem-ficon">' + (open ? MEM_ICON_FOLDER_OPEN : MEM_ICON_FOLDER) + '</span>' +
    '<span>' + escapeHtml(STATUS_LABEL[status] || status) + '</span>' +
    '<span class="mem-group-count">' + list.length + '</span>';
  node.addEventListener('click', (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
    groupClick(status, list.length);
  });
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
  // A real anchor so CMD/Ctrl/middle click opens the claim in a new tab; a
  // plain click is intercepted below and opens the claim in-page instead.
  const node = document.createElement('a');
  node.className = 'mem-claim-node' + (currentClaimUuid === c.uuid ? ' sel' : '');
  node.href = '/memory?id=' + encodeURIComponent(c.uuid);
  const label = c.text + (c.stale ? '  (stale)' : '');
  node.innerHTML = '<span class="mem-claim-label" title="' + escapeHtml(c.text) + '">' +
    escapeHtml(label) + '</span>' +
    '<button class="mem-kebab" title="Actions"></button>';
  node.addEventListener('click', (e) => {
    // The kebab sits inside the anchor — never follow the link from it.
    if (e.target.closest('.mem-kebab')) { e.stopPropagation(); e.preventDefault(); openClaimMenu(c, e.target.closest('.mem-kebab')); return; }
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
    e.preventDefault();
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
  const badge = (cls, t, tip) => '<span class="mem-badge ' + cls + '"' +
    (tip ? ' title="' + escapeHtml(tip) + '"' : '') + '>' + escapeHtml(t) + '</span>';
  const STATUS_TIPS = {
    active: 'Status: retrievable — this memory can be recalled and injected into prompts.',
    candidate: 'Status: proposed but not yet trusted. Not retrieved until you Activate it (or Reject it).',
    superseded: 'Status: replaced by a newer correction; kept as history. Not retrieved.',
    rejected: 'Status: forgotten — excluded from retrieval; a tombstone suppresses re-learning the same value.',
    expired: 'Status: past its expiry timestamp. Not retrieved; can be reactivated.',
  };
  const SCOPE_TIPS = {
    global: 'Scope: recallable in every chatroom and for every agent.',
    room: 'Scope: recallable ONLY inside its own chatroom (named after the dot). Change via Scope… to widen.',
    agent: 'Scope: recallable only for its owning agent, in any room.',
    project: 'Scope: currently never retrieved — no project context exists yet.',
  };
  const SENS_TIPS = {
    public: 'Sensitivity: no restrictions on recall or display.',
    private: 'Sensitivity: recalled normally; treated as operator-private data.',
    secret: 'Sensitivity: never injected into prompts unless secrets are explicitly included; masked in lists.',
  };
  let badges = '<div class="mem-badges">' +
    badge('status-' + d.status, d.status, STATUS_TIPS[d.status]) +
    badge('', d.scope + (d.room_name ? ' · ' + d.room_name : ''), SCOPE_TIPS[d.scope]) +
    badge('', d.kind, 'Kind: what sort of memory this is (fact, preference, project_decision, procedure, episode_summary).') +
    badge(d.sensitivity === 'secret' ? 'sens-secret' : '', d.sensitivity, SENS_TIPS[d.sensitivity]) +
    badge('', 'conf ' + d.confidence, 'Confidence (0..1): how certain the system is about this memory; a ranking tie-breaker during retrieval.') +
    (d.stale ? badge('stale', 'stale (expired)', 'The expiry timestamp has passed — this memory is due for cleanup and is not retrieved.') : '') +
    (d.conflicts_with_uuid ? badge('conflict', 'conflict', 'Contradicts another claim — resolve it in the conflict section below.') : '') + '</div>';

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

  const kpis = recallKpisHtml(d);

  let ret = '<div class="mem-section"><div class="mem-section-label">Retrieval</div>';
  if (!d.retrieval.length) ret += '<div class="muted">no retrieval recorded</div>';
  d.retrieval.forEach(r => {
    ret += '<div class="mem-ret-row"><b>' + escapeHtml(r.stage) + '</b>' +
      (r.source ? ' · ' + escapeHtml(r.source) : '') +
      (r.query ? ' · <span class="muted">' + escapeHtml(r.query) + '</span>' : '') +
      ' · <span class="muted">' + fmt(r.created_at) + '</span></div>';
  });
  ret += '</div>';

  const conflictHtml = conflictSectionHtml(d);
  const tombstoneHtml = tombstoneHitsHtml(d);
  el.innerHTML = textHtml + badges + actionsHtml(d) + conflictHtml + ts + lineage + emb + kpis + evi + ret + tombstoneHtml;
}

// Recall KPIs: the recall filter's verdict FIFOs for this memory. A
// "false positive" is a verdict where retrieval surfaced the memory but the
// filter judged it irrelevant to the query — the starting point for
// troubleshooting "why does this show up when it shouldn't".
function recallKpisHtml(d) {
  const k = d.recall_kpis;
  if (!k) return '';
  const total = k.used_count + k.rejected_count;
  const fpRate = total ? Math.round(100 * k.rejected_count / total) : 0;
  const row = (e) => '<div class="mem-ret-row">' +
    (e.scales ? '<b title="direct/indirect/relevancy Likert scores">' + escapeHtml(e.scales) + '</b> · ' : '') +
    escapeHtml(e.query || '(no query)') +
    (e.signals ? ' · <span class="muted">' + escapeHtml(e.signals) + '</span>' : '') +
    ' · <span class="muted">' + fmt(e.created_at) + '</span></div>';
  let html = '<div class="mem-section"><div class="mem-section-label">Recall KPIs' +
    ' <span class="mem-group-count">last ' + k.capacity + ' verdicts each · ' +
    'false-positive rate ' + fpRate + '%</span></div>';
  html += '<div class="mem-section-label" style="margin-top:6px">False positives — surfaced but judged irrelevant (' + k.rejected_count + ')</div>';
  html += k.last_rejected.length ? k.last_rejected.map(row).join('')
    : '<div class="muted">none recorded</div>';
  html += '<div class="mem-section-label" style="margin-top:10px">True positives — recalled and used (' + k.used_count + ')</div>';
  html += k.last_used.length ? k.last_used.map(row).join('')
    : '<div class="muted">none recorded</div>';
  return html + '</div>';
}

function actionsHtml(d) {
  const btns = [];
  const act = (label, cls, fn) => '<button class="' + cls + '" onclick="' + fn + '">' + label + '</button>';
  if (d.status === 'candidate') {
    // A conflict candidate must be resolved via the conflict buttons (rendered
    // separately), not activated directly — Activate would leave two conflicting
    // active beliefs and a dangling conflict pointer (the server refuses it too).
    if (!d.conflicts_with_uuid) {
      btns.push(act('Activate', '', 'memActivate(\'' + d.uuid + '\')'));
    }
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
    btns.push(act('Scope…', 'secondary', 'memOpenScope(\'' + d.uuid + '\')'));
    btns.push(act('Expiry…', 'secondary', 'memOpenExpiry(\'' + d.uuid + '\')'));
  }
  return '<div class="mem-section"><div class="mem-section-label">Actions</div><div class="mem-actions">' +
    (btns.join('') || '<span class="muted">read-only</span>') + '</div></div>';
}

function conflictSectionHtml(d) {
  if (!d.conflicts_with_uuid) return '';
  const rival = claimByUuid(d.conflicts_with_uuid);
  const rivalText = rival ? rival.text : d.conflicts_with_uuid;
  const btn = (label, res, cls) =>
    '<button class="' + cls + '" onclick="memResolveConflict(\'' + d.uuid + '\',\'' + res + '\')">' + label + '</button>';
  return '<div class="mem-section">' +
    '<div class="mem-section-label">Conflict resolution</div>' +
    '<div class="muted" style="margin-bottom:6px">Conflicts with: <a onclick="openClaim(\'' + d.conflicts_with_uuid + '\')">' +
    escapeHtml(rivalText) + '</a></div>' +
    '<div class="mem-actions">' +
    btn('Supersede rival', 'supersede', '') +
    btn('Not a conflict', 'not_conflict', 'secondary') +
    btn('Reject this claim', 'reject', 'danger') +
    btn('Scoped exception', 'scoped_exception', 'secondary') +
    '</div></div>';
}

function tombstoneHitsHtml(d) {
  // Show tombstones that match this claim's room (or global if no room) AND
  // whose subj_pred_key matches this claim's key (skip free-text hits with empty key).
  const matching = tombstoneHits.filter(t => {
    const roomMatch = (d.room_uuid ? t.room_uuid === d.room_uuid : t.room_uuid === null) ||
      t.room_uuid === null;
    const keyMatch = t.subj_pred_key && d.subj_pred_key && t.subj_pred_key === d.subj_pred_key;
    return roomMatch && keyMatch;
  });
  if (!matching.length) return '';
  let html = '<div class="mem-section"><div class="mem-section-label">Suppressed re-assertions (' + matching.length + ')</div>';
  matching.forEach(t => {
    html += '<div class="mem-evi-row"><b>' + escapeHtml(t.claim_text) + '</b>' +
      ' · suppressed <b>' + t.hit_count + '</b>×' +
      (t.reason ? ' · ' + escapeHtml(t.reason) : '') +
      ' · <span class="muted">last ' + fmt(t.last_hit_at) + '</span></div>';
  });
  html += '</div>';
  return html;
}

function fmt(iso) { if (!iso) return '—'; try { const d = new Date(iso); if (isNaN(d)) return iso; const p = (n) => String(n).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()); } catch (_) { return iso; } }
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
  if (c.status === 'candidate') {
    // Conflict candidates can't be activated directly (server refuses); they
    // must be resolved from the detail pane's conflict buttons.
    if (!c.conflicts_with_uuid) items.push(it('Activate', false, 'activate'));
    items.push(it('Correct…', false, 'correct'));
    items.push(it('Reject', true, 'reject'));
  }
  else if (c.status === 'active') { items.push(it('Correct…', false, 'correct')); items.push(it('Forget', true, 'reject')); }
  else if (c.status === 'rejected' || c.status === 'expired') { items.push(it('Reactivate', false, 'reactivate')); }
  items.push(it('Copy memory id', false, 'copyid'));  // always available
  menu.innerHTML = items.join('');
  document.body.appendChild(menu);
  // Clamp inside the viewport: below the anchor when it fits, flipped above
  // when it would overflow the bottom edge (leaves at the bottom of the tree).
  const rect = anchor.getBoundingClientRect();
  const margin = 6;
  menu.style.left = Math.max(margin,
    Math.min(rect.left, window.innerWidth - menu.offsetWidth - margin)) + 'px';
  let top = rect.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - margin){
    top = rect.top - menu.offsetHeight - 4;
  }
  menu.style.top = Math.max(margin, top) + 'px';
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
async function memResolveConflict(uuid, resolution) {
  const r = await fetch('/api/memory/' + uuid + '/resolve', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({resolution}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { memToast(data.error || ('resolve failed (' + r.status + ')')); return; }
  await afterMutation(uuid);
}

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

function memOpenScope(uuid) {
  const c = claimByUuid(uuid);
  modalState = {uuid};
  const select = document.getElementById('mem-scope-input');
  select.value = (c && c.scope) || 'global';
  // Narrowing needs the matching key on the claim (the server refuses it too);
  // a claim that never had a room/agent can only be global.
  for (const opt of select.options) {
    if (opt.value === 'room') opt.disabled = !(c && c.room_uuid);
    if (opt.value === 'agent') opt.disabled = !(c && c.agent_uuid);
  }
  openBackdrop(); document.getElementById('mem-scope-modal').hidden = false;
}
function memCloseScope() { document.getElementById('mem-scope-modal').hidden = true; closeBackdrop(); modalState = {}; }
async function memConfirmScope() {
  const v = document.getElementById('mem-scope-input').value;
  const res = await doAction(modalState.uuid, 'scope', {scope: v});
  if (res) memCloseScope();
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
  if (!document.getElementById('mem-scope-modal').hidden) memCloseScope();
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
// ?id= addresses either a claim (uuid) or a status group (its status string);
// statuses can never collide with uuids. "All memories" maps to no ?id=.
function syncUrl() {
  const u = new URL(window.location);
  if (currentClaimUuid) u.searchParams.set('id', currentClaimUuid);
  else if (selectedGroup && selectedGroup !== 'all') u.searchParams.set('id', selectedGroup);
  else u.searchParams.delete('id');
  history.replaceState(null, '', u);
}
function restoreFromUrl() {
  const id = new URL(window.location).searchParams.get('id');
  if (id && claimByUuid(id)) { openClaim(id); return true; }
  if (id && STATUS_ORDER.includes(id)) { groupClick(id, 0); return true; }
  return false;
}

// --- init ------------------------------------------------------------------
document.getElementById('mem-all').addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;  // browser handles new tab/window
  e.preventDefault();
  selectAll();
});
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
