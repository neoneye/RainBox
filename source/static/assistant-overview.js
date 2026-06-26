// /assistant-overview page logic (vanilla JS, no framework). The HTML shell +
// CSS live in webapp/assistant_overview_views.py; this file is served at
// /static/assistant-overview.js with an mtime cache-buster. It hydrates a
// dense, sortable, paginated table from /assistant-overview/api/runs and links
// each row to the inspector at /assistant?id=<uuid>. Server-side filtering and
// paging scale past the inspector's 50-run left panel.
'use strict';

const aoState = { q: '', status: 'all', range: 'all', sort: 'started', dir: 'desc', page: 1, perPage: 25 };

const AO_TABS = [
  ['all', 'All'], ['running', 'Running'], ['stopped', 'Stopped'],
  ['resolved', 'Resolved'], ['unresolved', 'Unresolved'],
];

const aoEl = (id) => document.getElementById(id);

function aoChip(label, kind) {
  const span = document.createElement('span');
  span.className = 'ao-chip ' + kind;
  if (kind === 'running') {
    const dot = document.createElement('span');
    dot.className = 'ao-dot';
    span.appendChild(dot);
  }
  span.appendChild(document.createTextNode(label));
  return span;
}

function aoRow(run) {
  const tr = document.createElement('tr');
  tr.onclick = () => { location.href = '/assistant?id=' + encodeURIComponent(run.uuid); };

  const date = document.createElement('td');
  const d1 = document.createElement('div');
  d1.className = 'ao-date';
  d1.textContent = run.started_date;
  const d2 = document.createElement('div');
  d2.className = 'ao-time';
  d2.textContent = run.started_time;
  date.append(d1, d2);

  const status = document.createElement('td');
  status.appendChild(aoChip(run.status_label, run.status_kind));

  const sum = document.createElement('td');
  const s = document.createElement('div');
  s.className = 'ao-sum' + (run.summary ? '' : ' pending');
  s.textContent = run.summary || 'summarizing…';
  sum.appendChild(s);

  const steps = document.createElement('td');
  steps.className = 'ao-mono';
  steps.textContent = run.steps;

  const dur = document.createElement('td');
  dur.className = 'ao-mono';
  dur.textContent = run.duration || '—';

  tr.append(date, status, sum, steps, dur);
  return tr;
}

function aoRenderTabs(counts) {
  const wrap = aoEl('ao-tabs');
  wrap.textContent = '';
  AO_TABS.forEach(([key, label]) => {
    const b = document.createElement('button');
    b.className = 'ao-tab' + (aoState.status === key ? ' sel' : '');
    b.textContent = label;
    const ct = document.createElement('span');
    ct.className = 'ct';
    ct.textContent = (counts && counts[key] != null) ? counts[key] : 0;
    b.appendChild(ct);
    b.onclick = () => { aoState.status = key; aoState.page = 1; aoLoad(); };
    wrap.appendChild(b);
  });
}

function aoRenderHeaders() {
  document.querySelectorAll('#ao-table th.sortable').forEach((th) => {
    const key = th.dataset.sort;
    const base = th.textContent.replace(/[↑↓]\s*$/, '').trim();
    th.textContent = base + (aoState.sort === key ? (aoState.dir === 'asc' ? ' ↑' : ' ↓') : '');
    th.onclick = () => {
      if (aoState.sort === key) {
        aoState.dir = aoState.dir === 'asc' ? 'desc' : 'asc';
      } else {
        aoState.sort = key;
        aoState.dir = key === 'summary' ? 'asc' : 'desc';
      }
      aoState.page = 1;
      aoLoad();
    };
  });
}

