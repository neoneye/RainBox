import json
from typing import Any

from flask import (
    Response,
    abort,
    redirect,
    render_template_string,
    request,
    url_for,
)

from agents.config import DREAMER_UUID, agent_config
from db import Inbox, Journal, db, enqueue, reset_demo_data

from .core import app


INDEX_TEMPLATE: str = """
<!doctype html>
<title>rainbox</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:0} .ok{color:#080}</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>rainbox</h1>

<h2>Demo</h2>
<form method="post" action="{{ url_for('demo') }}">
  <button type="submit">Run demo (reset + seed 5 dreamer tasks)</button>
</form>
{% if demo %}<p class="ok">demo started &mdash; 5 dreamer tasks seeded; the supervisor will wake the agents.</p>{% endif %}

<h2>Try it</h2>
<ul>
  <li><a href="{{ url_for('demo_multimodal') }}"><b>Multimodal</b></a> &mdash; poke a local vision+audio model with an image or audio file (streamed, nothing saved)</li>
</ul>

<h2>Agents</h2>
<ul>
{% for name, params in agents.items() %}
  <li><a href="{{ url_for('agent_page', name=name) }}"><b>{{ name }}</b></a> &mdash; {{ params.description }}</li>
{% endfor %}
</ul>
</div>
"""


AGENT_TEMPLATE: str = """
<!doctype html>
<title>{{ name }} &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top;text-align:left}
  pre{margin:0;white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:90%}
  textarea{width:100%;font-family:ui-monospace,monospace}
  .ok{color:#080}
  .err{color:#a00}
  code{background:#eee;padding:1px 4px;border-radius:3px}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>{{ name }}</h1>
<p><b>uuid:</b> <code>{{ params.uuid }}</code></p>
<p><b>description:</b> {{ params.description }}</p>

<h2>Enqueue a message</h2>
<form method="post">
  <label>Payload (JSON):</label>
  <textarea name="payload" rows="5">{{ default_payload }}</textarea>
  <p><button type="submit">Enqueue</button></p>
</form>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if flash %}<p class="ok">{{ flash }}</p>{% endif %}

<h2>Pending inbox ({{ inbox|length }})</h2>
<table>
  <tr><th>id</th><th>enqueued_at</th><th>payload</th></tr>
  {% for r in inbox %}
  <tr><td>{{ r.id }}</td><td>{{ r.enqueued_at }}</td><td><pre>{{ r.payload }}</pre></td></tr>
  {% else %}
  <tr><td colspan="3"><i>empty</i></td></tr>
  {% endfor %}
</table>

<h2>Recent journal (last 20)</h2>
<table>
  <tr><th>id</th><th>state</th><th>payload</th><th>result</th><th>updated_at</th></tr>
  {% for r in journal %}
  <tr><td>{{ r.id }}</td><td>{{ r.state }}</td><td><pre>{{ r.payload }}</pre></td><td><pre>{{ r.result or '' }}</pre></td><td>{{ r.updated_at }}</td></tr>
  {% else %}
  <tr><td colspan="5"><i>empty</i></td></tr>
  {% endfor %}
</table>
</div>
"""


@app.route("/")
def index() -> str:
    demo_flash = bool(request.args.get("demo"))
    return render_template_string(INDEX_TEMPLATE, agents=agent_config, demo=demo_flash)


@app.route("/demo", methods=["POST"])
def demo() -> Response:
    reset_demo_data()
    for i in range(5):
        enqueue(DREAMER_UUID, {"task": f"dreamer_task_{i}"})
    return redirect(url_for("index", demo=1))


@app.route("/agent/<name>", methods=["GET", "POST"])
def agent_page(name: str) -> str | Response:
    if name not in agent_config:
        abort(404)
    params = agent_config[name]
    error: str | None = None
    flash: str | None = None
    default_payload: str = '{"task": "..."}'

    if request.method == "POST":
        raw = request.form.get("payload", "")
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            error = f"invalid JSON: {e}"
            default_payload = raw
        else:
            db.session.add(Inbox(
                agent_uuid=params["uuid"],
                payload=json.dumps(parsed),
            ))
            db.session.commit()
            return redirect(url_for("agent_page", name=name, ok=1))

    if request.args.get("ok"):
        flash = "enqueued"

    inbox = (
        db.session.query(Inbox)
        .filter_by(agent_uuid=params["uuid"])
        .order_by(Inbox.id.asc())
        .all()
    )
    journal = (
        db.session.query(Journal)
        .filter_by(agent_uuid=params["uuid"])
        .order_by(Journal.id.desc())
        .limit(20)
        .all()
    )
    return render_template_string(
        AGENT_TEMPLATE,
        name=name,
        params=params,
        inbox=inbox,
        journal=journal,
        error=error,
        flash=flash,
        default_payload=default_payload,
    )
