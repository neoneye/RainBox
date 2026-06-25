"""The /assistant page — a run-centric inspector over the assistant trace.

Split layout (mirrors /memory's facet tree): the left pane groups recent
`AssistantRun`s into **virtual status folders** (Recent / Running / Stopped /
Resolved / Unresolved — computed each load, not editable), the right pane shows
the selected run's summary, details, and `AssistantStep` timeline with each
`AssistantWriteIntent` inline (joined by `step_uuid`). Read-only except the
lifecycle actions the existing endpoints already own — confirm / reject / undo a
write-intent, and stop / redirect a live run (`webapp/chat_api.py`). The selected
run carries a kebab (Copy id / Open in chat / Stop). See
docs/ui-left-panel-tree.md.
"""

import json
from uuid import UUID

from flask import render_template_string, request

import db
from agents.assistant import CAPABILITIES
from .core import app

# action value -> short human-readable summary, for the timeline's "function
# call" section (the verbose `description` is LLM-facing). Static (the capability
# registry is defined in code), so resolve once at import.
_ACTION_DESCRIPTIONS = {
    n.value: (c.summary or c.description) for n, c in CAPABILITIES.items()
}

# Lucide folder icons (verbatim from /chat — the convention's shared SVGs).
_ICON_FOLDER = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
                'fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 '
                '2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 '
                '0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>')
_ICON_FOLDER_OPEN = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
                     'fill="none" stroke="currentColor" stroke-width="2" '
                     'stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 '
                     '1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 '
                     '0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 '
                     '1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>')