function aoRenderPager(total, page, pages) {
  const range = aoEl('ao-range');
  const pager = aoEl('ao-pager');
  pager.textContent = '';
  const from = total === 0 ? 0 : (page - 1) * aoState.perPage + 1;
  const to = Math.min(page * aoState.perPage, total);
  range.textContent = 'Showing ' + from + '–' + to + ' of ' + total + ' runs';

  const btn = (label, disabled, onClick, sel) => {
    const b = document.createElement('button');
    b.className = 'ao-pg' + (sel ? ' sel' : '');
    b.textContent = label;
    b.disabled = disabled;
    if (!disabled) b.onclick = onClick;
    return b;
  };
  pager.appendChild(btn('‹ Prev', page <= 1,
    () => { aoState.page = page - 1; aoLoad(); }));
  for (let n = 1; n <= pages; n++) {
    pager.appendChild(btn(String(n), false,
      () => { aoState.page = n; aoLoad(); }, n === page));
  }
  pager.appendChild(btn('Next ›', page >= pages,
    () => { aoState.page = page + 1; aoLoad(); }));
}

function aoRender(data) {
  aoRenderTabs(data.counts);
  aoRenderHeaders();
  const body = aoEl('ao-body');
  body.textContent = '';
  (data.runs || []).forEach((r) => body.appendChild(aoRow(r)));

  const empty = (data.runs || []).length === 0;
  aoEl('ao-table').hidden = empty;
  aoEl('ao-empty').hidden = !empty;
  aoEl('ao-foot').hidden = empty;
  if (!empty) aoRenderPager(data.total, data.page, data.pages);
}

// Reflect the active filter/sort/page into the URL (defaults omitted) so the
// view is shareable and reloadable. replaceState, not push, so live typing
// doesn't flood the back-button history.
function aoSyncUrl() {
  const p = new URLSearchParams();
  if (aoState.q) p.set('q', aoState.q);
  if (aoState.status !== 'all') p.set('status', aoState.status);
  if (aoState.range !== 'all') p.set('range', aoState.range);
  if (aoState.sort !== 'started') p.set('sort', aoState.sort);
  if (aoState.dir !== 'desc') p.set('dir', aoState.dir);
  if (aoState.page > 1) p.set('page', aoState.page);
  const qs = p.toString();
  history.replaceState(null, '', qs ? '?' + qs : location.pathname);
}

// Seed state from the URL on load, validating each param against the values
// the UI actually offers (so a hand-edited link can't wedge the page).
function aoReadUrl() {
  const p = new URLSearchParams(location.search);
  aoState.q = p.get('q') || '';
  const statuses = AO_TABS.map((t) => t[0]);
  if (statuses.includes(p.get('status'))) aoState.status = p.get('status');
  const sorts = [...document.querySelectorAll('#ao-table th.sortable')]
    .map((th) => th.dataset.sort);
  if (sorts.includes(p.get('sort'))) aoState.sort = p.get('sort');
  if (p.get('dir') === 'asc' || p.get('dir') === 'desc') aoState.dir = p.get('dir');
  const pg = parseInt(p.get('page'), 10);
  if (pg > 0) aoState.page = pg;
  // Validate range by assigning to the <select> and reading what stuck.
  const rangeSel = aoEl('ao-range-select');
  if (p.get('range')) { rangeSel.value = p.get('range'); aoState.range = rangeSel.value; }
}

async function aoLoad() {
  aoSyncUrl();
  const p = new URLSearchParams({
    q: aoState.q, status: aoState.status, range: aoState.range,
    sort: aoState.sort, dir: aoState.dir, page: aoState.page,
    per_page: aoState.perPage,
  });
  try {
    const r = await fetch('/assistant-overview/api/runs?' + p.toString());
    const data = await r.json();
    if (!data || !data.ok) return;
    aoRender(data);
  } catch (e) { /* transient: next interaction retries */ }
}

let aoSearchTimer = null;
function aoInit() {
  aoReadUrl();
  aoEl('ao-search').value = aoState.q;
  aoEl('ao-range-select').value = aoState.range;
  aoRenderTabs(null);
  aoRenderHeaders();
  aoEl('ao-search').addEventListener('input', (e) => {
    clearTimeout(aoSearchTimer);
    const v = e.target.value;
    aoSearchTimer = setTimeout(() => {
      aoState.q = v;
      aoState.page = 1;
      aoLoad();
    }, 250);
  });
  aoEl('ao-range-select').addEventListener('change', (e) => {
    aoState.range = e.target.value;
    aoState.page = 1;
    aoLoad();
  });
  aoLoad();
}

document.addEventListener('DOMContentLoaded', aoInit);
