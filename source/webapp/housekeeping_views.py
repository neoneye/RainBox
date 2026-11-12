"""The Housekeeping page: what ran after each turn, and what is running now.

`/assistant/housekeeping` is the log, one line per job, newest first, under a
line that says what the worker is doing at this moment. A line opens the job:
every attempt it took, the prompt each sent and the reply each got, and what
the job wrote.

Server-rendered with GET filters, like /second-opinion. The pages stay
current by polling one small value (`…/api/revision`) and reloading their own
content when it moves, so nothing is pushed and nothing is diffed in script.
Design: notes/housekeeping-design.md.
"""
import itertools
from datetime import UTC, datetime
from uuid import UUID

from flask import Response, abort, jsonify, redirect, render_template_string, request

import db

from .core import app

_OUTCOMES = ("all", "running", "waiting", "deferred", "failed", "done")
_PER_PAGE = 50


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _seconds(ms: int | None) -> str:
    if not ms:
        return ""
    return f"{ms / 1000:.1f} s"


def _job_label(job_type: str) -> str:
    return db.HOUSEKEEPING_JOB_TYPES.get(job_type, (2, job_type))[1]


def _outcome(job) -> tuple[str, str]:
    """What the log prints for a job, and the class that colours it."""
    if job.state == "final":
        text = job.outcome or "done"
        kind = "failed" if text.startswith("failed") else "done"
        return text, kind
    if job.state == "deferred":
        reason = job.deferred_reason or "waiting"
        text = {
            "turn": "a new message stopped it; waiting to run again",
            "cooldown": "resting the machine before the next call",
        }.get(reason, f"deferred: {reason}")
        return text, "deferred"
    return job.state, job.state


def _chunk_link(job) -> dict | None:
    """For a chunk job: which chunk it is, and the room page that shows it."""
    if job.job_type != db.ROOM_CHUNK_JOB or job.room_uuid is None:
        return None
    spec = db.parse_chunk_subject(job.subject)
    if spec is None:
        return None
    first = spec["seq"] * spec["size"]
    return {
        "text": f"messages {first}\u2013{first + spec['size'] - 1}",
        "href": (f"/assistant/housekeeping/room/{job.room_uuid}"
                 f"?size={spec['size']}&overlap={spec['overlap']}"
                 f"&prompt={spec['version']}#chunk-{spec['seq']}"),
    }


def _reaction_texts(part: dict) -> list[str]:
    """A part's reactions in order, with a run of the same one written once
    and counted."""
    reactions = part.get("reactions") or (
        [part["human_reaction"]] if part.get("human_reaction") else [])
    out: list[str] = []
    for reaction, run in itertools.groupby(reactions):
        n = len(list(run))
        out.append(reaction.replace("_", " ") + (f" \u00d7{n}" if n > 1 else ""))
    return out


def _signal_texts(signals: dict | None) -> list[str]:
    """A part's records as short labels: runs by outcome, replies that did
    not meet their criteria, revisions, votes. Only what is non-zero."""
    if not signals:
        return []
    out = [f"{n} {outcome}" for outcome, n in (signals.get("runs") or {}).items()]
    if signals.get("criteria_not_met"):
        out.append(f"criteria not met \u00d7{signals['criteria_not_met']}")
    if signals.get("revisions"):
        out.append(f"{signals['revisions']} revision"
                   f"{'' if signals['revisions'] == 1 else 's'}")
    if signals.get("up"):
        out.append(f"\u25b2 {signals['up']}")
    if signals.get("down"):
        out.append(f"\u25bc {signals['down']}")
    return out


def _elapsed(since: datetime | None) -> str:
    if since is None:
        return ""
    return f"{max(0, int((datetime.now(UTC) - since).total_seconds()))} s"


