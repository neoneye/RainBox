"""The /assistant page — a run-centric inspector over the assistant trace.

Split layout (mirrors /memory's facet tree): the left pane groups recent
`AssistantRun`s into **virtual status folders** (Recent / Running / Stopped /
Resolved / Unresolved — computed each load, not editable). The right pane shows
the selected run as a dashboard over a stream of typed events — a gantt for
spotting an anomaly by its width, a list for reading down what happened, and an
inspector for whichever row is selected. Read-only except the lifecycle actions
the existing endpoints already own — confirm / reject / undo a write-intent,
and stop / redirect a live run (`webapp/chat_api.py`). The selected run carries
a kebab (Copy run id / Copy journal id / Stop). See
notes/ui-left-panel-tree.md.

This module owns the page and its markdown twin, and no more than that. What a
run consists of is `db.assistant_log`; what one event says is
`webapp.assistant_components`; where a row sits on the gantt is
`webapp.assistant_log_view`. Both surfaces render the same events through the
same components, which is the point — the export used to walk the step rows and
rebuild every pane, so one run had two readings that could disagree.
"""

from uuid import UUID

from flask import Response, render_template_string, request

import db
from .assistant_components import event_markdown, fence
from .assistant_log_view import log_view
from .core import app

ASSISTANT_TEMPLATE = """
<!doctype html>
<title>Assistant run &mdash; rainbox</title>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; height: 100vh;
         display: flex; flex-direction: column; overflow: hidden; }
  .badge { display:inline-block; padding:1px 7px; border-radius:10px; font-size:0.74rem; font-weight:600; }
  /* In-progress → blue. */
  .b-running,.b-stopping,.b-executing { background:#e0edff; color:#1d4ed8; }
  /* Genuine success → green (a write went through / was approved). */
  .b-completed,.b-confirmed { background:#e6f4ea; color:#1e7e34; }
  /* Errored → red. */
  .b-failed,.b-killed { background:#fdecea; color:#c0392b; }
  /* Neutral phases & non-success terminal states → gray. "observed"/"final" are
     lifecycle phases, not outcomes, so they must not read as green. */
  .b-stopped,.b-rejected,.b-planned,.b-observed,.b-final { background:#f1f3f5; color:#555; }
  /* A call the loop could not make at all — neither an outcome nor a failure. */
  .b-skipped { background:#fff4e5; color:#b06f00; }
  /* A run finished — terminal but outcome-agnostic (the Resolved/Unresolved
     verdict says whether it succeeded) → blue-gray, not optimistic green. */
  .b-finished { background:#eef2f6; color:#475569; }
  .b-undone { background:#fef3c7; color:#92400e; }
  .b-control { background:#f3e8ff; color:#7e22ce; }
  .b-proposed { background:#fff4e5; color:#b06f00; }
  .b-obstacle { background:#fff4e5; color:#b06f00; }
  .b-out-resolved { background:#e6f4ea; color:#1e7e34; }
  .b-out-partial  { background:#fff4e5; color:#b06f00; }
  .b-out-failed   { background:#fdecea; color:#c0392b; }

  /* Full-height single-run detail pane; the run finder is /assistant-overview. */
  .as-main { overflow:auto; min-height:0; min-width:0; flex:1 1 auto;
             padding:12px 18px 3.5rem; }
  .as-empty { color:#667085; padding:1rem 0; }
  .as-empty a { color:#2563eb; }

  /* Detail header: run id + kebab actions menu. */
  .as-kebab { margin-left:auto; flex:0 0 auto; border:none; background:none; cursor:pointer;
             color:#6b7280; width:1.9rem; height:1.9rem; padding:0; border-radius:6px;
             display:inline-flex; align-items:center; justify-content:center; }
  .as-kebab::before { content:""; width:3px; height:3px; border-radius:50%; background:currentColor;
             box-shadow:0 -5px 0 currentColor, 0 5px 0 currentColor; }
  .as-kebab:hover { background:#eef0f6; color:#1a1a2e; }
  .as-menu { position:fixed; z-index:1000; min-width:150px; background:#fff; border:1px solid #d1d5db;
             border-radius:8px; box-shadow:0 6px 18px rgba(0,0,0,0.14); padding:0.25em;
             display:flex; flex-direction:column; }
  .as-menu[hidden] { display:none; }
  .as-menu .item { text-align:left; border:none; background:none; cursor:pointer; font:inherit;
                   font-size:0.85rem; color:#333; padding:0.45em 0.6em; border-radius:6px; }
  .as-menu .item:hover { background:#eef0f6; }
  .as-menu .item.danger { color:#b91c1c; }
  .as-toast { position:fixed; bottom:18px; right:18px; max-width:420px; background:#1f2937;
              color:#fff; padding:10px 14px; border-radius:8px; font-size:0.9rem;
              box-shadow:0 4px 14px rgba(0,0,0,0.3); z-index:2000; opacity:0;
              transition:opacity .25s; pointer-events:none; }
  .as-toast.show { opacity:1; }

  /* Right detail pane. */
  /* Full-bleed band: negative margins cancel .as-main's 12px/18px padding so it
     reaches the pane edges; only a bottom divider, no rounded box. */
  .as-main .dash { position:relative; display:grid; grid-template-columns:1.1fr 0.6fr 0.6fr 1.2fr 1fr;
                   gap:24px; margin:-12px -18px 1.4rem; padding:18px 18px;
                   border-bottom:1px solid #e5e7eb; }
  /* The run's controls sit in the dash's top-right free space (over the
     Tokens cell): the kebab, and — while the run is live — Stop and Redirect,
     which are only ever wanted while watching it from up here. */
  .as-main .dash .dash-actions { grid-column:1 / -1; justify-self:end;
        display:flex; align-items:center; gap:0.5rem; margin-bottom:-14px; }
  .as-main .dash .dash-actions .as-kebab { margin:0; }
  .as-main .dash .dcell { display:flex; flex-direction:column; }
  .as-main .dash .dlabel { font-size:0.66rem; font-weight:700; text-transform:uppercase;
                           letter-spacing:0.05em; color:#9ca3af; margin-bottom:8px; }
  .as-main .dash .dval { font-size:0.92rem; color:#374151; line-height:1.5;
                         font-variant-numeric:tabular-nums; }
  .as-main .dash .dval-big { font-size:1.3rem; font-weight:700; color:#1a1a2e;
                             font-variant-numeric:tabular-nums; }
  .as-main .dash .dsep { grid-column:1 / -1; margin:0 -18px; border:0; border-top:1px solid #e5e7eb; }
  .as-main .dash .dcell a { color:inherit; }
  .as-main .dash .dts { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .as-main .dash .dcell .dval + .dlabel { margin-top:8px; }
  .as-main .dash .dsummary { grid-column:1 / span 3; }
  .as-main .dash .dsummary + .dcell .dlabel { margin-bottom:2px; }
  .as-main .dash .dstatus-resolved { color:#1e7e34; }
  .as-main .dash .dstatus-unresolved { color:#c0392b; }
  .as-main .dash .dstatus-running { color:#1d4ed8; }
  .as-main .dash .dstatus-pending { color:#98a2b3; }
  .as-main h1 { margin:0.1rem 0 0.5rem; }
  .as-main .muted { color:#667085; font-size:0.85rem; }
  .as-main .grp { font-weight:600; margin:0.8rem 0 0.3rem; }
  .as-main pre { background:#f6f8fa; border:1px solid #e1e4e8; border-radius:6px;
                 padding:0.45rem 0.6rem; overflow-x:auto; white-space:pre-wrap;
                 margin:0.3rem 0; font-size:0.82rem; }
  .as-main button { font:inherit; padding:0.28rem 0.7rem; cursor:pointer; border:1px solid #ccc;
                    border-radius:6px; background:#fff; color:#222; }
  .as-main button.primary { background:#2563eb; border-color:#2563eb; color:#fff; }
  .as-main button.danger { color:#c0392b; border-color:#e7b9b3; }
  .as-main .summary { border:1px solid #e5e7eb; border-radius:8px;
                    padding:0.5rem 0.7rem; margin:0.6rem 0; background:#fbfdff; }
  .as-main .summary .grp { margin:0 0 0.25rem; }
  .as-main .obstacles { margin:0.2rem 0 0; padding-left:1.2rem; }
  .as-main .obstacles li { margin:0.1rem 0; }
  .as-main .card-body pre { margin:0; }
  .as-main .pending { background:#fff4e5; color:#92400e; border:1px solid #fde68a;
                      border-radius:6px; padding:0.4rem 0.6rem; margin:0.4rem 0; }
  /* The run header and each ReAct step are self-contained cards: a header band
     (.card-header) over a padded body, so each reads as one grouped
     unit. The name matches its children, card-title and card-link. */
  .as-main .card { border:1px solid #e5e7eb; border-radius:8px;
                   overflow:hidden; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,0.05);
                   margin-bottom:16px; }
  /* One gap for every header, matching the padding the divider box adds on
     its other side — otherwise each part sits closer to the rule on its right
     than to the one on its left. The horizontal padding matches the body
     below, so a header's first word starts where the body's text does. */
  .as-main .card .card-header { display:flex; gap:1rem; align-items:center;
                       flex-wrap:wrap; padding:10px 16px; background:#fbfdff;
                       border-bottom:1px solid #e5e7eb; }
  .as-main .card .card-header .card-title { font-size:1rem; font-weight:400; }
  .as-main .card .card-header .card-link { margin-left:auto; font-size:0.82rem; color:#2563eb; text-decoration:none; }
  .as-main .card .card-header .card-link:hover { text-decoration:underline; }
  /* Outcome chip after the card title, separated like the step header's spans. */
  .as-main .card .card-header .outcome { align-self:stretch; display:flex; align-items:center;
                                margin:-10px 0; padding:10px 0 10px 1rem;
                                border-left:1px solid #e5e7eb; font-weight:600; }
  /* The chip carries the run's outcome (same reading as the dashboard's
     headline status), not its lifecycle status: "finished" only means the loop
     terminated, so colouring it green would sell an unresolved run as a win. */
  .as-main .card .card-header .out-resolved { color:#1e7e34; }
  .as-main .card .card-header .out-unresolved { color:#c0392b; }
  .as-main .card .card-header .out-running { color:#1d4ed8; }
  .as-main .card .card-header .out-pending { color:#98a2b3; }
  /* The inspector's pane is a card body and takes the same padding as one,
     rather than restating a near-miss of it — its content lined up two pixels
     inside the header above it. */
  .as-main .card-body, .as-main .log-detail { padding:14px 16px; }
  /* The divider between a header's parts: a rule drawn by the box, not a
     pipe character typed between them. Shared so the inspect header and a
     step's header separate the same way. */
  .as-main .inspect .card-header > span:not(:first-child) {
                       align-self:stretch; display:flex; align-items:center;
                       margin:-10px 0; padding:10px 0 10px 1rem; border-left:1px solid #e5e7eb; }
  /* Model-call waterfall: name | track | duration. Bars are positioned on the
     run's wall-clock span, so a wide gap between two bars is time no model was
     working — which is what the chart is FOR, and what a bar per kind in a
     different colour was competing with. One neutral bar, one neutral name,
     and a single exception: a rejected call, in red, both bar and name. The
     kind of every other call is already written next to it in the name
     column, so colouring it too said nothing twice and left the row that is
     actually a problem as one colour among five. */
  /* No gap between the rows. A gap belongs to the container, not to either
     row beside it, so a click landing in one selected nothing — and on a
     timeline every row is a click target. The rows carry the spacing as their
     own padding instead, which keeps the pitch and gives every pixel an
     owner. */
  .as-main .wf { display:flex; flex-direction:column; }
  /* A button, because the row selects the pane below rather than navigating.
     The reset is what keeps it looking like a row and not a form control. */
  .as-main .wf-row { display:grid; grid-template-columns:14rem 1fr 4rem; gap:0.8rem;
                     align-items:center; text-decoration:none; color:inherit;
                     padding:3px 4px; border-radius:4px;
                     width:100%; text-align:left; background:none; border:0;
                     font:inherit; cursor:pointer; }
  .as-main .wf-row:hover { background:#f3f4f6; }
  .as-main .wf-name { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                      font-size:0.76rem; white-space:nowrap; overflow:hidden;
                      text-overflow:ellipsis; }
  .as-main .wf-track { position:relative; height:0.85rem; background:#f1f3f5;
                       border-radius:3px; overflow:hidden; }
  .as-main .wf-bar { position:absolute; top:0; bottom:0; border-radius:3px;
                     background:#98a2b3; }
  /* The one exception: a call whose response was thrown away and asked for
     again. Same red as the error text — the run paid for it and got nothing
     back, and against neutral bars it is the row the eye finds first. */
  .as-main .wf-tick { position:absolute; top:0; bottom:0; width:2px;
        background:#9aa3af; }
  .as-main .wf-row.on .wf-name { font-weight:600; }
  .as-main .wf-row:hover .wf-name { color:#1a1a2e; }
  .as-main .wf-row.on { background:#eef4ff; }
  .as-main .log-detail { overflow-x:auto; }
  .as-main .ev-pane { display:none; }
  .as-main .ev-pane.on { display:block; }
  .as-main .ev-detail h4 { margin:0 0 0.1rem; font-size:0.95rem; }
  .as-main .ev-caption { color:#6b7280; font-size:80%; margin-bottom:0.6rem; }
  /* Only what a line standing on its own needs: the step's version is
     right-aligned by margin-left:auto inside its label row, which does
     nothing for a flex container that already fills the width. */
  /* What is being inspected, in the card header beside "Inspect". The
     divider between them comes from the shared header rule below. */
  .as-main .ev-crumb-step { color:#6b7280; white-space:nowrap; }
  /* A row can belong to no step — the run's opening, or any row at all on a
     run whose steps recorded no timing to place them against. The span still
     has to exist for the selection to write into, so it hides itself rather
     than leaving a divider with nothing after it. */
  .as-main .inspect .card-header > span.ev-crumb-step:empty { display:none; }
  .as-main .ev-crumb-label { font-family:ui-monospace,monospace;
        font-weight:600; white-space:nowrap; }
  .as-main .ev-crumb-desc { color:#6b7280; min-width:0; overflow:hidden;
        text-overflow:ellipsis; white-space:nowrap; }
  .as-main .ev-kpi { white-space:nowrap; }
  .as-main details.ev-block > summary:hover { color:#1a1a2e; }
  .as-main .ev-block { margin-bottom:0.7rem; }
  .as-main .ev-block h5 { margin:0 0 0.2rem; font-size:70%;
        letter-spacing:0.05em; text-transform:uppercase; color:#6b7280; }
  /* Only what `.as-main pre` does not already give it. The box and the size
     come from there — restating them produced a 4px radius beside a 6px one
     and #f7f8fa beside #f6f8fa, which reads as two kinds of block when it is
     one.
     No inner scroller: a scroll area inside a scrolling page traps the wheel,
     and a prompt here runs to tens of thousands of characters. A long block is
     clamped to EV_CLAMP_LINES lines with a toggle, so the page is the only
     thing that scrolls. */
  .as-main .ev-pre { margin:0; word-break:break-word; }
  .as-main .ev-pre.clamped { overflow-y:hidden;
        -webkit-mask-image:linear-gradient(#000 72%, transparent);
        mask-image:linear-gradient(#000 72%, transparent); }
  .as-main .ev-more { display:block; margin:0.25rem 0 0; padding:0;
        border:0; background:none; font:inherit; font-size:0.72rem;
        color:#2563eb; cursor:pointer;
        -webkit-user-select:none; user-select:none; }
  .as-main .ev-more:hover { text-decoration:underline; }
  .as-main .ev-links { margin:0 0 0.6rem; font-size:85%; }
  .as-main .ev-links a { color:#2563eb; text-decoration:none; }
  .as-main .ev-links a:hover { text-decoration:underline; }
  .as-main .ev-note { color:#6b7280; font-size:85%; }
  .as-main .wf-bar.kind-rejected { background:#e8746f; }
  .as-main .wf-name.kind-rejected { color:#c0392b; }
  /* Visibly not a measurement of anything: nothing reported this time, which
     is why it is worth looking at. */
  .as-main .wf-bar.kind-unaccounted {
        background:repeating-linear-gradient(45deg,#fde68a,#fde68a 4px,
                                             transparent 4px,transparent 8px);
        border:1px solid #f0c674; }
  .as-main .wf-name.kind-unaccounted { color:#b45309; font-style:italic; }
  .as-main .wf-undated { position:absolute; left:4px; font-size:0.68rem; color:#98a2b3; }
  .as-main .wf-secs { font-size:0.76rem; color:#667085; text-align:right;
                      font-variant-numeric:tabular-nums; }
  .as-main .card .card-header .outcome.muted { color:#667085; font-weight:400; font-size:0.85rem; }
  /* The meta line under a pane's header: what the call cost, right-aligned,
     with the fields separated by the flex gap rather than by punctuation.
     ONE rule — it was two while a step section had a meta line of its own,
     and two rules for one line is how a monospace face here and a sans-serif
     one there comes back. */
  .as-main .ev-kpis { margin-left:auto; display:flex; gap:1rem; align-items:center;
                      flex-wrap:wrap; justify-content:flex-end;
                      margin-bottom:0.7rem;
                      font-size:0.72rem; font-weight:400; text-transform:none;
                      letter-spacing:normal; color:#98a2b3;
                      font-variant-numeric:tabular-nums; }
  .as-main .ev-kpi a { color:#2563eb; text-decoration:none; }
  .as-main .ev-kpi a:hover { text-decoration:underline; }
  /* Every collapsed block in a pane: the summary reads a notch smaller
     than the block titles beside it. */
  .as-main details.ev-block > summary { font-size:0.64rem; text-transform:uppercase;
                             letter-spacing:0.04em; color:#6b7280; margin-bottom:0.15rem;
                             cursor:pointer; -webkit-user-select:none; user-select:none; }
  .as-main .intent { border-left:3px solid #cbd5e1; margin:0.45rem 0 0.2rem 0.4rem;
                     padding:0.4rem 0.6rem; background:#fcfcfd; border-radius:0 6px 6px 0; }
  .as-main .intent.proposed { border-left-color:#f59e0b; }
  .as-main .intent.undone { border-left-color:#d97706; background:#fffbeb; }
  .as-main .intent.undone .cap { text-decoration:line-through; color:#92400e; }
  .as-main .intent.rejected { background:#f8f9fb; }
  .as-main .intent.rejected .cap { text-decoration:line-through; color:#6b7280; }
  .as-main .intent .cap { font-weight:600; }
  .as-main .acts { margin-top:0.35rem; display:flex; gap:0.4rem; flex-wrap:wrap; }
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
  {# /assistant is a single-run detail view; the run finder is /assistant-overview.
     The .as-main detail pane has a Markdown twin: _run_markdown() serializes the
     same sections (dashboard → summary → timeline → verdict) for the
     kebab's "View as markdown". Keep the two in sync when editing either. #}
  <section class="as-main">
    {% if not selected %}
      <h1>Assistant run</h1>
      <div class="as-empty">No run selected — open the
        <a href="{{ url_for('assistant_overview_page') }}">Assistant overview</a>
        to pick a run.</div>
    {% else %}
      <div class="dash">
        <div class="dash-actions">
          {# Acting on a live run belongs where the reader already is. The
             dashboard says it is still going and the timeline below is what
             they are reading, so the controls sit here rather than under a
             timeline that grows for as long as the run does. #}
          {% if selected.status in ('running', 'stopping') %}
          <button class="danger" onclick="ppConfirmAct('/chat/api/assistant/runs/{{ selected.uuid }}/stop', 'Stop this run?')">Stop</button>
          <button onclick="ppRedirect('{{ selected.uuid }}')">Redirect…</button>
          {% endif %}
          <button class="as-kebab" title="actions"
                  onclick="asKebab(event, '{{ selected.uuid }}', '{{ selected.status }}', '{{ selected.journal_id or '' }}')"></button>
        </div>
        <div class="dcell">
          <div class="dlabel">Status</div>
          <div class="dval-big dstatus-{{ dash.status_class }}">{{ dash.status }}</div>
          <div style="margin-top:6px"><span class="badge b-{{ selected.status }}">{{ selected.status | capitalize }}</span></div>
        </div>
        <div class="dcell">
          <div class="dlabel">Steps</div>
          <div class="dval-big">{{ dash.steps }}</div>
        </div>
        <div class="dcell">
          <div class="dlabel" title="Every model call the run made, including the ones with no step row of their own (the second opinion, the criteria revision)">LLM calls</div>
          <div class="dval-big">{{ dash.llm_calls }}</div>
        </div>
        <div class="dcell">
          <div class="dlabel">Time</div>
          <div class="dval">total {{ dash.total_time }}</div>
          <div class="dval">model {{ dash.model_time }}</div>
          {% if dash.embed_calls %}<div class="dval" title="Wall-clock inside the embedder ({{ dash.embed_calls }} call(s)) — a second model on the same runtime, called by memory retrieval">embed {{ dash.embed_time }}</div>{% endif %}
          <div class="dval" title="Wall-clock outside both models: action execution and loop overhead">action {{ dash.action_time }}</div>
        </div>
        <div class="dcell">
          <div class="dlabel">Tokens</div>
          <div class="dval">in {{ dash.in_tokens }}</div>
          <div class="dval">out {{ dash.out_tokens }}</div>
          {% if dash.llm_tps %}<div class="dval">{{ dash.llm_tps }} tok/s</div>{% endif %}
        </div>
        <hr class="dsep">
        <div class="dcell dsummary">
          <div class="dlabel">Summary</div>
          {% if selected.summary %}
            <div>{{ selected.summary.trigger }}</div>
            <div class="dlabel" style="margin-top:1.5rem">Obstacles</div>
            {% if selected.summary.obstacles %}
              <ul class="obstacles">
                {% for o in selected.summary.obstacles %}<li>{{ o }}</li>{% endfor %}
              </ul>
            {% else %}
              <div>None</div>
            {% endif %}
          {% else %}
            {% if selected.status in ('failed', 'killed') %}
              <div>{{ selected.final_summary or 'The run failed before diagnostics could be recorded.' }}</div>
            {% else %}
              <div class="muted">Not yet summarized (runs shortly after the assistant finishes).</div>
            {% endif %}
          {% endif %}
        </div>
        <div class="dcell">
          <div class="dlabel">Start</div>
          <div class="dval"><span class="dts">{{ selected.started_at.strftime('%Y-%m-%d %H:%M:%S') if selected.started_at else '—' }}</span></div>
          {% if selected.finished_at %}
          <div class="dlabel">Finish</div>
          <div class="dval"><span class="dts">{{ selected.finished_at.strftime('%Y-%m-%d %H:%M:%S') }}</span></div>
          {% endif %}
        </div>
      </div>

      {% if log.events %}
      {# Where the run's wall-clock went, in full: one bar per activity, laid
         end to end. Model calls, embedding calls, and each action's own work,
         none of them drawn over another — a bar spanning other bars hides
         them, and hides any stall between them. So one activity ends where
         the next begins, and a remaining gap is genuinely unmeasured time.
         The run's totals are in the dashboard above; repeating them here only
         asked the reader which of the two to believe. #}
      <div class="card">
        <div class="card-header">
          <div class="card-title">Timeline</div>
        </div>
        <div class="card-body">
          <div class="wf">
            {% for e in log.events %}
            <button type="button" class="wf-row ev-pick{% if e.selected %} on{% endif %}"
                    data-ev="{{ e.row_id }}" data-key="{{ e.key }}"
                    data-variant="{{ e.variant or e.kind }}"
                    {% if e.primary_for %}data-primary="{{ e.primary_for }}"{% endif %}
                    title="{{ e.label }} — {{ e.seconds }} at {{ e.clock }}">
              <span class="wf-name kind-{{ e.variant or e.kind }}">{{ e.label }}</span>
              <span class="wf-track">
                {% if e.width_pct is not none %}
                <span class="wf-bar kind-{{ e.variant or e.kind }}" style="left:{{ e.offset_pct }}%;width:{{ e.width_pct }}%"></span>
                {% elif e.offset_pct is not none %}
                <span class="wf-tick" style="left:{{ e.offset_pct }}%"></span>
                {% else %}
                <span class="wf-undated">not timed</span>
                {% endif %}
              </span>
              <span class="wf-secs">{{ e.seconds }}</span>
            </button>
            {% endfor %}
          </div>
        </div>
      </div>
      {% endif %}

      {% if log.events %}
      {# The detail for whichever event the gantt above has selected — one
         renderer per kind, so a new kind costs a component rather than more
         markup here. The gantt IS the list: a second list beside this pane
         said the same thing twice and made the reader choose which to read.
         Every pane is rendered once into the page, which is what makes
         selection a client-side swap rather than a round trip. #}
      <div class="card inspect">
        <div class="card-header">
          <div class="card-title">Inspect</div>
          {# What is being inspected right now, as the header's own children so
             they take the same divider a step's header parts do. Filled from
             the selected event's data attributes, so the header and the pane
             cannot describe different things. #}
          <span class="ev-crumb-step">{{ log.events[0].step_ref }}</span>
          <span class="ev-crumb-label">{{ log.events[0].label }}</span>
          <span class="ev-crumb-desc">{{ log.events[0].description }}</span>
          {# A link to whatever is being inspected. Real href so the context
             menu can copy it; clicking copies the absolute URL, because a
             click that only rewrote the address bar looks like nothing
             happened. #}
          <a class="card-link" id="ev-permalink"
             href="#ev-{{ log.events[0].key }}">link</a>
        </div>
        <div class="log-detail">
          {% for e in log.events %}
          <div class="ev-pane{% if loop.first %} on{% endif %}" id="{{ e.row_id }}">{{ e.detail_html|safe }}</div>
          {% endfor %}
        </div>
      </div>
      {% endif %}

      {% for c in pending_controls %}
      <div class="pending">⏳ pending {{ c.command }}{% if c.payload and c.payload.get('instruction') %}: {{ c.payload.get('instruction') }}{% endif %}</div>
      {% endfor %}

      {% if not log.events %}<div class="as-empty">This run has no steps.</div>{% endif %}

      {% if verdict %}
      <div class="card">
        <div class="card-header">
          <div class="card-title">Verdict</div>
          <span class="outcome out-{{ dash.status_class }}">{{ dash.status }}</span>
          {% if reply %}<a class="card-link" href="/chat?id={{ selected.room_uuid }}&msg={{ reply.id }}">chat ↗</a>{% endif %}
        </div>
        <div class="card-body">
          <pre>{{ verdict }}</pre>
        </div>
      </div>
      {% endif %}
    {% endif %}
  </section>

<div id="as-menu" class="as-menu" hidden></div>
<div id="as-toast" class="as-toast"></div>

<script>
// The gantt bars and the log rows are the same events, so selecting is one
// operation on a shared data-ev id. Every pane is already in the page, which
// is why this is a class swap rather than a fetch.
// How many lines of a long block are shown before its toggle. Measured in
// lines rather than pixels because that is the unit a reader scans in, and
// the block's own line-height is what says how tall a line is.
var EV_CLAMP_LINES = 6;

// A block is only measurable once its pane is shown and its <details> open,
// so this runs then rather than at load, and once per block.
function clampBlocks(root) {
  if (!root) { return; }
  root.querySelectorAll(".ev-pre").forEach(function (pre) {
    if (pre.dataset.clamped || !pre.offsetHeight) { return; }
    pre.dataset.clamped = "1";
    var cs = getComputedStyle(pre);
    var line = parseFloat(cs.lineHeight);
    if (!line) { line = parseFloat(cs.fontSize) * 1.2; }
    var max = Math.round(line * EV_CLAMP_LINES);
    // A block that already fits gets no control: a toggle that does nothing
    // is worse than no toggle.
    if (pre.scrollHeight <= max + 4) { return; }
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ev-more";
    function apply(on) {
      pre.classList.toggle("clamped", on);
      pre.style.maxHeight = on ? max + "px" : "";
      btn.textContent = on ? "show more" : "show less";
    }
    btn.addEventListener("click", function () {
      apply(!pre.classList.contains("clamped"));
    });
    apply(true);
    pre.parentNode.insertBefore(btn, pre.nextSibling);
  });
}

// Set by initEventLog, called by the live refresh: after a swap the selected
// row has to be restored through the same path a click takes, or the pane and
// the header say different things.
var asSelectEvent = null;

(function initEventLog() {
  function select(id) {
    document.querySelectorAll(".ev-pane").forEach(function (p) {
      p.classList.toggle("on", p.id === id);
    });
    document.querySelectorAll(".ev-pick").forEach(function (b) {
      b.classList.toggle("on", b.dataset.ev === id);
    });
    // The header says what is being inspected, read off the pane itself so
    // the two cannot drift apart.
    var detail = document.querySelector("#" + id + " .ev-detail");
    var label = document.querySelector(".ev-crumb-label");
    var desc = document.querySelector(".ev-crumb-desc");
    var step = document.querySelector(".ev-crumb-step");
    if (detail && label) { label.textContent = detail.dataset.label || ""; }
    if (detail && desc) { desc.textContent = detail.dataset.desc || ""; }
    if (detail && step) { step.textContent = detail.dataset.step || ""; }
    var pick = document.querySelector('.ev-pick[data-ev="' + id + '"]');
    var key = pick ? pick.getAttribute("data-key") : "";
    var link = document.getElementById("ev-permalink");
    if (link && key) { link.setAttribute("href", "#ev-" + key); }
    // The address bar holds a link to what is on screen, without stacking a
    // history entry per click — replaceState fires no hashchange, so this
    // cannot loop back into selectFromHash.
    if (key && window.history && history.replaceState) {
      history.replaceState(null, "", location.pathname + location.search
                           + "#ev-" + key);
    }
    clampBlocks(document.getElementById(id));
  }
  document.addEventListener("click", function (ev) {
    var pick = ev.target.closest(".ev-pick");
    if (!pick) { return; }
    ev.preventDefault();
    select(pick.dataset.ev);
  });
  // A collapsed block has no height until it opens, so it is measured then.
  document.addEventListener("toggle", function (ev) {
    if (ev.target.classList && ev.target.classList.contains("ev-block")) {
      clampBlocks(ev.target);
    }
  }, true);
  clampBlocks(document.querySelector(".ev-pane.on"));
  asSelectEvent = select;

  // Two fragment formats. `#ev-<key>` names a row directly, by the identity
  // the live refresh already uses. `#step-<uuid>` is the published format
  // db.assistant_step_path mints — chat proposal cards, cron provenance and
  // the uuid lookup all emit it — so it keeps resolving, through the one row
  // marked primary for that step.
  function pickFromHash(hash) {
    if (hash.indexOf("#ev-") === 0) {
      return document.querySelector(
        '.ev-pick[data-key="' + hash.slice(4) + '"]');
    }
    if (hash.indexOf("#step-") === 0) {
      return document.querySelector(
        '.ev-pick[data-primary="' + hash.slice(6) + '"]');
    }
    return null;
  }
  function selectFromHash() {
    var pick = pickFromHash(location.hash || "");
    // A fragment naming nothing leaves the page as it is rather than
    // selecting a neighbour the reader did not ask for.
    if (pick) { select(pick.dataset.ev); pick.scrollIntoView({block: "nearest"}); }
  }
  selectFromHash();
  window.addEventListener("hashchange", selectFromHash);
})();

  // --- kebab menu on the selected run ----------------------------------------
  var asMenu = document.getElementById('as-menu');
  function asCloseMenu() { asMenu.hidden = true; asMenu.replaceChildren(); }
  document.addEventListener('click', function (e) {
    if (!asMenu.hidden && !asMenu.contains(e.target)) asCloseMenu();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') asCloseMenu(); });

  function asItem(label, fn, danger) {
    var b = document.createElement('button');
    b.className = 'item' + (danger ? ' danger' : '');
    b.textContent = label;
    b.addEventListener('click', function () { asCloseMenu(); fn(); });
    return b;
  }
  function asKebab(event, uuid, status, journalId) {
    event.preventDefault();
    event.stopPropagation();
    asMenu.replaceChildren();
    asMenu.appendChild(asItem('Copy run id', function () { ppCopyText(uuid); }));
    if (journalId) {
      asMenu.appendChild(asItem('Copy journal id', function () { ppCopyText(journalId); }));
    }
    asMenu.appendChild(asItem('View as markdown', function () {
      window.location = '/assistant/' + uuid + '/markdown';
    }));
    // The run's opening row links the room for every run that has a
    // triggering message. One seeded outside the chat flow has no such row,
    // and the room is still where the run happened.
    asMenu.appendChild(asItem('Open chat room', function () {
      window.location = '/chat?id={{ selected.room_uuid }}';
    }));
    asMenu.appendChild(asItem('Refresh summary', function () {
      // The summarizer runs out-of-process, so just confirm it's queued — the
      // new digest appears on a later reload, not immediately.
      fetch('/chat/api/assistant/runs/' + uuid + '/resummarize', {method: 'POST'})
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          if (d && d.ok === false) { alert(d.text || 'Action failed'); return; }
          asToast((d && d.text) || 'Summary refresh queued.');
        })
        .catch(function (e) { alert('Request failed: ' + e); });
    }));
    if (status === 'running' || status === 'stopping') {
      asMenu.appendChild(asItem('Stop', function () {
        ppConfirmAct('/chat/api/assistant/runs/' + uuid + '/stop', 'Stop this run?');
      }, true));
    }
    var r = event.currentTarget.getBoundingClientRect();
    asMenu.style.left = Math.min(r.left, window.innerWidth - 170) + 'px';
    asMenu.style.top = (r.bottom + 4) + 'px';
    asMenu.hidden = false;
  }

  // --- shared actions --------------------------------------------------------
  function asToast(msg) {
    var t = document.getElementById('as-toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(function () { t.classList.remove('show'); }, 3500);
  }
  function ppAct(url) {
    fetch(url, {method: 'POST'})
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        if (d && d.ok === false) { alert(d.text || 'Action failed'); return; }
        // Flash survives the reload via sessionStorage (shown on load below).
        try { sessionStorage.setItem('as.flash', (d && d.text) || 'Done.'); } catch (e) {}
        location.reload();
      })
      .catch(function (e) { alert('Request failed: ' + e); });
  }
  (function () {
    var f = null;
    try { f = sessionStorage.getItem('as.flash'); sessionStorage.removeItem('as.flash'); } catch (e) {}
    if (f) asToast(f);
  })();
  function ppConfirmAct(url, msg) { if (window.confirm(msg)) ppAct(url); }
  function ppCopyText(text) { navigator.clipboard.writeText(text); }
  function ppRedirect(runId) {
    var instruction = prompt('Redirect instruction for the running run:');
    if (!instruction) return;
    fetch('/chat/api/assistant/runs/' + runId + '/redirect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({instruction: instruction}),
    }).then(function () { location.reload(); })
      .catch(function (e) { alert('Request failed: ' + e); });
  }

  // Copying the link to what is being inspected. The href is already right —
  // this is so a click does something visible rather than silently rewriting
  // the address bar.
  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('#ev-permalink');
    if (!link) return;
    e.preventDefault();
    ppCopyText(location.origin + location.pathname + location.search
               + link.getAttribute('href'));
    asToast('Link copied.');
  });

  // Deep-link to a step section: #step-<uuid> scrolls the .as-main pane to it
  // on load. (.as-main is the scroll container, so a bare fragment isn't
  // reliable here.) The inspector understands the same fragment and selects
  // the step's primary row; see selectFromHash.
  (function () {
    var h = location.hash;
    if (h.indexOf('#step-') === 0) {
      var el = document.getElementById(h.slice(1));
      if (el) el.scrollIntoView();
    }
  })();

  // --- live refresh ----------------------------------------------------------
  // Rides the same chat_events SSE stream as /chat (notes/chat-frontend-rules.md:
  // no polling, hidden tab stays silent and catches up on refocus). The step
  // helpers in db/assistant.py NOTIFY with {assistant_run_uuid} on run/step/
  // model-checkpoint writes; on an event for THIS run the page refetches its
  // own server-rendered HTML and swaps the .as-main pane in place — same Jinja
  // renderer, no client-side duplicate. The 300ms timer is a one-shot
  // coalescer armed only by an event, never self-rescheduling.
  (function () {
    var runId = {% if selected %}'{{ selected.uuid }}'{% else %}null{% endif %};
    if (!runId) return;
    var timer = null, dirty = false, connectedOnce = false;
    // Which collapsed block is which, across a swap. By its `data-k` — the row
    // it belongs to plus its role — and NOT by position: a live run grows rows
    // while it is being read, so an index would slide under the reader and
    // reopen the wrong one.
    function detailsKey(d) { return d.getAttribute('data-k'); }
    // The row being inspected, by the key that survives the run growing. The
    // server renders the first row selected, so without this a reader is
    // thrown back to `start` every few seconds — while watching the step they
    // are inspecting actually run.
    function selectedKey(root) {
      var picked = root.querySelector('.ev-pick.on');
      return picked ? picked.getAttribute('data-key') : null;
    }
    // The row the in-flight call BECAME, for a reader who was watching it.
    // The stream is chronological and the call that just landed is the newest
    // one on it, so the last decide / code-driven row is the row that was in
    // flight a moment ago.
    function landedCall(root) {
      var calls = root.querySelectorAll(
        '.ev-pick[data-variant="decide"], .ev-pick[data-variant="code-driven"]');
      return calls.length ? calls[calls.length - 1] : null;
    }
    function reselect(root, key) {
      if (!key || !asSelectEvent) { return; }
      var pick = root.querySelector('.ev-pick[data-key="' + key + '"]');
      // The in-flight row is the one row that is GUARANTEED to go: it exists
      // only between the request going out and the row landing. Dropping the
      // reader back to the top at that moment is the worst possible time to
      // do it — they were watching that exact call. Follow it to the row it
      // became instead. (A run still going keeps its live row, and the key
      // above matches it, so this only fires once the call has landed.)
      if (!pick && key.indexOf('llm:live:') === 0) { pick = landedCall(root); }
      // Otherwise: gone means the row it named is no longer on the stream (an
      // unaccounted gap that has since been filled). Leave the server's choice
      // standing rather than selecting something the reader did not ask for.
      if (pick) { asSelectEvent(pick.getAttribute('data-ev')); }
    }
    function openDetails(root) {
      var open = {};
      Array.prototype.forEach.call(
        root.querySelectorAll('details[data-k]'),
        function (d) { if (d.open) open[detailsKey(d)] = true; });
      return open;
    }
    function refresh() {
      timer = null;
      if (document.hidden) { dirty = true; return; }
      fetch(window.location.pathname + window.location.search)
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var next = new DOMParser().parseFromString(html, 'text/html')
            .querySelector('.as-main');
          var cur = document.querySelector('.as-main');
          if (!next || !cur) return;
          asCloseMenu();  // its buttons would reference pre-swap run state
          // A live run refreshes every few seconds. Re-collapsing what the
          // reader opened made a running run impossible to inspect — the
          // prompt closed under them before they had read it — so carry the
          // open blocks (and the scroll) across the swap.
          var scrollTop = cur.scrollTop;
          var open = openDetails(cur);
          var key = selectedKey(cur);
          cur.innerHTML = next.innerHTML;
          Array.prototype.forEach.call(
            cur.querySelectorAll('details[data-k]'),
            function (d) { if (open[detailsKey(d)]) d.open = true; });
          reselect(cur, key);
          cur.scrollTop = scrollTop;
        })
        .catch(function () { dirty = true; });
    }
    function schedule() {
      if (timer === null) timer = setTimeout(refresh, 300);
    }
    function startRunStream() {
      var es = new EventSource('/chat/stream');
      es.onopen = function () {
        // Catch up after a reconnect — events may have been missed while down.
        if (connectedOnce) schedule();
        connectedOnce = true;
      };
      es.onmessage = function (e) {
        var d;
        try { d = JSON.parse(e.data); } catch (err) { return; }
        if (d.assistant_run_uuid === runId) schedule();
      };
      es.onerror = function () {
        // While CONNECTING the browser retries on its own; only rebuild once
        // it has given up (CLOSED).
        if (es.readyState === EventSource.CLOSED) setTimeout(startRunStream, 3000);
      };
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && dirty) { dirty = false; schedule(); }
    });
    startRunStream();
  })();
</script>
"""


