"""The /assistant page — a run-centric inspector over the assistant trace.

Split layout (mirrors /memory's facet tree): the left pane groups recent
`AssistantRun`s into **virtual status folders** (Recent / Running / Stopped /
Resolved / Unresolved — computed each load, not editable), the right pane shows
the selected run's summary, details, and `AssistantStep` timeline with each
`AssistantWriteIntent` inline (joined by `step_uuid`). Read-only except the
lifecycle actions the existing endpoints already own — confirm / reject / undo a
write-intent, and stop / redirect a live run (`webapp/chat_api.py`). The selected
run carries a kebab (Copy run id / Copy journal id / Stop). See
docs/ui-left-panel-tree.md.
"""

import json
from uuid import UUID

from flask import Response, render_template_string, request

import db
from agents.assistant import CAPABILITIES
from .core import app

# action value -> short human-readable summary, for the timeline's "action
# call" section (the verbose `description` is LLM-facing). Static (the capability
# registry is defined in code), so resolve once at import.
_ACTION_DESCRIPTIONS = {
    n.value: (c.summary or c.description) for n, c in CAPABILITIES.items()
}

ASSISTANT_TEMPLATE = """
<!doctype html>
<title>Assistant run &mdash; rainbox</title>
{% macro render_intent(it) %}
  <div class="intent {{ it.state }}">
    <span class="cap">{{ it.capability_name }}</span>
    <span class="badge b-{{ it.state }}">{% if it.state == 'undone' %}↩ {% endif %}{{ it.state }}</span>
    {% if it.preview_text %}<div class="muted">{{ it.preview_text }}</div>{% endif %}
    {% if it.payload %}<pre>{{ it.payload | tojson }}</pre>{% endif %}
    <div class="acts">
      {% if it.state == 'proposed' %}
        <button class="primary" onclick="ppAct('/chat/api/assistant/write-intents/{{ it.uuid }}/confirm')">Confirm</button>
        <button class="danger" onclick="ppConfirmAct('/chat/api/assistant/write-intents/{{ it.uuid }}/reject', 'Reject this {{ it.capability_name }} write?')">Reject</button>
      {% elif it.state == 'completed' and it.result and it.result.get('undo') %}
        <button onclick="ppConfirmAct('/chat/api/assistant/write-intents/{{ it.uuid }}/undo', 'Undo this {{ it.capability_name }} write? This reverts the change.')">Undo</button>
      {% endif %}
    </div>
  </div>
{% endmacro %}
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
  .as-main .dash { position:relative; display:grid; grid-template-columns:1.2fr 1fr 1.4fr 1fr;
                   gap:24px; margin:-12px -18px 1.4rem; padding:18px 18px;
                   border-bottom:1px solid #e5e7eb; }
  /* Kebab sits in the dash's top-right free space (over the Tokens cell). */
  .as-main .dash .as-kebab { position:absolute; top:12px; right:14px; margin:0; }
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
  .as-main .summary .grp, .as-main .trigger .grp { margin:0 0 0.25rem; }
  .as-main .obstacles { margin:0.2rem 0 0; padding-left:1.2rem; }
  .as-main .obstacles li { margin:0.1rem 0; }
  .as-main .trigmsg { white-space:pre-wrap; word-break:break-word; margin:0; }
  .as-main .card-body pre { margin:0; }
  .as-main .pending { background:#fff4e5; color:#92400e; border:1px solid #fde68a;
                      border-radius:6px; padding:0.4rem 0.6rem; margin:0.4rem 0; }
  /* The run header and each ReAct step are self-contained cards: a header band
     (.hd) over a padded body, so each reads as one grouped unit. */
  .as-main .step, .as-main .card { border:1px solid #e5e7eb; border-radius:8px;
                   overflow:hidden; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,0.05);
                   margin-bottom:16px; }
  .as-main .step { scroll-margin-top:14px; }
  .as-main .step:target { border-color:#2563eb; box-shadow:0 0 0 2px rgba(37,99,235,0.25); }
  .as-main .step-anchor { text-decoration:none; padding:0.05rem 0.3rem; border-radius:4px; }
  .as-main .step .step-anchor:hover { color:#2563eb; background:#eef2ff; }
  .as-main .step:target .step-anchor { color:#2563eb; }
  .as-main .step .hd, .as-main .card .hd { display:flex; gap:0.5rem; align-items:center;
                       flex-wrap:wrap; padding:10px 14px; background:#fbfdff;
                       border-bottom:1px solid #e5e7eb; }
  .as-main .card .hd .card-title { font-size:1rem; font-weight:400; }
  .as-main .card .hd .card-link { margin-left:auto; font-size:0.82rem; color:#2563eb; text-decoration:none; }
  .as-main .card .hd .card-link:hover { text-decoration:underline; }
  /* Outcome chip after the card title, separated like the step header's spans. */
  .as-main .card .hd .outcome { align-self:stretch; display:flex; align-items:center;
                                margin:-10px 0; padding:10px 0 10px 1rem;
                                border-left:1px solid #e5e7eb; font-weight:600; }
  .as-main .card .hd .out-finished { color:#1e7e34; }
  .as-main .card .hd .out-stopped { color:#555; }
  .as-main .card .hd .out-failed, .as-main .card .hd .out-killed { color:#c0392b; }
  .as-main .step-body, .as-main .card-body { padding:14px 16px; }
  .as-main .step-body > :first-child { margin-top:0; }
  .as-main .step-body > :last-child { margin-bottom:0; }
  .as-main .step.phase-control .step-body { background:#faf5ff; }
  .as-main .step .ix { color:#98a2b3; font-variant-numeric:tabular-nums; }
  .as-main .step .hd { gap:1rem; }
  .as-main .step .hd > span:not(:first-child) { align-self:stretch; display:flex; align-items:center;
                       margin:-10px 0; padding:10px 0 10px 1rem; border-left:1px solid #e5e7eb; }
  .as-main .step .hd .ix { color:inherit; }
  .as-main .step .hd .action { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  /* Right-aligned metadata on io-labels: model link, token counts, duration, timestamp.
     Fields are separated by the flex gap, not punctuation. */
  .as-main .step .io-meta { margin-left:auto; display:flex; gap:1rem; align-items:center;
                            font-size:0.72rem; font-weight:400; text-transform:none;
                            letter-spacing:normal; color:#98a2b3;
                            font-variant-numeric:tabular-nums; }
  .as-main .step .io-model { color:#2563eb; text-decoration:none; }
  .as-main .step .io-model:hover { text-decoration:underline; }
  .as-main .step .action { font-weight:400; }
  .as-main .step .reason { color:#475467; margin:0.3rem 0; }
  /* Each step bundles the model's structured output (request) and the action's
     result (response); the uppercase io-label tells them apart. */
  .as-main .step .io { margin:0.4rem 0; }
  /* Extra space above these so the labels are easy to scan for. */
  .as-main .step .io-out, .as-main .step .io-call, .as-main .step .io-in,
  .as-main .step .io-think { margin-top:1.4rem; }
  .as-main .step .io-label { font-size:0.68rem; text-transform:uppercase;
                             letter-spacing:0.04em; color:#6b7280; margin-bottom:0.2rem;
                             display:flex; align-items:center; }
  .as-main .step .io > pre { margin:0; }
  .as-main .step .io-req pre { max-height:20rem; overflow:auto; }
  /* Compact counts table for structured action data (e.g. memory_query). */
  .as-main .step .io-data { border-collapse:collapse; font-size:0.8rem; margin:0.6rem 0 0; }
  .as-main .step .io-data th, .as-main .step .io-data td {
     border:1px solid #d1d5db; padding:2px 8px; text-align:right; }
  .as-main .step .io-data th { background:#f3f4f6; font-weight:600; cursor:help; }
  /* The chosen action's human description, shown after the action in the header band. */
  .as-main .step .hd .action-desc { color:inherit; font-size:inherit; font-weight:400; }
  /* The observation's ok flag, derived from the step phase (observed=ok). */
  .as-main .step .fn-ok { text-transform:none; font-weight:600; margin-left:0.3rem; }
  .as-main .step .fn-ok.ok-true { color:#1e7e34; }
  .as-main .step .fn-ok.ok-false { color:#c0392b; }
  /* Timestamps and durations live inside io-meta; spacing comes from its gap. */
  .as-main .step .io-time, .as-main .step .io-dur { text-transform:none; font-weight:400;
                            color:#98a2b3; font-size:0.72rem; font-variant-numeric:tabular-nums; }
  /* Per-step debug log: collapsed by default, placed before the model
     request. Entries are {label, text, uuid?, href?} rows. */
  .as-main .step .steplog { margin:0 0 0.3rem; }
  .as-main .step .steplog > summary { font-size:0.64rem; text-transform:uppercase;
                             letter-spacing:0.04em; color:#98a2b3; cursor:pointer;
                             -webkit-user-select:none; user-select:none; }
  .as-main .step .steplog-body { margin:0.2rem 0 0 0.4rem; font-size:0.78rem; }
  .as-main .step .steplog-entry { padding:1px 0; }
  .as-main .step .steplog-entry .k { color:#6b7280; font-weight:600;
                             margin-right:0.35rem; }
  .as-main .step .steplog-entry .u { color:#98a2b3; font-size:0.7rem;
                             margin-left:0.35rem; }
  /* "model request" sub-parts: system and user prompt, each collapsed in a
     <details>. The summaries mirror .io-label but a notch smaller. */
  .as-main .step .prompt { margin:0.25rem 0 0; }
  .as-main .step .prompt > summary { font-size:0.64rem; text-transform:uppercase;
                             letter-spacing:0.04em; color:#6b7280; margin-bottom:0.15rem;
                             cursor:pointer; -webkit-user-select:none; user-select:none; }
  .as-main .err { color:#c0392b; }
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
     same sections (dashboard → summary → trigger → timeline → verdict) for the
     kebab's "View as markdown". Keep the two in sync when editing either. #}
  <section class="as-main">
    {% if not selected %}
      <h1>Assistant run</h1>
      <div class="as-empty">No run selected — open the
        <a href="{{ url_for('assistant_overview_page') }}">Assistant overview</a>
        to pick a run.</div>
    {% else %}
      <div class="dash">
        <button class="as-kebab" title="actions"
                onclick="asKebab(event, '{{ selected.uuid }}', '{{ selected.status }}', '{{ selected.journal_id or '' }}')"></button>
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
          <div class="dlabel">Time</div>
          <div class="dval">total {{ dash.total_time }}</div>
          <div class="dval">model {{ dash.model_time }}</div>
          <div class="dval">action {{ dash.action_time }}</div>
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

      <div class="card">
        <div class="hd">
          <div class="card-title">{% if trigger %}Started by <a href="/user?id={{ trigger.sender_uuid }}">{{ trigger.sender_name }} ↗</a>{% else %}Run{% endif %}</div>
          {% if selected.status in ('running', 'stopping') %}
            <button class="danger" onclick="ppConfirmAct('/chat/api/assistant/runs/{{ selected.uuid }}/stop', 'Stop this run?')">Stop</button>
            <button onclick="ppRedirect('{{ selected.uuid }}')">Redirect…</button>
          {% endif %}
          <a class="card-link" href="/chat?id={{ selected.room_uuid }}{% if trigger %}&msg={{ trigger.id }}{% endif %}">chat ↗</a>
        </div>
        <div class="card-body">
          <div class="trigger">
            {% if trigger %}
              <pre class="trigmsg">{{ trigger.text }}</pre>
            {% else %}
              <div class="muted">No triggering chat message found.</div>
            {% endif %}
          </div>
        </div>
      </div>

      {% for c in pending_controls %}
      <div class="pending">⏳ pending {{ c.command }}{% if c.payload and c.payload.get('instruction') %}: {{ c.payload.get('instruction') }}{% endif %}</div>
      {% endfor %}

      {% if not timeline %}<div class="as-empty">This run has no steps.</div>{% endif %}
      {% for step, intents in timeline %}
      <div class="step phase-{{ step.phase }}" id="step-{{ step.uuid }}">
        <div class="hd">
          <a class="ix step-anchor" href="#step-{{ step.uuid }}" title="Link to this step (internal step index={{ step.step_index }})">Step {{ step.step_index + 1 }} of {{ timeline|length }}</a>
          <span class="action" title="The action the model decided to take for this step">{{ step.action or '—' }}</span>
          {% if step.action and action_descriptions.get(step.action) %}<span class="action-desc">{{ action_descriptions[step.action] }}</span>{% endif %}
        </div>
        <div class="step-body">
        {% if step.phase == 'control' %}
          {% if step.reason %}<div class="reason">{{ step.reason }}</div>{% endif %}
        {% else %}
        {% if step.log %}
        <details class="steplog">
          <summary>log</summary>
          <div class="steplog-body">
          {% for entry in step.log %}
            <div class="steplog-entry"><span class="k">{{ entry.label }}</span>
              {%- if entry.href %} <a href="{{ entry.href }}">{{ entry.text }}</a>
              {%- else %} {{ entry.text }}{% endif %}
              {%- if entry.uuid %} <code class="u">{{ entry.uuid }}</code>{% endif %}</div>
          {% endfor %}
          </div>
        </details>
        {% endif %}
        {% if step.system_prompt or step.user_prompt %}
        <div class="io io-req">
          <div class="io-label">model request{% if step.requested_at %}<span class="io-meta"><span class="io-time" title="When this model request was made: {{ step.requested_at.replace(microsecond=0).isoformat() }}">{{ step.requested_at.strftime('%H:%M:%S') }}</span></span>{% endif %}</div>
          {% if step.system_prompt %}
          <details class="prompt">
            <summary>system prompt</summary>
            <pre>{{ step.system_prompt }}</pre>
          </details>
          {% endif %}
          {% if step.user_prompt %}
          <details class="prompt">
            <summary>user prompt</summary>
            <pre>{{ step.user_prompt }}</pre>
          </details>
          {% endif %}
        </div>
        {% endif %}
        {% if step.reasoning %}
        <div class="io io-think">
          {# The model's native reasoning channel (a reasoning model's thinking
             before it emitted the structured decision); absent for
             non-reasoning models. Collapsed like the request prompts. #}
          <div class="io-label">model reasoning</div>
          <details class="prompt">
            <summary>reasoning ({{ step.reasoning | length }} chars)</summary>
            <pre>{{ step.reasoning }}</pre>
          </details>
        </div>
        {% endif %}
        <div class="io io-out">
          {% set decision_text = decision_json.get(step.uuid|string, '') %}
          {% set has_toks = step.input_tokens is not none or step.output_tokens is not none %}
          {% set has_right = step.model_uuid or has_toks or step.duration_ms is not none %}
          {# This io-meta line (model · tokens · throughput · duration · time) is
             duplicated in Python by _response_meta_md(); change both together. #}
          <div class="io-label">{% if step.model_response and not decision_text %}partial model response{% else %}model response{% endif %}{% if has_right or step.created_at %}<span class="io-meta">
            {% if step.model_uuid %}<a class="io-model" href="/model?id={{ step.model_uuid }}"
                title="{{ model_names.get(step.model_uuid|string, (step.model_uuid|string)[:8]) }}">model ↗</a>{% endif %}
            {% if has_toks %}<span title="Input tokens: the size of the prompt sent to the model for this step">in {{ step.input_tokens or 0 }}</span>
            <span title="Output tokens: the amount of text the model generated for this step">out {{ step.output_tokens or 0 }}</span>{% endif %}
            {% if has_toks and step.duration_ms %}<span title="Throughput: total tokens (input + output) processed per second">{{ '%.0f'|format(((step.input_tokens or 0) + (step.output_tokens or 0)) * 1000 / step.duration_ms) }} tok/s</span>{% endif %}
            {% if step.duration_ms is not none %}<span title="Duration: how long the model took to produce this response">took {{ '%.1f'|format(step.duration_ms / 1000) }}s</span>{% endif %}
            {% if step.created_at %}<span class="io-time" title="When this model response was recorded: {{ step.created_at.replace(microsecond=0).isoformat() }}">{{ step.created_at.strftime('%H:%M:%S') }}</span>{% endif %}
          </span>{% endif %}</div>
          <pre>{{ decision_text or step.model_response or '' }}</pre>
        </div>
        {% if step.action %}
        <div class="io io-call">
          <div class="io-label">action call{% if step.created_at %}<span class="io-meta"><span class="io-time" title="When this action was called: {{ step.created_at.replace(microsecond=0).isoformat() }}">{{ step.created_at.strftime('%H:%M:%S') }}</span></span>{% endif %}</div>
          {% if step.args %}<pre>{{ step.args | tojson }}</pre>{% endif %}
        </div>
        {% endif %}
        {% endif %}
        {% set obs = step.observation %}
        {# The model request / action call / action result io-blocks below are
           mirrored in Python by _step_md(); keep them aligned. #}
        {% if obs is not none or step.observation_preview %}
        <div class="io io-in">
          <div class="io-label">action result{% if obs is not none %}<span class="fn-ok {{ 'ok-true' if obs.ok else 'ok-false' }}">ok: {{ 'true' if obs.ok else 'false' }}</span>{% endif %}{% if step.settled_at %}<span class="io-meta">{% if step.created_at %}<span class="io-dur" title="Duration: how long the action took to complete">took {{ '%.1f'|format((step.settled_at - step.created_at).total_seconds()) }}s</span>{% endif %}<span class="io-time" title="When this action result was recorded: {{ step.settled_at.replace(microsecond=0).isoformat() }}">{{ step.settled_at.strftime('%H:%M:%S') }}</span></span>{% endif %}</div>
          {% if obs is not none %}
            {% if obs.text %}<pre>{{ obs.text }}</pre>{% endif %}
            {% if obs.data %}
              {% if 'qa_static' in obs.data %}
              <table class="io-data"><thead><tr>
                <th title="number of QA static items">QA static</th>
                <th title="number of QA dynamic items">QA dynamic</th>
                <th title="number of memory items">memory</th>
                <th title="number of facts shortened because they exceeded the 1200-char per-fact cap (tagged truncate1200); read one in full via memory_query with its uuid">truncated</th>
                <th title="number of lower-ranked facts dropped because the whole block exceeded the 11000-char budget; narrow the query or fetch a fact by its uuid">omitted</th>
              </tr></thead><tbody><tr>
                <td>{{ obs.data.qa_static }}</td>
                <td>{{ obs.data.qa_dynamic }}</td>
                <td>{{ obs.data.memory }}</td>
                <td>{{ obs.data.truncated }}</td>
                <td>{{ obs.data.omitted }}</td>
              </tr></tbody></table>
              {% else %}<pre>{{ obs.data | tojson }}</pre>{% endif %}
            {% endif %}
          {% elif step.observation_preview %}
            <pre>{{ step.observation_preview }}</pre>
          {% endif %}
        </div>
        {% endif %}
        {% if step.error %}<div class="err">{{ step.error }}</div>{% endif %}
        {% for it in intents %}{{ render_intent(it) }}{% endfor %}
        </div>
      </div>
      {% endfor %}

      {% if unlinked %}
        <div class="grp">Unlinked writes <span class="muted">(no step reference)</span></div>
        {% for it in unlinked %}{{ render_intent(it) }}{% endfor %}
      {% endif %}

      {# Live view of the model call in flight (streamed checkpoints, updated
         ~1s). Present only between "request sent" and "step row landed", so it
         never duplicates a timeline step. Live-view only: intentionally NOT
         mirrored in _run_markdown(), which exports the durable run record. #}
      {% if active_call %}
      <div class="step phase-running" id="active-call">
        <div class="hd">
          <span class="ix">{% if active_call.step_index is not none %}Step {{ active_call.step_index + 1 }}{% else %}Step{% endif %}</span>
          <span class="action">model call in progress…</span>
          {% if active_call.model_name %}<span class="action-desc">{{ active_call.model_name }}</span>{% endif %}
        </div>
        <div class="step-body">
          {% if active_call.partial_reasoning %}
          <div class="io io-think">
            <div class="io-label">model reasoning (streaming)</div>
            <pre>{{ active_call.partial_reasoning }}</pre>
          </div>
          {% endif %}
          {% if active_call.partial_response %}
          <div class="io io-out">
            <div class="io-label">partial model response</div>
            <pre>{{ active_call.partial_response }}</pre>
          </div>
          {% endif %}
          {% if active_call.error %}<div class="err">{{ active_call.error }}</div>{% endif %}
          {% if not active_call.partial_reasoning and not active_call.partial_response and not active_call.error %}
          <div class="muted">Waiting for the model…</div>
          {% endif %}
        </div>
      </div>
      {% endif %}

      {% if verdict %}
      <div class="card">
        <div class="hd">
          <div class="card-title">Verdict</div>
          <span class="outcome out-{{ selected.status }}">{{ selected.status | capitalize }}</span>
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

  // Deep-link to a step: #step-<uuid> scrolls the .as-main pane to it on load.
  // (.as-main is the scroll container, so a bare fragment isn't reliable here.)
  (function () {
    var h = location.hash;
    if (h.indexOf('#step-') === 0) {
      var el = document.getElementById(h.slice(1));
      if (el) el.scrollIntoView();
    }
  })();

  // --- live refresh ----------------------------------------------------------
  // Rides the same chat_events SSE stream as /chat (docs/chat-frontend-rules.md:
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
          var scrollTop = cur.scrollTop;
          cur.innerHTML = next.innerHTML;
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


def _run_dashboard(run, steps: list) -> dict:
    """Aggregate metrics for the top-of-detail mini dashboard."""
    label, cls = _dash_status(run)
    in_tokens = sum((s.input_tokens or 0) for s in steps)
    out_tokens = sum((s.output_tokens or 0) for s in steps)
    llm_ms = sum((s.duration_ms or 0) for s in steps)
    llm_seconds = llm_ms / 1000
    # "action" time = wall-clock spent outside the model (action execution +
    # overhead) = total - model. Only computable once the run has finished.
    total_seconds = None
    if run.started_at and run.finished_at:
        total_seconds = (run.finished_at - run.started_at).total_seconds()
    return {
        "status": label,
        "status_class": cls,
        "steps": len(steps),
        "total_time": _format_seconds(total_seconds) if total_seconds is not None else "—",
        "model_time": _format_seconds(llm_seconds),
        "action_time": (_format_seconds(total_seconds - llm_seconds)
                        if total_seconds is not None else "—"),
        "llm_tps": round((in_tokens + out_tokens) / (llm_ms / 1000)) if llm_ms else None,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
    }


# --- markdown export ---------------------------------------------------------
# Serialize the /assistant detail pane to Markdown, section-for-section with
# ASSISTANT_TEMPLATE's `.as-main`: dashboard → summary → trigger → timeline →
# unlinked writes → verdict. Built from the data model (not the DOM) so it stays
# stable as the HTML evolves.


def _fence(text: str, lang: str = "") -> str:
    """A fenced code block whose fence is long enough to survive backticks in
    `text` (CommonMark: the closing fence must be at least as long as any run of
    backticks inside)."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _hms(dt) -> str | None:
    return dt.strftime("%H:%M:%S") if dt else None


