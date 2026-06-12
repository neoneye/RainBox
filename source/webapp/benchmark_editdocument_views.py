"""Views for /benchmark_editdocument — the EditDocumentAgentV1 benchmark page.

Mirrors webapp/benchmark_views.py's shape: a single rendered page with a
small amount of JS that polls a JSON state endpoint while the runner works.
"""

import json

from flask import Response, jsonify, render_template_string, request

from benchmark_editdocument import EDIT_DOCUMENT_TESTS

from .core import app, benchmark_editdocument_runner


PAGE_TEMPLATE: str = """
<!doctype html>
<title>EditDocument Benchmark &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .controls{margin:1em 0;padding:0.6em;border:1px solid #ddd;border-radius:4px;background:#fafafa}
  .controls label{margin-right:1em}
  .status{margin-left:1em;color:#555}
  table{border-collapse:collapse;width:100%;margin-top:0.6em}
  th,td{border:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}
  th{background:#f0f0f0;font-size:90%}
  td.target{font-weight:600;background:#fbfbfb;width:24em}
  td.target small{font-weight:400;color:#666;display:block}
  td.target .target-row{display:flex;align-items:flex-start;gap:0.6em;justify-content:space-between}
  td.target .target-lines{flex:1 1 auto;min-width:0}
  td.target .target-lines .provider{color:#1e40af}
  td.target .target-actions{flex:0 0 auto}
  td.cell{font-family:ui-monospace,monospace;font-size:88%;min-width:7em}
  td.cell.pass{background:#eaf7ea}
  td.cell.fail{background:#fdecec}
  td.cell.err{background:#fdecec}
  td.cell.pending{color:#888}
  td.cell.running{background:#fff7d6}
  tr.target-running{background:#fffbe6}
  tr.target-running td.target{background:#fff3a8;font-weight:600}
  .pill{display:inline-block;font-size:75%;padding:0 0.4em;border-radius:0.8em;margin-left:0.3em;background:#eee;color:#555}
  .pill.running{background:#fff3a8;color:#7a5b00}
  .pill.done{background:#d2f1d2;color:#185018}
  .pill.error{background:#fdd;color:#800}
  details.drill{margin-top:0.4em}
  details.drill > summary{cursor:pointer;font-size:80%;color:#555}
  pre.diff{background:#fafafa;border:1px solid #eee;padding:6px;font-size:80%;overflow:auto;max-height:20em}
  pre.diff .add{background:#e6ffe6}
  pre.diff .del{background:#ffe6e6}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>EditDocument Benchmark</h1>
<p>Runs the selected agent (<code>edit_document</code>,
<code>edit_document_v2</code>, <code>edit_document_v3</code>,
<code>edit_document_v4</code>, <code>edit_document_v5</code>, or
<code>edit_document_v6</code>) against
every available model in the <a href="/models">/models</a> tree. Each cell
runs one test end-to-end through the agent with the target model pinned
(no fallback). Pass = the agent's patches, when applied to the test
document, exactly equal the reference expected output.</p>

<div class="controls">
  <strong>Agent:</strong>
  <label><input type="radio" name="agent" value="v1"> EditDocumentAgentV1 (v1)</label>
  <label><input type="radio" name="agent" value="v2"> EditDocumentAgentV2 (v2)</label>
  <label><input type="radio" name="agent" value="v3"> EditDocumentAgentV3 (v3)</label>
  <label><input type="radio" name="agent" value="v4"> EditDocumentAgentV4 (v4)</label>
  <label><input type="radio" name="agent" value="v5"> EditDocumentAgentV5 (v5)</label>
  <label><input type="radio" name="agent" value="v6" checked> EditDocumentAgentV6 (v6)</label>
  <button id="start-btn">Start all</button>
  <button id="stop-btn">Stop</button>
  <span class="status" id="status">loading…</span>
</div>

<table id="grid">
  <thead>
    <tr>
      <th>Target</th>
      {% for name in test_names %}
      <th title="{{ test_descriptions[name] }}">{{ name }}</th>
      {% endfor %}
    </tr>
  </thead>
  <tbody id="grid-body">
    <tr><td colspan="{{ test_names|length + 1 }}" class="cell pending">No run started yet — click <b>Start all</b>.</td></tr>
  </tbody>
</table>

<script>
const testNames = {{ test_names_json|safe }};

async function call(path, init) {
  const r = await fetch(path, init || {});
  if (!r.ok) throw new Error(`${(init && init.method) || 'GET'} ${path} -> ${r.status}`);
  return r.json();
}

// Friendly name for a provider id. Falls back to the raw id for unknown
// providers — still legible, just not as pretty.
function providerLabel(id) {
  if (id === 'lm_studio') return 'LM Studio';
  if (id === 'jan') return 'Jan';
  if (id === 'ollama') return 'Ollama';
  return id || '';
}
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function fmtElapsed(e) {
  if (e == null) return '';
  return ` ${Number(e).toFixed(2)}s`;
}

function fmtCell(trial) {
  if (trial.status === 'pending') return `<div class="cell pending">—</div>`;
  if (trial.status === 'running') return `<div>running…</div>`;
  if (trial.error) {
    return `<div>✗ err${fmtElapsed(trial.elapsed)}</div>
            <div style="font-size:80%;color:#a00">${escapeHtml(trial.error)}</div>` +
            renderDrill(trial);
  }
  const mark = trial.correct ? '✓' : '✗';
  return `<div>${mark}${fmtElapsed(trial.elapsed)}</div>` + renderDrill(trial);
}

function renderDrill(trial) {
  const v2 = trial.agent_status ? `<div><b>status:</b> ${escapeHtml(trial.agent_status)}</div>
       <div><b>comment:</b> ${escapeHtml(trial.agent_comment)}</div>` : '';
  const counts = (trial.thinking_chars != null || trial.content_chars != null)
    ? `<div><b>thinking:</b> ${trial.thinking_chars ?? 0} chars &middot; <b>content:</b> ${trial.content_chars ?? 0} chars</div>`
    : '';
  return `<details class="drill"><summary>details</summary>
    ${v2}
    ${counts}
    <div><b>expected:</b><pre class="diff">${escapeHtml(trial.expected)}</pre></div>
    <div><b>applied:</b><pre class="diff">${escapeHtml(trial.applied)}</pre></div>
    <div><b>patches:</b><pre class="diff">${escapeHtml(JSON.stringify(trial.patches, null, 2))}</pre></div>
  </details>`;
}

function cellClass(trial) {
  if (trial.status === 'pending') return 'cell pending';
  if (trial.status === 'running') return 'cell running';
  if (trial.error) return 'cell err';
  return trial.correct ? 'cell pass' : 'cell fail';
}

function render(state) {
  const statusEl = document.getElementById('status');
  if (state.total_targets === 0) {
    statusEl.textContent = 'no available targets — add a model in /models first';
  } else if (state.running) {
    statusEl.textContent = `running (${state.agent_choice}) — target ${state.current_target_index + 1} of ${state.total_targets}`;
  } else if (state.ended_at) {
    statusEl.textContent = state.aborted ? 'aborted' : 'complete';
  } else {
    statusEl.textContent = 'idle';
  }

  // Reflect the runner's current agent choice into the picker.
  document.querySelectorAll('input[name=agent]').forEach(inp => {
    inp.checked = inp.value === state.agent_choice;
    inp.disabled = !!state.running;
  });

  const body = document.getElementById('grid-body');
  if (!state.targets || state.targets.length === 0) {
    body.innerHTML = `<tr><td colspan="${testNames.length + 1}" class="cell pending">No available model configs.</td></tr>`;
    return;
  }
  body.innerHTML = state.targets.map(t => {
    const cells = testNames.map(n => {
      const trial = t.trials[n] || {status: 'pending'};
      return `<td class="${cellClass(trial)}">${fmtCell(trial)}</td>`;
    }).join('');
    const startBtn = `<button class="row-start" data-uuid="${escapeHtml(t.uuid)}" ${state.running ? 'disabled' : ''}>Start</button>`;
    const providerLine = t.provider
      ? `<small class="provider">${escapeHtml(providerLabel(t.provider))}</small>`
      : '';
    const sub = t.display_name
      ? `<small>${escapeHtml(t.display_name)}</small>`
      : '<small>(base config)</small>';
    const rowCls = t.status === 'running' ? 'target-running' : '';
    return `<tr class="${rowCls}">
      <td class="target">
        <div class="target-row">
          <div class="target-lines">${providerLine}${escapeHtml(t.model_display_name || t.model_name)}${sub}</div>
          <div class="target-actions">${startBtn}</div>
        </div>
      </td>
      ${cells}
    </tr>`;
  }).join('');
}

// Polling is driven by the run state: only poll while a run is in
// progress. Once the runner reports running=false we stop, so the DOM
// stays stable — expanded <details> panels survive, and text selection
// for copy-to-clipboard isn't interrupted by re-renders every second.
let pollHandle = null;

function startPolling() {
  if (pollHandle === null) {
    pollHandle = setInterval(poll, 1000);
  }
}

function stopPolling() {
  if (pollHandle !== null) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}

async function poll() {
  try {
    const s = await call('/benchmark_editdocument/state');
    render(s);
    if (s.running) startPolling();
    else stopPolling();
  } catch (e) {
    document.getElementById('status').textContent = `error: ${e.message}`;
    stopPolling();
  }
}

document.getElementById('start-btn').addEventListener('click', async () => {
  const choice = document.querySelector('input[name=agent]:checked').value;
  await call('/benchmark_editdocument/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({agent_choice: choice}),
  });
  poll();
});

document.getElementById('stop-btn').addEventListener('click', async () => {
  await call('/benchmark_editdocument/stop', {method: 'POST'});
  poll();
});

// Per-row Start: delegate from the grid body so dynamically-rendered
// buttons keep working after every poll re-render.
document.getElementById('grid-body').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button.row-start');
  if (!btn || btn.disabled) return;
  const uuid = btn.dataset.uuid;
  const choice = document.querySelector('input[name=agent]:checked').value;
  const url = '/benchmark_editdocument/start'
    + '?agent_choice=' + encodeURIComponent(choice)
    + '&target_uuid=' + encodeURIComponent(uuid);
  await call(url, {method: 'POST'});
  poll();
});

// One initial poll on load. If a run is already in progress (e.g.
// started from another tab) poll() will start the interval itself.
poll();
</script>
</div>
"""


