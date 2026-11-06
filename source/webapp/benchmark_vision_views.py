"""The /benchmark_vision page — vision models reading the text in one image.

The cases and scoring are in benchmarks/vision_ocr.py, the run loop in
benchmarks/vision_runner.py. Every trial is stored as it lands, so this page
reads results from the database (`/results`) and polls `/state` only for what
is running now; `revision` on the state says when to refetch the results.
"""

import json
from uuid import UUID

from flask import Response, abort, render_template_string, request, send_file

import db
from benchmarks.vision_ocr import CASES, IMAGE_PATH
from benchmarks.vision_runner import MAX_TRIALS, vision_targets

from .core import app, vision_benchmark_runner

VISION_INTRO = (
    "Each model gets the same image and six questions about it: list every "
    "word, then what is in the center and in each corner. A case is correct "
    "when the answer contains the expected word, in any case or formatting; "
    "naming other words too is allowed, and shown as “also named”. "
    "Ollama models run through Ollama's own chat API, so an override's "
    "thinking, temperature and context window apply as they do in chat."
)


def _json(payload: object) -> Response:
    return app.response_class(json.dumps(payload), mimetype="application/json")


@app.route("/benchmark_vision")
def benchmark_vision_page() -> str:
    cases = [{"name": c.name, "system": c.system, "user": c.user,
              "expected": list(c.expected)} for c in CASES]
    return render_template_string(
        VISION_TEMPLATE,
        intro=VISION_INTRO,
        targets_json=json.dumps(vision_targets()),
        cases_json=json.dumps(cases),
        max_trials=MAX_TRIALS,
    )


@app.route("/benchmark_vision/image.png")
def benchmark_vision_image() -> Response:
    return send_file(IMAGE_PATH, mimetype="image/png")


@app.route("/benchmark_vision/state")
def benchmark_vision_state() -> Response:
    return _json(vision_benchmark_runner.get_state())


@app.route("/benchmark_vision/results")
def benchmark_vision_results() -> Response:
    return _json(db.vision_runs())


@app.route("/benchmark_vision/reasoning")
def benchmark_vision_reasoning() -> Response:
    """One trial's reasoning text — fetched when the operator opens it, since
    a run's worth would make /results heavy."""
    try:
        uid = UUID(request.args.get("uuid", ""))
    except ValueError:
        abort(400, "uuid must be a trial uuid")
    text = db.vision_trial_reasoning(uid)
    if text is None:
        abort(404, "no such trial")
    return app.response_class(text, mimetype="text/plain; charset=utf-8")


@app.route("/benchmark_vision/start", methods=["POST"])
def benchmark_vision_start() -> Response:
    body = request.get_json(silent=True) or {}
    targets = body.get("targets") or []
    try:
        trials = int(body.get("trials", 3))
        started = vision_benchmark_runner.start(
            app, [str(t) for t in targets], trials=trials,
            warmup=bool(body.get("warmup", True)))
    except ValueError as e:
        abort(400, str(e))
    return _json({"started": started})


@app.route("/benchmark_vision/stop", methods=["POST"])
def benchmark_vision_stop() -> Response:
    vision_benchmark_runner.stop()
    return _json({"stopping": True})