def _format_seconds(secs: float) -> str:
    """Human-readable elapsed seconds (e.g. 5.1s / 1m 5s / 1h 30m)."""
    secs = max(0.0, secs)
    if secs < 60:
        return f"{secs:.1f}s"
    if secs < 3600:
        return f"{int(secs // 60)}m {int(secs % 60)}s"
    return f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"


def _format_duration(start, finish) -> str | None:
    """Human-readable elapsed time (finish - start), or None if either is unset."""
    if start is None or finish is None:
        return None
    return _format_seconds((finish - start).total_seconds())


def _dash_status(run) -> tuple[str, str]:
    """The run's headline status for the dashboard: (label, css-suffix)."""
    if run.status in ("running", "stopping"):
        return ("Running", "running")
    outcome = (run.summary or {}).get("outcome")
    if outcome == "resolved":
        return ("Resolved", "resolved")
    if outcome in ("partial", "failed") or run.status in ("failed", "killed"):
        return ("Unresolved", "unresolved")
    if not run.summary:
        return ("—", "pending")        # terminal but not yet summarized
    return ("Unresolved", "unresolved")


def _run_dashboard(run, steps: list, reviews: list | None = None) -> dict:
    """Aggregate metrics for the top-of-detail mini dashboard.

    Cost comes from `db.assistant_run_stats` — every model call the run made,
    including the ones with no step row of their own. Counting rows instead left
    the second opinion and the criteria revision's inner call out of tokens and
    model time, which then reappeared as unexplained "action" time. The in-chat
    progress row reads the same helper, so the two cannot quote different
    figures for one run."""
    label, cls = _dash_status(run)
    stats = db.assistant_run_stats(steps, reviews, run=run)
    in_tokens, out_tokens = stats["input_tokens"], stats["output_tokens"]
    llm_ms = stats["duration_ms"]
    llm_seconds = llm_ms / 1000
    # The embedder gets its own line rather than hiding inside "action": it is
    # a model call on the same local runtime, and a run whose retrieval spends
    # seconds embedding looks, without this, like a run with slow actions.
    embed_seconds = stats["embedding_ms"] / 1000
    # "action" time = wall-clock spent outside either model (action execution +
    # overhead) = total - model - embed. Only computable once the run has
    # finished.
    total_seconds = None
    if run.started_at and run.finished_at:
        total_seconds = (run.finished_at - run.started_at).total_seconds()
    return {
        "status": label,
        "status_class": cls,
        "steps": len(steps),
        "llm_calls": stats["calls"],
        "total_time": _format_seconds(total_seconds) if total_seconds is not None else "—",
        "model_time": _format_seconds(llm_seconds),
        "embed_calls": stats["embedding_calls"],
        "embed_time": _format_seconds(embed_seconds),
        "action_time": (_format_seconds(total_seconds - llm_seconds - embed_seconds)
                        if total_seconds is not None else "—"),
        "llm_tps": stats["tps"],
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
    }


