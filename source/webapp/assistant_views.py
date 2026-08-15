"""The /assistant page — a run-centric inspector over the assistant trace.

Split layout (mirrors /memory's facet tree): the left pane groups recent
`AssistantRun`s into **virtual status folders** (Recent / Running / Stopped /
Resolved / Unresolved — computed each load, not editable), the right pane shows
the selected run's summary, details, and `AssistantStep` timeline with each
`AssistantWriteIntent` inline (joined by `step_uuid`). Read-only except the
lifecycle actions the existing endpoints already own — confirm / reject / undo a
write-intent, and stop / redirect a live run (`webapp/chat_api.py`). The selected
run carries a kebab (Copy run id / Copy journal id / Stop). See
notes/ui-left-panel-tree.md.
"""

import json
from datetime import datetime, timedelta
from uuid import UUID

from flask import Response, render_template_string, request

import db
from agents.assistant import CAPABILITIES, problem_texts
from .core import app

# action value -> short human-readable summary, for the timeline's "action
# call" section (the verbose `description` is LLM-facing). Static (the capability
# registry is defined in code), so resolve once at import.
_ACTION_DESCRIPTIONS = {
    n.value: (c.summary or c.description) for n, c in CAPABILITIES.items()
}
# Code-driven trace rows are not model-selectable capabilities, so they do not
# belong in CAPABILITIES. Give them the same compact timeline description via
# this companion registry.
_ACTION_DESCRIPTIONS.update({
    "response_language_classifier": (
        "determine which language(s) the reply should use"
    ),
    "reply_audit": "check the finished reply before it is sent",
    "request_summary": (
        "describe a request too long to fit in the prompt whole"
    ),
})
# Consulted first for a `code_driven` row. `acceptance_criteria` is the one
# action that is both: the catalog summary describes the revision the model can
# request, which is not what the loop's own call does.
_CODE_DRIVEN_DESCRIPTIONS = {
    "acceptance_criteria": "establish what a good reply must satisfy",
}

