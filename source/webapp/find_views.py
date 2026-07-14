"""The /find page + API — paste a uuid (or a fragment of one), learn what it
is, and jump to it.

Backed by db.find_uuid: exact / substring / typo-tolerant matching across
every uuid-bearing table, no need to know which table to search. Each result
shows the entity's kind, name, parent chain, and (when a page exists for it)
an Open link to its `?id=` deep link. Read-only.

NOTE: this template is a plain (non-raw) Python string — no backslash
escapes in the inline JS; results are rendered via DOM APIs (textContent),
never innerHTML, so entity names can't inject markup.
"""

from flask import jsonify, render_template_string, request

import db

from .core import app

FIND_TEMPLATE = """
<!doctype html>
<title>Find &mdash; rainbox</title>
{% include "_nav.html" %}
<style>
  body { margin: 0; font-family: system-ui, sans-serif; }
  .pp-find { max-width: 900px; margin: 1rem auto; padding: 0 1rem; }
  .pp-find h1 { margin: 0.2rem 0 0.6rem; }
  .pp-find .hint { color: #6c757d; margin-bottom: 0.8rem; }
  .pp-find .bar { display: flex; gap: 0.6rem; }
  .pp-find input { flex: 1 1 auto; font: inherit; font-family: ui-monospace, monospace;
                   padding: 0.5rem 0.7rem; border: 1px solid #cbd5e1; border-radius: 8px; }
  .pp-find button { font: inherit; padding: 0.45rem 1rem; cursor: pointer;
                    border: 1px solid #cbd5e1; border-radius: 8px; background: #fff; }
  .pp-find button:hover { border-color: #9aa3af; }
  .pp-find .status { margin: 0.8rem 0; color: #6c757d; }
  .pp-find .status.err { color: #c0392b; }
  .pp-find table { border-collapse: collapse; width: 100%; margin-top: 0.4rem; }
  .pp-find th { text-align: left; font-size: 0.8rem; color: #6c757d;
                text-transform: uppercase; letter-spacing: 0.04em;
                padding: 0.4rem 0.6rem; border-bottom: 2px solid #e5e7eb; }
  .pp-find td { padding: 0.5rem 0.6rem; border-bottom: 1px solid #eee;
                vertical-align: top; }
  .pp-find td.kind { white-space: nowrap; color: #374151; font-weight: 600; }
  .pp-find td.match { white-space: nowrap; color: #6c757d; font-size: 0.85rem; }
  .pp-find code { font-family: ui-monospace, monospace; font-size: 0.85rem;
                  background: #f6f8fa; padding: 1px 5px; border-radius: 4px; }
  .pp-find .parents { color: #6c757d; font-size: 0.85rem; margin-top: 0.15rem; }
  .pp-find a.open { color: #2563eb; text-decoration: none; white-space: nowrap; }
  .pp-find a.open:hover { text-decoration: underline; }
</style>
<main class="pp-find">
  <h1>Find</h1>
  <div class="hint">Paste a uuid — or a fragment of one (beginning, end, middle,
    even with a typo) — and find out what it is, wherever it lives.</div>
  <div class="bar">
    <input id="find-q" type="text" autocomplete="off" spellcheck="false"
           placeholder="e.g. 213a2397-8187-4596-8f32-c6ca22d7c5f8 or 213a2397" autofocus>
    <button type="button" id="find-go">Find</button>
  </div>
  <div class="status" id="find-status"></div>
  <table id="find-results" hidden>
    <thead><tr><th>Kind</th><th>What it is</th><th>Match</th><th></th></tr></thead>
    <tbody></tbody>
  </table>
</main>
<script>
'use strict';
const fQ = document.getElementById('find-q');
const fStatus = document.getElementById('find-status');
const fTable = document.getElementById('find-results');
const fBody = fTable.querySelector('tbody');
let fTimer = null;
let fSeq = 0;

function fSetStatus(text, isErr){
  fStatus.textContent = text;
  fStatus.className = 'status' + (isErr ? ' err' : '');
}

// Keep the url in step with the search box (?q=<fragment>), so the address
// bar is always a permanent link to the current search.
function fSyncUrl(q){
  const url = new URL(window.location);
  if (q) url.searchParams.set('q', q); else url.searchParams.delete('q');
  history.replaceState(null, '', url);
}

async function fSearch(){
  const q = fQ.value.trim();
  fSyncUrl(q);
  fBody.innerHTML = '';
  fTable.hidden = true;
  if (!q){ fSetStatus(''); return; }
  const seq = ++fSeq;
  fSetStatus('searching…');
  let data = null;
  try {
    const r = await fetch('/find/api/search?q=' + encodeURIComponent(q));
    data = await r.json();
  } catch (e) { /* fall through */ }
  if (seq !== fSeq) return;  // a newer search superseded this one
  if (!data || data.ok === false){
    fSetStatus((data && data.error) || 'Search failed.', true);
    return;
  }
  const matches = data.matches || [];
  if (!matches.length){ fSetStatus('No matches.'); return; }
  fSetStatus(matches.length + ' match(es)');
  matches.forEach(m => {
    const tr = document.createElement('tr');
    const kindTd = document.createElement('td');
    kindTd.className = 'kind';
    kindTd.textContent = m.kind;
    const whatTd = document.createElement('td');
    const name = document.createElement('div');
    name.textContent = m.name || '(unnamed)';
    whatTd.appendChild(name);
    const code = document.createElement('code');
    code.textContent = m.uuid;
    whatTd.appendChild(code);
    if (m.parents && m.parents.length){
      const par = document.createElement('div');
      par.className = 'parents';
      par.textContent = 'in: ' + m.parents.map(p =>
        p.kind + ' “' + (p.name || '(unnamed)') + '”').join(' → ');
      whatTd.appendChild(par);
    }
    const matchTd = document.createElement('td');
    matchTd.className = 'match';
    matchTd.textContent = m.match + ' ' + Math.round(m.confidence * 100) + '%';
    const openTd = document.createElement('td');
    if (m.url){
      const a = document.createElement('a');
      a.className = 'open';
      a.href = m.url;
      a.textContent = 'Open';
      openTd.appendChild(a);
    }
    tr.appendChild(kindTd);
    tr.appendChild(whatTd);
    tr.appendChild(matchTd);
    tr.appendChild(openTd);
    fBody.appendChild(tr);
  });
  fTable.hidden = false;
}

fQ.addEventListener('input', () => {
  clearTimeout(fTimer);
  fTimer = setTimeout(fSearch, 400);
});
fQ.addEventListener('keydown', e => {
  if (e.key === 'Enter'){ clearTimeout(fTimer); fSearch(); }
});
document.getElementById('find-go').addEventListener('click', () => {
  clearTimeout(fTimer); fSearch();
});
// Deep link: /find?q=<fragment> searches on load.
const fWant = new URLSearchParams(window.location.search).get('q');
if (fWant){ fQ.value = fWant; fSearch(); }
</script>
"""


@app.route("/find")
def find_page() -> str:
    return render_template_string(FIND_TEMPLATE)


@app.route("/find/api/search")
def find_api_search():
    """Resolve ?q= (a uuid or fragment) across every uuid-bearing table.
    {ok, matches: [{kind, uuid, name, url, parents, match, confidence}]};
    a too-short query is a 400."""
    q = request.args.get("q", "")
    try:
        matches = db.find_uuid(q)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "matches": matches})