ASSISTANT_TEMPLATE = """
<!doctype html>
<title>Assistant runs &mdash; rainbox</title>
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
{% macro run_leaf(r) %}
  <li>
    <div class="as-run-node {{ 'sel' if selected and r.uuid == selected.uuid }}">
      <a class="as-run-link" href="{{ url_for('assistant_page') }}?id={{ r.uuid }}">
        {% if r.status in ('running', 'stopping') %}<span class="as-ind run" title="running">⏳</span>
        {% elif r.status in ('failed', 'killed') %}<span class="as-ind fail" title="failed">✗</span>{% endif %}
        {% if r.summary %}<span class="rsum">{{ r.summary.trigger }}</span>
        {% else %}<span class="rsum pending">summarizing…</span>{% endif %}
      </a>
      <button class="as-kebab" title="actions"
              onclick="asKebab(event, '{{ r.uuid }}', '{{ r.room_uuid }}', '{{ r.status }}')"></button>
    </div>
  </li>
{% endmacro %}
<style>
  body { margin: 0; font-family: system-ui, sans-serif; height: 100vh;
         display: flex; flex-direction: column; overflow: hidden; }
  .badge { display:inline-block; padding:1px 7px; border-radius:10px; font-size:0.74rem; font-weight:600; }
  .b-running,.b-stopping { background:#e0edff; color:#1d4ed8; }
  .b-finished,.b-observed,.b-final,.b-completed,.b-confirmed,.b-executing { background:#e6f4ea; color:#1e7e34; }
  .b-failed,.b-killed { background:#fdecea; color:#c0392b; }
  .b-stopped,.b-rejected,.b-planned { background:#f1f3f5; color:#555; }
  .b-undone { background:#fef3c7; color:#92400e; }
  .b-control { background:#f3e8ff; color:#7e22ce; }
  .b-proposed { background:#fff4e5; color:#b06f00; }
  .b-obstacle { background:#fff4e5; color:#b06f00; }
  .b-out-resolved { background:#e6f4ea; color:#1e7e34; }
  .b-out-partial  { background:#fff4e5; color:#b06f00; }
  .b-out-failed   { background:#fdecea; color:#c0392b; }

  /* Full-height split: virtual folder tree (left) | run detail (right). */
  .as-split { display:grid; grid-template-columns:340px minmax(0,1fr);
              grid-template-rows:1fr; flex:1 1 auto; min-height:0; }
  .as-tree { overflow:auto; min-height:0; border-right:1px solid #e5e7eb;
             background:#fbfbfb; padding:10px; font-size:0.9rem; }
  .as-main { overflow:auto; min-height:0; min-width:0; padding:12px 18px 3.5rem; }
  .as-empty { color:#667085; padding:1rem 0; }

  /* Tree: virtual folders via <details>; folder icon is the expand indicator. */
  .as-folder { margin-bottom:2px; }
  .as-folder > summary { list-style:none; display:flex; align-items:center; gap:5px;
              padding:8px 4px; border-radius:4px; cursor:pointer; white-space:nowrap;
              -webkit-user-select:none; user-select:none; }
  .as-folder > summary::-webkit-details-marker { display:none; }
  .as-folder > summary:hover { background:#f1f5f9; }
  .as-ficon { display:inline-flex; align-items:center; color:#6b7280; }
  .as-ficon svg { width:15px; height:15px; display:block; }
  .as-folder:not([open]) .as-ficon-open { display:none; }
  .as-folder[open] .as-ficon-closed { display:none; }
  .as-group-count { margin-left:4px; color:#6b7280; font-weight:400; font-size:0.82rem; }
  .as-tree-list, .as-tree-list ul { list-style:none; margin:0; padding:0; }
  .as-tree-list { margin-left:0.85em; border-left:1px solid #e5e7eb; padding-left:0.35em; }
  .as-none { color:#98a2b3; font-style:italic; font-size:0.82rem; padding:3px 4px; }

  .as-run-node { display:flex; align-items:flex-start; gap:4px; padding:3px 4px;
                 border-radius:4px; }
  .as-run-node:hover { background:#f1f5f9; }
  .as-run-node.sel { background:#dbeafe; }
  .as-run-link { flex:1 1 auto; min-width:0; text-decoration:none; color:#222;
                 display:flex; gap:5px; align-items:flex-start; padding:2px 2px; }
  .as-run-node.sel .as-run-link { font-weight:600; }
  .as-ind { flex:0 0 auto; font-size:0.9rem; line-height:1.35; }
  .as-ind.fail { color:#c0392b; }
  .as-run-link .rsum { flex:1 1 auto; min-width:0; font-size:0.82rem; color:#344054;
                       line-height:1.35; display:-webkit-box; -webkit-line-clamp:2;
                       -webkit-box-orient:vertical; overflow:hidden; }
  .as-run-link .rsum.pending { color:#98a2b3; font-style:italic; }
  .as-kebab { margin-left:auto; flex:0 0 auto; align-self:center; border:none; background:none;
              cursor:pointer; color:#6b7280; width:1.4rem; height:1.4rem; padding:0; border-radius:5px;
              display:inline-flex; align-items:center; justify-content:center; visibility:hidden; }
  .as-run-node.sel .as-kebab { visibility:visible; }
  .as-kebab::before { content:""; width:3px; height:3px; border-radius:50%; background:currentColor;
                      box-shadow:-5px 0 0 currentColor, 5px 0 0 currentColor; }
  .as-kebab:hover { background:#d2ddf6; color:#1a1a2e; }
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
  .as-main .dash { display:grid; grid-template-columns:1.2fr 1fr 1.4fr 1fr;
                   gap:24px; margin:-12px -18px 1.4rem; padding:18px 18px;
                   border-bottom:1px solid #e5e7eb; }
  .as-main .dash .dcell { display:flex; flex-direction:column; }
  .as-main .dash .dlabel { font-size:0.66rem; font-weight:700; text-transform:uppercase;
                           letter-spacing:0.05em; color:#9ca3af; margin-bottom:8px; }
  .as-main .dash .dval { font-size:0.92rem; color:#374151; line-height:1.5;
                         font-variant-numeric:tabular-nums; }
  .as-main .dash .dval-big { font-size:1.3rem; font-weight:700; color:#1a1a2e;
                             font-variant-numeric:tabular-nums; }
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
  .as-main .trigmsg { white-space:pre-wrap; word-break:break-word; margin-top:0.25rem; }
  .as-main hr.sep { border:0; border-top:1px solid #e5e7eb; margin:1rem 0; }
  .as-main .pending { background:#fff4e5; color:#92400e; border:1px solid #fde68a;
                      border-radius:6px; padding:0.4rem 0.6rem; margin:0.4rem 0; }
  /* The run header and each ReAct step are self-contained cards: a header band
     (.hd) over a padded body, so each reads as one grouped unit. */
  .as-main .step, .as-main .runcard { border:1px solid #e5e7eb; border-radius:8px;
                   overflow:hidden; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,0.05);
                   margin-bottom:16px; }
  .as-main .step .hd, .as-main .runcard .hd { display:flex; gap:0.5rem; align-items:center;
                       flex-wrap:wrap; padding:10px 14px; background:#fbfdff;
                       border-bottom:1px solid #e5e7eb; }
  .as-main .step-body, .as-main .runcard-body { padding:14px 16px; }
  .as-main .runcard-body .trigger { margin-top:0.8rem; }
  .as-main .step-body > :first-child { margin-top:0; }
  .as-main .step-body > :last-child { margin-bottom:0; }
  .as-main .step.phase-control .step-body { background:#faf5ff; }
  .as-main .step .ix { color:#98a2b3; font-variant-numeric:tabular-nums; }
  .as-main .step .hd { gap:1rem; }
  .as-main .step .hd .ix { color:inherit; }
  .as-main .step .hd .action { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  /* Model/token meta on the "model response" label — matches io-time typography. */
  .as-main .step .step-right { margin-left:auto; display:flex; gap:0.5rem; align-items:center;
                               font-size:0.72rem; font-weight:400; text-transform:none;
                               letter-spacing:normal; color:#98a2b3;
                               font-variant-numeric:tabular-nums; }
  .as-main .step .step-model { color:#2563eb; text-decoration:none; }
  .as-main .step .step-model:hover { text-decoration:underline; }
  .as-main .step .toks { color:#98a2b3; font-variant-numeric:tabular-nums; }
  .as-main .step .action { font-weight:600; }
  .as-main .step .reason { color:#475467; margin:0.3rem 0; }
  /* Each step bundles the model's structured output (request) and the action's
     result (response); the uppercase io-label tells them apart. */
  .as-main .step .io { margin:0.4rem 0; }
  /* Extra space above these so the labels are easy to scan for. */
  .as-main .step .io-out, .as-main .step .io-call, .as-main .step .io-in { margin-top:1.4rem; }
  .as-main .step .io-label { font-size:0.68rem; text-transform:uppercase;
                             letter-spacing:0.04em; color:#6b7280; margin-bottom:0.2rem;
                             display:flex; align-items:center; }
  .as-main .step .io > pre { margin:0; }
  .as-main .step .io-req pre { max-height:20rem; overflow:auto; }
  /* The chosen action's human description, shown after the action in the header band. */
  .as-main .step .hd .action-desc { color:inherit; font-size:0.85rem; font-weight:400; }
  /* The observation's ok flag, derived from the step phase (observed=ok). */
  .as-main .step .fn-ok { text-transform:none; font-weight:600; margin-left:0.3rem; }
  .as-main .step .fn-ok.ok-true { color:#1e7e34; }
  .as-main .step .fn-ok.ok-false { color:#c0392b; }
  /* Invocation / response timestamps on the call & result labels. */
  .as-main .step .io-time { text-transform:none; font-weight:400; color:#98a2b3;
                            font-size:0.72rem; margin-left:auto;
                            font-variant-numeric:tabular-nums; }
  /* Function-call execution time, shown right before the result timestamp:
     "took 5.3s · 12:34:56". io-dur takes the auto margin so the pair stays
     together on the right. */
  .as-main .step .io-dur { text-transform:none; font-weight:400; color:#98a2b3;
                           font-size:0.72rem; margin-left:auto;
                           font-variant-numeric:tabular-nums; }
  .as-main .step .io-dur + .io-time, .as-main .step .step-right + .io-time { margin-left:0; }
  /* Separator between the meta (duration / model+tokens) and the timestamp. A
     pseudo-element with real margins, so the spacing survives flex edge-trimming. */
  .as-main .step .io-dur + .io-time::before,
  .as-main .step .step-right + .io-time::before { content:"·"; margin:0 0.4em; }
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
<div class="as-split">
  <aside class="as-tree">
    {% if not runs %}<div class="as-empty">No assistant runs yet.</div>{% endif %}
    {% for f in folders %}
    <details class="as-folder" data-folder="{{ f.name }}" {{ 'open' if f.default_open }}>
      <summary>
        <span class="as-ficon as-ficon-open">{{ icon_open | safe }}</span>
        <span class="as-ficon as-ficon-closed">{{ icon_closed | safe }}</span>
        <span class="as-fname">{{ f.name }}</span>
        <span class="as-group-count">{{ f.count }}</span>
      </summary>
      <ul class="as-tree-list">
        {% for r in f.runs %}{{ run_leaf(r) }}{% endfor %}
        {% if not f.runs %}<li class="as-none">none</li>{% endif %}
      </ul>
    </details>
    {% endfor %}
  </aside>

  <section class="as-main">
    {% if not selected %}
      <h1>Timeline</h1>
      <div class="as-empty">Select a run on the left to see its summary and step timeline.</div>
    {% else %}
      <div class="dash">
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
          <div class="dval">function {{ dash.function_time }}</div>
        </div>
        <div class="dcell">
          <div class="dlabel">Tokens</div>
          <div class="dval">in {{ dash.in_tokens }}</div>
          <div class="dval">out {{ dash.out_tokens }}</div>
          {% if dash.llm_tps %}<div class="dval">{{ dash.llm_tps }} tok/s</div>{% endif %}
        </div>
      </div>

      <div class="summary">
        <div class="grp">Summary</div>
        {% if selected.summary %}
          <div>{{ selected.summary.trigger }}</div>
          {% if selected.summary.obstacles %}
            <div class="grp" style="font-size:0.85rem; margin-top:0.8rem">Obstacles</div>
            <ul class="obstacles">
              {% for o in selected.summary.obstacles %}<li>{{ o }}</li>{% endfor %}
            </ul>
          {% else %}
            <div class="muted">No obstacles reported.</div>
          {% endif %}
        {% else %}
          <div class="muted">Not yet summarized (runs shortly after the assistant finishes).</div>
        {% endif %}
      </div>

      <div class="runcard">
        <div class="hd">
          <h1 style="margin:0">Run</h1>
          {% if selected.status in ('running', 'stopping') %}
            <button class="danger" onclick="ppConfirmAct('/chat/api/assistant/runs/{{ selected.uuid }}/stop', 'Stop this run?')">Stop</button>
            <button onclick="ppRedirect('{{ selected.uuid }}')">Redirect…</button>
          {% endif %}
        </div>
        <div class="runcard-body">
          <div class="muted">
            journal {{ (selected.journal_id|string)[:8] if selected.journal_id else '—' }}
            · started {{ selected.started_at.strftime('%Y-%m-%d %H:%M:%S') if selected.started_at else '—' }}
            {% if selected.finished_at %}· finished {{ selected.finished_at.strftime('%H:%M:%S') }}{% endif %}
          </div>
          <div class="trigger">
            {% if trigger %}
              <div>Started by
                <strong><a href="/user?id={{ trigger.sender_uuid }}">{{ trigger.sender_name }} ↗</a></strong>
                · <a href="/chat?id={{ selected.room_uuid }}&msg={{ trigger.id }}">open in chat ↗</a>
              </div>
              <pre class="trigmsg">{{ trigger.text }}</pre>
            {% else %}
              <div class="muted">No triggering chat message found ·
                room {{ (selected.room_uuid|string)[:8] }} ·
                <a href="/chat?id={{ selected.room_uuid }}">open in chat ↗</a>
              </div>
            {% endif %}
          </div>
        </div>
      </div>

      {% for c in pending_controls %}
      <div class="pending">⏳ pending {{ c.command }}{% if c.payload and c.payload.get('instruction') %}: {{ c.payload.get('instruction') }}{% endif %}</div>
      {% endfor %}

      {% if not timeline %}<div class="as-empty">This run has no steps.</div>{% endif %}
      {% for step, intents in timeline %}
      <div class="step phase-{{ step.phase }}">
        <div class="hd">
          <span class="ix" title="internal step index={{ step.step_index }}">Step {{ step.step_index + 1 }} of {{ timeline|length }}</span>
          <span class="action">{{ step.action or '—' }}</span>
          {% if step.action and action_descriptions.get(step.action) %}<span class="action-desc">{{ action_descriptions[step.action] }}</span>{% endif %}
        </div>
        <div class="step-body">
        {% if step.phase == 'control' %}
          {% if step.reason %}<div class="reason">{{ step.reason }}</div>{% endif %}
        {% else %}
        {% if step.system_prompt or step.user_prompt %}
        <div class="io io-req">
          <div class="io-label">model request{% if step.requested_at %}<span class="io-time" title="{{ step.requested_at.replace(microsecond=0).isoformat() }}">{{ step.requested_at.strftime('%H:%M:%S') }}</span>{% endif %}</div>
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
        <div class="io io-out">
          {% set has_toks = step.input_tokens is not none or step.output_tokens is not none %}
          {% set has_right = step.model_uuid or has_toks or step.duration_ms is not none %}
          <div class="io-label">model response{% if has_right %}<span class="step-right">
            {% if step.model_uuid %}<a class="step-model" href="/models?id={{ step.model_uuid }}"
                >{{ model_names.get(step.model_uuid|string, (step.model_uuid|string)[:8]) }} ↗</a>{% endif %}
            {% if has_toks or step.duration_ms is not none %}
              <span class="toks">
                {%- if has_toks %}in {{ step.input_tokens or 0 }} tok · out {{ step.output_tokens or 0 }} tok{% endif -%}
                {%- if has_toks and step.duration_ms %} · {{ '%.0f'|format(((step.input_tokens or 0) + (step.output_tokens or 0)) * 1000 / step.duration_ms) }} tok/s{% endif -%}
                {%- if step.duration_ms is not none %}{% if has_toks %} · {% endif %}took {{ '%.1f'|format(step.duration_ms / 1000) }}s{% endif -%}
              </span>
            {% endif %}
          </span>{% endif %}{% if step.created_at %}<span class="io-time" title="{{ step.created_at.replace(microsecond=0).isoformat() }}">{{ step.created_at.strftime('%H:%M:%S') }}</span>{% endif %}</div>
          <pre>{{ decision_json.get(step.uuid|string, '') }}</pre>
        </div>
        {% if step.action %}
        <div class="io io-call">
          <div class="io-label">function call{% if step.created_at %}<span class="io-time" title="{{ step.created_at.replace(microsecond=0).isoformat() }}">{{ step.created_at.strftime('%H:%M:%S') }}</span>{% endif %}</div>
          {% if step.args %}<pre>{{ step.args | tojson }}</pre>{% endif %}
        </div>
        {% endif %}
        {% endif %}
        {% set obs = step.observation %}
        {% if obs is not none or step.observation_preview %}
        <div class="io io-in">
          <div class="io-label">function result{% if obs is not none %}
            <span class="fn-ok {{ 'ok-true' if obs.ok else 'ok-false' }}">ok: {{ 'true' if obs.ok else 'false' }}</span>{% endif %}{% if step.settled_at and step.created_at %}<span class="io-dur">took {{ '%.1f'|format((step.settled_at - step.created_at).total_seconds()) }}s</span>{% endif %}{% if step.settled_at %}<span class="io-time" title="{{ step.settled_at.replace(microsecond=0).isoformat() }}">{{ step.settled_at.strftime('%H:%M:%S') }}</span>{% endif %}
          </div>
          {% if obs is not none %}
            {% if obs.text %}<pre>{{ obs.text }}</pre>{% endif %}
            {% if obs.data %}<pre>{{ obs.data | tojson }}</pre>{% endif %}
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

      {% if verdict %}
        <hr class="sep">
        <div class="grp">Verdict</div>
        <pre>{{ verdict }}</pre>
      {% endif %}
    {% endif %}
  </section>
</div>

<div id="as-menu" class="as-menu" hidden></div>
<div id="as-toast" class="as-toast"></div>

<script>
  // --- folder expand/collapse persistence (localStorage, keyed by name) -------
  document.querySelectorAll('details.as-folder').forEach(function (d) {
    var key = 'as.folder.' + d.dataset.folder;
    var saved = localStorage.getItem(key);
    if (saved === 'open') d.open = true;
    else if (saved === 'closed') d.open = false;
    d.addEventListener('toggle', function () {
      localStorage.setItem(key, d.open ? 'open' : 'closed');
    });
  });

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
  function asKebab(event, uuid, roomUuid, status) {
    event.preventDefault();
    event.stopPropagation();
    asMenu.replaceChildren();
    asMenu.appendChild(asItem('Copy id', function () { ppCopyText(uuid); }));
    asMenu.appendChild(asItem('Open in chat ↗', function () {
      window.location = '/chat?id=' + roomUuid;
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
    # "function" time = wall-clock spent outside the model (action execution +
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
        "function_time": (_format_seconds(total_seconds - llm_seconds)
                          if total_seconds is not None else "—"),
        "llm_tps": round((in_tokens + out_tokens) / (llm_ms / 1000)) if llm_ms else None,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
    }


def _bucket_runs(runs: list) -> list[dict]:
    """Group runs into the virtual status folders (facets — a run lands in every
    bucket it matches). Recent holds all; the rest are filtered subsets."""
    running, stopped, resolved, unresolved = [], [], [], []
    for r in runs:
        if r.status in ("running", "stopping"):
            running.append(r)
        if r.status == "stopped":
            stopped.append(r)
        outcome = (r.summary or {}).get("outcome")
        if outcome == "resolved":
            resolved.append(r)
        if outcome in ("partial", "failed") or r.status == "failed":
            unresolved.append(r)
    return [
        {"name": "Running", "runs": running, "count": len(running), "default_open": True},
        {"name": "Recent", "runs": runs, "count": len(runs), "default_open": True},
        {"name": "Stopped", "runs": stopped, "count": len(stopped), "default_open": False},
        {"name": "Resolved", "runs": resolved, "count": len(resolved), "default_open": False},
        {"name": "Unresolved", "runs": unresolved, "count": len(unresolved), "default_open": False},
    ]


@app.route("/assistant")
def assistant_page() -> str:
    runs = db.list_assistant_runs(limit=50)
    folders = _bucket_runs(runs)

    selected = None
    timeline: list = []
    decision_json: dict[str, str] = {}
    unlinked: list = []
    pending_controls: list = []
    trigger = None
    model_names: dict[str, str] = {}
    dash = None
    verdict = None
    # Runs are addressed by uuid via ?id= (consistent with /chat, /cron).
    run_arg = request.args.get("id")
    if run_arg:
        try:
            selected = db.get_assistant_run(UUID(run_arg))
        except ValueError:
            selected = None
    if selected is not None:
        steps = db.list_assistant_steps(selected.uuid)
        intents = db.list_write_intents_for_run(selected.uuid)
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
            for s in steps if s.phase != "control"
        }
        pending_controls = db.list_pending_controls(selected.uuid)
        trigger = db.get_run_trigger_message(selected)
        dash = _run_dashboard(selected, steps)
        # The full final reply (the run stores only a truncated final_summary).
        verdict = db.get_run_final_reply(selected) or selected.final_summary
        # Resolve each step's model uuid to a display name for the timeline link.
        for muid in {s.model_uuid for s in steps if s.model_uuid}:
            mc = db.get_model_config(muid)
            if mc is not None:
                model_names[str(muid)] = mc.display_name or mc.model_name

    duration = _format_duration(
        selected.started_at, selected.finished_at) if selected else None

    return render_template_string(
        ASSISTANT_TEMPLATE,
        runs=runs, folders=folders, selected=selected, trigger=trigger,
        timeline=timeline, decision_json=decision_json,
        action_descriptions=_ACTION_DESCRIPTIONS, unlinked=unlinked,
        pending_controls=pending_controls,
        duration=duration, model_names=model_names, dash=dash, verdict=verdict,
        icon_open=_ICON_FOLDER_OPEN, icon_closed=_ICON_FOLDER,
    )
