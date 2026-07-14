import json

from flask import Response, render_template_string, request

from benchmarks.runner import BENCHMARK_SPECS

from .core import app, benchmark_runner

# Plain-text explanation per benchmark name, shown in a legend at the top of the
# page. Keyed by the BENCHMARK_SPECS name; a benchmark without an entry here
# simply shows no description.
BENCHMARK_DESCRIPTIONS: dict[str, str] = {
    "base64_decode": "Decode a base64-encoded ASCII string back to the original plaintext. Structured JSON output.",
    "base64_encode": "Encode a random ASCII string to standard base64 (with = padding). Structured JSON output.",
    "reverse_string": "Reverse a string character-by-character. Structured JSON output.",
    "reverse_list": "Reverse the order of items in a list without modifying the individual items. Structured JSON output.",
    "tool_order": "Function calling: given three no-op tools func1, func2, func3, invoke all three in the order requested that trial. Each trial uses a different one of the 6 possible orderings (shuffled; 5 of the 6 at the default 5 trials).",
    "tool_route": "Function calling: call random (which returns a function name), then call exactly the function it named (func1 or func2) — a data-dependent dispatch.",
}


BENCHMARK_TEMPLATE: str = """
<!doctype html>
<title>{{ page_title }} &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  header{margin-bottom:1em}
  header a{margin-right:1em}
  .controls{margin:1em 0;padding:0.6em;border:1px solid #ddd;border-radius:4px;background:#fafafa}
  .status{margin-left:1em;color:#555}
  table{border-collapse:collapse;width:100%;margin-top:0.6em}
  th,td{border:1px solid #ddd;padding:6px 8px;vertical-align:middle;text-align:left}
  th{background:#f0f0f0;font-size:90%}
  td.target{font-weight:600;background:#fbfbfb}
  td.target small{font-weight:400;color:#666;display:block}
  td.target .target-row{display:flex;align-items:flex-start;gap:0.6em;justify-content:space-between}
  td.target .target-lines{flex:1 1 auto;min-width:0}
  td.target .target-lines .provider{color:#1e40af}
  td.target .target-actions{flex:0 0 auto}
  td.bench{font-family:ui-monospace,monospace;font-size:90%}
  progress{width:100%;height:12px}
  .ok{color:#080}
  .err{color:#a00}
  .muted{color:#888}
  details.drill > summary{cursor:pointer;font-size:80%;color:#555}
  details.drill > div{font-size:80%;color:#444}
  .pill{display:inline-block;font-size:75%;padding:0 0.4em;border-radius:0.8em;margin-left:0.3em;background:#eee;color:#555}
  .pill.running{background:#fff3a8;color:#7a5b00}
  .pill.done{background:#d2f1d2;color:#185018}
  .pill.error{background:#fdd;color:#800}
  .target-running{background:#fffbe6}
  td.score{text-align:right;font-family:ui-monospace,monospace;font-size:90%}
  .rank{display:inline-block;margin-left:0.4em;padding:0 0.4em;border-radius:0.6em;font-weight:600;font-size:80%}
  .rank-1{background:#ffd700;color:#5a4500}
  .rank-2{background:#c0c0c0;color:#333}
  .rank-3{background:#cd7f32;color:#fff}
  .bench-help{margin:1em 0;padding:0.6em 0.8em;border:1px solid #e5e7eb;border-radius:4px;background:#fafafa}
  .bench-help summary{cursor:pointer;font-weight:600}
  .bench-help dl{margin:0.6em 0 0;display:grid;grid-template-columns:max-content 1fr;gap:4px 14px}
  .bench-help dt{font-family:ui-monospace,monospace;font-weight:600;color:#222}
  .bench-help dd{margin:0;color:#444}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>{{ page_title }}</h1>
<p>{{ page_intro }}</p>

<details class="bench-help" open>
  <summary>What each benchmark measures</summary>
  <dl>
    {% for name, desc in benchmark_help %}
    <dt>{{ name }}</dt><dd>{{ desc }}</dd>
    {% endfor %}
  </dl>
</details>

<div class="controls">
  <button id="start-btn">Start all</button>
  <button id="stop-btn">Stop</button>
  <span class="status" id="status">loading…</span>
</div>

<table id="grid">
  <thead>
    <tr>
      <th>Target</th>
      {% for name, _cls, _kwargs in benchmarks %}
      <th>{{ name }}</th>
      {% endfor %}
      <th>Score</th>
    </tr>
  </thead>
  <tbody id="grid-body">
    <tr><td colspan="{{ benchmarks|length + 2 }}" class="muted">No run started yet — click <b>Start</b>.</td></tr>
  </tbody>
</table>

<script>
const benchmarkNames = {{ benchmark_names_json|safe }};

async function call(path, method='GET') {
  const r = await fetch(path, {method});
  if (!r.ok) throw new Error(`${method} ${path} -> ${r.status}`);
  return r.json();
}

function fmtCounts(b) {
  const okClass = b.correct > 0 ? 'ok' : 'muted';
  const errClass = b.mistakes > 0 ? 'err' : 'muted';
  const parts = [
    `<span class="${okClass}">${b.correct}r</span>`,
    `<span class="${errClass}">${b.mistakes}x</span>`,
    `<span class="muted">${b.failures}!</span>`,
  ];
  if (b.trials_done > 0) {
    const avg = b.total_elapsed / b.trials_done;
    parts.push(`<span class="muted">${avg.toFixed(2)}s</span>`);
  }
  return parts.join(' ');
}

// Expandable per-benchmark reasoning/content character totals across its trials,
// so a slow benchmark shows whether the time went into thinking or output.
function benchDetails(b) {
  if (b.reasoning_chars == null && b.content_chars == null) return '';
  return `<details class="drill"><summary>chars</summary>` +
    `<div>reasoning: <b>${b.reasoning_chars ?? 0}</b> &middot; content: <b>${b.content_chars ?? 0}</b></div>` +
    `</details>`;
}
function renderBench(b) {
  if (b.status === 'done') {
    return `<div>${fmtCounts(b)}</div>${benchDetails(b)}`;
  }
  if (b.status === 'error') {
    const errText = b.error ? `<div class="err" style="font-size:85%">${escapeHtml(b.error)}</div>` : '';
    return `<div>${fmtCounts(b)}<span class="pill error" style="margin-left:0.4em">error</span></div>${errText}${benchDetails(b)}`;
  }
  if (b.status === 'pending') {
    return `<div class="muted">pending</div>`;
  }
  // status === 'running'
  const pct = b.trials_total > 0 ? (b.trials_done / b.trials_total) : 0;
  return `<progress max="1" value="${pct}"></progress>` +
         `<div>${b.trials_done}/${b.trials_total} ${fmtCounts(b)}</div>`;
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
  d.textContent = String(s);
  return d.innerHTML;
}

function render(state) {
  const statusEl = document.getElementById('status');
  if (state.total_targets === 0) {
    statusEl.textContent = 'no targets (no available model configs)';
  } else if (state.running) {
    statusEl.textContent = `running — target ${state.current_target_index + 1} of ${state.total_targets}`;
  } else if (state.ended_at) {
    statusEl.textContent = state.aborted ? `aborted at ${new Date(state.ended_at * 1000).toLocaleTimeString()}`
                                         : `complete at ${new Date(state.ended_at * 1000).toLocaleTimeString()}`;
  } else {
    statusEl.textContent = 'idle';
  }

  const body = document.getElementById('grid-body');
  if (!state.targets || state.targets.length === 0) {
    body.innerHTML = `<tr><td colspan="${benchmarkNames.length + 2}" class="muted">No available model configs. Add one in <a href="/model">/model</a> first.</td></tr>`;
    return;
  }

  // Score = (∏(correct + 1) - 1) / (∏(trials_total + 1) - 1).
  // Both numerator and denominator have 1 subtracted, so a target with
  // zero correct answers everywhere lands on exactly 0.0, and a target
  // with every trial correct lands on exactly 1.0. Using trials_total per
  // benchmark (not a hardcoded 5) means the score stays normalized to
  // [0, 1] if a benchmark is ever reconfigured with a different num_trials.
  const scored = state.targets.map(t => {
    let num = 1;
    let denom = 1;
    for (const b of t.benchmarks) {
      num *= (b.correct + 1);
      denom *= (b.trials_total + 1);
    }
    return { t, score: (num - 1) / (denom - 1) };
  });
  const ranking = [...scored].sort((a, b) => b.score - a.score);
  const rankByIndex = new Map();
  for (let i = 0; i < Math.min(3, ranking.length); i++) {
    if (ranking[i].score > 0) {
      rankByIndex.set(ranking[i].t.index, i + 1);
    }
  }
  const rankLabel = ['1st', '2nd', '3rd'];

  const running = !!state.running;
  const rows = scored.map(({ t, score }) => {
    const rowCls = (t.status === 'running' || t.status === 'warming_up') ? 'target-running' : '';
    const providerLine = t.provider
      ? `<small class="provider">${escapeHtml(providerLabel(t.provider))}</small>`
      : '';
    const sub = t.display_name
      ? `<small>${escapeHtml(t.display_name)}</small>`
      : '<small>(base config)</small>';
    let warmup = '';
    if (t.status === 'warming_up') {
      // Live, ticking elapsed since warmup began. render() runs every poll
      // (500ms) while a run is active, so the integer seconds count up on
      // their own without a dedicated timer.
      let secs = '';
      if (t.warmup_started_at) {
        const el = Date.now() / 1000 - t.warmup_started_at;
        if (el > 0) secs = ` ${el.toFixed(0)}s`;
      }
      warmup = `<small class="muted">warming up…${secs}</small>`;
    } else if (t.warmup_elapsed !== null && t.warmup_elapsed !== undefined) {
      warmup = `<small class="muted">warmup ${t.warmup_elapsed.toFixed(1)}s</small>`;
    }
    const startBtn = `<button class="row-start" data-uuid="${escapeHtml(t.uuid)}" ${running ? 'disabled' : ''}>Start</button>`;
    const benchCells = benchmarkNames.map((bname, i) => {
      const b = t.benchmarks[i];
      return `<td class="bench">${renderBench(b)}</td>`;
    }).join('');
    const rank = rankByIndex.get(t.index);
    const rankBadge = rank ? `<span class="rank rank-${rank}">${rankLabel[rank - 1]}</span>` : '';
    const scoreCell = `<td class="score">${score.toFixed(4)}${rankBadge}</td>`;
    const targetCell = `<td class="target">
      <div class="target-row">
        <div class="target-lines">${providerLine}${escapeHtml(t.model_display_name || t.model_name)}${sub}${warmup}</div>
        <div class="target-actions">${startBtn}</div>
      </div>
    </td>`;
    return `<tr class="${rowCls}">` + targetCell + benchCells + scoreCell + `</tr>`;
  }).join('');
  body.innerHTML = rows;
}

let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(poll, 500);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
async function poll() {
  try {
    const state = await call('{{ state_url }}');
    render(state);
    // Only auto-refresh while a run is in progress. Once the run is done
    // (or aborted) we stop polling so the user can select text / copy
    // results / right-click without the DOM being clobbered every 500 ms.
    // Conversely, if a run is active (e.g. the user reloaded the page
    // mid-run), make sure the timer is going.
    if (state.running) startPolling();
    else stopPolling();
  } catch (e) { console.error(e); }
}

document.getElementById('start-btn').addEventListener('click', async () => {
  try {
    await call('{{ start_url }}', 'POST');
    startPolling(); poll();
  } catch (e) { alert(e); }
});
document.getElementById('grid-body').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button.row-start');
  if (!btn) return;
  const uuid = btn.dataset.uuid;
  if (!uuid) return;
  try {
    const url = '{{ start_url }}' + '?target_uuid=' + encodeURIComponent(uuid);
    await call(url, 'POST');
    startPolling(); poll();
  } catch (e) { alert(e); }
});
document.getElementById('stop-btn').addEventListener('click', async () => {
  try {
    await call('{{ stop_url }}', 'POST');
    // Keep polling for a moment to catch the worker reaching its
    // cancellation checkpoint and flipping running -> false. poll()
    // will stop the timer itself once it sees !state.running.
    startPolling();
  } catch (e) { alert(e); }
});

// Initial render: one fetch to populate the table. poll() will start the
// timer only if a run is already active (e.g. user reloaded mid-run).
poll().then(() => { /* timer already started inside poll() if needed */ });
</script>
</div>
"""