ASSISTANT_TEMPLATE = """
<!doctype html>
<title>Assistant run &mdash; rainbox</title>
{# The right-aligned meta line on an io-label. The fields come from the
   builders in this module (_response_meta and friends) — the same ones the
   markdown export renders through _meta_md — so this macro decides only how a
   field looks, never which fields there are. #}
{% macro io_meta(fields) %}
{%- if fields %}<span class="io-meta">
  {%- for f in fields %}
    {%- if f.href %}<a class="{{ f.cls }}" href="{{ f.href }}" title="{{ f.title }}">{{ f.html or f.text }}</a>
    {%- else %}<span class="{{ f.cls }}" title="{{ f.title }}">{{ f.html or f.text }}</span>{% endif %}
  {%- endfor %}
</span>{% endif %}
{%- endmacro %}
{# One LLM exchange of a step: what was sent, what the model thought, what it
   answered. Built by _exchanges(), which returns one of these per ATTEMPT —
   so a call that was refused and asked again renders as two, identical in
   shape, and no attempt is a special case that shows less than the others.
   Mirrored in Python by _exchange_md(); keep the two aligned. #}
{% macro llm_exchange(x) %}
  {% if x.system_prompt or x.user_prompt or x.turns %}
  <div class="io io-req">
    <div class="io-label">{{ x.request_label }}{{ io_meta(x.request_meta) }}</div>
    {% if x.system_prompt %}
    <details class="prompt" data-k="{{ x.key_prefix }}system">
      <summary>system prompt ({{ x.system_prompt | length }} chars)</summary>
      <pre>{{ x.system_prompt }}</pre>
    </details>
    {% endif %}
    {% if x.user_prompt %}
    <details class="prompt" data-k="{{ x.key_prefix }}user">
      <summary>user prompt ({{ x.user_prompt | length }} chars)</summary>
      <pre>{{ x.user_prompt }}</pre>
    </details>
    {% endif %}
    {# What a retry received on top of the shared prompt above: the refused
       answer replayed as the model's own turn, and the reason it was refused
       as the turn after it. #}
    {% for t in x.turns %}
    <details class="prompt" data-k="{{ x.key_prefix }}turn{{ loop.index }}">
      <summary>{{ t.role }} turn ({{ t.content | length }} chars)</summary>
      <pre>{{ t.content }}</pre>
    </details>
    {% endfor %}
  </div>
  {% endif %}
  {% if x.reasoning %}
  <div class="io io-think">
    {# The model's native reasoning channel (a reasoning model's thinking
       before it emitted the structured decision); absent for non-reasoning
       models. Collapsed like the request prompts. #}
    <div class="io-label">model reasoning</div>
    <details class="prompt" data-k="{{ x.key_prefix }}reasoning">
      <summary>reasoning ({{ x.reasoning | length }} chars)</summary>
      <pre>{{ x.reasoning }}</pre>
    </details>
  </div>
  {% endif %}
  {% if x.response_text or x.error %}
  <div class="io io-out{% if x.rejected %} io-rejected{% endif %}">
    <div class="io-label">{{ x.response_label }}{{ io_meta(x.response_meta) }}</div>
    {% if x.error %}<div class="err">{{ x.error }}</div>{% endif %}
    {% if x.response_text %}<pre>{{ x.response_text }}</pre>{% endif %}
  </div>
  {% endif %}
{% endmacro %}
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
  /* An over-long triggering message is collapsed to its opening lines: a
     pasted document or log otherwise pushes the step timeline — the reason
     this page exists — below the fold. The peek sits in the <summary>, so the
     whole closed card is one click target; open, the summary is just the
     toggle. */
  .as-main .trigwrap > summary { cursor:pointer; list-style:none;
                             -webkit-user-select:none; user-select:none; }
  .as-main .trigwrap > summary::-webkit-details-marker { display:none; }
  .as-main .trigwrap[open] > summary .peek { display:none; }
  .as-main .trigwrap .peek { -webkit-mask-image:linear-gradient(#000 70%, transparent);
                             mask-image:linear-gradient(#000 70%, transparent); }
  .as-main .trigtoggle { display:inline-block; margin-top:0.35rem; padding:0;
                             border:0; background:none; font:inherit;
                             font-size:0.72rem; color:#3b6fd4; cursor:pointer; }
  .as-main .trigtoggle:hover { text-decoration:underline; }
  .as-main .trigwrap > summary .trigtoggle::before { content:attr(data-more); }
  .as-main .trigwrap[open] > summary .trigtoggle::before { content:attr(data-less); }
  .as-main .trigwrap .trigless { display:none; }
  .as-main .trigwrap[open] .trigless { display:inline-block; }
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
  /* The chip carries the run's outcome (same reading as the dashboard's
     headline status), not its lifecycle status: "finished" only means the loop
     terminated, so colouring it green would sell an unresolved run as a win. */
  .as-main .card .hd .out-resolved { color:#1e7e34; }
  .as-main .card .hd .out-unresolved { color:#c0392b; }
  .as-main .card .hd .out-running { color:#1d4ed8; }
  .as-main .card .hd .out-pending { color:#98a2b3; }
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
  /* Model-call waterfall: name | track | duration. Bars are positioned on the
     run's wall-clock span, so a wide gap between two bars is time no model was
     working. Colours match the step headers: purple for the calls the loop
     drove, blue for the model's own decide calls. */
  .as-main .wf { display:flex; flex-direction:column; gap:2px; }
  .as-main .wf-row { display:grid; grid-template-columns:14rem 1fr 4rem; gap:0.8rem;
                     align-items:center; text-decoration:none; color:inherit;
                     padding:2px 4px; border-radius:4px; }
  .as-main .wf-row:hover { background:#f3f4f6; }
  .as-main .wf-name { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                      font-size:0.76rem; white-space:nowrap; overflow:hidden;
                      text-overflow:ellipsis; }
  .as-main .wf-track { position:relative; height:0.85rem; background:#f1f3f5;
                       border-radius:3px; overflow:hidden; }
  .as-main .wf-bar { position:absolute; top:0; bottom:0; border-radius:3px;
                     background:#93b4f5; }
  .as-main .wf-bar.kind-code-driven, .as-main .wf-bar.kind-inner { background:#d8b4fe; }
  .as-main .wf-bar.kind-review { background:#fcd34d; }
  .as-main .wf-name.kind-code-driven, .as-main .wf-name.kind-inner { color:#7e22ce; }
  .as-main .wf-name.kind-review { color:#b06f00; }
  /* The embedder — a different model on the same runtime, so a different
     colour from any of the assistant's own calls. Its bars are what most of
     the gaps between the others turn out to be. */
  .as-main .wf-bar.kind-embedding { background:#5eead4; }
  .as-main .wf-name.kind-embedding { color:#0f766e; }
  /* A call whose response was thrown away and asked for again. Same red as
     the error text: the run paid for it and got nothing back. */
  .as-main .wf-bar.kind-rejected { background:#fca5a5; }
  .as-main .wf-name.kind-rejected { color:#c0392b; }
  .as-main .wf-undated { position:absolute; left:4px; font-size:0.68rem; color:#98a2b3; }
  .as-main .wf-secs { font-size:0.76rem; color:#667085; text-align:right;
                      font-variant-numeric:tabular-nums; }
  .as-main .card .hd .outcome.muted { color:#667085; font-weight:400; font-size:0.85rem; }
  /* Rows the loop produced itself (warm-up / follow-up calls). Purple like the
     control badge, which marks the other kind of row the model didn't decide;
     the tinted header keeps the real ReAct steps scannable between them. */
  .as-main .step.aux .hd { background:#faf5ff; }
  .as-main .step .hd .kind { color:#7e22ce; font-weight:600; }
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
  .as-main .step .io-think, .as-main .step .io-so { margin-top:1.4rem; }
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
  /* Phase timing: where one action's own duration went. Names read left, the
     numbers stay right-aligned with the counts table above them. */
  .as-main .step .io-timing td.io-timing-name { text-align:left;
     font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
     font-size:0.76rem; }
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
     request. Entries are {label, text, uuid?, href?} rows. Its summary
     shares the .prompt > summary styling below (one rule, no drift). */
  .as-main .step .steplog { margin:0 0 0.3rem; }
  .as-main .step .steplog-body { margin:0.2rem 0 0 0.4rem; font-size:0.78rem; }
  .as-main .step .steplog-entry { padding:1px 0; }
  .as-main .step .steplog-entry .k { color:#6b7280; font-weight:600;
                             margin-right:0.35rem; }
  .as-main .step .steplog-entry .u { color:#98a2b3; font-size:0.7rem;
                             margin-left:0.35rem; }
  /* "model request" sub-parts: system and user prompt, each collapsed in a
     <details>. The summaries mirror .io-label but a notch smaller. */
  .as-main .step .prompt { margin:0.25rem 0 0; }
  .as-main .step .prompt > summary,
  .as-main .step .steplog > summary { font-size:0.64rem; text-transform:uppercase;
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
          <div class="dlabel" title="Every model call the run made, including the ones with no step row of their own (the second opinion, the criteria revision, the memory recall filter)">LLM calls</div>
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

      {% if waterfall %}
      {# Where the run's wall-clock went. Each bar is one model call, placed on
         the run's span and scaled by its duration — so the gaps between bars
         are the time no model was working, which is the half a per-step
         duration figure cannot show. #}
      <div class="card">
        <div class="hd">
          <div class="card-title">Model calls</div>
          <span class="outcome muted">{{ dash.llm_calls }} calls · model {{ dash.model_time }}{% if dash.embed_calls %} · {{ dash.embed_calls }} embed {{ dash.embed_time }}{% endif %} · total {{ dash.total_time }}</span>
        </div>
        <div class="card-body">
          <div class="wf">
            {% for c in waterfall %}
            <a class="wf-row" href="#step-{{ c.anchor }}" title="{{ c.label }} — {{ c.seconds }}{% if c.start %} at {{ c.start.strftime('%H:%M:%S') }}{% endif %}">
              <span class="wf-name kind-{{ c.kind }}">{{ c.label }}</span>
              <span class="wf-track">
                {% if c.width_pct is not none %}
                <span class="wf-bar kind-{{ c.kind }}" style="left:{{ c.offset_pct }}%;width:{{ c.width_pct }}%"></span>
                {% else %}
                <span class="wf-undated">not timed</span>
                {% endif %}
              </span>
              <span class="wf-secs">{{ c.seconds }}</span>
            </a>
            {% endfor %}
          </div>
        </div>
      </div>
      {% endif %}

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
            {% if trigger and trigger.peek %}
              {# Collapsed to its opening lines. A <details> rather than a
                 hand-rolled toggle so the live-refresh swap carries the open
                 state like every other collapsed block on this page (see
                 detailsKey below) — the peek lives in the <summary> because a
                 closed <details> hides everything else. #}
              <details class="trigwrap" data-k="trigger">
                <summary>
                  <pre class="trigmsg peek">{{ trigger.peek.head }}</pre>
                  <span class="trigtoggle" data-more="{{ trigger.peek.more }}"
                        data-less="show less"></span>
                </summary>
                <pre class="trigmsg">{{ trigger.text }}</pre>
                {# A second way out at the far end: scrolling back up past a
                   long message to collapse it is the whole complaint again. #}
                <button class="trigtoggle trigless" type="button"
                        onclick="this.closest('details').open=false">show less</button>
              </details>
            {% elif trigger %}
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
      {% set kind = step_kinds.get(step.uuid|string) %}
      <div class="step phase-{{ step.phase }}{% if kind %} aux{% endif %}" id="step-{{ step.uuid }}">
        <div class="hd">
          {# Numbered by position in the timeline, not by `step_index`: the
             code-driven rows deliberately share the decide index they sit
             beside, so numbering by it repeated "Step 1 of 4" three times. The
             decide-loop index stays in the tooltip. #}
          <a class="ix step-anchor" href="#step-{{ step.uuid }}" title="Link to this step (decide-loop step index={{ step.step_index }})">Step {{ loop.index }} of {{ timeline|length }}</a>
          {% if step.phase == 'skipped' %}<span><span class="badge b-skipped" title="The loop could not make this call at all — nothing ran, and nothing failed">skipped</span></span>{% endif %}
          {% if kind %}<span class="kind" title="Not a decide step: the loop issued this call itself {{ 'before the first decide step' if kind == 'warm-up' else 'in reaction to what the model decided' }}, so the model never chose it and it consumes none of the step budget">{{ kind }}</span>{% endif %}
          <span class="action" title="{% if kind %}The call the loop made at this point{% else %}The action the model decided to take for this step{% endif %}">{{ step.action or '—' }}</span>
          {% set desc = step.action and ((code_driven_descriptions.get(step.action) if step.code_driven else none) or action_descriptions.get(step.action)) %}
          {% if desc %}<span class="action-desc">{{ desc }}</span>{% endif %}
        </div>
        <div class="step-body">
        {% if step.phase == 'control' %}
          {% if step.reason %}<div class="reason">{{ step.reason }}</div>{% endif %}
        {% else %}
        {% if step.log %}
        <details class="steplog" data-k="log">
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
        {# Every attempt this step's call made, oldest first: the ones whose
           answer was thrown away, then the one that was kept. All render
           through one macro, so a rejected attempt shows its request,
           thinking and answer exactly like the attempt that replaced it. #}
        {% for x in exchanges.get(step.uuid|string, []) %}{{ llm_exchange(x) }}{% endfor %}
        {% set so = second_opinion.get(step.uuid|string) %}
        {% if so %}
        <div class="io io-so">
          {# The independent pre-execution review of a gated action (currently
             python_run). Chronologically it ran between the model response and
             the action executing, so it renders before the action call; its
             payload is stripped from the action-result data below. #}
          <div class="io-label">second opinion{% if 'approved' in so %}<span class="fn-ok {{ 'ok-true' if so.approved else 'ok-false' }}" title="The reviewer's verdict: false means the action was blocked and never executed">approved: {{ 'true' if so.approved else 'false' }}</span>{% endif %}{{ io_meta(review_meta(so, model_names)) }}</div>
          {% if so.system_prompt %}
          <details class="prompt" data-k="so-system">
            <summary>system prompt ({{ so.system_prompt | length }} chars)</summary>
            <pre>{{ so.system_prompt }}</pre>
          </details>
          {% endif %}
          {% if so.user_prompt %}
          <details class="prompt" data-k="so-user">
            <summary>user prompt ({{ so.user_prompt | length }} chars)</summary>
            <pre>{{ so.user_prompt }}</pre>
          </details>
          {% endif %}
          {% if so.reasoning %}
          <details class="prompt" data-k="so-reasoning">
            <summary>reasoning ({{ so.reasoning | length }} chars)</summary>
            <pre>{{ so.reasoning }}</pre>
          </details>
          {% endif %}
          {% if so.response %}<pre title="The reviewer model's verbatim response">{{ so.response }}</pre>{% endif %}
          {% if so.problems_text %}<pre>{{ so.problems_text }}</pre>{% endif %}
          {% if so.skipped %}<pre>review skipped: {{ so.skipped }}</pre>{% endif %}
          {% if so.error %}<pre>review failed open: {{ so.error }}</pre>{% endif %}
        </div>
        {% endif %}
        {# No action call on a code-driven row: nothing was dispatched from a
           decision, and the empty args made the block read as one that was. #}
        {% if step.action and not step.code_driven %}
        <div class="io io-call">
          <div class="io-label">action call{{ io_meta(call_meta(step)) }}</div>
          {% if step.args %}<pre>{{ step.args | tojson }}</pre>{% endif %}
        </div>
        {% endif %}
        {% endif %}
        {% set rf = recall_filter.get(step.uuid|string) %}
        {% if rf %}
        <div class="io io-so">
          {# The memory_query recall filter's own model call. It runs INSIDE
             the action, after the action call and before the result, on the
             query_filter_router's model group rather than the assistant's —
             so it is a second model, mid-decide-loop, that the run pays for
             and that has no step row of its own. #}
          <div class="io-label">recall filter{{ io_meta(recall_filter_meta(rf)) }}</div>
          {% if rf.system_prompt %}
          <details class="prompt" data-k="rf-system">
            <summary>system prompt ({{ rf.system_prompt | length }} chars)</summary>
            <pre>{{ rf.system_prompt }}</pre>
          </details>
          {% endif %}
          {% if rf.user_prompt %}
          <details class="prompt" data-k="rf-user">
            <summary>user prompt ({{ rf.user_prompt | length }} chars)</summary>
            <pre>{{ rf.user_prompt }}</pre>
          </details>
          {% endif %}
          {% if rf.reasoning %}<pre>{{ rf.reasoning }}</pre>{% endif %}
        </div>
        {% endif %}
        {% set obs = step.observation %}
        {# The model request / second opinion / action call / action result
           io-blocks are mirrored in Python by _step_md(); keep them aligned.
           The result is dropped when it only repeats the model response above
           (a code-driven call's response IS its result). #}
        {% if (obs is not none or step.observation_preview)
              and (step.uuid|string) not in duplicate_result %}
        <div class="io io-in">
          <div class="io-label">action result{% if obs is not none %}<span class="fn-ok {{ 'ok-true' if obs.ok else 'ok-false' }}">ok: {{ 'true' if obs.ok else 'false' }}</span>{% endif %}{{ io_meta(result_meta(step)) }}</div>
          {% if obs is not none %}
            {% if obs.text %}<pre>{{ obs.text }}</pre>{% endif %}
            {% set tm = timing.get(step.uuid|string) %}
            {% if tm %}
            {# Where the action's own duration went. The phases are the
               action's parts in the order they finished; the embedder line
               below counts the calls those phases made (already inside their
               durations — the per-call bars are in the waterfall above). #}
            <table class="io-data io-timing"><thead><tr>
              <th title="A named part of this action">phase</th>
              <th title="Wall-clock spent in this phase">took</th>
              <th title="When this phase started">at</th>
            </tr></thead><tbody>
              {% for r in tm.rows %}
              <tr><td class="io-timing-name">{{ r.name }}</td><td>{{ r.took }}</td><td>{{ r.at }}</td></tr>
              {% endfor %}
              {% if tm.embeddings %}
              <tr><td class="io-timing-name" title="Embedding calls made inside the phases above — a second model on the same runtime, which is what a warm cache for the assistant's own model competes with">embedder</td><td colspan="2">{{ tm.embeddings }}</td></tr>
              {% endif %}
            </tbody></table>
            {% endif %}
            {% set odata = obs_data.get(step.uuid|string) %}
            {% if odata %}
              {% if 'qa_static' in odata %}
              <table class="io-data"><thead><tr>
                <th title="number of QA static items">QA static</th>
                <th title="number of QA dynamic items">QA dynamic</th>
                <th title="number of memory items">memory</th>
                <th title="number of facts shortened because they exceeded the 1200-char per-fact cap (tagged truncate1200); read one in full via memory_query with its uuid">truncated</th>
                <th title="number of lower-ranked facts dropped because the whole block exceeded the 11000-char budget; narrow the query or fetch a fact by its uuid">omitted</th>
              </tr></thead><tbody><tr>
                <td>{{ odata.qa_static }}</td>
                <td>{{ odata.qa_dynamic }}</td>
                <td>{{ odata.memory }}</td>
                <td>{{ odata.truncated }}</td>
                <td>{{ odata.omitted }}</td>
              </tr></tbody></table>
              {% else %}<pre>{{ odata | tojson }}</pre>{% endif %}
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
          {# The in-flight call has no row yet, so it takes the position right
             after the last one — the number the timeline will give it. #}
          <span class="ix" title="{% if active_call.step_index is not none %}decide-loop step index={{ active_call.step_index }}{% endif %}">Step {{ timeline|length + 1 }}</span>
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
    // Which collapsed block is which, across a swap. Keyed by the step it
    // belongs to plus its `data-k` role, NOT by position: a live step grows
    // blocks as it runs (its reasoning, then its second opinion), so an index
    // would slide under the reader and reopen the wrong one.
    function detailsKey(d) {
      var step = d.closest('.step');
      return (step ? step.id : '') + '/' + d.getAttribute('data-k');
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
          cur.innerHTML = next.innerHTML;
          Array.prototype.forEach.call(
            cur.querySelectorAll('details[data-k]'),
            function (d) { if (open[detailsKey(d)]) d.open = true; });
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


def _waterfall(calls: list[dict], run) -> list[dict]:
    """Lay the calls out over the run's wall-clock span as percentages, so the
    page draws where each call sat and — by the gaps between bars — where the
    time went that no model was working."""
    starts = [c["start"] for c in calls if c["start"]]
    if not starts:
        return []
    first = min([run.started_at] + starts) if run.started_at else min(starts)
    ends = [c["start"] + timedelta(milliseconds=c["duration_ms"] or 0)
            for c in calls if c["start"]]
    last = max(ends + ([run.finished_at] if run.finished_at else []))
    span = (last - first).total_seconds()
    if span <= 0:
        return []
    rows = []
    for c in calls:
        row = dict(c)
        if c["start"]:
            offset = (c["start"] - first).total_seconds()
            width = (c["duration_ms"] or 0) / 1000
            row["offset_pct"] = round(offset / span * 100, 3)
            # A floor so a sub-second call against a long run stays visible.
            row["width_pct"] = round(max(width / span * 100, 0.6), 3)
        else:
            row["offset_pct"] = None
            row["width_pct"] = None
        row["seconds"] = (f"{c['duration_ms'] / 1000:.1f}s"
                          if c["duration_ms"] is not None else "—")
        rows.append(row)
    return rows


def _run_dashboard(run, steps: list, reviews: list | None = None) -> dict:
    """Aggregate metrics for the top-of-detail mini dashboard.

    Cost comes from `db.assistant_run_stats` — every model call the run made,
    including the ones with no step row of their own. Counting rows instead left
    the second opinion, the criteria revision's inner call and the recall
    filter's scorer out of tokens and model time, which then reappeared as
    unexplained "action" time. The in-chat progress row reads the same helper,
    so the two cannot quote different figures for one run."""
    label, cls = _dash_status(run)
    stats = db.assistant_run_stats(steps, reviews)
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


# --- io-meta ------------------------------------------------------------------
#
# The small right-aligned line on an io-label (model · tokens · throughput ·
# duration · timestamp). Every io block has one, and both renderers draw it: the
# page via the `io_meta` macro, the markdown export via _meta_md(). Each builder
# below returns the line's fields ONCE, so a change to what a field says, how a
# number is formatted, or which order they appear in is made in exactly one
# place. Renderers decide presentation only — never which fields exist.


def _field(text: str, title: str, *, html: str | None = None,
           href: str | None = None, cls: str = "") -> dict:
    """One io-meta field. `text` is the value both renderers show; `html`
    overrides it on the page when the link text differs from the exported text;
    `title` is the page's hover explanation."""
    return {"text": text, "title": title, "html": html, "href": href, "cls": cls}


