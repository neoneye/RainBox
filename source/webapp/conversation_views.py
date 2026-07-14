"""/conversations — a dedicated control page to start and stop persona
agent-to-agent conversation runs, without touching /chat.

Pure UI over the existing JSON endpoints in conversation_api.py:
  GET  /conversation/api/templates
  GET  /chat/api/rooms
  GET  /conversation/api/runs
  POST /conversation/api/runs            (start)
  POST /conversation/api/runs/<id>/stop  (stop)

The supervisor (main.py) must be running for a started run to actually advance,
and the participating personas must be bound to a model group on /agentmodel.
"""

from flask import render_template_string

from .core import app

CONVERSATIONS_TEMPLATE = """
<!doctype html>
<title>Conversations &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .panel{margin:1em 0;padding:1em;border:1px solid #e5e7eb;border-radius:8px;background:#fafafa}
  .panel h2{margin:0 0 .6em;font-size:1.05rem}
  label{display:block;font-size:.85rem;color:#444;margin:.5em 0 .2em}
  select,button{font-size:.95rem;padding:.45em .6em;border-radius:6px;border:1px solid #cbd5e1}
  select{min-width:280px;background:#fff}
  button{cursor:pointer;background:#2563eb;color:#fff;border-color:#2563eb}
  button:hover{background:#1d4ed8}
  button.secondary{background:#fff;color:#374151;border-color:#cbd5e1}
  button.secondary:hover{background:#f1f5f9}
  button.danger{background:#dc2626;border-color:#dc2626}
  button.danger:hover{background:#b91c1c}
  button:disabled{opacity:.5;cursor:default}
  .row{display:flex;gap:1em;flex-wrap:wrap;align-items:flex-end}
  .status{margin-left:.4em;color:#555;font-size:.9rem}
  table{border-collapse:collapse;width:100%;margin-top:.6em;font-size:.92rem}
  th,td{border:1px solid #e5e7eb;padding:6px 9px;text-align:left;vertical-align:middle}
  th{background:#f0f0f0;font-size:85%}
  .pill{display:inline-block;font-size:78%;padding:1px .5em;border-radius:.8em}
  .pill.running{background:#fff3a8;color:#7a5b00}
  .pill.finished{background:#d2f1d2;color:#185018}
  .pill.stopped{background:#fde0c8;color:#8a4b00}
  .pill.paused{background:#dbe9ff;color:#1e40af}
  .pill.failed{background:#fdd;color:#900}
  .muted{color:#888}
  code{background:#eef;padding:0 .3em;border-radius:3px}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Conversations</h1>
<p>Start and stop persona agent-to-agent conversation runs. A run drives the
chosen personas through bounded turns; watch it live in
<a href="{{ url_for('chat_page') }}">Chat</a>. The supervisor must be running, and
each participating persona must be bound to a model group on
<a href="{{ url_for('agent_models_page') }}">Agent models</a>.</p>

<div class="panel">
  <h2>Start a conversation</h2>
  <div class="row">
    <div>
      <label for="tpl">Template</label>
      <select id="tpl"></select>
    </div>
    <div>
      <label for="room">Room</label>
      <select id="room"></select>
    </div>
    <div>
      <button id="start-btn">Start</button>
      <span class="status" id="start-status"></span>
    </div>
  </div>
</div>

<div class="panel">
  <h2>Runs <span class="muted" id="runs-count"></span></h2>
  <table id="runs">
    <thead><tr>
      <th>Started</th><th>Room</th><th>Template / participants</th>
      <th>Status</th><th>Turn</th><th>Reason</th><th>Actions</th>
    </tr></thead>
    <tbody id="runs-body">
      <tr><td colspan="7" class="muted">loading…</td></tr>
    </tbody>
  </table>
</div>

<script>
let rooms = [];           // [{uuid, name, ...}]
const roomName = (u) => (rooms.find(r => r.uuid === u) || {}).name || u.slice(0, 8);

async function call(path, method='GET', body=null) {
  const opts = {method};
  if (body) { opts.headers = {'Content-Type':'application/json'}; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.message || (method + ' ' + path + ' -> ' + r.status));
  return data;
}
function esc(s){ const d=document.createElement('div'); d.textContent=String(s==null?'':s); return d.innerHTML; }

async function loadPickers() {
  const [tpls, rms] = await Promise.all([
    call('{{ url_for("conversation_templates") }}'),
    call('{{ url_for("chat_rooms") }}'),
  ]);
  rooms = rms;
  const tplSel = document.getElementById('tpl');
  tplSel.innerHTML = tpls.length
    ? tpls.map(t => `<option value="${esc(t.slug)}">${esc(t.name)} (${(t.participants||[]).join(', ')}; max ${t.max_turns ?? '?'})</option>`).join('')
    : '<option value="">(no templates in agent_profiles/conversations)</option>';
  const roomSel = document.getElementById('room');
  roomSel.innerHTML = rms.length
    ? rms.map(r => `<option value="${esc(r.uuid)}">${esc(r.name)} (${r.member_count} members)</option>`).join('')
    : '<option value="">(no rooms)</option>';
}

function actionsFor(run) {
  const id = esc(run.run_uuid);
  const open = `<a href="{{ url_for('chat_page') }}?room=${encodeURIComponent(run.room_uuid)}">Open</a>`;
  const stop = `<button class="danger" data-stop="${id}">Stop</button>`;
  const resume = `<button data-resume="${id}">Resume</button>`;
  const reconcile = `<button class="secondary" data-reconcile="${id}" title="Recover a turn whose speaker was killed and never replied">Reconcile</button>`;
  if (run.status === 'running') return `${stop} ${reconcile} &nbsp; ${open}`;
  if (run.status === 'paused')  return `${stop} ${resume} &nbsp; ${open}`;
  if (run.status === 'failed' || run.status === 'stopped') return `${resume} &nbsp; ${open}`;
  return open;  // finished (terminal: DONE / max_turns)
}

async function loadRuns() {
  let runs;
  try { runs = await call('{{ url_for("conversation_list_runs") }}'); }
  catch (e) { return; }
  lastRuns = runs;
  document.getElementById('runs-count').textContent = runs.length ? `(${runs.length})` : '';
  const body = document.getElementById('runs-body');
  if (!runs.length) { body.innerHTML = '<tr><td colspan="7" class="muted">No runs yet — start one above.</td></tr>'; return; }
  body.innerHTML = runs.map(run => `
    <tr>
      <td class="muted">${esc(run.created_at)}</td>
      <td>${esc(roomName(run.room_uuid))}</td>
      <td>${(run.participants||[]).map(esc).join(' &rarr; ')}</td>
      <td><span class="pill ${esc(run.status)}">${esc(run.status)}</span>${run.stop_requested && run.status==='running' ? ' <span class="muted">(stopping…)</span>' : ''}</td>
      <td>${esc(run.turn)}</td>
      <td class="muted">${esc(run.reason || '')}</td>
      <td>${actionsFor(run)}</td>
    </tr>`).join('');
}

document.getElementById('start-btn').addEventListener('click', async () => {
  const btn = document.getElementById('start-btn');
  const status = document.getElementById('start-status');
  const template_slug = document.getElementById('tpl').value;
  const room_uuid = document.getElementById('room').value;
  if (!template_slug || !room_uuid) { status.textContent = 'pick a template and a room'; return; }
  btn.disabled = true; status.textContent = 'starting…';
  try {
    const res = await call('{{ url_for("conversation_start") }}', 'POST', {template_slug, room_uuid});
    status.innerHTML = `started — <a href="{{ url_for('chat_page') }}?room=${encodeURIComponent(res.room_uuid)}">open room</a>`;
    await loadRuns();
  } catch (e) { status.textContent = 'error: ' + e.message; }
  finally { btn.disabled = false; }
});

document.getElementById('runs-body').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-stop],button[data-resume],button[data-reconcile]');
  if (!btn) return;
  const [id, verb] = btn.dataset.stop ? [btn.dataset.stop, 'stop']
    : btn.dataset.resume ? [btn.dataset.resume, 'resume']
    : [btn.dataset.reconcile, 'reconcile'];
  btn.disabled = true;
  try {
    const res = await call('/conversation/api/runs/' + encodeURIComponent(id) + '/' + verb, 'POST');
    if (verb === 'reconcile' && res.status === 'too_recent') {
      alert('Turn is not stale yet (still within the reconcile timeout).');
    }
    await loadRuns();
  } catch (e) { alert(e.message); btn.disabled = false; }
});

// Adaptive, visibility-aware polling: never poll a backgrounded tab, poll fast
// only while a run is live, and back off to a slow heartbeat when idle. This
// avoids hammering /conversation/api/runs every 3s forever in a hidden tab.
let lastRuns = [];
let pollTimer = null;

function pollDelayMs() {
  const active = lastRuns.some(r => r.status === 'running' || r.status === 'paused');
  return active ? 3000 : 15000;
}
function scheduleNext() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollTick, pollDelayMs());
}
async function pollTick() {
  if (!document.hidden) { await loadRuns(); }  // skip fetches while the tab is hidden
  scheduleNext();
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { loadRuns().then(scheduleNext); }  // refresh at once on return
});

loadPickers().catch(e => { document.getElementById('start-status').textContent = 'error: ' + e.message; });
loadRuns().then(scheduleNext);
</script>
</div>
"""


@app.route("/conversations")
def conversation_page() -> str:
    return render_template_string(CONVERSATIONS_TEMPLATE)