@app.route("/benchmark_editdocument")
def benchmark_editdocument_page() -> str:
    benchmark_editdocument_runner.ensure_targets_populated()
    test_names = [t.name for t in EDIT_DOCUMENT_TESTS]
    test_descriptions = {t.name: t.description for t in EDIT_DOCUMENT_TESTS}
    return render_template_string(
        PAGE_TEMPLATE,
        test_names=test_names,
        test_descriptions=test_descriptions,
        test_names_json=json.dumps(test_names),
    )


@app.route("/benchmark_editdocument/state")
def benchmark_editdocument_state() -> Response:
    return jsonify(benchmark_editdocument_runner.get_state())


@app.route("/benchmark_editdocument/start", methods=["POST"])
def benchmark_editdocument_start() -> Response | tuple[Response, int]:
    # agent_choice and target_uuid can arrive as a JSON body (start-all)
    # or as query params (per-row Start clicks). Per-row clicks send a
    # single target_uuid; a missing target_uuid means run every available
    # target.
    data = request.get_json(silent=True) or {}
    choice = (
        data.get("agent_choice")
        or request.args.get("agent_choice")
        or "v6"
    )
    target_uuid = data.get("target_uuid") or request.args.get("target_uuid")
    target_uuids = [target_uuid] if target_uuid else None
    try:
        started = benchmark_editdocument_runner.start(choice, target_uuids=target_uuids)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "started": started})


@app.route("/benchmark_editdocument/stop", methods=["POST"])
def benchmark_editdocument_stop() -> Response:
    benchmark_editdocument_runner.stop()
    return jsonify({"ok": True})