# --- markdown export ---------------------------------------------------------
# The detail pane serialized to Markdown, section-for-section with
# ASSISTANT_TEMPLATE's `.as-main`: dashboard → summary → model calls → run →
# timeline → verdict.
#
# Built from `run_events` — the same stream the page draws — rather than from
# the step rows. It used to walk the rows and rebuild every pane, which is how
# one run came to have two readings that could disagree. What each event says
# is decided once, in `webapp.assistant_components`.


def _run_markdown(run, ctx: dict) -> str:
    """Serialize a run's detail pane to Markdown, mirroring `.as-main`."""
    dash = ctx["dash"]
    trigger = ctx["trigger"]
    events = ctx.get("log", {}).get("events") or []

    def fmt_dt(dt) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"

    out: list[str] = [f"# Assistant run {run.uuid}", ""]

    # Dashboard metrics.
    toks = f"in {dash['in_tokens']} · out {dash['out_tokens']}"
    if dash.get("llm_tps"):
        toks += f" · {dash['llm_tps']} tok/s"
    out += [
        f"- **Status:** {dash['status']} ({run.status.capitalize()})",
        f"- **Steps:** {dash['steps']}",
        f"- **LLM calls:** {dash['llm_calls']}",
        (f"- **Time:** total {dash['total_time']} · model {dash['model_time']}"
         + (f" · embed {dash['embed_time']} ({dash['embed_calls']} calls)"
            if dash.get("embed_calls") else "")
         + f" · action {dash['action_time']}"),
        f"- **Tokens:** {toks}",
        f"- **Start:** {fmt_dt(run.started_at)}",
        f"- **Finish:** {fmt_dt(run.finished_at)}",
        f"- **Journal:** {run.journal_id or '—'}",
        "",
    ]

    # Summary + obstacles.
    out += ["## Summary", ""]
    summary = run.summary or {}
    if summary:
        out += [summary.get("trigger", "") or "", "", "### Obstacles", ""]
        obstacles = summary.get("obstacles") or []
        out += [f"- {o}" for o in obstacles] if obstacles else ["None"]
    else:
        if run.status in ("failed", "killed"):
            out.append(
                run.final_summary
                or "The run failed before diagnostics could be recorded."
            )
        else:
            out.append("Not yet summarized.")
    out.append("")

    # Model calls. The page draws these as a gantt; flat text keeps the same
    # reading — start offset from the run's beginning, then duration — so the
    # gaps that show where the time went survive the export.
    if events:
        out += ["## Model calls", "",
                "| call | kind | at | took |", "|---|---|---|---|"]
        for c in events:
            at = c["start"].strftime("%H:%M:%S") if c["start"] else "—"
            # An embed row's label quotes the text it was given, so the label
            # is no longer a fixed vocabulary: a pipe in it would split the
            # row into columns that aren't there.
            label = " ".join(c["label"].split()).replace("|", "\\|")
            # The finer `variant` (rejected, decide, code-driven) rather than
            # the coarse kind, so the export names a row the way the page
            # colours it.
            out.append(
                f"| {label} | {c.get('variant') or c['kind']} | {at} | {c['seconds']} |")
        out.append("")

    # Trigger message.
    out += ["## Run", ""]
    if trigger:
        out += [f"Started by {trigger['sender_name']}", "", fence(trigger["text"])]
    else:
        out.append("No triggering chat message found.")
    out.append("")

    # Pending controls.
    if ctx["pending_controls"]:
        out += ["## Pending controls", ""]
        for c in ctx["pending_controls"]:
            instr = (c.payload or {}).get("instruction") if c.payload else None
            out.append(f"- pending {c.command}" + (f": {instr}" if instr else ""))
        out.append("")

    # The stream, one section per event — the same events, the same dispatch
    # and the same blocks the inspector shows for whichever row is selected.
    out += ["## Timeline", ""]
    if not events:
        out += ["This run has no events.", ""]
    for event in events:
        out += event_markdown(event)

    # Verdict.
    if ctx["verdict"]:
        # Mirrors the HTML chip: the outcome, not the lifecycle status. The
        # not-yet-summarized label is "—", which reads as noise in a header.
        label = ctx["dash"]["status"]
        head = "## Verdict" + (f" — {label}" if label != "—" else "")
        out += [head, "", ctx["verdict"], ""]

    return "\n".join(out).rstrip() + "\n"