_STYLE = """
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#fbfbfb;color:#374151}
  .hk-wrap{max-width:1320px;margin:0 auto;padding:24px 28px 56px}
  .hk-head{margin:0 0 4px;font-size:1.35rem;color:#1a1a2e}
  .hk-lede{margin:0 0 20px;font-size:0.86rem;color:#6b7280;max-width:74ch}
  .hk-now{display:flex;flex-wrap:wrap;gap:10px 28px;align-items:baseline;
    border:1px solid #e5e7eb;border-radius:8px;background:#fff;padding:12px 16px;
    margin-bottom:18px;font-size:0.88rem}
  .hk-now .k{font-size:0.68rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;color:#9ca3af;margin-right:7px}
  .hk-now .v{color:#1a1a2e;font-variant-numeric:tabular-nums}
  .hk-now a{color:#2563eb;text-decoration:none}
  .hk-filters{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px;margin-bottom:16px}
  .hk-field{display:flex;flex-direction:column;gap:5px}
  .hk-field label{font-size:0.68rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;color:#9ca3af}
  .hk-select{padding:7px 11px;border:1px solid #e5e7eb;border-radius:6px;background:#fff;
    font:inherit;font-size:0.88rem;color:#1a1a2e;cursor:pointer}
  .hk-chips{display:flex;flex-wrap:wrap;gap:7px}
  .hk-chip{font-size:0.75rem;font-weight:600;padding:5px 12px;border-radius:999px;
    text-decoration:none;color:#6b7280;background:#eef0f4}
  .hk-chip.sel{outline:2px solid #2563eb;outline-offset:1px}
  table.hk-log{width:100%;border-collapse:collapse;background:#fff;
    border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;font-size:0.86rem}
  .hk-log th{text-align:left;font-size:0.68rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;color:#9ca3af;padding:9px 12px;background:#fbfdff;
    border-bottom:1px solid #e5e7eb}
  .hk-log td{padding:9px 12px;border-bottom:1px solid #f1f3f5;vertical-align:top}
  .hk-log tr:last-child td{border-bottom:0}
  .hk-log tr.row{cursor:pointer}
  .hk-log tr.row:hover td{background:#f8fafc}
  .hk-log a{color:#2563eb;text-decoration:none}
  .hk-log a:hover{text-decoration:underline}
  .hk-mono{font-family:ui-monospace,Menlo,monospace;font-size:0.78rem;color:#6b7280;
    white-space:nowrap}
  .hk-num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .hk-pill{display:inline-block;font-size:0.75rem;font-weight:700;padding:3px 10px;
    border-radius:999px;color:#6b7280;background:#eef0f4}
  .hk-pill.done{color:#166534;background:#dcfce7}
  .hk-pill.failed{color:#b91c1c;background:#fee2e2}
  .hk-pill.running{color:#1d4ed8;background:#dbeafe}
  .hk-pill.deferred,.hk-pill.waiting{color:#92400e;background:#fef3c7}
  .hk-pill.killed{color:#92400e;background:#fef3c7}
  .hk-empty{border:1px dashed #d1d5db;border-radius:8px;padding:52px 24px;
    text-align:center;background:#fff}
  .hk-empty .t{font-size:0.95rem;color:#1a1a2e;font-weight:600;margin-bottom:6px}
  .hk-empty .s{font-size:0.8rem;color:#6b7280}
  .hk-pager{display:flex;gap:10px;align-items:center;margin-top:16px;font-size:0.82rem}
  .hk-pager a{color:#2563eb;text-decoration:none}
  .hk-back{font-size:0.84rem;color:#2563eb;text-decoration:none}
  .hk-card{border:1px solid #e5e7eb;border-radius:8px;background:#fff;margin-bottom:14px}
  .hk-card .hd{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 14px;
    background:#fbfdff;border-bottom:1px solid #e5e7eb;font-size:0.86rem}
  .hk-card .bd{padding:12px 14px}
  .hk-card h3{margin:0;font-size:0.9rem;color:#1a1a2e}
  .hk-kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;font-size:0.86rem;margin:0}
  .hk-kv dt{font-size:0.68rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;color:#9ca3af;padding-top:3px}
  .hk-kv dd{margin:0;color:#1a1a2e}
  .hk-kv a{color:#2563eb;text-decoration:none}
  details.hk-text{margin-top:10px}
  details.hk-text > summary{font-size:0.68rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.05em;color:#6b7280;cursor:pointer}
  .hk-pre{margin:8px 0 0;padding:10px 12px;background:#f8fafc;border:1px solid #eef0f4;
    border-radius:6px;font-family:ui-monospace,Menlo,monospace;font-size:0.78rem;
    white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;color:#1a1a2e}
  .hk-err{color:#b91c1c;font-size:0.84rem;margin:8px 0 0}
  table.hk-parts{border-collapse:collapse}
  .hk-parts td{padding:5px 16px 5px 0;vertical-align:top;font-size:0.86rem}
  .hk-result{color:#374151;margin-top:2px}
  .hk-signals{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px}
  .hk-signals .hk-chip{font-weight:500;padding:2px 9px}
  .hk-pill.hk-cal-calibrated{color:#166534;background:#dcfce7}
  .hk-pill.hk-cal-overconfident{color:#b91c1c;background:#fee2e2}
  .hk-pill.hk-cal-underconfident{color:#92400e;background:#fef3c7}
  .hk-btn{padding:7px 14px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;
    font:inherit;font-size:0.86rem;cursor:pointer;color:#374151}
  .hk-btn:hover{border-color:#9aa3af;color:#1a1a2e}
</style>
"""