def _model_field(model_uuid, model_names: dict[str, str], title: str) -> dict:
    """The link to the model that answered. The page shows a compact "model ↗"
    with the name on hover; the export has no hover, so it prints the name."""
    name = model_names.get(str(model_uuid), str(model_uuid)[:8])
    return _field(name, f"{title}: {name}", html="model ↗",
                  href=f"/model?id={model_uuid}", cls="io-model")


def _time_field(dt, title: str) -> list[dict]:
    when = _hms(dt)
    if not when:
        return []
    return [_field(when, f"{title}: {dt.replace(microsecond=0).isoformat()}",
                   cls="io-time")]


# How much of the triggering message the card shows before the reader asks for
# the rest. Two limits because one is not enough: a pasted log is many short
# lines, and a pasted document can be one line thousands of characters long —
# clamping only by line count leaves the second one filling the screen.
TRIGGER_PEEK_LINES: int = 12
TRIGGER_PEEK_CHARS: int = 900


def _trigger_peek(text: str) -> dict | None:
    """The opening of an over-long triggering message, or None when the whole
    message is short enough to show.

    The card led with the operator's message in full, which was fine until the
    assistant started accepting pasted documents and logs: a 20 000-character
    trigger pushed the entire step timeline — the reason the page exists —
    below the fold. Returning None for a short message keeps the common card
    exactly as it was, with no toggle to ignore."""
    lines = text.splitlines()
    if len(lines) <= TRIGGER_PEEK_LINES and len(text) <= TRIGGER_PEEK_CHARS:
        return None
    head = "\n".join(lines[:TRIGGER_PEEK_LINES])
    if len(head) > TRIGGER_PEEK_CHARS:
        head = head[:TRIGGER_PEEK_CHARS]
    return {
        "head": head + " …",
        # What the reader gives up by leaving it collapsed, in the units they
        # can see: a bare "show more" cannot tell 3 held-back lines from 3000.
        "more": f"show all {len(lines):,} lines ({len(text):,} characters)",
    }