# A raw string: the inline script must reach the browser exactly as written,
# with no Python escape processing.
VISION_TEMPLATE = r"""
<!doctype html>
<title>Benchmark vision &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .intro{display:flex;gap:1.5em;align-items:flex-start;flex-wrap:wrap}
  .intro-text{flex:1 1 28em}
  .intro-text p{margin-top:0}
  .intro img{width:180px;height:180px;border:1px solid #ddd;border-radius:4px}
  .cases{border-collapse:collapse;font-size:88%;margin:0.6em 0 0}
  .cases th,.cases td{border:1px solid #e5e7eb;padding:3px 7px;text-align:left}
  .cases th{background:#f5f5f5}
  .cases code{font-size:95%}
  .controls{margin:1em 0;padding:0.6em;border:1px solid #ddd;border-radius:4px;background:#fafafa;
            display:flex;gap:0.8em;align-items:center;flex-wrap:wrap}
  .controls label{font-size:0.92em;color:#444}
  .controls input[type=number]{width:3.5em}
  .status{color:#555}
  progress{width:12em;height:12px}
  .grid-wrap{overflow-x:auto}
  table.grid{border-collapse:collapse;width:100%;margin-top:0.6em}
  table.grid th,table.grid td{border:1px solid #ddd;padding:5px 8px;vertical-align:top;text-align:left}
  table.grid th{background:#f0f0f0;font-size:85%;white-space:nowrap}
  table.grid th small{display:block;font-weight:400;color:#666}
  td.num{text-align:right;font-family:ui-monospace,monospace;font-size:88%;white-space:nowrap}
  td.model{min-width:14em}
  td.model b{display:block}
  td.model small{display:block;color:#666}
  td.model .settings{color:#1e40af}
  tr.target-running td{background:#fffbe6}
  tr.removed td.model b{color:#888}
  .tag{display:inline-block;font-size:72%;padding:0 0.45em;border-radius:0.8em;background:#eee;color:#555;margin-left:0.3em;font-weight:400}
  .tag.run{background:#fff3a8;color:#7a5b00}
  .ok{color:#080}.bad{color:#a00}.muted{color:#888}
  .score-full{background:#e9f8e9}.score-part{background:#fff7e0}.score-none{background:#fdeaea}
  button.toggle{border:none;background:none;cursor:pointer;font-size:0.9em;padding:0 0.3em;color:#475569}
  tr.detail > td{background:#fcfcfd;padding:0.6em 0.8em}
  .detail-head{display:flex;gap:1em;align-items:center;margin-bottom:0.5em;font-size:90%}
  table.cases-detail{border-collapse:collapse;width:100%;font-size:90%}
  table.cases-detail th,table.cases-detail td{border:1px solid #e5e7eb;padding:4px 7px;vertical-align:top;text-align:left}
  table.cases-detail th{background:#f5f5f5;white-space:nowrap}
  table.cases-detail tr.answers td{border-top:none;padding:2px 10px 8px 2.2em}
  .trial{margin:0.15em 0;font-size:95%}
  .trial .ans{font-family:ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}
  .trial .meta{color:#666;font-size:88%}
  .trial details summary{cursor:pointer;color:#2563eb;font-size:88%}
  .trial pre{white-space:pre-wrap;word-break:break-word;max-height:20em;overflow:auto;background:#f8fafc;
             border:1px solid #e2e8f0;padding:0.5em;font-size:85%}
  .errors{color:#a00;font-size:90%}
  .legend{font-size:85%;color:#555;margin-top:0.6em}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Benchmark vision</h1>
<div class="intro">
  <img src="{{ url_for('benchmark_vision_image') }}" alt="The test image: cat, bug, Testing, ant and dog">
  <div class="intro-text"><p>{{ intro }}</p>
    <table class="cases" id="cases"></table>
  </div>
</div>

<div class="controls">
  <label>Trials per case <input type="number" id="trials" min="1" max="{{ max_trials }}" value="3"></label>
  <label title="One throwaway 'hi' before a model's first case, so its load time stays out of the first answer's timing."><input type="checkbox" id="warmup" checked> Warm up</label>
  <button id="start-btn">Start selected</button>
  <button id="stop-btn">Stop</button>
  <progress id="progress" value="0" max="1" hidden></progress>
  <span class="status" id="status">loading…</span>
</div>
<div class="errors" id="errors"></div>

<div class="grid-wrap"><table class="grid">
  <thead><tr>
    <th><input type="checkbox" id="select-all" title="Select all"></th>
    <th>Model</th>
    <th>Correct<small>latest run</small></th>
    <th>Time<small>avg per answer</small></th>
    <th>First token<small>avg</small></th>
    <th>Thinking<small>avg, before answer</small></th>
    <th>Reasoning<small>≈ tokens avg</small></th>
    <th>Output<small>tokens avg</small></th>
    <th>Prompt<small>tokens avg (cached)</small></th>
    <th>Decode<small>tok/s</small></th>
    <th>Prefill<small>avg</small></th>
    <th>Last run</th>
    <th></th>
  </tr></thead>
  <tbody id="grid-body"></tbody>
</table></div>
<p class="legend">
  Time is wall clock from sending the request to the last token. Thinking is the
  time between the first token and the first answer token. Reasoning tokens are an
  estimate: providers report only the output total, so it is split by the length of
  the reasoning and answer text. Prompt tokens include the image; cached ones were
  reused from an earlier request. Decode and prefill come from the provider, where it
  reports them. Click &#9656; to see every answer.
</p>
</div>

<script>
const TARGETS = {{ targets_json|safe }};
const CASES = {{ cases_json|safe }};
const els = {
  body: document.getElementById('grid-body'),
  status: document.getElementById('status'),
  progress: document.getElementById('progress'),
  errors: document.getElementById('errors'),
  start: document.getElementById('start-btn'),
  stop: document.getElementById('stop-btn'),
  trials: document.getElementById('trials'),
  warmup: document.getElementById('warmup'),
  all: document.getElementById('select-all'),
};
let results = {};
let state = null;
let lastRevision = null;
let wasRunning = false;
const expanded = new Set();
const chosenRun = {};
// Reasoning sections the operator opened, uuid -> text (null while loading),
// so a redraw keeps them open with their text.
const openReasoning = new Map();
let lastSignature = null;
let pollTimer = null;

function store(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {} }
function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}
let selected = new Set(load('pp-benchmark-vision-selected', []));
els.trials.value = load('pp-benchmark-vision-trials', 3);
els.warmup.checked = load('pp-benchmark-vision-warmup', true);
els.trials.addEventListener('change', () => store('pp-benchmark-vision-trials', Number(els.trials.value)));
els.warmup.addEventListener('change', () => store('pp-benchmark-vision-warmup', els.warmup.checked));

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function mean(xs) {
  const v = xs.filter(x => x !== null && x !== undefined);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}
function fmt(x, digits, unit) {
  return x === null ? '<span class="muted">–</span>' : x.toFixed(digits) + (unit || '');
}
function when(iso) {
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
         pad(d.getHours()) + ':' + pad(d.getMinutes());
}

// The KPIs of a list of trials. Errored trials count against correctness but
// carry no timings, so they are left out of the averages.
function summarize(trials) {
  const ok = trials.filter(t => !t.error);
  const pick = k => mean(ok.map(t => t[k]));
  return {
    n: trials.length,
    correct: trials.filter(t => t.passed).length,
    errors: trials.length - ok.length,
    elapsed: pick('elapsed'), first: pick('first_token'), thinking: pick('thinking'),
    reasoning: pick('reasoning_tokens'), output: pick('output_tokens'),
    prompt: pick('prompt_tokens'), cached: pick('cached_tokens'),
    tps: pick('decode_tps'), prefill: pick('prefill_s'),
  };
}
// A run that did not reach every case (stopped, or the worker died) — its
// score covers only what ran, so it must not read as a clean result.
function partial(run) {
  const ran = new Set(run.trials.map(t => t.case));
  return CASES.some(c => !ran.has(c.name));
}
function scoreClass(s) {
  if (!s.n) return '';
  return s.correct === s.n ? 'score-full' : (s.correct ? 'score-part' : 'score-none');
}
function kpiCells(s) {
  const prompt = s.prompt === null ? fmt(null) :
    s.prompt.toFixed(0) + (s.cached !== null ? ' <span class="muted">(' + s.cached.toFixed(0) + ')</span>' : '');
  return '<td class="num">' + fmt(s.elapsed, 2, 's') + '</td>' +
    '<td class="num">' + fmt(s.first, 2, 's') + '</td>' +
    '<td class="num">' + fmt(s.thinking, 2, 's') + '</td>' +
    '<td class="num">' + fmt(s.reasoning, 0) + '</td>' +
    '<td class="num">' + fmt(s.output, 0) + '</td>' +
    '<td class="num">' + prompt + '</td>' +
    '<td class="num">' + fmt(s.tps, 0) + '</td>' +
    '<td class="num">' + fmt(s.prefill, 2, 's') + '</td>';
}

function renderCases() {
  let html = '<tr><th>case</th><th>system</th><th>user</th><th>expects</th></tr>';
  for (const c of CASES) {
    html += '<tr><td><code>' + esc(c.name) + '</code></td><td>' + esc(c.system) + '</td><td>' +
            esc(c.user) + '</td><td>' + esc(c.expected.join(', ')) + '</td></tr>';
  }
  document.getElementById('cases').innerHTML = html;
}

// Listed targets first, then any target with stored runs that is no longer
// listed (a deleted override), so its numbers stay readable.
function rows() {
  const out = TARGETS.map(t => ({...t, removed: false}));
  const known = new Set(out.map(t => t.uuid));
  for (const [uuid, runs] of Object.entries(results)) {
    if (known.has(uuid) || !runs.length) continue;
    const r = runs[0];
    out.push({uuid, label: r.target_label, model_name: r.model_name, provider: r.provider,
              settings: '', vision: true, removed: true});
  }
  return out;
}

function render() {
  const running = state && state.running;
  const cur = state && state.current;
  let html = '';
  for (const t of rows()) {
    const runs = results[t.uuid] || [];
    const run = runs.find(r => r.run_uuid === chosenRun[t.uuid]) || runs[0];
    const s = run ? summarize(run.trials) : null;
    const isCur = cur && cur.uuid === t.uuid;
    const queued = running && state.queue.includes(t.uuid) && !isCur;
    let tags = '';
    if (t.vision === null) tags += '<span class="tag" title="This provider does not say whether the model takes images.">vision unknown</span>';
    if (t.removed) tags += '<span class="tag">removed</span>';
    if (isCur) {
      const what = cur.phase === 'running' ? esc(cur.case) + ' #' + (cur.trial + 1) : esc(cur.phase.replace('_', ' '));
      tags += '<span class="tag run" data-since="' + cur.since + '">' + what + ' <span class="tick"></span></span>';
    } else if (queued) {
      tags += '<span class="tag">queued</span>';
    }
    html += '<tr class="' + (isCur ? 'target-running ' : '') + (t.removed ? 'removed' : '') + '">' +
      '<td>' + (t.removed ? '' : '<input type="checkbox" class="pick" data-uuid="' + t.uuid + '"' +
               (selected.has(t.uuid) ? ' checked' : '') + '>') + '</td>' +
      '<td class="model">' + (runs.length ? '<button class="toggle" data-uuid="' + t.uuid + '">' +
               (expanded.has(t.uuid) ? '&#9662;' : '&#9656;') + '</button>' : '') +
      '<b style="display:inline">' + esc(t.label) + '</b>' + tags +
      '<small>' + esc(t.model_name) + ' · ' + esc(t.provider) + '</small>' +
      (t.settings ? '<small class="settings">' + esc(t.settings) + '</small>' : '') + '</td>';
    if (s) {
      html += '<td class="num ' + scoreClass(s) + '">' + s.correct + '/' + s.n +
              (s.errors ? ' <span class="bad" title="requests that failed">' + s.errors + ' err</span>' : '') +
              (partial(run) ? ' <span class="tag" title="This run stopped before every case ran.">partial</span>' : '') +
              '</td>' +
              kpiCells(s) + '<td class="num">' + when(run.started_at) + '</td>';
    } else {
      html += '<td colspan="10" class="muted">no runs yet</td>';
    }
    html += '<td>' + (t.removed ? '' : '<button class="run-one" data-uuid="' + t.uuid + '"' +
            (running || (state && state.blocked_by) ? ' disabled' : '') + '>Run</button>') + '</td></tr>';
    if (expanded.has(t.uuid) && run) html += detailRow(t, runs, run);
  }
  if (!html) html = '<tr><td colspan="13" class="muted">No vision models found among the available model configs.</td></tr>';
  els.body.innerHTML = html;
  tick();
}

function detailRow(t, runs, run) {
  let opts = '';
  for (const r of runs) {
    const s = summarize(r.trials);
    opts += '<option value="' + r.run_uuid + '"' + (r === run ? ' selected' : '') + '>' +
            when(r.started_at) + ' · ' + s.correct + '/' + s.n + (partial(r) ? ' · partial' : '') + '</option>';
  }
  let html = '<tr class="detail"><td></td><td colspan="12">' +
    '<div class="detail-head"><label>Run <select class="run-pick" data-uuid="' + t.uuid + '">' + opts +
    '</select></label><span class="muted">' + esc(run.target_label) + ' · ' + esc(run.model_name) + '</span></div>' +
    '<table class="cases-detail"><tr><th>case</th><th>correct</th><th>time</th><th>first token</th>' +
    '<th>thinking</th><th>reasoning ≈</th><th>output</th><th>prompt (cached)</th><th>tok/s</th><th>prefill</th>' +
    '</tr>';
  for (const c of CASES) {
    const trials = run.trials.filter(x => x.case === c.name);
    if (!trials.length) continue;
    const s = summarize(trials);
    html += '<tr><td><code>' + esc(c.name) + '</code><br><span class="muted">expects ' +
            esc(c.expected.join(', ')) + '</span></td>' +
            '<td class="num ' + scoreClass(s) + '">' + s.correct + '/' + s.n + '</td>' + kpiCells(s) + '</tr>' +
            // The answers get the table's full width: they are what explains a number.
            '<tr class="answers"><td colspan="10">' + trials.map(trialHtml).join('') + '</td></tr>';
  }
  return html + '</table></td></tr>';
}

function trialHtml(tr) {
  const mark = tr.passed ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>';
  let meta = [];
  if (tr.missing.length && !tr.error) meta.push('missing ' + tr.missing.join(', '));
  if (tr.also_named.length) meta.push('also named ' + tr.also_named.join(', '));
  if (tr.done_reason && tr.done_reason !== 'stop') meta.push('ended: ' + tr.done_reason);
  let html = '<div class="trial">' + mark + ' #' + (tr.trial_index + 1) + ' ';
  html += tr.error ? '<span class="bad">' + esc(tr.error) + '</span>'
                   : '<span class="ans">' + esc(tr.content.trim()) + '</span>';
  if (meta.length) html += ' <span class="meta">(' + esc(meta.join('; ')) + ')</span>';
  if (tr.reasoning_chars) {
    const open = openReasoning.has(tr.uuid);
    const text = open ? openReasoning.get(tr.uuid) : null;
    html += '<details data-trial="' + tr.uuid + '"' + (open ? ' open' : '') + '><summary>reasoning, ' +
            tr.reasoning_chars + ' chars</summary><pre>' + esc(text === null ? 'loading…' : text) +
            '</pre></details>';
  }
  return html + '</div>';
}

function tick() {
  for (const el of document.querySelectorAll('.tag.run[data-since]')) {
    const secs = Date.now() / 1000 - Number(el.dataset.since);
    el.querySelector('.tick').textContent = secs.toFixed(0) + 's';
  }
}

function renderStatus() {
  const s = state;
  els.errors.innerHTML = (s.errors || []).map(e => esc(e.label) + ': ' + esc(e.error)).join('<br>');
  const blocked = s.blocked_by;
  els.start.disabled = s.running || !!blocked || selected.size === 0;
  els.stop.disabled = !s.running;
  els.progress.hidden = !s.running;
  if (s.running) {
    els.progress.max = Math.max(1, s.trials_total);
    els.progress.value = s.trials_done;
    els.status.textContent = s.trials_done + ' of ' + s.trials_total + ' answers';
  } else if (blocked) {
    els.status.textContent = blocked + ' is running; this page can start when it ends.';
  } else if (selected.size === 0) {
    els.status.textContent = "Tick the models to run, or use a row's Run button.";
  } else {
    els.status.textContent = s.ended_at ? (s.aborted ? 'Stopped.' : 'Done.') : 'Ready.';
  }
}

async function fetchResults() {
  const r = await fetch('{{ url_for("benchmark_vision_results") }}');
  results = await r.json();
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    const r = await fetch('{{ url_for("benchmark_vision_state") }}');
    state = await r.json();
    if (state.revision !== lastRevision || (wasRunning && !state.running)) {
      lastRevision = state.revision;
      await fetchResults();
    }
    wasRunning = state.running;
    renderStatus();
    // Redraw only on a change: a redraw rebuilds the table, which would reset
    // a run picker mid-choice and restart every ticking timer.
    const c = state.current;
    const signature = JSON.stringify([state.revision, state.running, state.queue, state.blocked_by,
                                      c && [c.uuid, c.phase, c.case, c.trial]]);
    if (signature !== lastSignature) {
      lastSignature = signature;
      render();
    }
  } catch (e) {
    els.status.textContent = 'Could not reach the server: ' + e;
  }
  pollTimer = setTimeout(poll, state && (state.running || state.blocked_by) ? 1000 : 4000);
}

async function start(targets) {
  const r = await fetch('{{ url_for("benchmark_vision_start") }}', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({targets, trials: Number(els.trials.value), warmup: els.warmup.checked}),
  });
  if (!r.ok) { els.status.textContent = 'Could not start: ' + (await r.text()); return; }
  const body = await r.json();
  if (!body.started) { els.status.textContent = 'Could not start: another run is going.'; return; }
  // Show the run at once rather than at the next idle poll.
  poll();
}

els.start.addEventListener('click', () => {
  // Run in the table's order, not the order the boxes were ticked.
  start(rows().filter(t => selected.has(t.uuid)).map(t => t.uuid));
});
els.stop.addEventListener('click', () => fetch('{{ url_for("benchmark_vision_stop") }}', {method: 'POST'}));
els.all.addEventListener('change', () => {
  selected = new Set(els.all.checked ? TARGETS.map(t => t.uuid) : []);
  store('pp-benchmark-vision-selected', [...selected]);
  renderStatus(); render();
});
els.body.addEventListener('change', e => {
  const el = e.target;
  if (el.classList.contains('pick')) {
    if (el.checked) selected.add(el.dataset.uuid); else selected.delete(el.dataset.uuid);
    store('pp-benchmark-vision-selected', [...selected]);
    renderStatus();
  } else if (el.classList.contains('run-pick')) {
    chosenRun[el.dataset.uuid] = el.value;
    render();
  }
});
els.body.addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.classList.contains('toggle')) {
    const u = btn.dataset.uuid;
    if (expanded.has(u)) expanded.delete(u); else expanded.add(u);
    render();
  } else if (btn.classList.contains('run-one')) {
    start([btn.dataset.uuid]);
  }
});
// Reasoning loads when its section is first opened. The toggle event does not
// bubble, so it is caught on the way down.
els.body.addEventListener('toggle', async e => {
  const d = e.target;
  const uuid = d.dataset && d.dataset.trial;
  if (!uuid) return;
  if (!d.open) { openReasoning.delete(uuid); return; }
  if (openReasoning.has(uuid)) return;
  openReasoning.set(uuid, null);
  const r = await fetch('{{ url_for("benchmark_vision_reasoning") }}?uuid=' + encodeURIComponent(uuid));
  const text = r.ok ? await r.text() : 'Could not load: ' + r.status;
  if (!openReasoning.has(uuid)) return;
  openReasoning.set(uuid, text);
  const live = els.body.querySelector('details[data-trial="' + uuid + '"] pre');
  if (live) live.textContent = text;
}, true);
setInterval(tick, 1000);

// The selection may name targets that are gone since; drop them.
selected = new Set([...selected].filter(u => TARGETS.some(t => t.uuid === u)));
renderCases();
poll();
</script>
"""