def render_benchmark_page(
    page_title: str, page_intro: str, specs: list, descriptions: dict[str, str],
    state_endpoint: str, start_endpoint: str, stop_endpoint: str,
) -> str:
    """Render the shared benchmark-suite page (table of targets × specs with
    live polling) for one spec set + runner. Used by /benchmark_basic and
    /benchmark_kanban; the endpoints differ, the page mechanics don't."""
    from flask import url_for

    return render_template_string(
        BENCHMARK_TEMPLATE,
        page_title=page_title,
        page_intro=page_intro,
        benchmarks=specs,
        benchmark_names_json=json.dumps([n for n, _, _ in specs]),
        benchmark_help=[(name, descriptions.get(name, "")) for name, _, _ in specs],
        state_url=url_for(state_endpoint),
        start_url=url_for(start_endpoint),
        stop_url=url_for(stop_endpoint),
    )


GENERAL_INTRO = (
    "Iterates the /model tree (available configs first, then each config's "
    "overrides) and runs every benchmark per target. One LLM stays loaded for "
    "each target group. Unavailable configs are skipped. Function-calling "
    "trials (tool_order, tool_route) are capped at 60s each; after 2 timeouts "
    "the benchmark is abandoned and marked failed."
)


@app.route("/benchmark_basic")
def benchmark_basic_page() -> str:
    return render_benchmark_page(
        "Benchmark basic", GENERAL_INTRO, BENCHMARK_SPECS, BENCHMARK_DESCRIPTIONS,
        "benchmark_basic_state", "benchmark_basic_start", "benchmark_basic_stop",
    )


@app.route("/benchmark_basic/state")
def benchmark_basic_state() -> Response:
    benchmark_runner.ensure_targets_populated()
    return app.response_class(
        json.dumps(benchmark_runner.get_state()),
        mimetype="application/json",
    )


@app.route("/benchmark_basic/start", methods=["POST"])
def benchmark_basic_start() -> Response:
    target_uuid = request.args.get("target_uuid") or request.form.get("target_uuid")
    target_uuids = [target_uuid] if target_uuid else None
    started = benchmark_runner.start(app, target_uuids=target_uuids)
    return app.response_class(
        json.dumps({"started": started}),
        mimetype="application/json",
    )


@app.route("/benchmark_basic/stop", methods=["POST"])
def benchmark_basic_stop() -> Response:
    benchmark_runner.stop()
    return app.response_class(
        json.dumps({"stopping": True}),
        mimetype="application/json",
    )