def _with_trigger_peek(trigger: dict | None) -> dict | None:
    """Attach the card's peek to the trigger dict, leaving db's shape alone —
    how much of a message fits on a page is a rendering question."""
    if trigger is None:
        return None
    peek = _trigger_peek(str(trigger.get("text") or ""))
    return trigger if peek is None else {**trigger, "peek": peek}


def _usage_fields(
    input_tokens: int | None, output_tokens: int | None, duration_ms: int | None
) -> list[dict]:
    """What one model call cost, as the meta fields every such line shares.

    One renderer because a model call costs the same thing wherever it ran: a
    line that showed only a duration read as a call that was free, which is
    exactly how the reply audit's cost went unnoticed."""
    fields: list[dict] = []
    if input_tokens is not None or output_tokens is not None:
        tokens = (input_tokens or 0) + (output_tokens or 0)
        fields.append(_field(
            f"in {input_tokens or 0}",
            "Input tokens: the size of the prompt sent to the model for this step"))
        fields.append(_field(
            f"out {output_tokens or 0}",
            "Output tokens: the amount of text the model generated for this step"))
        if duration_ms:
            fields.append(_field(
                f"{tokens * 1000 / duration_ms:.0f} tok/s",
                "Throughput: total tokens (input + output) processed per second"))
    if duration_ms is not None:
        fields.append(_field(
            f"took {duration_ms / 1000:.1f}s",
            "Duration: how long the model took to produce this response",
            cls="io-dur"))
    return fields


