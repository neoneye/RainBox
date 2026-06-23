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

from uuid import UUID

from flask import render_template_string, request

import db
from .core import app

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
  .b-stopped,.b-rejected,.b-undone,.b-planned { background:#f1f3f5; color:#555; }
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
  .as-main { overflow:auto; min-height:0; min-width:0; padding:12px 18px; }
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

  /* Right detail pane. */
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
  .as-main button.copy { padding:0.15rem 0.55rem; font-size:0.82rem; }
  .as-main .summary, .as-main .trigger { border:1px solid #e5e7eb; border-radius:8px;
                    padding:0.5rem 0.7rem; margin:0.6rem 0; background:#fbfdff; }
  .as-main .summary .grp, .as-main .trigger .grp { margin:0 0 0.25rem; }
  .as-main .obstacles { margin:0.2rem 0 0; padding-left:1.2rem; }
  .as-main .obstacles li { margin:0.1rem 0; }
  .as-main .trigmsg { white-space:pre-wrap; word-break:break-word; margin-top:0.25rem; }
  .as-main hr.sep { border:0; border-top:1px solid #e5e7eb; margin:1rem 0; }
  .as-main .runhd { display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap; margin-bottom:0.5rem; }
  .as-main .uuidline { display:flex; gap:0.5rem; align-items:center; margin:0.3rem 0; }
  .as-main .ruuid { font-family:ui-monospace,monospace; font-size:0.86rem; background:#f6f8fa;
                    border:1px solid #e1e4e8; border-radius:6px; padding:0.15rem 0.45rem; }
  .as-main .pending { background:#fff4e5; color:#92400e; border:1px solid #fde68a;
                      border-radius:6px; padding:0.4rem 0.6rem; margin:0.4rem 0; }
  .as-main .step { border:1px solid #e5e7eb; border-radius:8px; padding:0.55rem 0.7rem; margin-bottom:0.55rem; }
  .as-main .step.control { background:#faf5ff; border-color:#e9d5ff; }
  .as-main .step .hd { display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap; }
  .as-main .step .ix { color:#98a2b3; font-variant-numeric:tabular-nums; }
  .as-main .step .action { font-weight:600; }
  .as-main .step .reason { color:#475467; margin:0.3rem 0; }
  .as-main .err { color:#c0392b; }
  .as-main .intent { border-left:3px solid #cbd5e1; margin:0.45rem 0 0.2rem 0.4rem;
                     padding:0.4rem 0.6rem; background:#fcfcfd; border-radius:0 6px 6px 0; }
  .as-main .intent.proposed { border-left-color:#f59e0b; }
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
      <div class="summary">
        <div class="grp">Summary</div>
        {% if selected.summary %}
          <div>
            {% if selected.summary.outcome %}<span class="badge b-out-{{ selected.summary.outcome }}">{{ selected.summary.outcome }}</span>{% endif %}
            {{ selected.summary.trigger }}
          </div>
          {% if selected.summary.obstacles %}
            <div class="grp" style="font-size:0.85rem">Obstacles</div>
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

      <hr class="sep">

      <div class="runhd">
        <h1 style="margin:0">Run</h1>
        <span class="badge b-{{ selected.status }}">{{ selected.status }}</span>
        {% if selected.status in ('running', 'stopping') %}
          <button class="danger" onclick="ppAct('/chat/api/assistant/runs/{{ selected.uuid }}/stop')">Stop</button>
          <button onclick="ppRedirect('{{ selected.uuid }}')">Redirect…</button>
        {% endif %}
      </div>
      <div class="uuidline">
        <code class="ruuid">{{ selected.uuid }}</code>
        <button class="copy" onclick="ppCopy('{{ selected.uuid }}', this)">Copy</button>
      </div>
      <div class="muted">
        journal {{ (selected.journal_id|string)[:8] if selected.journal_id else '—' }}
        · started {{ selected.started_at.strftime('%Y-%m-%d %H:%M:%S') if selected.started_at else '—' }}
        {% if selected.finished_at %}· finished {{ selected.finished_at.strftime('%H:%M:%S') }}{% endif %}
      </div>

      <div class="trigger">
        <div class="grp">Trigger</div>
        {% if trigger %}
          <div><strong>{{ trigger.sender_name }}</strong>
            <span class="muted">{{ trigger.timestamp }}</span>
            · <a href="/chat?id={{ selected.room_uuid }}&msg={{ trigger.id }}">open in chat ↗</a>
          </div>
          <div class="trigmsg">{{ trigger.text | truncate(400) }}</div>
        {% else %}
          <div class="muted">No triggering chat message found ·
            room {{ (selected.room_uuid|string)[:8] }} ·
            <a href="/chat?id={{ selected.room_uuid }}">open in chat ↗</a>
          </div>
        {% endif %}
      </div>

      {% for c in pending_controls %}
      <div class="pending">⏳ pending {{ c.command }}{% if c.payload and c.payload.get('instruction') %}: {{ c.payload.get('instruction') }}{% endif %}</div>
      {% endfor %}

      {% if not timeline %}<div class="as-empty">This run has no steps.</div>{% endif %}
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

      {% if selected.final_summary %}
        <div class="grp">Verdict</div>
        <pre>{{ selected.final_summary }}</pre>
      {% endif %}
    {% endif %}
  </section>
</div>

<div id="as-menu" class="as-menu" hidden></div>

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
    if (status === 'running' || status === 'stopping') {
      asMenu.appendChild(asItem('Stop', function () {
        ppAct('/chat/api/assistant/runs/' + uuid + '/stop');
      }, true));
    }
    var r = event.currentTarget.getBoundingClientRect();
    asMenu.style.left = Math.min(r.left, window.innerWidth - 170) + 'px';
    asMenu.style.top = (r.bottom + 4) + 'px';
    asMenu.hidden = false;
  }

  // --- shared actions --------------------------------------------------------
  function ppAct(url) {
    fetch(url, {method: 'POST'})
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (d) {
        if (d && d.ok === false) { alert(d.text || 'Action failed'); return; }
        location.reload();
      })
      .catch(function (e) { alert('Request failed: ' + e); });
  }
  function ppCopyText(text) { navigator.clipboard.writeText(text); }
  function ppCopy(text, btn) {
    navigator.clipboard.writeText(text).then(function () {
      var old = btn.textContent; btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = old; }, 1200);
    });
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
        {"name": "Recent", "runs": runs, "count": len(runs), "default_open": True},
        {"name": "Running", "runs": running, "count": len(running), "default_open": True},
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
    unlinked: list = []
    pending_controls: list = []
    trigger = None
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
        pending_controls = db.list_pending_controls(selected.uuid)
        trigger = db.get_run_trigger_message(selected)

    return render_template_string(
        ASSISTANT_TEMPLATE,
        runs=runs, folders=folders, selected=selected, trigger=trigger,
        timeline=timeline, unlinked=unlinked, pending_controls=pending_controls,
        icon_open=_ICON_FOLDER_OPEN, icon_closed=_ICON_FOLDER,
    )