def _response_meta_md(step, model_names: dict[str, str]) -> str:
    """The "model response" meta line — model, tokens, throughput, duration,
    time — joined with ' · '. Mirror of the template's `io-out`/`io-meta` line
    (search ASSISTANT_TEMPLATE for "duplicated in Python by _response_meta_md");
    the throughput formula must match the one there. Edit both together."""
    parts: list[str] = []
    if step.model_uuid:
        parts.append(model_names.get(str(step.model_uuid), str(step.model_uuid)[:8]))
    has_toks = step.input_tokens is not None or step.output_tokens is not None
    if has_toks:
        parts.append(f"in {step.input_tokens or 0}")
        parts.append(f"out {step.output_tokens or 0}")
    if has_toks and step.duration_ms:
        tps = ((step.input_tokens or 0) + (step.output_tokens or 0)) * 1000 / step.duration_ms
        parts.append(f"{tps:.0f} tok/s")
    if step.duration_ms is not None:
        parts.append(f"took {step.duration_ms / 1000:.1f}s")
    when = _hms(step.created_at)
    if when:
        parts.append(when)
    return " · ".join(parts)


def _intent_md(it) -> list[str]:
    """One write-intent as Markdown: a bullet with capability + state, optional
    preview, and the payload as a JSON block."""
    lines = [f"- write intent `{it.capability_name}` — {it.state}"]
    if it.preview_text:
        lines.append(f"  - {it.preview_text}")
    if it.payload:
        lines.append("")
        lines.append(_fence(json.dumps(it.payload, ensure_ascii=False, indent=2), "json"))
    lines.append("")
    return lines