# Polls the revision and swaps the live container when it moves. Open
# <details> blocks are remembered by id across the swap, so reading a prompt
# is not interrupted by a line being added elsewhere. No backslashes: this
# string is not raw, and Python would interpret them before the browser does.
_LIVE_SCRIPT = """
<script>
(function () {
  var box = document.getElementById('hk-live');
  if (!box) { return; }
  var seen = box.getAttribute('data-revision');
  var busy = false;
  function openIds() {
    var ids = [];
    box.querySelectorAll('details[open][id]').forEach(function (d) { ids.push(d.id); });
    return ids;
  }
  function reload(rev) {
    var url = new URL(window.location.href);
    url.searchParams.set('fragment', '1');
    var keep = openIds();
    return fetch(url.toString()).then(function (r) { return r.text(); }).then(function (html) {
      box.innerHTML = html;
      keep.forEach(function (id) {
        var d = document.getElementById(id);
        if (d) { d.open = true; }
      });
      seen = rev;
    });
  }
  function tick() {
    if (busy) { return; }
    busy = true;
    fetch('/assistant/housekeeping/api/revision')
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j.revision !== seen) { return reload(j.revision); } })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  box.addEventListener('click', function (e) {
    if (e.target.closest('a, button, summary')) { return; }
    var row = e.target.closest('tr[data-href]');
    if (row) { window.location.href = row.getAttribute('data-href'); }
  });
  setInterval(tick, 2000);
})();
</script>
"""

_NOW_FRAGMENT = """
<div class="hk-now">
  <span><span class="k">Now</span>
    {% if now.running %}
      <span class="v">running <a href="/assistant/housekeeping/{{ now.running.uuid }}">{{ label(now.running.job_type) }}</a>{% if now.attempt %}
        for {{ elapsed(now.attempt.started_at) }}{% if now.attempt.model %} on {{ now.attempt.model }}{% endif %}{% endif %}</span>
    {% elif now.turn_active and (now.waiting[1] or now.waiting[2]) %}
      <span class="v">giving way to a turn</span>
    {% else %}
      <span class="v">idle</span>
    {% endif %}
  </span>
  <span><span class="k">Waiting</span><span class="v">{{ now.waiting[1] }} for the next turn &middot; {{ now.waiting[2] }} generative</span></span>
  <span><span class="k">Failed</span><span class="v">{% if now.failed %}<a href="/assistant/housekeeping?outcome=failed">{{ now.failed }}</a>{% else %}0{% endif %}</span></span>
</div>
"""

_LOG_FRAGMENT = _NOW_FRAGMENT + """
{% if rows %}
<table class="hk-log">
  <thead><tr><th>When</th><th>Job</th><th>For</th><th>Model</th><th>Time</th><th>Outcome</th></tr></thead>
  <tbody>
  {% for r in rows %}
    {% set text, kind = outcome(r.job) %}
    <tr class="row" data-href="/assistant/housekeeping/{{ r.job.uuid }}">
      <td class="hk-mono">{{ when(r.job.created_at) }}{% if r.job.finished_at %}<br>{{ when(r.job.finished_at) }}{% endif %}</td>
      <td><a href="/assistant/housekeeping/{{ r.job.uuid }}">{{ label(r.job.job_type) }}</a></td>
      <td>
        {% if r.job.room_uuid %}<a href="/chat?id={{ r.job.room_uuid }}">{{ rooms.get(r.job.room_uuid, 'room') }}</a>
          (<a href="/assistant/housekeeping/room/{{ r.job.room_uuid }}">chunks</a>){% endif %}
        {% if r.job.run_uuid %}{% if r.job.room_uuid %} &middot; {% endif %}<a href="/assistant?id={{ r.job.run_uuid }}">run</a>{% endif %}
        {% set chunk = chunk_link(r.job) %}
        {% if chunk %} &middot; <a href="{{ chunk.href }}">{{ chunk.text }}</a>
        {% elif r.job.subject %} &middot; {{ r.job.subject }}{% endif %}
      </td>
      <td>{{ r.model or '' }}</td>
      <td class="hk-num">{{ seconds(r.model_ms) }}{% if r.attempts > 1 %} &middot; {{ r.attempts }} attempts{% endif %}</td>
      <td><span class="hk-pill {{ kind }}">{{ text }}</span></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<div class="hk-pager">
  {% if page > 1 %}<a href="{{ page_url(page - 1) }}">&larr; Newer</a>{% endif %}
  <span>{{ total }} job{{ '' if total == 1 else 's' }}{% if pages > 1 %} &middot; page {{ page }} of {{ pages }}{% endif %}</span>
  {% if page < pages %}<a href="{{ page_url(page + 1) }}">Older &rarr;</a>{% endif %}
</div>
{% else %}
<div class="hk-empty">
  <div class="t">No housekeeping jobs{{ '' if unfiltered else ' match this filter' }}</div>
  <div class="s">A job appears here when an assistant run ends.</div>
</div>
{% endif %}
"""