def _response_meta(step, model_names: dict[str, str]) -> list[dict]:
    """The model-response line: which model, what the call cost, how fast."""
    fields: list[dict] = []
    if step.model_uuid:
        fields.append(_model_field(
            step.model_uuid, model_names, "The model that produced this response"))
    fields += _usage_fields(
        step.input_tokens, step.output_tokens, step.duration_ms)
    return fields + _time_field(
        step.created_at, "When this model response was recorded")


def _exchanges(step, model_names: dict[str, str], decision_text: str) -> list[dict]:
    """One step's LLM exchanges, oldest first: every attempt its call made.

    A retried call is several exchanges, not one exchange plus a footnote. A
    rejected attempt is an LLM invocation like any other — it was sent a
    request, it thought, it answered — so it renders through the same shape as
    the kept one, and prompts, reasoning and response appear for all of them or
    for none.

    Every attempt shares the step's system and user prompt; a retry differs
    only by the turns appended after them, which is what `turns` carries (the
    feedback from each earlier rejection, accumulated). The rejected attempts
    store no prompt of their own for exactly this reason.
    """
    exchanges: list[dict] = []
    attempts = list(step.rejected_attempts or [])
    turns: list[dict] = []
    for index, attempt in enumerate(attempts, start=1):
        exchanges.append({
            "key_prefix": f"attempt{index}-",
            "rejected": True,
            "request_label": f"model request (attempt {index})",
            "request_meta": [_field(
                _iso_hms(attempt.get("requested_at")),
                "When this attempt was sent", cls="io-time")]
            if attempt.get("requested_at") else [],
            "system_prompt": step.system_prompt,
            "user_prompt": step.user_prompt,
            "turns": list(turns),
            "reasoning": attempt.get("reasoning"),
            "response_label": f"rejected response {index} of {len(attempts)}",
            "response_meta": _rejected_meta(attempt, model_names),
            "response_text": attempt.get("response"),
            "error": attempt.get("error"),
        })
        turns = turns + list(attempt.get("feedback") or [])
    # The attempt the step itself records — the one whose answer was kept,
    # which on a call that never retried is the only one there was.
    if step.system_prompt or step.user_prompt or decision_text or step.model_response:
        exchanges.append({
            "key_prefix": "",
            "rejected": False,
            # Only worth numbering when there is something to number it
            # against; a call that got it right first time reads as before.
            "request_label": ("model request"
                              if not attempts
                              else f"model request (attempt {len(attempts) + 1})"),
            # A retry went out when the attempt before it was refused, not
            # when the call started — `requested_at` is the first attempt's
            # (the same reading the waterfall places these bars by).
            "request_meta": (
                _request_meta(step) if not attempts else
                [_field(_hms(db.retry_resumed_at(step)) or "—",
                        "When this attempt was sent", cls="io-time")]),
            "system_prompt": step.system_prompt,
            "user_prompt": step.user_prompt,
            "turns": turns,
            "reasoning": step.reasoning,
            # "partial" means the call died mid-stream and this is as far as
            # it got — a row that never produced a decision AND recorded an
            # error. A code-driven row that succeeded holds its complete
            # response, so it stays "model response".
            "response_label": (
                "partial model response"
                if step.model_response and not decision_text and step.error
                else "model response"),
            "response_meta": _response_meta(step, model_names),
            "response_text": decision_text or step.model_response,
            "error": None,
        })
    return [x for x in exchanges
            if x["system_prompt"] or x["user_prompt"] or x["turns"]
            or x["reasoning"] or x["response_text"] or x["error"]]


def _rejected_meta(attempt: dict, model_names: dict[str, str]) -> list[dict]:
    """The rejected-response line: which model wrote it, what it cost, when.

    The same fields as the accepted response beside it, because the question
    the operator is asking is the same one — where did the time go — and this
    attempt spent as much of it as the one that worked."""
    fields: list[dict] = []
    if attempt.get("model_uuid"):
        fields.append(_model_field(
            attempt["model_uuid"], model_names,
            "The model that produced this rejected response"))
    fields += _usage_fields(
        attempt.get("input_tokens"), attempt.get("output_tokens"),
        attempt.get("ms"))
    if attempt.get("requested_at"):
        fields.append(_field(
            _iso_hms(attempt["requested_at"]),
            "When this attempt was sent", cls="io-time"))
    return fields


def _request_meta(step) -> list[dict]:
    return _time_field(step.requested_at, "When this model request was made")


def _call_meta(step) -> list[dict]:
    return _time_field(step.created_at, "When this action was called")


