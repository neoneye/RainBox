/* /memory/developer page logic.
 *
 * One action: POST the typed query to /memory/api/developer/query and render
 * what the assistant's memory_query returned for it. The last query is kept
 * in localStorage so a page reload doesn't lose it.
 */

const MEMDEV_QUERY_KEY = 'memoryDeveloper.lastQuery';
const MEMDEV_TOPK_VECTOR_KEY = 'memoryDeveloper.topKVector';
const MEMDEV_TOPK_FULLTEXT_KEY = 'memoryDeveloper.topKFulltext';
const MEMDEV_ROOM_KEY = 'memoryDeveloper.roomUuid';

// Populate the room selector ("" = no room) and restore the saved choice once
// the options exist.
async function memdevLoadRooms() {
  try {
    const resp = await fetch('/chat/api/rooms');
    const rooms = await resp.json();
    const select = document.getElementById('memdev-room');
    for (const room of rooms) {
      const opt = document.createElement('option');
      opt.value = room.uuid;
      opt.textContent = room.name;
      select.appendChild(opt);
    }
    const saved = localStorage.getItem(MEMDEV_ROOM_KEY);
    if (saved && [...select.options].some(o => o.value === saved)) {
      select.value = saved;
    }
  } catch (_) { /* selector stays "(no room)" */ }
}

function memdevBudget(elementId) {
  // Per-signal candidate budget; 0 disables the signal. Clamp to the input's
  // own bounds; the server clamps again.
  const n = parseInt(document.getElementById(elementId).value, 10);
  if (isNaN(n)) return 5;
  return Math.max(0, Math.min(20, n));
}

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

// --- models overview -------------------------------------------------------
function memdevMemberList(info) {
  if (!info || !info.bound) return '<span class="muted">(no group bound)</span>';
  const members = (info.members || []).map(m => {
    if (m.error) return '<span class="err">' + memdevEscape(m.error) + '</span>';
    // "provider / model / override-label" — the label is the override's
    // effective display name ("t0.15 c100k struct" when unnamed); plain
    // configs have no third segment.
    let name = memdevEscape(m.provider) + ' / ' +
      memdevEscape(m.model_display_name || m.model_name);
    if (m.display_name) name += ' / ' + memdevEscape(m.display_name);
    let s = '<a href="/model?id=' + memdevEscape(m.uuid) + '">' + name + '</a>';
    if (m.available === false) s += ' <span class="err">(unavailable)</span>';
    return s;
  });
  return '<a href="/modelgroup?id=' + memdevEscape(info.uuid) + '"><b>' +
    memdevEscape(info.name) + '</b></a>' +
    ' <span class="muted">via ' + memdevEscape(info.from) + '</span><br>' +
    members.join('<br>');
}

function memdevRenderModels(m) {
  if (!m) return '';
  if (m.error) {
    return '<div class="err">' + memdevEscape(m.error) + '</div>';
  }
  const emb = memdevEscape((m.embedding_seed || {}).model || '?') +
    ' <span class="muted">(seed questions, ' +
    memdevEscape((m.embedding_seed || {}).base || '') + ')</span> · ' +
    memdevEscape((m.embedding_claims || {}).model || '?') +
    ' <span class="muted">(claims)</span>';
  return '<table class="memdev-table"><tbody>' +
    '<tr><th>embedding</th><td>' + emb + '</td></tr>' +
    '<tr><th>filter scorer</th><td>' +
      memdevMemberList(m.filter_assistant_panel) + '</td></tr>' +
    '</tbody></table>';
}