_LOG_PAGE = """
<!doctype html>
<title>Housekeeping &mdash; rainbox</title>
""" + _STYLE + """
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="hk-wrap">
  <h1 class="hk-head">Housekeeping</h1>
  <p class="hk-lede">The work that follows a turn. It starts when the reply has
    posted, and a new message stops the model call in progress so the turn has
    the machine to itself. A stopped job runs again afterwards.</p>
  <form class="hk-filters" method="get" action="/assistant/housekeeping">
    <div class="hk-field">
      <label>Outcome</label>
      <div class="hk-chips">
        {% for o in outcomes %}
        <a class="hk-chip {{ 'sel' if o == sel_outcome }}" href="{{ filter_url(o) }}">{{ o }}</a>
        {% endfor %}
      </div>
    </div>
    <input type="hidden" name="outcome" value="{{ sel_outcome }}">
    <div class="hk-field">
      <label for="hk-type">Job</label>
      <select class="hk-select" id="hk-type" name="type" onchange="this.form.submit()">
        <option value="all">Any job</option>
        {% for key, pair in job_types.items() %}
        <option value="{{ key }}" {{ 'selected' if key == sel_type }}>{{ pair[1] }}</option>
        {% endfor %}
      </select>
    </div>
    {% if sel_room %}
    <div class="hk-field">
      <label>Room</label>
      <a class="hk-chip sel" href="{{ clear_room_url }}">{{ rooms.get(sel_room, 'room') }} &times;</a>
      <input type="hidden" name="room" value="{{ sel_room }}">
    </div>
    {% endif %}
  </form>
  <div id="hk-live" data-revision="{{ revision }}">""" + _LOG_FRAGMENT + """</div>
</div>
""" + _LIVE_SCRIPT

_JOB_FRAGMENT = """
{% set text, kind = outcome(job) %}
<div class="hk-card">
  <div class="hd"><h3>{{ label(job.job_type) }}</h3><span class="hk-pill {{ kind }}">{{ text }}</span></div>
  <div class="bd">
    <dl class="hk-kv">
      <dt>For</dt>
      <dd>
        {% if job.room_uuid %}<a href="/chat?id={{ job.room_uuid }}">{{ rooms.get(job.room_uuid, 'room') }}</a>{% endif %}
        {% if job.run_uuid %}{% if job.room_uuid %} &middot; {% endif %}<a href="/assistant?id={{ job.run_uuid }}">run {{ job.run_uuid }}</a>{% endif %}
        {% if request_message %} &middot; <a href="/chat?id={{ job.room_uuid }}&msg={{ request_message }}">the request</a>{% endif %}
        {% set chunk = chunk_link(job) %}
        {% if chunk %} &middot; <a href="{{ chunk.href }}">{{ chunk.text }}</a>
        {% elif job.subject %} &middot; {{ job.subject }}{% endif %}
      </dd>
      <dt>Created</dt><dd class="hk-mono">{{ when(job.created_at) }}</dd>
      {% if job.finished_at %}<dt>Finished</dt><dd class="hk-mono">{{ when(job.finished_at) }}</dd>{% endif %}
      <dt>Attempts</dt><dd>{{ attempts|length }}{% if job.retries %} &middot; {{ job.retries }} retried after a failure{% endif %}</dd>
      {% if wrote %}<dt>Wrote</dt><dd>{{ wrote }}</dd>{% endif %}
    </dl>
    {% if job.state == 'final' and kind == 'failed' %}
    <form method="post" action="/assistant/housekeeping/{{ job.uuid }}/requeue" style="margin-top:12px">
      <button class="hk-btn" type="submit">Enqueue again</button>
    </form>
    {% endif %}
  </div>
</div>

{% for a in attempts %}
<div class="hk-card">
  <div class="hd">
    <h3>Attempt {{ loop.index }}</h3>
    {% if a.row.result %}
      <span class="hk-pill {{ 'killed' if a.row.result.startswith('killed') else ('done' if a.row.result in ('done', 'skipped') else 'failed') }}">{{ a.row.result }}</span>
    {% else %}
      <span class="hk-pill running">running for {{ elapsed(a.row.started_at) }}</span>
    {% endif %}
    <span class="hk-mono">{{ when(a.row.started_at) }}</span>
    {% if a.row.model %}<span>{{ a.row.model }}</span>{% endif %}
    {% if a.row.model_ms %}<span class="hk-num">{{ seconds(a.row.model_ms) }}</span>{% endif %}
    {% if a.call %}<a class="hk-back" style="margin-left:auto" href="/activity/call/{{ a.call.uuid }}">the call on /activity</a>{% endif %}
  </div>
  <div class="bd">
    {% if a.row.error %}<p class="hk-err">{{ a.row.error }}</p>{% endif %}
    {% if a.call %}
      {% for m in a.call.messages or [] %}
      <details class="hk-text" id="a{{ a.row.uuid }}-m{{ loop.index }}">
        <summary>{{ m.role }} prompt</summary>
        <pre class="hk-pre">{{ m.content }}</pre>
      </details>
      {% endfor %}
    {% endif %}
    {% set reply = (a.call.response_text if a.call and a.call.response_text else a.row.response_preview) %}
    {% if reply %}
      <details class="hk-text" id="a{{ a.row.uuid }}-reply" open>
        <summary>{{ 'reply so far' if not a.row.result else 'reply' }}</summary>
        <pre class="hk-pre">{{ reply }}</pre>
      </details>
    {% elif not a.row.result %}
      <p class="hk-lede" style="margin:8px 0 0">Waiting for the model's first words.</p>
    {% elif not a.call and not a.row.error %}
      <p class="hk-lede" style="margin:8px 0 0">This attempt made no model call.</p>
    {% endif %}
  </div>
</div>
{% else %}
<div class="hk-empty"><div class="t">Not started yet</div>
  <div class="s">The job is waiting for its turn in the queue.</div></div>
{% endfor %}
"""

