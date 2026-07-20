/* /memory/developer page logic.
 *
 * One action: POST the typed query to /memory/api/developer/query and render
 * the two pipeline results side by side (assistant memory_query on the left,
 * query_filter_router stage-by-stage on the right). The last query is kept in
 * localStorage so a page reload doesn't lose it.
 */

const MEMDEV_QUERY_KEY = 'memoryDeveloper.lastQuery';

function memdevEscape(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function memdevBadge(text, cls) {
  return '<span class="memdev-badge ' + (cls || '') + '">' + memdevEscape(text) + '</span>';
}

function memdevSection(label, bodyHtml) {
  return '<div class="memdev-section">' +
    '<div class="memdev-section-label">' + memdevEscape(label) + '</div>' +
    bodyHtml + '</div>';
}

function memdevPre(text) {
  return '<pre class="memdev-pre">' + memdevEscape(text) + '</pre>';
}

// --- left panel: assistant memory_query ------------------------------------
function memdevRenderAssistant(a) {
  const parts = [];
  const badges = [memdevBadge(a.elapsed_ms + ' ms')];
  if (a.error) {
    badges.push(memdevBadge('error', 'bad'));
  } else {
    badges.push(memdevBadge(a.ok ? 'ok' : 'not ok', a.ok ? 'good' : 'bad'));
  }
  const d = a.data || {};
  if (d.qa_static != null) badges.push(memdevBadge('seed static: ' + d.qa_static));
  if (d.qa_dynamic != null) badges.push(memdevBadge('seed dynamic: ' + d.qa_dynamic));
  if (d.memory != null) badges.push(memdevBadge('claims: ' + d.memory));
  if (d.truncated) badges.push(memdevBadge('truncated: ' + d.truncated, 'warn'));
  if (d.omitted) badges.push(memdevBadge('omitted: ' + d.omitted, 'warn'));
  const sf = d.seed_filter || {};
  if (sf.mode) {
    const label = 'seed filter: ' + sf.mode + (sf.reason ? ' (' + sf.reason + ')' : '');
    badges.push(memdevBadge(label, sf.mode === 'llm' ? 'good' : 'warn'));
  }
  parts.push('<div class="memdev-meta">' + badges.join('') + '</div>');
  if (a.error) {
    parts.push(memdevSection('error', '<div class="err">' + memdevEscape(a.error) + '</div>'));
  }
  if ((sf.candidates || []).length) {
    const keptIds = sf.candidates.filter(c => c.kept).map(c => c.qa_id);
    parts.push(memdevSection('seed candidates + LLM filter',
      memdevCandidateTable(sf.candidates, keptIds)));
  }
  if (a.text) {
    parts.push(memdevSection('observation text (what the assistant model sees)', memdevPre(a.text)));
  } else if (!a.error) {
    parts.push('<p class="memdev-empty">Empty response.</p>');
  }
  return parts.join('');
}

// --- right panel: query_filter_router --------------------------------------
function memdevCandidateTable(candidates, keptIds) {
  if (!candidates.length) {
    return '<p class="memdev-empty">No semantic candidates.</p>';
  }
  const kept = new Set(keptIds || []);
  const rows = candidates.map(c => {
    const detail = c.kind === 'dynamic'
      ? 'handler: ' + memdevEscape(c.handler || '')
      : memdevEscape(c.answer_preview || '');
    return '<tr class="' + (kept.has(c.qa_id) ? 'kept' : '') + '">' +
      '<td class="num">' + memdevEscape(c.score) + '</td>' +
      '<td><code>' + memdevEscape(c.qa_id) + '</code>' +
      (c.path ? '<br>' + memdevEscape(c.path) : '') + '</td>' +
      '<td>' + memdevEscape(c.kind) + '</td>' +
      '<td>' + memdevEscape(c.matched_question || '') + '<br>' +
      '<span class="muted">' + detail + '</span></td>' +
      '<td>' + (kept.has(c.qa_id) ? 'kept' : 'dropped') + '</td></tr>';
  });
  return '<table class="memdev-table"><thead><tr>' +
    '<th>score</th><th>qa_id / path</th><th>kind</th>' +
    '<th>matched question / answer</th><th>filter</th>' +
    '</tr></thead><tbody>' + rows.join('') + '</tbody></table>';
}

function memdevRenderRouter(r) {
  const parts = [];
  const badges = [memdevBadge(r.elapsed_ms + ' ms')];
  if (r.error) badges.push(memdevBadge('error', 'bad'));
  if (r.memory_command) badges.push(memdevBadge('memory command: ' + r.memory_command, 'warn'));
  if (r.exact) badges.push(memdevBadge('exact match', 'good'));
  parts.push('<div class="memdev-meta">' + badges.join('') + '</div>');

  if (r.error) {
    parts.push(memdevSection('error', '<div class="err">' + memdevEscape(r.error) + '</div>'));
    return parts.join('');
  }
  if (r.memory_command) {
    parts.push('<p class="muted">In a chatroom this query would run as the ' +
      '<code>' + memdevEscape(r.memory_command) + '</code> memory command and never ' +
      'reach retrieval. The stages below show what retrieval would have surfaced.</p>');
  }
  if (r.exact) {
    parts.push(memdevSection('1 · exact alias match (no LLM stages run)',
      '<table class="memdev-table"><tbody>' +
      '<tr><th>qa_id</th><td><code>' + memdevEscape(r.exact.qa_id) + '</code></td></tr>' +
      '<tr><th>score</th><td>' + memdevEscape(r.exact.score) + '</td></tr>' +
      '<tr><th>matched</th><td>' + memdevEscape(r.exact.matched_question || '') + '</td></tr>' +
      '</tbody></table>'));
    parts.push(memdevSection('reply', memdevPre(r.exact.reply || '')));
    return parts.join('');
  }

  parts.push(memdevSection('1 · semantic candidates + 2 · LLM filter',
    memdevCandidateTable(r.candidates || [], r.filter_kept || [])));
  if (r.filter_error) {
    parts.push(memdevSection('filter error', '<div class="err">' + memdevEscape(r.filter_error) + '</div>'));
  }

  const resolvedIds = Object.keys(r.resolved || {});
  if (resolvedIds.length) {
    const body = resolvedIds.map(id =>
      '<div class="memdev-section-label"><code>' + memdevEscape(id) + '</code></div>' +
      memdevPre(r.resolved[id])).join('');
    parts.push(memdevSection('3 · resolved replies for kept candidates', body));
  }

  if (r.route) {
    parts.push(memdevSection('4 · route LLM (synthetic one-message transcript)',
      '<table class="memdev-table"><tbody>' +
      '<tr><th>subject</th><td>' + memdevEscape(r.route.subject || '') + '</td></tr>' +
      '<tr><th>action</th><td>' + memdevEscape(r.route.action || '') + '</td></tr>' +
      '</tbody></table>'));
    parts.push(memdevSection('reply', memdevPre(r.route.reply || '')));
  } else if (r.route_error) {
    parts.push(memdevSection('route error', '<div class="err">' + memdevEscape(r.route_error) + '</div>'));
  }
  return parts.join('');
}

// --- run -------------------------------------------------------------------
async function memdevRun() {
  const input = document.getElementById('memdev-query');
  const button = document.getElementById('memdev-run');
  const assistantOut = document.getElementById('memdev-assistant-out');
  const routerOut = document.getElementById('memdev-router-out');
  const query = input.value.trim();
  if (!query) { input.focus(); return; }
  try { localStorage.setItem(MEMDEV_QUERY_KEY, query); } catch (_) {}
  button.disabled = true;
  button.textContent = 'Running…';
  assistantOut.innerHTML = '<p class="memdev-empty">Running…</p>';
  routerOut.innerHTML = '<p class="memdev-empty">Running… (two LLM calls; can take a while)</p>';
  try {
    const resp = await fetch('/memory/api/developer/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query}),
    });
    const data = await resp.json();
    if (!resp.ok) {
      const msg = '<div class="err">' + memdevEscape(data.error || ('HTTP ' + resp.status)) + '</div>';
      assistantOut.innerHTML = msg;
      routerOut.innerHTML = msg;
      return;
    }
    assistantOut.innerHTML = memdevRenderAssistant(data.assistant || {});
    routerOut.innerHTML = memdevRenderRouter(data.filter_router || {});
  } catch (e) {
    const msg = '<div class="err">' + memdevEscape(String(e)) + '</div>';
    assistantOut.innerHTML = msg;
    routerOut.innerHTML = msg;
  } finally {
    button.disabled = false;
    button.textContent = 'Run';
  }
}

document.getElementById('memdev-query').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') memdevRun();
});
try {
  const last = localStorage.getItem(MEMDEV_QUERY_KEY);
  if (last) document.getElementById('memdev-query').value = last;
} catch (_) {}