def _active_model_call(run) -> dict | None:
    """The in-flight model call checkpoint for the live view: present only
    while the loop is inside a model call (the checkpoint is cleared as soon
    as the step row lands, so this never duplicates a timeline step). Returns
    the newest attempt's streamed partials, or None when idle/settled."""
    if run.status not in ("running", "stopping"):
        return None
    active = (run.metadata_ or {}).get("active_call")
    if not active:
        return None
    attempts = active.get("attempts") or []
    newest = attempts[-1] if attempts else {}
    return {
        "step_index": active.get("step_index"),
        "model_name": newest.get("model_name"),
        "partial_reasoning": newest.get("partial_reasoning"),
        "partial_response": newest.get("partial_response"),
        "error": newest.get("error"),
    }


def _load_run_detail(selected) -> dict:
    """The per-run detail both surfaces read: the event stream, the dashboard
    metrics above it, the run's controls, and the trigger and reply messages.

    One shape, because the page and the markdown export are two renderings of
    it. Anything derived per-event — prompts, results, write intents, review
    verdicts — is on the events themselves and is not assembled again here.
    """
    # Causal order, not commit order: the reply audit's row is written before
    # the reply row it audits, so ordering by id put it above the decide call
    # that produced the reply — and disagreed with the gantt on the same page.
    # See `assistant_trace_steps`.
    steps = db.assistant_trace_steps(selected.uuid)
    review_rows = db.list_second_opinion_reviews(selected.uuid)
    trigger = db.get_run_trigger_message(selected)
    # The full final reply (the run stores only a truncated final_summary).
    reply = db.get_run_final_reply(selected)
    return {
        "pending_controls": db.list_pending_controls(selected.uuid),
        "trigger": trigger,
        "dash": _run_dashboard(selected, steps, review_rows),
        # The gantt and the log read one stream, so a kind added to
        # db.assistant_log appears in both without either learning about it.
        "log": log_view(selected, steps, review_rows,
                        trigger=trigger,
                        intents=db.list_write_intents_for_run(selected.uuid),
                        active=_active_model_call(selected)),
        "reply": reply,
        "verdict": reply["text"] if reply else selected.final_summary,
    }