_JOB_PAGE = """
<!doctype html>
<title>Housekeeping job &mdash; rainbox</title>
""" + _STYLE + """
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="hk-wrap">
  <p style="margin:0 0 12px"><a class="hk-back" href="/assistant/housekeeping">&larr; Housekeeping</a></p>
  <div id="hk-live" data-revision="{{ revision }}">""" + _JOB_FRAGMENT + """</div>
</div>
""" + _LIVE_SCRIPT


def run_housekeeping_lines(run_uuid: UUID) -> list[dict]:
    """A run's jobs as the lines of its page's After the turn group."""
    lines = []
    for job in db.list_run_housekeeping_jobs(run_uuid):
        attempts = db.list_housekeeping_attempts(job.uuid)
        text, kind = _outcome(job)
        lines.append({
            "uuid": str(job.uuid), "label": _job_label(job.job_type),
            "at": _when(attempts[0].started_at if attempts else job.created_at),
            "model": next((a.model for a in reversed(attempts) if a.model), ""),
            "time": _seconds(sum(a.model_ms or 0 for a in attempts)),
            "attempts": len(attempts), "outcome": text, "kind": kind,
        })
    return lines


def _helpers() -> dict:
    return {"when": _when, "seconds": _seconds, "label": _job_label,
            "outcome": _outcome, "elapsed": _elapsed, "chunk_link": _chunk_link,
            "signal_texts": _signal_texts,
            "reaction_texts": _reaction_texts}


def _room_arg() -> UUID | None:
    raw = request.args.get("room")
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        abort(400, "invalid room")