def _step_md(step, decision_json: dict[str, str], model_names: dict[str, str]) -> list[str]:
    """A single timeline step's body: model request/response, action call/result
    and any error. Mirror of the template's per-step io-blocks (search
    ASSISTANT_TEMPLATE for "mirrored in Python by _step_md"); keep the set of
    blocks and their order aligned with the HTML."""
    lines: list[str] = []
    if step.phase == "control":
        if step.reason:
            lines.append(step.reason)
            lines.append("")
        return lines
    if step.log:
        lines.append("**log**")
        lines.append("")
        for entry in step.log:
            text = str(entry.get("text") or "")
            suffix = f" `{entry['uuid']}`" if entry.get("uuid") else ""
            lines.append(f"- {entry.get('label')}: {text}{suffix}")
        lines.append("")
    if step.system_prompt or step.user_prompt:
        when = _hms(step.requested_at)
        lines.append("**model request**" + (f" · {when}" if when else ""))
        lines.append("")
        if step.system_prompt:
            lines.append("_system prompt_")
            lines.append(_fence(step.system_prompt))
            lines.append("")
        if step.user_prompt:
            lines.append("_user prompt_")
            lines.append(_fence(step.user_prompt))
            lines.append("")
    if step.reasoning:
        lines.append("**model reasoning**")
        lines.append("")
        lines.append(_fence(step.reasoning))
        lines.append("")
    meta = _response_meta_md(step, model_names)
    decision = decision_json.get(str(step.uuid), "")
    response_label = (
        "partial model response"
        if step.model_response and not decision
        else "model response"
    )
    lines.append(f"**{response_label}**" + (f" · {meta}" if meta else ""))
    lines.append("")
    response_text = decision or step.model_response or ""
    if response_text:
        lines.append(_fence(response_text, "json" if decision else ""))
        lines.append("")
    if step.action:
        when = _hms(step.created_at)
        lines.append("**action call**" + (f" · {when}" if when else ""))
        lines.append("")
        if step.args:
            lines.append(_fence(json.dumps(step.args, ensure_ascii=False, indent=2), "json"))
            lines.append("")
    obs = step.observation
    if obs is not None or step.observation_preview:
        label = "**action result**"
        if obs is not None:
            label += f" · ok: {'true' if obs.get('ok') else 'false'}"
        if step.settled_at:
            if step.created_at:
                label += f" · took {(step.settled_at - step.created_at).total_seconds():.1f}s"
            label += f" · {_hms(step.settled_at)}"
        lines.append(label)
        lines.append("")
        if obs is not None:
            if obs.get("text"):
                lines.append(_fence(obs["text"]))
                lines.append("")
            if obs.get("data"):
                data = obs["data"]
                if "qa_static" in data:
                    lines.append("| QA static | QA dynamic | memory | truncated | omitted |")
                    lines.append("|---|---|---|---|---|")
                    lines.append(f"| {data['qa_static']} | {data['qa_dynamic']} | "
                                 f"{data['memory']} | {data['truncated']} | {data['omitted']} |")
                else:
                    lines.append(_fence(json.dumps(data, ensure_ascii=False, indent=2), "json"))
                lines.append("")
        elif step.observation_preview:
            lines.append(_fence(step.observation_preview))
            lines.append("")
    if step.error:
        lines.append(f"**error:** {step.error}")
        lines.append("")
    return lines