// --- assistant memory_query ------------------------------------------------
function memdevCandidateTable(candidates, keptIds) {
  if (!candidates.length) {
    return '<p class="memdev-empty">No semantic candidates.</p>';
  }
  const kept = new Set(keptIds || []);
  const rows = candidates.map(c => {
    // What the reranker backend actually read, when that is the backend —
    // it replaces the matched question rather than joining it, because the
    // document opens with that same question. The LLM backend sends no
    // document and this stays empty.
    const detail = c.document ? memdevEscape(c.document) : '';
    // Likert scores from the filter LLM (direct/indirect/relevancy);
    // absent until the filter stage has run.
    const dir = c.direct != null
      ? c.direct + ' / ' + c.indirect + ' / ' + c.relevancy : '';
    // uuids eat table width — show the first 6 chars, full value on hover.
    const shortId = '<code title="' + memdevEscape(c.qa_id) + '">' +
      memdevEscape(String(c.qa_id).slice(0, 6)) + '</code>';
    return '<tr class="' + (kept.has(c.qa_id) ? 'kept' : '') + '">' +
      '<td class="num">' + memdevEscape(c.score) +
      (c.signals ? '<br><span class="muted">' + memdevEscape(c.signals) + '</span>' : '') +
      '</td>' +
      '<td>' + shortId +
      (c.path ? '<br>' + memdevEscape(c.path) : '') + '</td>' +
      '<td>' + memdevEscape(c.kind) + '</td>' +
      '<td>' + memdevEscape(c.matched_question || '') + '<br>' +
      '<span class="muted">' + detail + '</span></td>' +
      '<td class="num">' + memdevEscape(dir) + '</td>' +
      '<td>' + (kept.has(c.qa_id) ? 'kept' : 'dropped') + '</td></tr>';
  });
  return '<table class="memdev-table"><thead><tr>' +
    '<th>score</th><th>qa_id / path</th><th>kind</th>' +
    '<th>matched question / answer</th><th>dir / ind / rel</th><th>filter</th>' +
    '</tr></thead><tbody>' + rows.join('') + '</tbody></table>';
}

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
  const sf = d.recall_filter || {};
  if (sf.mode) {
    // group_from: whose binding supplied the scorer group — 'memory_filter'
    // (dedicated) or 'assistant.default' (the fallback link).
    let label = 'memory filter: ' + sf.mode;
    if (sf.reason) label += ' (' + sf.reason + ')';
    if (sf.group_from) label += ' · ' + sf.group_from + ' group';
    badges.push(memdevBadge(label, sf.mode === 'llm' ? 'good' : 'warn'));
  }
  if (sf.scorer_model) badges.push(memdevBadge('scored by: ' + sf.scorer_model));
  parts.push('<div class="memdev-meta">' + badges.join('') + '</div>');
  if (a.error) {
    parts.push(memdevSection('error', '<div class="err">' + memdevEscape(a.error) + '</div>'));
  }
  if ((sf.candidates || []).length) {
    const keptIds = sf.candidates.filter(c => c.kept).map(c => c.qa_id);
    parts.push(memdevSection('recalled candidates + LLM filter',
      memdevCandidateTable(sf.candidates, keptIds)));
  }
  if (sf.reasoning) {
    parts.push(memdevSection('filter reasoning (written before scoring)',
      memdevPre(sf.reasoning)));
  }
  if (a.text) {
    parts.push(memdevSection('observation text (what the assistant model sees)', memdevPre(a.text)));
  } else if (!a.error) {
    parts.push('<p class="memdev-empty">Empty response.</p>');
  }
  return parts.join('');
}

// --- run -------------------------------------------------------------------
async function memdevRun() {
  const input = document.getElementById('memdev-query');
  const button = document.getElementById('memdev-run');
  const assistantOut = document.getElementById('memdev-assistant-out');
  const query = input.value.trim();
  if (!query) { input.focus(); return; }
  const topKVector = memdevBudget('memdev-topk-vector');
  const topKFulltext = memdevBudget('memdev-topk-fulltext');
  const roomUuid = document.getElementById('memdev-room').value;
  try {
    localStorage.setItem(MEMDEV_QUERY_KEY, query);
    localStorage.setItem(MEMDEV_TOPK_VECTOR_KEY, String(topKVector));
    localStorage.setItem(MEMDEV_TOPK_FULLTEXT_KEY, String(topKFulltext));
    localStorage.setItem(MEMDEV_ROOM_KEY, roomUuid);
  } catch (_) {}
  button.disabled = true;
  button.textContent = 'Running…';
  assistantOut.innerHTML = '<p class="memdev-empty">Running…</p>';
  try {
    const resp = await fetch('/memory/api/developer/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query, top_k_vector: topKVector,
                            top_k_fulltext: topKFulltext,
                            room_uuid: roomUuid || null}),
    });
    const data = await resp.json();
    if (!resp.ok) {
      assistantOut.innerHTML =
        '<div class="err">' + memdevEscape(data.error || ('HTTP ' + resp.status)) + '</div>';
      return;
    }
    const modelsWrap = document.getElementById('memdev-models');
    modelsWrap.hidden = false;
    document.getElementById('memdev-models-out').innerHTML =
      memdevRenderModels(data.models);
    assistantOut.innerHTML = memdevRenderAssistant(data.assistant || {});
  } catch (e) {
    assistantOut.innerHTML = '<div class="err">' + memdevEscape(String(e)) + '</div>';
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
  const topKVector = parseInt(localStorage.getItem(MEMDEV_TOPK_VECTOR_KEY), 10);
  if (!isNaN(topKVector)) document.getElementById('memdev-topk-vector').value = topKVector;
  const topKFulltext = parseInt(localStorage.getItem(MEMDEV_TOPK_FULLTEXT_KEY), 10);
  if (!isNaN(topKFulltext)) document.getElementById('memdev-topk-fulltext').value = topKFulltext;
} catch (_) {}
memdevLoadRooms();