@app.route("/assistant/housekeeping")
def housekeeping_page() -> str:
    sel_outcome = request.args.get("outcome", "all")
    if sel_outcome not in _OUTCOMES:
        sel_outcome = "all"
    sel_type = request.args.get("type", "all")
    if sel_type != "all" and sel_type not in db.HOUSEKEEPING_JOB_TYPES:
        sel_type = "all"
    sel_room = _room_arg()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    rows, total = db.list_housekeeping_jobs(
        outcome=sel_outcome, job_type=sel_type, room_uuid=sel_room,
        limit=_PER_PAGE, offset=(page - 1) * _PER_PAGE)
    room_uuids = [r["job"].room_uuid for r in rows if r["job"].room_uuid]
    if sel_room:
        room_uuids.append(sel_room)

    def url(**changes) -> str:
        args = {"outcome": sel_outcome, "type": sel_type,
                "room": str(sel_room) if sel_room else None, **changes}
        query = "&".join(f"{k}={v}" for k, v in args.items()
                         if v not in (None, "all", 1))
        return "/assistant/housekeeping" + (f"?{query}" if query else "")

    ctx = dict(
        rows=rows, total=total, page=page,
        pages=max(1, -(-total // _PER_PAGE)),
        rooms=db.housekeeping_room_names(room_uuids),
        now=db.housekeeping_now(), revision=db.housekeeping_revision(),
        outcomes=_OUTCOMES, sel_outcome=sel_outcome, sel_type=sel_type,
        sel_room=sel_room, job_types=db.HOUSEKEEPING_JOB_TYPES,
        unfiltered=(sel_outcome == "all" and sel_type == "all" and not sel_room),
        filter_url=lambda o: url(outcome=o), page_url=lambda p: url(page=p),
        clear_room_url=url(room=None), **_helpers())
    template = _LOG_FRAGMENT if request.args.get("fragment") else _LOG_PAGE
    return render_template_string(template, **ctx)


def _wrote(job) -> str:
    """What a finished job left behind, in words. The link to it is the run."""
    if job.state != "final" or (job.outcome or "") != "done":
        return ""
    if job.job_type == db.RUN_SUMMARY_JOB:
        return "the summary on its run"
    if job.job_type == db.ROOM_CHUNK_JOB:
        return "the chunk's summary, on the room's page"
    return ""


@app.route("/assistant/housekeeping/<uuid:job_uuid>")
def housekeeping_job_page(job_uuid: UUID) -> str:
    job = db.get_housekeeping_job(job_uuid)
    if job is None:
        abort(404, "housekeeping job not found")
    attempts = [{"row": a, "call": db.housekeeping_attempt_call(job, a)}
                for a in db.list_housekeeping_attempts(job.uuid)]
    run = db.get_assistant_run(job.run_uuid) if job.run_uuid else None
    ctx = dict(
        job=job, attempts=attempts, wrote=_wrote(job),
        request_message=(run.metadata_ or {}).get("request_message") if run else None,
        rooms=db.housekeeping_room_names([job.room_uuid] if job.room_uuid else []),
        revision=db.housekeeping_revision(), **_helpers())
    template = _JOB_FRAGMENT if request.args.get("fragment") else _JOB_PAGE
    return render_template_string(template, **ctx)


@app.route("/assistant/housekeeping/<uuid:job_uuid>/requeue", methods=["POST"])
def housekeeping_job_requeue(job_uuid: UUID) -> Response:
    """Enqueue a job whose failure was final. Its attempts stay on its page."""
    job = db.get_housekeeping_job(job_uuid)
    if job is None:
        abort(404, "housekeeping job not found")
    if job.state == "final":
        db.requeue_housekeeping_job(job)
        from agents.config import HOUSEKEEPING_UUID

        # Wakes the worker; the job itself is already waiting in its table.
        db.enqueue(HOUSEKEEPING_UUID, {"sweep": True})
    return redirect(f"/assistant/housekeeping/{job_uuid}")


_ROOM_FRAGMENT = """
<div class="hk-now">
  <span><span class="k">Messages</span><span class="v">{{ counted }}</span></span>
  <span><span class="k">Summarized</span><span class="v">{{ chunks|length }} chunk{{ '' if chunks|length == 1 else 's' }} of {{ size }}, {{ covered }} messages</span></span>
  <span><span class="k">Queued</span><span class="v">{{ queued }}</span></span>
  <span><span class="k">Not yet queued</span><span class="v">{{ due }} chunk{{ '' if due == 1 else 's' }}</span></span>
  {% if is_current and due %}
  <form method="post" action="/assistant/housekeeping/room/{{ room_uuid }}/summarize" style="margin-left:auto">
    <button class="hk-btn" type="submit" title="Two model calls per chunk, the summary and its check, and two more when the check has it written again. Ten in a row and then a minute's rest. A new message stops the call in progress.">Summarize the rest: {{ due }} chunk{{ '' if due == 1 else 's' }}</button>
  </form>
  {% endif %}
</div>
{% if not is_current %}
<p class="hk-lede">This series was written with other settings or an earlier prompt
  than the current ones ({{ cur_size }} per chunk, {{ cur_overlap }} of context,
  prompt {{ cur_version }}), so no new chunks are added to it.</p>
{% endif %}
{% for c in chunks %}
<div class="hk-card" id="chunk-{{ c.row.seq }}">
  <div class="hd">
    <h3>Messages {{ c.first_index }}&ndash;{{ c.last_index }}</h3>
    <span class="hk-mono">{{ c.dates }}</span>
    {% if c.stale %}<span class="hk-pill deferred" title="A message in this chunk was edited or deleted after the summary was written">messages changed since</span>{% endif %}
    <span style="margin-left:auto">{{ c.row.model or '' }}</span>
    {% if c.row.job_uuid %}<a class="hk-back" href="/assistant/housekeeping/{{ c.row.job_uuid }}">prompt and reply</a>{% endif %}
  </div>
  <div class="bd">
    <dl class="hk-kv">
      <dt>Summary</dt><dd>{{ c.row.summary or '' }}</dd>
      <dt>Topics</dt><dd>{% for t in c.row.topics %}<span class="hk-chip">{{ t }}</span> {% endfor %}</dd>
      {% if c.row.parts %}
      <dt>Parts</dt>
      <dd>
        <table class="hk-parts">
        {% for part in c.row.parts %}
          <tr>
            <td class="hk-mono">{% if part.first_message is not none %}m{{ part.first_message }}{% if part.last_message != part.first_message %}&ndash;m{{ part.last_message }}{% endif %}{% endif %}</td>
            <td>{{ part.summary }}{% if part.requests and part.requests > 1 %} <span class="hk-mono">&times;{{ part.requests }}</span>{% endif %}
              {% if part.result %}<div class="hk-result">&rarr; {{ part.result }}</div>{% endif %}
              {% if part.calibration %}
              <div class="hk-signals">
                <span class="hk-pill hk-cal-{{ part.calibration }}" title="{{ 'Decided by rule from the reaction and the records' if part.calibration_basis else 'Judged by the model' }}">{{ part.calibration }}</span>
                {% if part.calibration_basis %}<span>{{ part.calibration_basis }}</span>{% endif %}
                {% for r in reaction_texts(part) %}<span class="hk-mono" title="What the human did after each reply">{{ r }}</span>{% endfor %}
                {% for text in signal_texts(part.signals) %}<span class="hk-chip" title="From the records">{{ text }}</span>{% endfor %}
              </div>
              {% endif %}
              {% if part.audit and not (part.audit.request and part.audit.result and part.audit.get('reaction', true)) %}
              <div class="hk-signals">
                <span class="hk-pill failed" title="A second model call compared this part with its messages">check</span>
                <span>{% if not part.audit.request %}the request {% endif %}{% if not part.audit.result %}{{ 'and ' if not part.audit.request }}the result {% endif %}{% if not part.audit.get('reaction', true) %}{{ 'and ' if not (part.audit.request and part.audit.result) }}the reaction {% endif %}may not match the messages: {{ part.audit.reasoning }}</span>
              </div>
              {% endif %}
            </td>
            <td>{% for t in part.topics %}<span class="hk-chip">{{ t }}</span> {% endfor %}</td>
          </tr>
        {% endfor %}
        </table>
      </dd>
      {% endif %}
      {% if c.row.audit %}
      <dt>Check</dt>
      <dd>
        {% if c.row.audit.rewritten %}
        Written twice. The check found {{ c.row.audit.rewritten|length }} part{{ '' if c.row.audit.rewritten|length == 1 else 's' }} of the first answer wrong, and what is shown is the second.
        <details class="hk-text"><summary>what the first answer had</summary>
          {% for w in c.row.audit.rewritten %}
          <div class="hk-result"><span class="hk-mono">m{{ w.first_message }}&ndash;m{{ w.last_message }}</span> {{ w.summary }} &rarr; {{ w.result }}<br>{{ w.reasoning }}</div>
          {% endfor %}
        </details>
        {% elif not c.row.audit.error %}
        Written once. The check found every part's request and result in the messages.
        {% endif %}
        {% if c.row.audit.error %}<div class="hk-err" style="margin:0">{{ c.row.audit.error }}</div>{% endif %}
      </dd>
      {% endif %}
      {% if c.row.problems %}<dt>Format</dt><dd class="hk-err" style="margin:0">{{ c.row.problems|join('; ') }}</dd>{% endif %}
    </dl>
    <details class="hk-text" id="chunk-{{ c.row.seq }}-messages">
      <summary>the {{ c.messages|length }} messages</summary>
      <pre class="hk-pre">{% for m in c.messages %}[m{{ c.first_index + loop.index0 }} {{ m.sender }}] {{ m.text }}
{% endfor %}</pre>
    </details>
  </div>
</div>
{% else %}
<div class="hk-empty"><div class="t">No chunk of this room is summarized yet</div>
  <div class="s">A chunk is summarized after a turn in the room, two at a time, or all at once with the button above.</div></div>
{% endfor %}
"""

_ROOM_PAGE = """
<!doctype html>
<title>Room chunks &mdash; rainbox</title>
""" + _STYLE + """
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="hk-wrap">
  <p style="margin:0 0 12px"><a class="hk-back" href="/assistant/housekeeping">&larr; Housekeeping</a></p>
  <h1 class="hk-head">{{ room_name }}</h1>
  <p class="hk-lede">This room's messages in chunks, each summarized by a
    local model: the chunk split into parts, each with what was wanted, what
    came of it and how you reacted, under the names of its subjects. A second call
    checks every part against its messages; when it finds a request or a
    result wrong, the chunk is written once more. The grey
    labels beside a part are records (how its runs ended, acceptance
    criteria, your votes). The coloured label says how well calibrated the
    assistant was, decided by rule from your reaction and those records.
    The summaries are shown here and reach no prompt. <a class="hk-back" href="/chat?id={{ room_uuid }}">Open the room</a></p>
  <div class="hk-filters">
    <div class="hk-field">
      <label>Series</label>
      <div class="hk-chips">
        {% for s_size, s_overlap, s_version, n in series %}
        <a class="hk-chip {{ 'sel' if (s_size, s_overlap, s_version) == (size, overlap, version) }}"
           href="/assistant/housekeeping/room/{{ room_uuid }}?size={{ s_size }}&overlap={{ s_overlap }}&prompt={{ s_version }}">{{ s_size }} per chunk, {{ s_overlap }} of context, prompt {{ s_version }}{% if n %} &middot; {{ n }}{% endif %}</a>
        {% endfor %}
      </div>
    </div>
  </div>
  <div id="hk-live" data-revision="{{ revision }}">""" + _ROOM_FRAGMENT + """</div>
</div>
""" + _LIVE_SCRIPT


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except ValueError:
        return default


@app.route("/assistant/housekeeping/room/<uuid:room_uuid>")
def housekeeping_room_page(room_uuid: UUID) -> str:
    from agents.room_chunks import PROMPT_VERSION, content_hash

    names = db.housekeeping_room_names([room_uuid])
    if room_uuid not in names:
        abort(404, "room not found")
    cur_size = int(db.get_setting("housekeeping.room_chunk_size"))
    cur_overlap = int(db.get_setting("housekeeping.room_chunk_overlap"))
    size = _int_arg("size", cur_size)
    overlap = _int_arg("overlap", cur_overlap)
    version = _int_arg("prompt", PROMPT_VERSION)
    is_current = (size, overlap, version) == (cur_size, cur_overlap, PROMPT_VERSION)

    messages = db.counted_room_messages(room_uuid)
    index_of = {m["id"]: i for i, m in enumerate(messages)}
    chunks = []
    for row in db.list_room_chunks(room_uuid, size, overlap, version):
        inside = [m for m in messages
                  if row.first_message_id <= m["id"] <= row.last_message_id]
        first_index = index_of.get(inside[0]["id"], row.seq * size) if inside \
            else row.seq * size
        dates = ""
        if inside:
            a, b = inside[0]["created_at"], inside[-1]["created_at"]
            dates = a.strftime("%Y-%m-%d") if a.date() == b.date() \
                else f"{a.strftime('%Y-%m-%d')} to {b.strftime('%Y-%m-%d')}"
        chunks.append({
            "row": row, "messages": inside, "dates": dates,
            "first_index": first_index,
            "last_index": first_index + max(len(inside), 1) - 1,
            "stale": content_hash(inside) != row.content_hash,
        })
    series = db.list_room_chunk_series(room_uuid)
    if not any((s, o, v) == (cur_size, cur_overlap, PROMPT_VERSION)
               for s, o, v, _n in series):
        series.append((cur_size, cur_overlap, PROMPT_VERSION, 0))
    queued = sum(
        1 for r in db.list_housekeeping_jobs(
            job_type=db.ROOM_CHUNK_JOB, room_uuid=room_uuid, limit=500)[0]
        if r["job"].state != "final"
        and r["job"].subject.startswith(f"{size}/{overlap}/v{version}/"))
    due = len(db.due_room_chunks(room_uuid, size, overlap, version, 500)) \
        if is_current else 0
    ctx = dict(
        room_uuid=room_uuid, room_name=names[room_uuid], chunks=chunks,
        counted=len(messages), covered=sum(c["row"].message_count for c in chunks),
        size=size, overlap=overlap, version=version, cur_size=cur_size,
        cur_overlap=cur_overlap, cur_version=PROMPT_VERSION,
        is_current=is_current, series=series, queued=queued,
        due=max(0, due - queued), revision=db.housekeeping_revision(),
        **_helpers())
    template = _ROOM_FRAGMENT if request.args.get("fragment") else _ROOM_PAGE
    return render_template_string(template, **ctx)


@app.route("/assistant/housekeeping/room/<uuid:room_uuid>/summarize", methods=["POST"])
def housekeeping_room_summarize(room_uuid: UUID) -> Response:
    """Queue a summary for every chunk of the room that is due. The worker
    takes them ten calls at a time, and a new message stops the one running."""
    if room_uuid not in db.housekeeping_room_names([room_uuid]):
        abort(404, "room not found")
    from agents.config import HOUSEKEEPING_UUID

    db.enqueue(HOUSEKEEPING_UUID, {"room_uuid": str(room_uuid), "room_chunks": True})
    return redirect(f"/assistant/housekeeping/room/{room_uuid}")


@app.route("/assistant/housekeeping/api/revision")
def housekeeping_revision_api() -> Response:
    return jsonify({"revision": db.housekeeping_revision()})