def _run_markdown(run, ctx: dict) -> str:
    """Serialize a run's detail pane to Markdown, mirroring `.as-main`."""
    dash = ctx["dash"]
    trigger = ctx["trigger"]
    timeline = ctx["timeline"]

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
        f"- **Time:** total {dash['total_time']} · model {dash['model_time']} · action {dash['action_time']}",
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

    # Trigger message.
    out += ["## Run", ""]
    if trigger:
        out += [f"Started by {trigger['sender_name']}", "", _fence(trigger["text"])]
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

    # Step timeline.
    out += ["## Timeline", ""]
    if not timeline:
        out += ["This run has no steps.", ""]
    n = len(timeline)
    for step, intents in timeline:
        head = f"Step {step.step_index + 1} of {n}"
        if step.phase == "control":
            out.append(f"### {head} — control")
        else:
            desc = _ACTION_DESCRIPTIONS.get(step.action or "")
            title = f"{head} — {step.action or '—'}" + (f" — {desc}" if desc else "")
            out.append(f"### {title}")
        out.append("")
        out += _step_md(step, ctx["decision_json"], ctx["model_names"])
        for it in intents:
            out += _intent_md(it)

    # Unlinked writes.
    if ctx["unlinked"]:
        out += ["## Unlinked writes", ""]
        for it in ctx["unlinked"]:
            out += _intent_md(it)

    # Verdict.
    if ctx["verdict"]:
        out += [f"## Verdict — {run.status.capitalize()}", "", ctx["verdict"], ""]

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
    """Assemble the per-run detail shared by the HTML page and the markdown
    export: the step timeline (each step with its write-intents), the verbatim
    decision dumps, unlinked write-intents, pending controls, trigger/reply
    messages, dashboard metrics, model display names, and the verdict text."""
    steps = db.list_assistant_steps(selected.uuid)
    intents = db.list_write_intents_for_run(selected.uuid)
    unlinked: list = []
    by_step: dict[str, list] = {}
    for it in intents:
        if it.step_uuid is None:
            unlinked.append(it)
        else:
            by_step.setdefault(str(it.step_uuid), []).append(it)
    timeline = [(s, by_step.get(str(s.uuid), [])) for s in steps]
    # The model emits one AssistantStepDecision per step; dump it verbatim
    # (field order preserved, not Flask's key-sorted tojson) for the trace.
    # Control steps are operator events, not model responses, so skip them.
    decision_json = {
        str(s.uuid): json.dumps(
            {"reason": s.reason, "action": s.action, "args": s.args or {}},
            ensure_ascii=False,
        )
        for s in steps
        if s.phase != "control" and (s.action is not None or s.reason is not None)
    }
    # The full final reply (the run stores only a truncated final_summary).
    reply = db.get_run_final_reply(selected)
    model_names: dict[str, str] = {}
    for muid in {s.model_uuid for s in steps if s.model_uuid}:
        mc = db.get_model_config(muid)
        if mc is not None:
            model_names[str(muid)] = mc.display_name or mc.model_name
    return {
        "timeline": timeline,
        "decision_json": decision_json,
        "unlinked": unlinked,
        "pending_controls": db.list_pending_controls(selected.uuid),
        "trigger": db.get_run_trigger_message(selected),
        "dash": _run_dashboard(selected, steps),
        "reply": reply,
        "verdict": reply["text"] if reply else selected.final_summary,
        "model_names": model_names,
        "active_call": _active_model_call(selected),
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
        timeline=ctx.get("timeline", []),
        decision_json=ctx.get("decision_json", {}),
        action_descriptions=_ACTION_DESCRIPTIONS,
        unlinked=ctx.get("unlinked", []),
        pending_controls=ctx.get("pending_controls", []),
        duration=duration, model_names=ctx.get("model_names", {}),
        dash=ctx.get("dash"), verdict=ctx.get("verdict"), reply=ctx.get("reply"),
        active_call=ctx.get("active_call"),
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