def _selected_run():
    """The run addressed by ?id= (consistent with /chat, /cron), or None for a
    missing/malformed id."""
    run_arg = request.args.get("id")
    if not run_arg:
        return None
    try:
        return db.get_assistant_run(UUID(run_arg))
    except ValueError:
        return None

@app.route("/assistant")
def assistant_page() -> str:
    selected = _selected_run()
    ctx = _load_run_detail(selected) if selected is not None else {}
    duration = _format_duration(
        selected.started_at, selected.finished_at) if selected else None

    return render_template_string(
        ASSISTANT_TEMPLATE,
        selected=selected,
        trigger=ctx.get("trigger"),
        log=ctx.get("log", {"events": [], "span_seconds": 0.0}),
        pending_controls=ctx.get("pending_controls", []),
        duration=duration,
        dash=ctx.get("dash"),
        verdict=ctx.get("verdict"), reply=ctx.get("reply"),
    )



@app.route("/assistant/<run_id>/markdown")
def assistant_markdown(run_id: str):
    """The selected run's detail pane (`.as-main`) serialized to Markdown —
    backs the kebab's "View as markdown". Served as text/plain so the browser
    shows the raw source inline rather than offering a download."""
    try:
        selected = db.get_assistant_run(UUID(run_id))
    except ValueError:
        selected = None
    if selected is None:
        return Response("Run not found.", status=404, mimetype="text/plain")
    md = _run_markdown(selected, _load_run_detail(selected))
    return Response(md, mimetype="text/plain; charset=utf-8")