def _result_meta(step) -> list[dict]:
    """The action-result line. Its duration is the action's own — wall-clock
    from the call to the observation — not the model call's."""
    if not step.settled_at:
        return []
    fields: list[dict] = []
    if step.created_at:
        elapsed = (step.settled_at - step.created_at).total_seconds()
        fields.append(_field(
            f"took {elapsed:.1f}s",
            "Duration: how long the action took to complete", cls="io-dur"))
    return fields + _time_field(
        step.settled_at, "When this action result was recorded")


def _review_meta(so: dict, model_names: dict[str, str]) -> list[dict]:
    """The second-opinion line: the reviewer model, where its group came from,
    and what the review cost — the review is a model call like any other, and
    it is the one the operator is most likely to be weighing against the step
    it gates."""
    fields: list[dict] = []
    if so.get("model_uuid"):
        fields.append(_model_field(
            so["model_uuid"], model_names, "The reviewing model"))
    if so.get("group_from"):
        fields.append(_field(
            f"group: {so['group_from']}",
            "Which agent binding supplied the reviewer's model group "
            "(second_opinion on /agentmodel, else the assistant's own)"))
    usage = so.get("usage") or {}
    return fields + _usage_fields(
        usage.get("input"), usage.get("output"), usage.get("ms"))


def _recall_filter_meta(rf: dict) -> list[dict]:
    """The recall-filter line: the scorer model, where its group came from, and
    what the call cost. The model matters more here than on the other blocks —
    this one runs on the query_filter_router's binding, so it is usually a
    different model from the one deciding the step it sits inside."""
    fields: list[dict] = []
    if rf.get("scorer_model"):
        fields.append(_field(
            str(rf["scorer_model"]),
            "The model that scored the recalled candidates", cls="io-model"))
    if rf.get("group_from"):
        fields.append(_field(
            f"group: {rf['group_from']}",
            "Which agent binding supplied the scorer's model group "
            "(query_filter_router on /agentmodel, else the assistant's own)"))
    usage = rf.get("usage") or {}
    return fields + _usage_fields(
        usage.get("input"), usage.get("output"), usage.get("ms"))


def _meta_md(fields: list[dict]) -> str:
    """The export's rendering of an io-meta line: the field values, in order."""
    return " · ".join(f["text"] for f in fields)


def _labelled(label: str, fields: list[dict]) -> str:
    """An export io-label with its meta line appended — the flat-text
    counterpart of the page's label + right-aligned `io_meta` span."""
    meta = _meta_md(fields)
    return f"{label} · {meta}" if meta else label


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


def _review_payload(row) -> dict:
    """A second_opinion_review row in the shape both renderers already expect,
    so the row became the source of truth without either of them changing.
    `approved` is present only for a real verdict — a skipped or failed-open
    review never approved anything, and an `approved: false` badge on one would
    read as a rejection."""
    payload: dict = {
        "problems": list(row.problems or []),
        "group_from": row.group_from,
        "model_uuid": str(row.model_uuid) if row.model_uuid else None,
        "system_prompt": row.system_prompt,
        "user_prompt": row.user_prompt,
        "reasoning": row.reasoning,
        "response": row.response,
        "skipped": row.skip_reason,
        "error": row.error,
        # Same shape the inline payload carries, so `_review_meta` reads one
        # thing whether the review came from its own row or from the step's
        # observation fallback.
        "usage": {"input": row.input_tokens, "output": row.output_tokens,
                  "ms": row.duration_ms},
    }
    if row.verdict in ("approved", "rejected"):
        payload["approved"] = row.verdict == "approved"
    return payload


def _split_second_opinion(step, reviews: dict | None = None) -> tuple[dict | None, dict]:
    """A gated step's observation data points at the second-opinion review, but
    chronologically the review ran BEFORE the action — so both renderers (HTML
    and markdown) show it as its own block above the action call and strip the
    pointer from the action-result data. Returns (review_or_None, remaining_data).

    `reviews` maps review uuid -> row. Steps written before the row existed
    carry the whole payload inline instead of a pointer and are passed through
    unchanged; a pointer with no row (the write failed, or the run was pruned)
    yields None rather than breaking the trace.
    """
    obs = step.observation or {}
    data = dict(obs.get("data") or {})
    so = data.pop("second_opinion", None)
    if isinstance(so, dict) and "review_uuid" in so:
        row = (reviews or {}).get(str(so["review_uuid"]))
        return (_review_payload(row) if row is not None else None), data
    return so, data


def _split_recall_filter(step) -> dict | None:
    """The memory_query recall filter's own model call, lifted out of the
    step's observation data so it renders as its own block.

    It is a second LLM call nested inside one memory_query action, on the
    query_filter_router's model group rather than the assistant's — a real
    cost the run pays with no step row of its own. Left inside the
    action-result data it rendered as an anonymous JSON dump, which is how a
    call on a different model, in the middle of the decide loop, stayed
    invisible. Returns None for a gated/failed filter that never called a
    model; those keep their one-line note in the result data.
    """
    data = (step.observation or {}).get("data") or {}
    rf = data.get("recall_filter")
    if isinstance(rf, dict) and rf.get("mode") == "llm":
        return rf
    return None


def _split_timing(step) -> dict | None:
    """The action's phase timing, lifted out of the observation data so it
    renders as a table instead of a JSON dump inside the result.

    Only memory_query records it today. Its step duration says the action took
    half a minute; the phases say whether that was the vector search, the seed
    KB, or the relevance filter's own LLM call — three different problems with
    three different fixes."""
    data = (step.observation or {}).get("data") or {}
    timing = data.get("timing")
    return timing if isinstance(timing, dict) and timing.get("phases") else None


def _timing_view(timing: dict) -> dict:
    """The timing block as the page and the export both render it: one row per
    phase, plus the embedder's totals underneath.

    The embedding calls are NOT rows here — their time is already inside the
    phase that made them, and a second set of bars adding up to more than the
    action took would read as a contradiction. They are listed individually in
    the run's model-call waterfall, where they sit on the same wall-clock as
    everything else."""
    rows = []
    for p in timing.get("phases") or []:
        ms = p.get("ms")
        rows.append({
            "name": str(p.get("name") or "—"),
            "took": _format_seconds(ms / 1000) if ms is not None else "—",
            "at": _iso_hms(p.get("started_at")),
        })
    embeds = timing.get("embeddings") or {}
    summary = None
    if embeds.get("count"):
        parts = [f"{embeds['count']} call{'s' if embeds['count'] != 1 else ''}",
                 _format_seconds((embeds.get("ms") or 0) / 1000)]
        if embeds.get("chars"):
            parts.append(f"{embeds['chars']} chars")
        parts += [str(m) for m in (embeds.get("models") or [])]
        summary = " · ".join(parts)
    return {"rows": rows, "embeddings": summary}


