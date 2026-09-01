import json

from flask import Response, abort, render_template_string, request

import db

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
  .warmup-toggle{margin-left:0.5em;font-size:0.9em;color:#444;cursor:pointer;user-select:none}
  .warmup-toggle input{vertical-align:-0.1em;margin-right:0.25em}
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
  td.bench{font-family:ui-monospace,monospace;font-size:90%;position:relative}
  button.cell-start{float:right;margin-left:0.4em;font-size:75%;line-height:1;
        padding:2px 5px;cursor:pointer;border:1px solid #cbd5e1;border-radius:4px;
        background:#fff;color:#475569}
  button.cell-start:hover:enabled{border-color:#9aa3af;color:#1a1a2e}
  button.cell-start:disabled{opacity:0.35;cursor:default}
  td.bench .historic{color:#64748b;font-style:italic}
  td.bench .historic-mark{font-style:normal;opacity:0.7;margin-right:0.25em}
  .cell-history{display:none;position:absolute;z-index:20;left:0;top:100%;
        min-width:22em;padding:0.5em 0.6em;border:1px solid #cbd5e1;
        border-radius:5px;background:#fff;box-shadow:0 4px 14px rgba(0,0,0,0.14);
        font-style:normal;color:#1a1a2e;text-align:left;white-space:nowrap}
  td.bench:hover .cell-history{display:block}
  .cell-history h4{margin:0 0 0.35em;font-size:90%;color:#475569;font-weight:600}
  .cell-history .hrow{display:flex;gap:0.8em;justify-content:space-between}
  .cell-history .hwhen{color:#475569}
  .cell-history .hsep{margin:0.4em 0 0.25em;padding-top:0.3em;
        border-top:1px solid #e2e8f0;color:#94a3b8;font-size:85%}
  .cell-history .hwarn{margin-top:0.4em;color:#b45309;font-size:85%;
        white-space:normal}
  progress{width:100%;height:12px}
  .ok{color:#080}
  .err{color:#a00}
  .muted{color:#888}
  .stories{margin-top:0.25em;font-size:80%;color:#555}
  .stories button{font:inherit;padding:0 0.4em;margin-left:0.15em;cursor:pointer;
                  border:1px solid #cbd5e1;border-radius:5px;background:#fff}
  .stories button:hover{border-color:#9aa3af}
  .stories a.json-story{margin-left:0.25em;color:#2563eb;text-decoration:none}
  .stories a.json-story:hover{text-decoration:underline}
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
  {% if show_warmup_toggle %}
  <label class="warmup-toggle" title="Send one throwaway 'hi' to each target before its first trial, so a cold model's load time doesn't land inside the first benchmark's average. Off while reading a run for cache behaviour, where a model warmed before the first trial is the thing being measured.">
    <input type="checkbox" id="warmup-toggle"> Warm up LLM
  </label>
  {% endif %}
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

// Stored results, {benchmark_name: {target_uuid: {complete:[], partial:[]}}}.
// Fetched from its own endpoint rather than read off the once-a-second /state
// poll: putting every cell's history on that poll would put the whole table's
// past on the wire every second for as long as the page is open.
let history = {};
const historyUrl = {{ history_url|tojson }};

async function loadHistory() {
  if (!historyUrl) return;
  try {
    const resp = await fetch(historyUrl);
    if (resp.ok) history = await resp.json();
  } catch (e) {
    // A missing history is a degraded page, never a broken one.
    history = {};
  }
}

function cellHistory(benchName, targetUuid) {
  const byTarget = history[benchName];
  return (byTarget && byTarget[targetUuid]) || null;
}

// The newest complete result, else the newest partial — what a cell shows
// when this session has not run it yet.
function historicEntry(benchName, targetUuid) {
  const h = cellHistory(benchName, targetUuid);
  if (!h) return null;
  return (h.complete && h.complete[0]) || (h.partial && h.partial[0]) || null;
}

function fmtWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString();
}

function historyRow(e) {
  const counts = '&#10003;' + e.correct + ' &#10007;' + e.mistakes + ' !' + e.failures;
  const per = e.trials_done > 0
    ? (e.total_elapsed / e.trials_done).toFixed(1) + 's/tr'
    : (e.status === 'done' ? '' : escapeHtml(e.status));
  return '<div class="hrow"><span class="hwhen">' + escapeHtml(fmtWhen(e.ended_at)) + '</span>' +
         '<span>' + e.trials_done + '/' + e.trials_total + '</span>' +
         '<span>' + counts + '</span><span>' + per + '</span></div>';
}

// The card. Absent entirely when a cell has no stored history — an empty box
// on hover is worse than no box.
function historyCard(benchName, targetUuid) {
  const h = cellHistory(benchName, targetUuid);
  if (!h) return '';
  const complete = h.complete || [];
  const partial = h.partial || [];
  if (!complete.length && !partial.length) return '';
  let inner = '<h4>' + escapeHtml(benchName) + '</h4>';
  inner += complete.map(historyRow).join('');
  if (partial.length) {
    inner += '<div class="hsep">partial</div>' + partial.map(historyRow).join('');
  }
  // Flagged, never hidden: seeing what changed when you retuned the model is
  // the reason to keep the earlier number at all.
  const newest = complete[0] || partial[0];
  const stale = complete.concat(partial).find(
    e => newest && e.config_fingerprint !== newest.config_fingerprint);
  if (stale) {
    inner += '<div class="hwarn">&#9888; model arguments changed since ' +
             escapeHtml(fmtWhen(stale.ended_at)) + '</div>';
  }
  return '<div class="cell-history">' + inner + '</div>';
}
// Whether trials of this spec set carry a readable artifact to copy.
const SHOW_ARTIFACTS = {{ 'true' if show_artifacts else 'false' }};
const ARTIFACT_URL = {{ artifact_url|tojson }};

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
// Per-trial artifacts. Two buttons each: the piece as markdown on the
// clipboard, and the run as a JSON file — system prompt, every request and
// response, and what the tool did — which is the one to reach for when a
// trial failed and it is not obvious why.
//
// Neither payload is in the polled state; both are fetched from the artifact
// endpoint on click. A sweep's transcripts are hundreds of kilobytes and the
// page refreshes about once a second.
function artifactHref(ti, bi, trial, format) {
  return `${ARTIFACT_URL}?target=${ti}&bench=${bi}&trial=${trial}` +
         (format ? `&format=${format}` : '');
}
function benchStories(b, ti, bi) {
  if (!SHOW_ARTIFACTS || !b.stories || !b.stories.length || !ARTIFACT_URL) return '';
  const buttons = b.stories.map(function (s) {
    const mark = s.correct ? '' : ' ×';
    const brief = s.topic ? escapeHtml(s.topic) : `trial ${s.trial + 1}`;
    return `<button type="button" class="copy-story"` +
           ` data-href="${artifactHref(ti, bi, s.trial, '')}"` +
           ` title="Copy the piece — ${brief}">#${s.trial + 1}${mark}</button>` +
           `<a class="json-story" download href="${artifactHref(ti, bi, s.trial, 'json')}"` +
           ` title="Download the full exchange as JSON — ${brief}">json</a>`;
  }).join(' ');
  return `<div class="stories">trial: ${buttons}</div>`;
}

// Copy via a hidden textarea, reporting what actually happened: 'ok',
// 'blocked' (execCommand claimed success but nothing was copied — the call can
// be proxied by an extension that returns true and copies nothing), or
// 'unavailable' (the path cannot run here: no execCommand, no focus, or no user
// gesture left). The browser's own `copy` event is what separates the first two
// — it fires only on a real copy.
function legacyCopy(text) {
  if (!document.execCommand) { return 'unavailable'; }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  const previous = document.activeElement;
  ta.select();
  let copied = false;
  const witness = function () { copied = true; };
  document.addEventListener('copy', witness, true);
  let claimed = false;
  try { claimed = document.execCommand('copy'); } catch (err) { claimed = false; }
  document.removeEventListener('copy', witness, true);
  document.body.removeChild(ta);
  if (previous && previous.focus) { previous.focus(); }
  if (claimed && copied) { return 'ok'; }
  return claimed ? 'blocked' : 'unavailable';
}

// One delegated listener, so buttons rebuilt by polling keep working.
document.addEventListener('click', async function (e) {
  const btn = e.target.closest && e.target.closest('button.copy-story');
  if (!btn) return;
  const old = btn.dataset.label || btn.textContent;
  btn.dataset.label = old;
  let text = '';
  try {
    const r = await fetch(btn.dataset.href);
    if (!r.ok) throw new Error(r.status);
    text = await r.text();
  } catch (err) {
    btn.textContent = 'fetch failed';
    setTimeout(function () { btn.textContent = old; }, 1400);
    return;
  }
  function say(msg) {
    btn.textContent = msg;
    setTimeout(function () { btn.textContent = old; }, 1400);
  }
  // Always report the outcome: a silent no-op leaves the operator pasting
  // whatever was on the clipboard before, which looks like the wrong story
  // rather than like a failure. The verified path goes first, since the
  // Clipboard API resolves the same way whether the write landed or was
  // intercepted. This button fetches the story before copying, so the user
  // gesture is usually spent by now and the verified path reports
  // 'unavailable' — the fallback is then a best effort that cannot be checked.
  const status = legacyCopy(text);
  if (status !== 'unavailable') {
    say(status === 'ok' ? 'copied' : 'copy failed');
  } else if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      function () { say('copied'); },
      function () { say('copy failed'); }
    );
  } else {
    say('copy failed');
  }
});

// Every cell gets its own start, because the interesting benchmark is often
// the last column and waiting through the others is minutes of nothing. The
// row and sweep buttons still do what they always did.
function cellStart(b, uuid, bi, running) {
  return `<button class="cell-start" data-uuid="${escapeHtml(uuid)}" data-bench="${bi}"` +
         `${running ? ' disabled' : ''} title="Run just this benchmark">&#9654;</button>`;
}

function renderBench(b, ti, bi, targetUuid) {
  const bname = benchmarkNames[bi];
  const card = historyCard(bname, targetUuid);
  if (b.status === 'done') {
    return `<div>${fmtCounts(b)}</div>${benchDetails(b)}${benchStories(b, ti, bi)}${card}`;
  }
  if (b.status === 'error') {
    const errText = b.error ? `<div class="err" style="font-size:85%">${escapeHtml(b.error)}</div>` : '';
    return `<div>${fmtCounts(b)}<span class="pill error" style="margin-left:0.4em">error</span></div>${errText}${benchDetails(b)}${benchStories(b, ti, bi)}${card}`;
  }
  if (b.status === 'pending') {
    // Nothing ran this session, so the last stored result stands in — marked
    // historic so a stale number is never mistaken for a fresh one.
    const e = historicEntry(bname, targetUuid);
    if (e) {
      const counts = '&#10003;' + e.correct + ' &#10007;' + e.mistakes + ' !' + e.failures;
      return `<div class="historic"><span class="historic-mark">&#8987;</span>` +
             `${e.trials_done}/${e.trials_total} ${counts}</div>${card}`;
    }
    return `<div class="muted">pending</div>${card}`;
  }
  // status === 'running'
  const pct = b.trials_total > 0 ? (b.trials_done / b.trials_total) : 0;
  return `<progress max="1" value="${pct}"></progress>` +
         `<div>${b.trials_done}/${b.trials_total} ${fmtCounts(b)}</div>${card}`;
}

// Friendly name for a provider id. Falls back to the raw id for unknown
// providers — still legible, just not as pretty.
function providerLabel(id) {
  if (id === 'ollama') return 'Ollama';
  if (id === 'jan') return 'Jan';
  if (id === 'lm_studio') return 'LM Studio';
  if (id === 'openrouter') return 'OpenRouter';
  return id || '';
}
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function render(state) {
  const statusEl = document.getElementById('status');
  if (state.blocked_by) {
    statusEl.textContent = `${state.blocked_by} is running — benchmarks run one at a time`;
  } else if (state.total_targets === 0) {
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
    for (let i = 0; i < t.benchmarks.length; i++) {
      const b = t.benchmarks[i];
      // A cell with no live result contributes its stored one, so a table
      // restored after a restart still ranks instead of showing 0.0000 for
      // every row. Cells with neither contribute (0 + 1)/(total + 1) exactly
      // as they do today.
      const e = b.status === 'pending'
        ? historicEntry(benchmarkNames[i], t.uuid) : null;
      const correct = e ? e.correct : b.correct;
      const total = e ? e.trials_total : b.trials_total;
      num *= (correct + 1);
      denom *= (total + 1);
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

  const busy = !!state.running || !!state.blocked_by;
  const topStart = document.getElementById('start-btn');
  if (topStart) topStart.disabled = busy;
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
    const startBtn = `<button class="row-start" data-uuid="${escapeHtml(t.uuid)}" ${busy ? 'disabled' : ''}>Start</button>`;
    const benchCells = benchmarkNames.map((bname, i) => {
      const b = t.benchmarks[i];
      return `<td class="bench">${cellStart(b, t.uuid, i, busy)}${renderBench(b, t.index, i, t.uuid)}</td>`;
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

// "Warm up LLM" lives in localStorage, not in the runner: it is a property of
// how this operator wants to read a run, not of the run itself, so it must
// survive a page load and must not be reset by whatever the last run used.
// Off by default — the warmup call is only worth its time when the numbers
// being read are timings.
const WARMUP_KEY = 'pp-benchmark-warmup';
// Returns "" on the pages that have no toggle, so their start URLs are
// untouched and the endpoint keeps its warm-up default.
function warmupQuery(separator) {
  const el = document.getElementById('warmup-toggle');
  return el ? separator + 'warmup=' + (el.checked ? '1' : '0') : '';
}
(function initWarmupToggle() {
  const el = document.getElementById('warmup-toggle');
  if (!el) return;
  el.checked = localStorage.getItem(WARMUP_KEY) === '1';
  el.addEventListener('change', () => {
    localStorage.setItem(WARMUP_KEY, el.checked ? '1' : '0');
  });
})();

let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(poll, 500);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
// History is refetched when a run finishes, so a cell that just completed
// shows its new entry without a page reload.
let wasRunning = false;
async function refreshHistoryIfRunEnded(state) {
  if (wasRunning && !state.running) {
    await loadHistory();
  }
  wasRunning = !!state.running;
}
async function poll() {
  try {
    const state = await call('{{ state_url }}');
    await refreshHistoryIfRunEnded(state);
    render(state);
    // Only auto-refresh while a run is in progress. Once the run is done
    // (or aborted) we stop polling so the user can select text / copy
    // results / right-click without the DOM being clobbered every 500 ms.
    // Conversely, if a run is active (e.g. the user reloaded the page
    // mid-run), make sure the timer is going.
    if (state.running || state.blocked_by) startPolling();
    else stopPolling();
  } catch (e) { console.error(e); }
}

document.getElementById('start-btn').addEventListener('click', async () => {
  try {
    const res = await call('{{ start_url }}' + warmupQuery('?'), 'POST');
    // started=false means another suite holds the machine; re-poll so the
    // status line names it instead of the click doing nothing visible.
    if (res && res.started === false) { poll(); return; }
    startPolling(); poll();
  } catch (e) { alert(e); }
});
document.getElementById('grid-body').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button.row-start, button.cell-start');
  if (!btn) return;
  const uuid = btn.dataset.uuid;
  if (!uuid) return;
  try {
    let url = '{{ start_url }}' + '?target_uuid=' + encodeURIComponent(uuid);
    if (btn.dataset.bench !== undefined) {
      url += '&bench=' + encodeURIComponent(btn.dataset.bench);
    }
    url += warmupQuery('&');
    const res = await call(url, 'POST');
    if (res && res.started === false) { poll(); return; }
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

// Initial render: history first, so the very first paint already shows the
// stored baseline rather than a grid of "pending" that fills in a beat later.
// poll() will start the timer only if a run is already active (e.g. user
// reloaded mid-run).
loadHistory().then(poll).then(() => { /* timer started inside poll() if needed */ });
</script>
</div>
"""


def render_benchmark_page(
    page_title: str, page_intro: str, specs: list, descriptions: dict[str, str],
    state_endpoint: str, start_endpoint: str, stop_endpoint: str,
    history_endpoint: str | None = None,
    show_artifacts: bool = False,
    artifact_endpoint: str | None = None,
    show_warmup_toggle: bool = False,
) -> str:
    """Render the shared benchmark-suite page (table of targets × specs with
    live polling) for one spec set + runner. Used by /benchmark_basic and
    /benchmark_kanban; the endpoints differ, the page mechanics don't.

    `show_artifacts` turns on the per-trial copy buttons for spec sets whose
    trials produce something worth reading — the story suite. Off elsewhere,
    where there is nothing to copy.

    `show_warmup_toggle` adds the "Warm up LLM" checkbox. Only the story suite
    has it: that is the set being read for cache behaviour, where warming a
    model before the first trial hides the thing under observation. The other
    pages are read for timings and always warm up. Turning it on elsewhere is
    this one flag plus passing `warmup` through that page's start endpoint."""
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
        history_url=url_for(history_endpoint) if history_endpoint else '',
        show_artifacts=show_artifacts,
        artifact_url=url_for(artifact_endpoint) if artifact_endpoint else '',
        show_warmup_toggle=show_warmup_toggle,
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
        history_endpoint="benchmark_basic_history",
    )


@app.route("/benchmark_basic/state")
def benchmark_basic_state() -> Response:
    benchmark_runner.ensure_targets_populated()
    return app.response_class(
        json.dumps(benchmark_runner.get_state()),
        mimetype="application/json",
    )


@app.route("/benchmark_basic/history")
def benchmark_basic_history() -> Response:
    """Stored per-cell results for this suite.

    Its own endpoint rather than a field on /state: that one is polled once a
    second, and history on it would put every stored result on the wire every
    second for as long as the page is open — the same reason story artifacts
    are fetched on demand.
    """
    return app.response_class(
        json.dumps(db.benchmark_history("general")),
        mimetype="application/json",
    )


@app.route("/benchmark_basic/start", methods=["POST"])
def benchmark_basic_start() -> Response:
    target_uuid = request.args.get("target_uuid") or request.form.get("target_uuid")
    target_uuids = [target_uuid] if target_uuid else None
    # `bench` selects one cell; absent runs the whole row.
    raw_bench = request.args.get("bench") or request.form.get("bench")
    bench_indices = None
    if raw_bench not in (None, ""):
        try:
            bench_indices = [int(raw_bench)]
        except ValueError:
            abort(400, "bench must be an integer")
    try:
        started = benchmark_runner.start(
            app, target_uuids=target_uuids, bench_indices=bench_indices
        )
    except ValueError as e:
        abort(400, str(e))
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
