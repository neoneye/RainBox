"""The /assistant page — a run-centric inspector over the assistant trace.

Master-detail: the left pane lists recent `AssistantRun`s; the right pane shows
the selected run's `AssistantStep` timeline with each step's
`AssistantWriteIntent` rendered inline (joined by `step_uuid`). Read-only except
for the lifecycle actions the existing endpoints already own — confirm / reject /
undo a write-intent, and stop / redirect a live run (`webapp/chat_api.py`). The
four models are also in Flask-Admin as flat tables; this page adds the join those
can't show. No field editing (the trace stays trustworthy).
"""

from flask import render_template_string, request
from sqlalchemy import func

import db
from db.models import AssistantStep
from .core import app

ASSISTANT_TEMPLATE = """
<!doctype html>
<title>Assistant runs &mdash; rainbox</title>
{% include "_nav.html" %}
{% macro render_intent(it) %}
  <div class="intent {{ 'proposed' if it.state == 'proposed' }}">
    <span class="cap">{{ it.capability_name }}</span>
    <span class="badge b-{{ it.state }}">{{ it.state }}</span>
    {% if it.preview_text %}<div class="muted">{{ it.preview_text }}</div>{% endif %}
    {% if it.payload %}<pre>{{ it.payload | tojson(indent=2) }}</pre>{% endif %}
    <div class="acts">
      {% if it.state == 'proposed' %}
        <button class="primary" onclick="ppAct('/chat/api/assistant/write-intents/{{ it.uuid }}/confirm')">Confirm</button>
        <button class="danger" onclick="ppAct('/chat/api/assistant/write-intents/{{ it.uuid }}/reject')">Reject</button>
      {% elif it.state == 'completed' and it.result and it.result.get('undo') %}
        <button onclick="ppAct('/chat/api/assistant/write-intents/{{ it.uuid }}/undo')">Undo</button>
      {% endif %}
    </div>
  </div>
{% endmacro %}
<style>
  .pp-as { display: flex; gap: 1rem; max-width: 1200px; margin: 1rem auto;
           padding: 0 1rem; font-family: system-ui, sans-serif; align-items: flex-start; }
  .pp-as h1 { margin: 0.2rem 0 0.6rem; }
  .pp-as .runs { flex: 0 0 270px; }
  .pp-as .detail { flex: 1 1 auto; min-width: 0; }
  .pp-as .run { display: block; text-decoration: none; color: #222;
                border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.5rem 0.65rem;
                margin-bottom: 0.45rem; }
  .pp-as .run:hover { background: #f8fafc; }
  .pp-as .run.active { border-color: #2563eb; background: #eff6ff; }
  .pp-as .run .id { font-weight: 600; }
  .pp-as .run .meta { color: #667085; font-size: 0.82rem; margin-top: 2px; }
  .pp-as .empty { color: #667085; padding: 1rem 0; }
  .pp-as .badge { display: inline-block; padding: 1px 7px; border-radius: 10px;
                  font-size: 0.74rem; font-weight: 600; }
  .b-running,.b-stopping { background:#e0edff; color:#1d4ed8; }
  .b-finished,.b-observed,.b-final,.b-completed,.b-confirmed,.b-executing { background:#e6f4ea; color:#1e7e34; }
  .b-failed,.b-killed { background:#fdecea; color:#c0392b; }
  .b-stopped,.b-rejected,.b-undone,.b-planned { background:#f1f3f5; color:#555; }
  .b-control { background:#f3e8ff; color:#7e22ce; }
  .b-proposed { background:#fff4e5; color:#b06f00; }
  .pp-as .step { border:1px solid #e5e7eb; border-radius:8px; padding:0.55rem 0.7rem;
                 margin-bottom:0.55rem; }
  .pp-as .step.control { background:#faf5ff; border-color:#e9d5ff; }
  .pp-as .step .hd { display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; }
  .pp-as .step .ix { color:#98a2b3; font-variant-numeric: tabular-nums; }
  .pp-as .step .action { font-weight:600; }
  .pp-as .step .reason { color:#475467; margin:0.3rem 0; }
  .pp-as pre { background:#f6f8fa; border:1px solid #e1e4e8; border-radius:6px;
               padding:0.45rem 0.6rem; overflow-x:auto; white-space:pre-wrap;
               margin:0.3rem 0; font-size:0.82rem; }
  .pp-as .err { color:#c0392b; }
  .pp-as .intent { border-left:3px solid #cbd5e1; margin:0.45rem 0 0.2rem 0.4rem;
                   padding:0.4rem 0.6rem; background:#fcfcfd; border-radius:0 6px 6px 0; }
  .pp-as .intent.proposed { border-left-color:#f59e0b; }
  .pp-as .intent .cap { font-weight:600; }
  .pp-as button { font:inherit; padding:0.28rem 0.7rem; cursor:pointer;
                  border:1px solid #ccc; border-radius:6px; background:#fff; color:#222; }
  .pp-as button.primary { background:#2563eb; border-color:#2563eb; color:#fff; }
  .pp-as button.danger { color:#c0392b; border-color:#e7b9b3; }
  .pp-as .acts { margin-top:0.35rem; display:flex; gap:0.4rem; flex-wrap:wrap; }
  .pp-as .runhd { display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap;
                  margin-bottom:0.5rem; }
  .pp-as .pending { background:#fff4e5; color:#92400e; border:1px solid #fde68a;
                    border-radius:6px; padding:0.4rem 0.6rem; margin:0.4rem 0; }
  .pp-as .muted { color:#667085; font-size:0.85rem; }
  .pp-as .grp { font-weight:600; margin:0.8rem 0 0.3rem; }
</style>
<main class="pp-as">
  <div class="runs">
    <h1>Runs</h1>
    {% if not runs %}<div class="empty">No assistant runs yet.</div>{% endif %}
    {% for r in runs %}
    <a class="run {{ 'active' if selected and r.id == selected.id }}"
       href="{{ url_for('assistant_page') }}?run={{ r.id }}">
      <span class="id">#{{ r.id }}</span>
      <span class="badge b-{{ r.status }}">{{ r.status }}</span>
      <div class="meta">
        {{ r.started_at.strftime('%Y-%m-%d %H:%M') if r.started_at else '—' }}
        · {{ counts.get(r.id, 0) }} step{{ '' if counts.get(r.id, 0) == 1 else 's' }}
        · room {{ (r.room_uuid|string)[:8] }}
      </div>
    </a>
    {% endfor %}
  </div>

  <div class="detail">
    {% if not selected %}
      <h1>Timeline</h1>
      <div class="empty">Select a run on the left to see its step timeline.</div>
    {% else %}
      <div class="runhd">
        <h1 style="margin:0">Run #{{ selected.id }}</h1>
        <span class="badge b-{{ selected.status }}">{{ selected.status }}</span>
        <a class="muted" href="{{ url_for('assistant_page') }}?run={{ selected.id }}">Refresh</a>
        {% if selected.status in ('running', 'stopping') %}
          <button class="danger" onclick="ppAct('/chat/api/assistant/runs/{{ selected.id }}/stop')">Stop</button>
          <button onclick="ppRedirect({{ selected.id }})">Redirect…</button>
        {% endif %}
      </div>
      <div class="muted">
        journal {{ (selected.journal_id|string)[:8] if selected.journal_id else '—' }}
        · started {{ selected.started_at.strftime('%Y-%m-%d %H:%M:%S') if selected.started_at else '—' }}
        {% if selected.finished_at %}· finished {{ selected.finished_at.strftime('%H:%M:%S') }}{% endif %}
      </div>
      {% if selected.final_summary %}<pre>{{ selected.final_summary }}</pre>{% endif %}

      {% for c in pending_controls %}
      <div class="pending">⏳ pending {{ c.command }}{% if c.payload and c.payload.get('instruction') %}: {{ c.payload.get('instruction') }}{% endif %}</div>
      {% endfor %}

      {% if not timeline %}<div class="empty">This run has no steps.</div>{% endif %}
      {% for step, intents in timeline %}
      <div class="step {{ 'control' if step.phase == 'control' }}">
        <div class="hd">
          <span class="ix">#{{ step.step_index }}</span>
          <span class="badge b-{{ step.phase }}">{{ step.phase }}</span>
          <span class="action">{{ step.action or '—' }}</span>
          {% if step.model_uuid %}<span class="muted">model {{ (step.model_uuid|string)[:8] }}</span>{% endif %}
        </div>
        {% if step.reason %}<div class="reason">{{ step.reason }}</div>{% endif %}
        {% if step.args %}<pre>{{ step.args | tojson(indent=2) }}</pre>{% endif %}
        {% if step.observation_preview %}<pre>{{ step.observation_preview }}</pre>{% endif %}
        {% if step.error %}<div class="err">{{ step.error }}</div>{% endif %}
        {% for it in intents %}{{ render_intent(it) }}{% endfor %}
      </div>
      {% endfor %}

      {% if unlinked %}
        <div class="grp">Unlinked writes <span class="muted">(no step reference)</span></div>
        {% for it in unlinked %}{{ render_intent(it) }}{% endfor %}
      {% endif %}
    {% endif %}
  </div>
</main>

<script>
  function ppAct(url) {
    fetch(url, {method: 'POST'})
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        if (d && d.ok === false) { alert(d.text || 'Action failed'); return; }
        location.reload();
      })
      .catch(function (e) { alert('Request failed: ' + e); });
  }
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


@app.route("/assistant")
def assistant_page() -> str:
    runs = db.list_assistant_runs(limit=50)
    counts: dict[int, int] = {}
    if runs:
        run_ids = [r.id for r in runs]
        counts = dict(
            db.db.session.query(AssistantStep.run_id, func.count())
            .filter(AssistantStep.run_id.in_(run_ids))
            .group_by(AssistantStep.run_id)
            .all()
        )

    selected = None
    timeline: list = []
    unlinked: list = []
    pending_controls: list = []
    run_arg = request.args.get("run", type=int)
    if run_arg is not None:
        selected = db.get_assistant_run(run_arg)
    if selected is not None:
        steps = db.list_assistant_steps(selected.id)
        intents = db.list_write_intents_for_run(selected.id)
        by_step: dict[str, list] = {}
        for it in intents:
            if it.step_uuid is None:
                unlinked.append(it)
            else:
                by_step.setdefault(str(it.step_uuid), []).append(it)
        timeline = [(s, by_step.get(str(s.uuid), [])) for s in steps]
        pending_controls = db.list_pending_controls(selected.id)

    return render_template_string(
        ASSISTANT_TEMPLATE,
        runs=runs, counts=counts, selected=selected,
        timeline=timeline, unlinked=unlinked, pending_controls=pending_controls,
    )