def _iso_hms(value: str | None) -> str:
    """An ISO timestamp from a JSONB payload as wall-clock HH:MM:SS, in the
    same zone as the other trace times (the payloads store UTC). Unparseable
    or missing renders as an em dash rather than raising — the timing block is
    diagnostics, and diagnostics must not be what breaks the page."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return "—"
    return (parsed.astimezone() if parsed.tzinfo else parsed).strftime("%H:%M:%S")


def _second_opinion_md(so: dict, model_names: dict[str, str]) -> list[str]:
    """The second-opinion block as Markdown: verdict and reviewer on the label,
    the exact prompts the reviewer model was given, then the problems (or why
    the review was skipped / failed open) as bullets."""
    label = "**second opinion**"
    if "approved" in so:
        label += f" · approved: {'true' if so.get('approved') else 'false'}"
    lines = [_labelled(label, _review_meta(so, model_names)), ""]
    if so.get("system_prompt"):
        lines.append("_system prompt_")
        lines.append(_fence(so["system_prompt"]))
        lines.append("")
    if so.get("user_prompt"):
        lines.append("_user prompt_")
        lines.append(_fence(so["user_prompt"]))
        lines.append("")
    if so.get("reasoning"):
        lines.append("_reasoning_")
        lines.append(_fence(so["reasoning"]))
        lines.append("")
    if so.get("response"):
        lines.append("_response_")
        lines.append(_fence(so["response"], "json"))
        lines.append("")
    for text in problem_texts(so.get("problems")):
        lines.append(f"- {text}")
    if so.get("skipped"):
        lines.append(f"- review skipped: {so['skipped']}")
    if so.get("error"):
        lines.append(f"- review failed open: {so['error']}")
    if lines[-1] != "":
        lines.append("")
    return lines


def _exchange_md(exchange: dict, *, fence_json: bool) -> list[str]:
    """One LLM exchange as Markdown — the mirror of the template's
    `llm_exchange` macro (search ASSISTANT_TEMPLATE for it); keep the two
    aligned. Both are fed the same dicts from `_exchanges`, so a rejected
    attempt and the attempt that replaced it cannot drift into different
    shapes in one renderer and not the other."""
    lines: list[str] = []
    if exchange["system_prompt"] or exchange["user_prompt"] or exchange["turns"]:
        lines.append(_labelled(f"**{exchange['request_label']}**",
                               exchange["request_meta"]))
        lines.append("")
        if exchange["system_prompt"]:
            lines.append("_system prompt_")
            lines.append(_fence(exchange["system_prompt"]))
            lines.append("")
        if exchange["user_prompt"]:
            lines.append("_user prompt_")
            lines.append(_fence(exchange["user_prompt"]))
            lines.append("")
        for turn in exchange["turns"]:
            lines.append(f"_{turn.get('role')} turn_")
            lines.append(_fence(str(turn.get("content") or "")))
            lines.append("")
    if exchange["reasoning"]:
        lines.append("**model reasoning**")
        lines.append("")
        lines.append(_fence(exchange["reasoning"]))
        lines.append("")
    if exchange["response_text"] or exchange["error"]:
        lines.append(_labelled(f"**{exchange['response_label']}**",
                               exchange["response_meta"]))
        lines.append("")
        if exchange["error"]:
            lines.append(f"**error:** {exchange['error']}")
            lines.append("")
        if exchange["response_text"]:
            lines.append(_fence(
                exchange["response_text"],
                "json" if fence_json and not exchange["rejected"] else ""))
            lines.append("")
    return lines


def _step_md(step, decision_json: dict[str, str], model_names: dict[str, str],
             reviews: dict | None = None,
             duplicate_result: set[str] | None = None) -> list[str]:
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
    decision = decision_json.get(str(step.uuid), "")
    for exchange in _exchanges(step, model_names, decision):
        lines.extend(_exchange_md(exchange, fence_json=bool(decision)))
    second_opinion, obs_data = _split_second_opinion(step, reviews)
    if second_opinion is not None:
        lines.extend(_second_opinion_md(second_opinion, model_names))
    if step.action and not step.code_driven:
        lines.append(_labelled("**action call**", _call_meta(step)))
        lines.append("")
        if step.args:
            lines.append(_fence(json.dumps(step.args, ensure_ascii=False, indent=2), "json"))
            lines.append("")
    obs = step.observation
    if (obs is not None or step.observation_preview) and (
            str(step.uuid) not in (duplicate_result or set())):
        label = "**action result**"
        if obs is not None:
            label += f" · ok: {'true' if obs.get('ok') else 'false'}"
        lines.append(_labelled(label, _result_meta(step)))
        lines.append("")
        if obs is not None:
            if obs.get("text"):
                lines.append(_fence(obs["text"]))
                lines.append("")
            # Phase timing, same table as the page (see _timing_view), before
            # the counts: it explains the duration on the label just above.
            tm = _split_timing(step)
            if tm is not None:
                view = _timing_view(tm)
                lines.append("| phase | took | at |")
                lines.append("|---|---|---|")
                for r in view["rows"]:
                    lines.append(f"| {r['name']} | {r['took']} | {r['at']} |")
                if view["embeddings"]:
                    lines.append(f"| embedder | {view['embeddings']} | |")
                lines.append("")
            obs_data.pop("timing", None)
            if obs_data:
                data = obs_data
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

    # Model calls. The page draws these as a waterfall; flat text keeps the
    # same reading — start offset from the run's beginning, then duration — so
    # the gaps that show where the time went survive the export.
    if ctx.get("waterfall"):
        out += ["## Model calls", "",
                "| call | kind | at | took |", "|---|---|---|---|"]
        for c in ctx["waterfall"]:
            at = c["start"].strftime("%H:%M:%S") if c["start"] else "—"
            out.append(f"| {c['label']} | {c['kind']} | {at} | {c['seconds']} |")
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
    kinds = ctx.get("step_kinds", {})
    for position, (step, intents) in enumerate(timeline, start=1):
        # Position, not step_index — see the template's numbering note.
        head = f"Step {position} of {n}"
        kind = kinds.get(str(step.uuid))
        if kind:
            head += f" — {kind}"
        if step.phase == "control":
            out.append(f"### {head} — control")
        else:
            desc = (step.code_driven
                    and _CODE_DRIVEN_DESCRIPTIONS.get(step.action or "")
                    ) or _ACTION_DESCRIPTIONS.get(step.action or "")
            title = f"{head} — {step.action or '—'}" + (f" — {desc}" if desc else "")
            out.append(f"### {title}")
        out.append("")
        out += _step_md(step, ctx["decision_json"], ctx["model_names"],
                        ctx["reviews"], ctx.get("duplicate_result", set()))
        for it in intents:
            out += _intent_md(it)

    # Unlinked writes.
    if ctx["unlinked"]:
        out += ["## Unlinked writes", ""]
        for it in ctx["unlinked"]:
            out += _intent_md(it)

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


def _step_kinds(steps) -> dict[str, str]:
    """Label the rows that are not decide steps, by uuid.

    A code-driven row is a call the loop made on its own initiative, and it
    consumes none of the step budget — so the operator needs to see at a glance
    that it isn't part of the ReAct sequence. Which label depends on when it
    ran: the response-language classifier and the acceptance-criteria step 0 go
    out before the first decide call (`warm-up`), while the reply audit and a
    mid-run criteria refresh react to something the model already decided
    (`follow-up`).

    `requested_at`, not row order, decides. The audit's row is written before
    the reply row it audits (the reply lands only once the audit says send), so
    ordering by row would call it a warm-up. Timing, not action name, also means
    a code-driven call added later is labelled right the day it lands. Rows
    predating `requested_at` capture fall back to row order."""
    first_decide = min(
        (s.requested_at for s in steps
         if not s.code_driven and s.phase != "control" and s.requested_at),
        default=None)
    kinds: dict[str, str] = {}
    seen_decide = False
    for s in steps:
        if not s.code_driven:
            if s.phase != "control":
                seen_decide = True
            continue
        started = (s.requested_at > first_decide
                   if s.requested_at and first_decide else seen_decide)
        kinds[str(s.uuid)] = "follow-up" if started else "warm-up"
    return kinds


def _same_payload(response: str | None, result: str | None) -> bool:
    """True when a step's raw model response and its recorded result are the
    same content. A code-driven call's result IS its response — the two differ
    only by the serializer's indentation — and printing it twice under two
    labels reads as two separate things having happened."""
    if not response or not result:
        return False
    try:
        return json.loads(response) == json.loads(result)
    except ValueError:
        return " ".join(response.split()) == " ".join(result.split())


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
    # Skipped for rows with no decision behind them, because rendering their
    # action/reason in decision shape would put words in the model's mouth:
    # control steps are operator events, and code-driven steps are calls the
    # loop issued itself (their real response is on `model_response`).
    decision_json = {
        str(s.uuid): json.dumps(
            {"reason": s.reason, "action": s.action, "args": s.args or {}},
            ensure_ascii=False,
        )
        for s in steps
        if s.phase != "control" and not s.code_driven
        and (s.action is not None or s.reason is not None)
    }
    step_kinds = _step_kinds(steps)
    duplicate_result = {
        str(s.uuid) for s in steps
        if s.observation is None
        and _same_payload(s.model_response, s.observation_preview)
    }
    # The review rows this run's steps point at — one query, like the steps and
    # write-intents above. Keyed by uuid for the pointer lookup; each is split
    # out of its step's observation data so the template renders it in
    # chronological position (before the action call) and the action result
    # shows only the remaining data.
    review_rows = db.list_second_opinion_reviews(selected.uuid)
    reviews = {str(r.uuid): r for r in review_rows}
    second_opinion: dict[str, dict] = {}
    recall_filter: dict[str, dict] = {}
    timing: dict[str, dict] = {}
    obs_data: dict[str, dict] = {}
    for s in steps:
        rf = _split_recall_filter(s)
        if rf is not None:
            recall_filter[str(s.uuid)] = rf
        tm = _split_timing(s)
        if tm is not None:
            timing[str(s.uuid)] = _timing_view(tm)
        so, data = _split_second_opinion(s, reviews)
        if so is not None:
            # problems_text is precomputed because ASSISTANT_TEMPLATE is a
            # non-raw string: a '\n' inside a Jinja expression would be
            # interpreted by Python before Jinja ever parses it.
            so = dict(so)
            so["problems_text"] = "\n".join(
                f"- {t}" for t in problem_texts(so.get("problems")))
            second_opinion[str(s.uuid)] = so
        # Timing renders as its own table under the result, so it is stripped
        # here for the same reason the review and the recall filter are: left
        # in, it reaches the page as a JSON dump nobody reads.
        data.pop("timing", None)
        obs_data[str(s.uuid)] = data
    # The full final reply (the run stores only a truncated final_summary).
    reply = db.get_run_final_reply(selected)
    model_names: dict[str, str] = {}
    reviewer_uuids = {
        UUID(so["model_uuid"])
        for so in second_opinion.values() if so.get("model_uuid")
    }
    for muid in {s.model_uuid for s in steps if s.model_uuid} | reviewer_uuids:
        mc = db.get_model_config(muid)
        if mc is not None:
            model_names[str(muid)] = mc.display_name or mc.model_name
    # Built here, not in the template: the export renders the same dicts, so
    # what an exchange consists of is decided once (see _exchanges).
    exchanges = {
        str(s.uuid): _exchanges(s, model_names, decision_json.get(str(s.uuid), ""))
        for s in steps if s.phase != "control"
    }
    return {
        "timeline": timeline,
        "decision_json": decision_json,
        "exchanges": exchanges,
        "step_kinds": step_kinds,
        "duplicate_result": duplicate_result,
        "second_opinion": second_opinion,
        "obs_data": obs_data,
        "recall_filter": recall_filter,
        "timing": timing,
        "unlinked": unlinked,
        "reviews": reviews,
        "pending_controls": db.list_pending_controls(selected.uuid),
        "trigger": _with_trigger_peek(db.get_run_trigger_message(selected)),
        "dash": _run_dashboard(selected, steps, review_rows),
        "waterfall": _waterfall(db.assistant_llm_calls(steps, review_rows), selected),
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
        exchanges=ctx.get("exchanges", {}),
        step_kinds=ctx.get("step_kinds", {}),
        duplicate_result=ctx.get("duplicate_result", set()),
        second_opinion=ctx.get("second_opinion", {}),
        obs_data=ctx.get("obs_data", {}),
        recall_filter=ctx.get("recall_filter", {}),
        timing=ctx.get("timing", {}),
        action_descriptions=_ACTION_DESCRIPTIONS,
        code_driven_descriptions=_CODE_DRIVEN_DESCRIPTIONS,
        # The io-meta field builders, called from the template so the page and
        # the markdown export read the same definitions.
        response_meta=_response_meta, request_meta=_request_meta,
        call_meta=_call_meta, result_meta=_result_meta,
        review_meta=_review_meta, recall_filter_meta=_recall_filter_meta,
        rejected_meta=_rejected_meta,
        unlinked=ctx.get("unlinked", []),
        pending_controls=ctx.get("pending_controls", []),
        duration=duration, model_names=ctx.get("model_names", {}),
        dash=ctx.get("dash"), waterfall=ctx.get("waterfall", []),
        verdict=ctx.get("verdict"), reply=ctx.get("reply"),
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
