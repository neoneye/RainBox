from collections.abc import Mapping
from typing import Any
from uuid import UUID

from flask import Response, abort, redirect, render_template_string, request, url_for

from agent_config import agent_config
from db import (
    get_model_group,
    get_model_group_member_uuids,
    list_agent_model_bindings,
    list_model_groups,
    set_agent_model_binding,
)

from .core import app


AGENT_MODELS_TEMPLATE: str = """
<!doctype html>
<title>Agent models &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  table.agents{border-collapse:collapse;width:100%;max-width:900px}
  table.agents th,table.agents td{border:1px solid #ddd;padding:6px 10px;text-align:left;vertical-align:top}
  table.agents th{background:#f0f0f0}
  table.agents .role{font-weight:600}
  table.agents .desc{color:#555;font-size:90%}
  table.agents code{background:#eee;padding:1px 4px;border-radius:3px;font-family:ui-monospace,monospace;font-size:90%}
  .current{font-size:90%}
  .current .unset{color:#888;font-style:italic}
  .current a{color:#0653a8;text-decoration:none}
  .bind-form{display:flex;gap:0.4em;align-items:center}
  .bind-form select{padding:0.3em}
  .bind-form button{padding:0.3em 0.9em;border:none;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
  .bind-form button:hover{background:#1d4ed8}
  .ok{color:#080}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Agent models</h1>
<p>Agents and their pipeline topology live in <code>agent_config.py</code>. This page only
controls which <b>model group</b> each agent runs &mdash; a prioritized fallback list
(try the first model, fall back to the next on failure), e.g. a fast/low-quality
group vs a slow/high-quality one. Stored in <code>agent_model_binding</code>, editable
without code changes. Manage the groups themselves on the
<a href="{{ url_for('modelgroups_page') }}">Model groups</a> page.</p>
{% if saved %}<p class="ok">Saved.</p>{% endif %}

<table class="agents">
  <tr><th>Agent</th><th>Model group</th><th>Change to</th></tr>
  {% for a in agents %}
  <tr>
    <td>
      <div class="role">{{ a.name }}</div>
      <div class="desc">{{ a.description }}</div>
      {% if a.requires_function_calling %}<div class="desc"><b>requires function calling</b></div>{% endif %}
      {% if a.requires_structured_output %}<div class="desc"><b>requires structured output</b></div>{% endif %}
      {% if a.excludes_structured_output %}<div class="desc"><b>requires structured output off</b></div>{% endif %}
      <code>{{ a.uuid }}</code>
    </td>
    <td class="current">
      {% if a.group %}
        <a href="{{ url_for('modelgroups_page', id=a.current_uuid) }}">{{ a.group.name }}</a>
        <br><small>{{ a.member_count }} model(s)</small>
      {% elif a.current_uuid %}
        <span class="unset">group missing</span>
      {% else %}
        <span class="unset">none assigned</span>
      {% endif %}
    </td>
    <td>
      <form class="bind-form" method="post">
        <input type="hidden" name="agent_uuid" value="{{ a.uuid }}">
        <select name="model_group">
          <option value="" {% if not a.current_uuid %}selected{% endif %}>&mdash; none &mdash;</option>
          {% for g in a.groups %}
          <option value="{{ g.uuid }}" {% if g.uuid == a.current_uuid %}selected{% endif %}>{{ g.label }}</option>
          {% endfor %}
        </select>
        <button type="submit">Save</button>
        {% if a.requires_function_calling %}<br><small class="desc">only groups that require function calling</small>{% endif %}
        {% if a.requires_structured_output %}<br><small class="desc">only groups that require structured output</small>{% endif %}
        {% if a.excludes_structured_output %}<br><small class="desc">only groups that exclude structured output</small>{% endif %}
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
</div>
"""


def _group_options() -> list[dict]:
    """Selectable model groups (alphabetical), labeled with their member count
    and tagged with the capability constraints they enforce."""
    options: list[dict] = []
    for group in list_model_groups():
        count = len(get_model_group_member_uuids(group.uuid))
        options.append(
            {
                "uuid": str(group.uuid),
                "label": f"{group.name} ({count} models)",
                "requires_function_calling": group.requires_function_calling,
                "requires_structured_output": group.requires_structured_output,
                "structured_output_constraint": group.structured_output_constraint,
            }
        )
    return options


def _agent_group_options(entry: Mapping[str, Any], groups: list[dict]) -> list[dict]:
    """Filter `groups` (option dicts from _group_options) to those an agent may
    bind to, given the capability constraints it declares in agent_config. The
    constraints are independent: a group may be required to support function
    calling, required to support structured output, or (for unstructured agents)
    required to forbid structured output."""
    requires_fc = bool(entry.get("requires_function_calling"))
    requires_struct = bool(entry.get("requires_structured_output"))
    excludes_struct = bool(entry.get("excludes_structured_output"))
    return [
        o
        for o in groups
        if (not requires_fc or o["requires_function_calling"])
        and (not requires_struct or o["requires_structured_output"])
        and (not excludes_struct or o["structured_output_constraint"] == "must_not_have")
    ]


@app.route("/agent_models", methods=["GET", "POST"])
def agent_models_page() -> str | Response:
    if request.method == "POST":
        try:
            agent_uuid = UUID(request.form.get("agent_uuid", ""))
        except ValueError:
            abort(400)
        ref_raw = request.form.get("model_group", "")
        group_uuid: UUID | None = None
        if ref_raw:
            try:
                group_uuid = UUID(ref_raw)
            except ValueError:
                abort(400)
        # An agent may only bind to a group that enforces each capability the
        # agent requires (function calling and/or structured output).
        entry = next(
            (e for e in agent_config.values() if e["uuid"] == agent_uuid), None
        )
        if group_uuid is not None and entry:
            g = get_model_group(group_uuid)
            if entry.get("requires_function_calling") and (
                g is None or not g.requires_function_calling
            ):
                abort(400, "this agent requires a group that requires function calling")
            if entry.get("requires_structured_output") and (
                g is None or not g.requires_structured_output
            ):
                abort(400, "this agent requires a group that requires structured output")
            if entry.get("excludes_structured_output") and (
                g is None or g.structured_output_constraint != "must_not_have"
            ):
                abort(400, "this agent requires a group that excludes structured output")
        set_agent_model_binding(agent_uuid, group_uuid)
        return redirect(url_for("agent_models_page", saved=1))

    bindings = list_agent_model_bindings()
    groups = _group_options()
    agents = []
    for name, entry in agent_config.items():
        binding = bindings.get(entry["uuid"])
        ref = binding.model_group_uuid if binding else None
        group = get_model_group(ref) if ref else None
        requires_fc = bool(entry.get("requires_function_calling"))
        requires_struct = bool(entry.get("requires_structured_output"))
        excludes_struct = bool(entry.get("excludes_structured_output"))
        agent_groups = _agent_group_options(entry, groups)
        agents.append(
            {
                "name": name,
                "description": entry["description"],
                "uuid": str(entry["uuid"]),
                "current_uuid": str(ref) if ref else "",
                "group": group,
                "member_count": len(get_model_group_member_uuids(group.uuid)) if group else 0,
                "requires_function_calling": requires_fc,
                "requires_structured_output": requires_struct,
                "excludes_structured_output": excludes_struct,
                "groups": agent_groups,
            }
        )
    return render_template_string(
        AGENT_MODELS_TEMPLATE,
        agents=agents,
        saved=bool(request.args.get("saved")),
    )
