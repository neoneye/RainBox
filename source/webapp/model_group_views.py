import json
from uuid import UUID

from flask import (
    Response,
    abort,
    render_template_string,
    request,
)

from db import (
    args_reasoning_on,
    create_model_group,
    delete_model_group,
    get_model_group,
    get_model_group_member_uuids,
    list_model_configs_with_overrides,
    list_model_groups,
    rename_model_group,
    resolve_member,
    set_model_group_members,
)

from .core import app


MODELGROUPS_TEMPLATE: str = """
<!doctype html>
<title>Model groups &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{padding:0.6em 1em;border-bottom:1px solid #ddd;background:#fafafa}
  header a{margin-right:1em}
  .split{display:grid;grid-template-columns:300px 1fr;grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .pane{overflow:auto;min-height:0;padding:0.8em 1em}
  .pane.left{border-right:1px solid #ddd;background:#fbfbfb}
  h2.left-h{margin:0 0 0.5em 0}
  ul.group-list{list-style:none;margin:0;padding:0}
  ul.group-list li{margin:0.1em 0}
  ul.group-list a{display:block;padding:0.3em 0.5em;border-radius:3px;text-decoration:none;color:inherit;cursor:pointer}
  ul.group-list a:hover{background:#eef}
  ul.group-list a.selected{background:#dde7ff;font-weight:600}
  ul.group-list a.new-group{color:#0653a8}
  .empty{color:#888;font-style:italic}
  .muted{color:#888}
  table.models{border-collapse:collapse;margin:0.3em 0;width:100%}
  table.models th,table.models td{border:1px solid #ddd;padding:4px 8px;text-align:left;font-size:90%;vertical-align:top}
  table.models th{background:#f0f0f0}
  table.models td.num{text-align:right;color:#888;width:2em}
  table.models td.link{text-align:right;white-space:nowrap}
  table.models a{text-decoration:none;color:#0653a8}
  table.models small{color:#666}
  table.models .provider small{color:#1e40af}
  input#rename-field{font-size:1.3em;font-weight:600;width:60%;padding:0.2em 0.3em}
  button{cursor:pointer}
  .danger{color:#a00;border:1px solid #a00;background:#fff;padding:0.3em 0.8em;border-radius:3px}
  .section{margin:1.2em 0}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>

<div class="split">
  <div class="pane left">
    <h2 class="left-h">Model groups</h2>
    <ul class="group-list" id="group-list"></ul>
  </div>
  <div class="pane right">
    <div id="group-detail"></div>
  </div>
</div>

<script>
// Group data is persisted in the database via the JSON endpoints below.
// The only UI state is which group is selected — kept in the URL (?id=...)
// so the view is portable across browsers.
const PRIORITIES_URL = '{{ url_for("modelgrouppriorities_page") }}';
const MODELS_URL = '{{ url_for("models_page") }}';
const DATA_URL = '{{ url_for("modelgroups_data") }}';
const CREATE_URL = '{{ url_for("modelgroups_create") }}';
const RENAME_URL = '{{ url_for("modelgroups_rename") }}';
const DELETE_URL = '{{ url_for("modelgroups_delete") }}';

let groups = [];  // [{uuid, name, function_calling_constraint, structured_output_constraint, members:[...]}]
let selectedId = new URLSearchParams(window.location.search).get('id') || null;
let creating = false;  // showing the "new group" form in the right pane

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}
// Friendly name for a provider id. Falls back to the raw id for unknown
// providers — still legible, just not as pretty.
function providerLabel(id) {
  if (id === 'lm_studio') return 'LM Studio';
  if (id === 'jan') return 'Jan';
  if (id === 'ollama') return 'Ollama';
  return id || '';
}
// Render a model member as a 3-line cell:
//   provider
//   model_name
//   override-display-name OR "(base config)"
// Used by the group detail panel and the priority editor so both views
// stay in sync.
function renderModelCell(m) {
  const providerLine = m.provider
    ? `<div class="provider"><small>${escapeHtml(providerLabel(m.provider))}</small></div>`
    : '';
  const subLine = m.kind === 'override'
    ? `<div><small>${escapeHtml(m.display_name || '(no name)')}</small></div>`
    : '<div><small>(base config)</small></div>';
  const unavail = m.available === false
    ? ' <span title="this model is no longer available in its provider" style="background:#fdd;color:#900;font-size:75%;padding:0 0.4em;border-radius:0.4em;vertical-align:0.08em">unavailable</span>'
    : '';
  return providerLine + `<div><b>${escapeHtml(m.model_display_name || m.model_name)}</b>${unavail}</div>` + subLine;
}
// Human-readable label for each tri-state capability constraint value.
const CONSTRAINT_LABELS = {
  dont_care: "Don't care",
  must_have: 'Must have',
  must_not_have: 'Must not have',
};
// Render one capability as a labelled set of three radios (Don't care is the
// default). `key` namespaces the radio group so multiple fields coexist.
function capabilityField(key, title, help) {
  const radios = Object.keys(CONSTRAINT_LABELS).map((val, i) =>
    `<label style="margin-right:1em"><input type="radio" name="new-${key}" value="${val}"${i === 0 ? ' checked' : ''}> ${escapeHtml(CONSTRAINT_LABELS[val])}</label>`
  ).join('');
  return `<div style="margin:0.5em 0">
    <div>${escapeHtml(title)}</div>
    <div style="margin:0.2em 0">${radios}</div>
    <small class="muted" style="display:block">${escapeHtml(help)}</small>
  </div>`;
}
// Short explanatory clause for a constraint in the read-only detail panel.
function constraintNote(constraint, label) {
  if (constraint === 'must_have') return ' &mdash; members must support ' + escapeHtml(label);
  if (constraint === 'must_not_have') return ' &mdash; members must not support ' + escapeHtml(label);
  return '';
}
function selectedGroup() {
  return groups.find(g => g.uuid === selectedId) || null;
}
function setSelected(uuid) {
  selectedId = uuid || null;
  // Reflect the selection in the URL so it's shareable / portable.
  const url = selectedId
    ? (window.location.pathname + '?id=' + encodeURIComponent(selectedId))
    : window.location.pathname;
  history.replaceState({}, '', url);
}
async function postJson(url, payload) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}
async function refresh() {
  const r = await fetch(DATA_URL);
  if (!r.ok) throw new Error(DATA_URL + ' -> ' + r.status);
  groups = (await r.json()).groups;  // already sorted by name server-side
  // Default selection: if nothing valid is selected (first visit, or the
  // selected group was deleted), fall back to the first group alphabetically.
  if (!groups.find(g => g.uuid === selectedId)) {
    setSelected(groups.length ? groups[0].uuid : null);
  }
  render();
}

function renderLeft() {
  const root = document.getElementById('group-list');
  // Real hrefs so CMD/Ctrl/middle click opens the group in a new tab; a plain
  // click is intercepted by the list's click delegate below.
  const items = groups.map(g =>
    `<li><a class="${g.uuid === selectedId ? 'selected' : ''}" data-id="${escapeHtml(g.uuid)}" href="?id=${encodeURIComponent(g.uuid)}">${escapeHtml(g.name)}</a></li>`
  ).join('');
  root.innerHTML = items +
    `<li><a class="new-group" id="new-group-btn">+ new group</a></li>`;
}

function renderRight() {
  const root = document.getElementById('group-detail');
  if (creating) {
    root.innerHTML = `
      <div class="section">
        <h3>New model group</h3>
        <p><label>Name:<br><input type="text" id="new-name" style="width:60%;padding:0.2em 0.3em"></label></p>
        <p style="margin-bottom:0.2em"><b style="color:#555">Capability constraints</b></p>
        ${capabilityField('fc', 'Function calling',
            'Whether members must support function calling.')}
        ${capabilityField('struct', 'Structured output',
            'Whether members must support structured output. Most agents need it; require it off for plain-text reasoning agents.')}
        ${capabilityField('reasoning', 'Reasoning',
            'Whether members must have reasoning (thinking) turned on.')}
        <p><button id="create-btn">Create</button> <button id="cancel-create-btn">Cancel</button></p>
      </div>`;
    const nm = document.getElementById('new-name');
    if (nm) nm.focus();
    return;
  }
  const g = selectedGroup();
  if (!g) {
    root.innerHTML = '<p class="empty">Select a group on the left, or create one with + new group.</p>';
    return;
  }
  const models = g.members || [];
  let modelsHtml;
  if (models.length === 0) {
    modelsHtml = '<p class="empty">(no models yet — use Edit priority list)</p>';
  } else {
    const rows = models.map((m, i) => {
      const href = MODELS_URL + '?id=' + encodeURIComponent(m.uuid);
      return `<tr>
        <td class="num">${i + 1}</td>
        <td>${renderModelCell(m)}</td>
        <td class="link"><a href="${href}" target="_blank" rel="noopener">model details &rarr;</a></td>
      </tr>`;
    }).join('');
    modelsHtml = `<table class="models">
      <thead><tr><th class="num">#</th><th>Model</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }
  root.innerHTML = `
    <div class="section">
      <input type="text" id="rename-field" value="${escapeHtml(g.name)}">
      <button id="rename-btn">Rename</button>
    </div>
    <div class="section">
      <p class="muted">Function calling: <b>${escapeHtml(CONSTRAINT_LABELS[g.function_calling_constraint] || g.function_calling_constraint)}</b>${constraintNote(g.function_calling_constraint, 'function calling')}</p>
      <p class="muted">Structured output: <b>${escapeHtml(CONSTRAINT_LABELS[g.structured_output_constraint] || g.structured_output_constraint)}</b>${constraintNote(g.structured_output_constraint, 'structured output')}</p>
      <p class="muted">Reasoning: <b>${escapeHtml(CONSTRAINT_LABELS[g.reasoning_constraint] || g.reasoning_constraint)}</b>${constraintNote(g.reasoning_constraint, 'reasoning')}</p>
    </div>
    <div class="section">
      <h3>Prioritized models</h3>
      ${modelsHtml}
      <p><button id="edit-btn">Edit priority list</button></p>
    </div>
    <div class="section">
      <button id="delete-btn" class="danger">Delete group</button>
    </div>
  `;
}

function render() { renderLeft(); renderRight(); }

document.getElementById('group-list').addEventListener('click', async (ev) => {
  const newBtn = ev.target.closest('#new-group-btn');
  if (newBtn) {
    creating = true;
    setSelected(null);
    render();
    return;
  }
  const row = ev.target.closest('a[data-id]');
  if (row) {
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;  // browser handles new tab/window
    ev.preventDefault();
    creating = false;
    setSelected(row.dataset.id);
    render();
  }
});

async function doCreate() {
  const name = (document.getElementById('new-name').value || '').trim();
  if (!name) { alert('name cannot be empty'); return; }
  const radioVal = (key) => {
    const el = document.querySelector(`input[name="new-${key}"]:checked`);
    return el ? el.value : 'dont_care';
  };
  const fc = radioVal('fc');
  const struct = radioVal('struct');
  const reasoning = radioVal('reasoning');
  try {
    const res = await postJson(CREATE_URL, {name, function_calling_constraint: fc, structured_output_constraint: struct, reasoning_constraint: reasoning});
    creating = false;
    setSelected(res.uuid);
    await refresh();
  } catch (e) { alert(e); }
}

async function doRename() {
  const g = selectedGroup();
  if (!g) return;
  const field = document.getElementById('rename-field');
  const name = (field.value || '').trim();
  if (!name) { alert('name cannot be empty'); return; }
  try {
    await postJson(RENAME_URL, {uuid: g.uuid, name});
    await refresh();
  } catch (e) { alert(e); }
}

document.getElementById('group-detail').addEventListener('keydown', (ev) => {
  if (ev.target.id === 'rename-field' && ev.key === 'Enter') {
    ev.preventDefault();
    doRename();
  }
  if (ev.target.id === 'new-name' && ev.key === 'Enter') {
    ev.preventDefault();
    doCreate();
  }
});

document.getElementById('group-detail').addEventListener('click', async (ev) => {
  // The "new group" form has no selected group yet, so handle it first.
  if (ev.target.closest('#create-btn')) { doCreate(); return; }
  if (ev.target.closest('#cancel-create-btn')) { creating = false; render(); return; }
  const g = selectedGroup();
  if (!g) return;
  if (ev.target.closest('#rename-btn')) {
    doRename();
    return;
  }
  if (ev.target.closest('#edit-btn')) {
    // The priorities page edits this group's members directly in the DB; the
    // group uuid travels in the URL.
    window.location = PRIORITIES_URL + '?id=' + encodeURIComponent(g.uuid);
    return;
  }
  if (ev.target.closest('#delete-btn')) {
    if (!window.confirm('Delete group "' + g.name + '"? This cannot be undone.')) return;
    try {
      await postJson(DELETE_URL, {uuid: g.uuid});
      if (selectedId === g.uuid) setSelected(null);
      await refresh();
    } catch (e) { alert(e); }
    return;
  }
});

refresh().catch(e => console.error(e));
</script>
"""


@app.route("/modelgroups")
def modelgroups_page() -> str:
    return render_template_string(MODELGROUPS_TEMPLATE)


@app.route("/modelgroups/data")
def modelgroups_data() -> Response:
    out = []
    for g in list_model_groups():
        members = [resolve_member(mu) for mu in get_model_group_member_uuids(g.uuid)]
        out.append(
            {
                "uuid": str(g.uuid),
                "name": g.name,
                "function_calling_constraint": g.function_calling_constraint,
                "structured_output_constraint": g.structured_output_constraint,
                "reasoning_constraint": g.reasoning_constraint,
                "members": members,
            }
        )
    return app.response_class(json.dumps({"groups": out}), mimetype="application/json")


@app.route("/modelgroups/create", methods=["POST"])
def modelgroups_create() -> Response:
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "name required")
    fc = data.get("function_calling_constraint") or "dont_care"
    struct = data.get("structured_output_constraint") or "dont_care"
    reasoning = data.get("reasoning_constraint") or "dont_care"
    try:
        g = create_model_group(
            name,
            function_calling_constraint=fc,
            structured_output_constraint=struct,
            reasoning_constraint=reasoning,
        )
    except ValueError as e:
        abort(400, str(e))
    return app.response_class(json.dumps({"uuid": str(g.uuid)}), mimetype="application/json")


@app.route("/modelgroups/rename", methods=["POST"])
def modelgroups_rename() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        g_uuid = UUID(data.get("uuid", ""))
    except (ValueError, TypeError):
        abort(400)
    name = (data.get("name") or "").strip()
    if not name:
        abort(400, "name required")
    try:
        rename_model_group(g_uuid, name)
    except LookupError:
        abort(404)
    return app.response_class(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/modelgroups/delete", methods=["POST"])
def modelgroups_delete() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        g_uuid = UUID(data.get("uuid", ""))
    except (ValueError, TypeError):
        abort(400)
    try:
        delete_model_group(g_uuid)
    except LookupError:
        abort(404)
    return app.response_class(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/modelgroups/members", methods=["POST"])
def modelgroups_members() -> Response:
    data = request.get_json(silent=True) or {}
    try:
        g_uuid = UUID(data.get("uuid", ""))
    except (ValueError, TypeError):
        abort(400)
    if get_model_group(g_uuid) is None:
        abort(404)
    member_uuids: list[UUID] = []
    for s in data.get("member_uuids") or []:
        try:
            member_uuids.append(UUID(s))
        except (ValueError, TypeError):
            continue
    try:
        set_model_group_members(g_uuid, member_uuids)
    except ValueError as e:
        abort(400, str(e))
    return app.response_class(json.dumps({"ok": True}), mimetype="application/json")


MODELGROUPPRIORITIES_TEMPLATE: str = """
<!doctype html>
<title>Model group priorities &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  header{padding:0.6em 1em;border-bottom:1px solid #ddd;background:#fafafa}
  header a{margin-right:1em}
  .split{display:grid;grid-template-columns:420px 1fr;height:calc(100vh - 3em)}
  .pane{overflow:auto;padding:0.8em 1em}
  .pane.left{border-right:1px solid #ddd;background:#fbfbfb}
  ul.tree{list-style:none;margin:0;padding:0}
  ul.tree ul{list-style:none;margin:0;padding:0 0 0 1.2em}
  ul.tree li{margin:0.15em 0;line-height:1.3}
  ul.tree .row{display:flex;align-items:center;gap:0.3em}
  ul.tree .row > .label{flex:1;min-width:0;padding:0.2em 0.4em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pp-provider-badge{display:inline-block;font-size:72%;padding:0 0.4em;border-radius:0.4em;background:#dbeafe;color:#1e40af;vertical-align:0.08em;margin-right:0.25em}
  .empty{color:#888;font-style:italic}
  button.select-btn,button.selected-btn,button.move-btn,button.deselect-btn{font-size:80%;padding:0.1em 0.55em;border:1px solid #bbb;background:#fff;border-radius:3px;cursor:pointer}
  button.selected-btn{color:#888;border-color:#ddd;background:#f5f5f5;cursor:default}
  button:disabled{opacity:0.4;cursor:not-allowed}
  table.priority-list{border-collapse:collapse;width:100%;margin-top:0.3em}
  table.priority-list th,table.priority-list td{border:1px solid #ddd;padding:5px 8px;text-align:left;font-size:90%;vertical-align:top}
  table.priority-list th{background:#f0f0f0}
  table.priority-list td.num{text-align:right;color:#888;width:2em}
  table.priority-list td.link,table.priority-list td.actions{white-space:nowrap}
  table.priority-list small{color:#666}
  table.priority-list .provider small{color:#1e40af}
  table.priority-list small{color:#666}
  table.priority-list a{text-decoration:none;color:#0653a8}
  table.priority-list .move-btn{font-size:130%;line-height:1;padding:0.3em 0.8em;min-width:2.4em}
  table.priority-list .deselect-btn{padding:0.35em 0.8em}
  h2.right-h{margin:0 0 0.3em 0}
  .muted-explain{margin:0 0 0.8em 0;color:#555;font-size:90%}
</style>
<header>
  {% if editing_group_id %}
  <a href="{{ url_for('modelgroups_page', id=editing_group_id) }}"><b>&times; Close</b></a>
  <span style="margin-left:0.5em">Model group: <b>{{ editing_group_name }}</b></span>
  {% else %}
  <a href="{{ url_for('index') }}">&larr; back</a>
  <a href="{{ url_for('models_page') }}">models</a>
  <a href="{{ url_for('admin.index') }}">admin</a>
  {% endif %}
</header>

<div class="split">
  <div class="pane left">
    <ul class="tree">
      {% for cfg, overrides in tree %}
      <li>
        <span class="row">
          <span class="label"><span class="pp-provider-badge">{% if cfg.provider == 'lm_studio' %}LM Studio{% elif cfg.provider == 'jan' %}Jan{% elif cfg.provider == 'ollama' %}Ollama{% else %}{{ cfg.provider }}{% endif %}</span><b>{{ cfg.effective_display_name }}</b></span>
          <button class="select-btn" data-uuid="{{ cfg.uuid }}" data-kind="config" data-provider="{{ cfg.provider }}" data-model-name="{{ cfg.model_name }}" data-model-display-name="{{ cfg.effective_display_name }}" data-display-name="" data-fc="{{ '1' if (cfg.uuid|string) in fc_uuids else '0' }}" data-struct="{{ '1' if (cfg.uuid|string) in struct_uuids else '0' }}" data-reasoning="{{ '1' if (cfg.uuid|string) in reasoning_uuids else '0' }}">Select</button>
        </span>
        <ul>
          {% for ov in overrides %}
          <li>
            <span class="row">
              <span class="label">{% if ov.effective_display_name %}{{ ov.effective_display_name }}{% else %}<span class="empty">(no name)</span>{% endif %}</span>
              <button class="select-btn" data-uuid="{{ ov.uuid }}" data-kind="override" data-provider="{{ cfg.provider }}" data-model-name="{{ cfg.model_name }}" data-model-display-name="{{ cfg.effective_display_name }}" data-display-name="{{ ov.effective_display_name }}" data-fc="{{ '1' if (ov.uuid|string) in fc_uuids else '0' }}" data-struct="{{ '1' if (ov.uuid|string) in struct_uuids else '0' }}" data-reasoning="{{ '1' if (ov.uuid|string) in reasoning_uuids else '0' }}">Select</button>
            </span>
          </li>
          {% endfor %}
        </ul>
      </li>
      {% else %}
      <li class="empty">no model configs yet</li>
      {% endfor %}
    </ul>
  </div>

  <div class="pane right">
    <h2 class="right-h">Priority list</h2>
    <p class="muted-explain">Fallback order. The top model is tried first; if it fails, the next is tried, and so on until all are exhausted.</p>
    {% if function_calling_constraint == 'must_have' %}
    <p class="muted-explain"><b>Function calling — must have:</b> members must support function calling; models that don't are disabled.</p>
    {% elif function_calling_constraint == 'must_not_have' %}
    <p class="muted-explain"><b>Function calling — must not have:</b> members must not support function calling; models that do are disabled.</p>
    {% endif %}
    {% if structured_output_constraint == 'must_have' %}
    <p class="muted-explain"><b>Structured output — must have:</b> members must support structured output; models that don't are disabled.</p>
    {% elif structured_output_constraint == 'must_not_have' %}
    <p class="muted-explain"><b>Structured output — must not have:</b> members must not support structured output; models that do are disabled.</p>
    {% endif %}
    {% if reasoning_constraint == 'must_have' %}
    <p class="muted-explain"><b>Reasoning — must have:</b> members must have reasoning on; models that don't are disabled.</p>
    {% elif reasoning_constraint == 'must_not_have' %}
    <p class="muted-explain"><b>Reasoning — must not have:</b> members must not have reasoning on; models that do are disabled.</p>
    {% endif %}
    <div id="priority-panel"></div>
  </div>
</div>

<script>
// This page edits ONE model group's ordered member list, identified by
// GROUP_ID (?id= in the URL). The list is loaded from and saved to the
// database. Each Select / Deselect / reorder persists immediately via
// POST, so closing back to /modelgroups shows the result.
const MODELS_URL = '{{ url_for("models_page") }}';
const MODELGROUPS_URL = '{{ url_for("modelgroups_page") }}';
const DATA_URL = '{{ url_for("modelgroups_data") }}';
const MEMBERS_URL = '{{ url_for("modelgroups_members") }}';
const GROUP_ID = {{ editing_group_id|tojson }};
const FC_CONSTRAINT = {{ function_calling_constraint|tojson }};
const STRUCT_CONSTRAINT = {{ structured_output_constraint|tojson }};
const REASONING_CONSTRAINT = {{ reasoning_constraint|tojson }};

let selected = [];  // [{uuid,kind,model_name,display_name}]

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}
// Friendly name for a provider id. Falls back to the raw id for unknown
// providers — still legible, just not as pretty.
function providerLabel(id) {
  if (id === 'lm_studio') return 'LM Studio';
  if (id === 'jan') return 'Jan';
  if (id === 'ollama') return 'Ollama';
  return id || '';
}
// Render a model member as a 3-line cell:
//   provider
//   model_name
//   override-display-name OR "(base config)"
// Used by the group detail panel and the priority editor so both views
// stay in sync.
function renderModelCell(m) {
  const providerLine = m.provider
    ? `<div class="provider"><small>${escapeHtml(providerLabel(m.provider))}</small></div>`
    : '';
  const subLine = m.kind === 'override'
    ? `<div><small>${escapeHtml(m.display_name || '(no name)')}</small></div>`
    : '<div><small>(base config)</small></div>';
  const unavail = m.available === false
    ? ' <span title="this model is no longer available in its provider" style="background:#fdd;color:#900;font-size:75%;padding:0 0.4em;border-radius:0.4em;vertical-align:0.08em">unavailable</span>'
    : '';
  return providerLine + `<div><b>${escapeHtml(m.model_display_name || m.model_name)}</b>${unavail}</div>` + subLine;
}

async function loadFromDb() {
  if (!GROUP_ID) { selected = []; return; }
  const r = await fetch(DATA_URL);
  if (!r.ok) throw new Error(DATA_URL + ' -> ' + r.status);
  const groups = (await r.json()).groups;
  const g = groups.find(x => x.uuid === GROUP_ID);
  selected = g ? (g.members || []) : [];
}

async function save() {
  if (!GROUP_ID) return;
  const r = await fetch(MEMBERS_URL, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({uuid: GROUP_ID, member_uuids: selected.map(s => s.uuid)}),
  });
  if (!r.ok) throw new Error(MEMBERS_URL + ' -> ' + r.status);
}

function renderSelectButtons() {
  const set = new Set(selected.map(s => s.uuid));
  // Returns a {text,title} block reason for a capability constraint, or null if
  // the model satisfies it. `has` is whether the model supports the capability.
  function blockReason(constraint, has, label, onWord, offWord) {
    if (constraint === 'must_have' && !has) {
      return {text: onWord, title: 'This group requires members that support ' + label};
    }
    if (constraint === 'must_not_have' && has) {
      return {text: offWord, title: 'This group requires members that do not support ' + label};
    }
    return null;
  }
  document.querySelectorAll('button.select-btn').forEach(btn => {
    const block =
      blockReason(FC_CONSTRAINT, btn.dataset.fc === '1', 'function calling', 'No tools', 'Has tools') ||
      blockReason(STRUCT_CONSTRAINT, btn.dataset.struct === '1', 'structured output', 'No struct', 'Has struct') ||
      blockReason(REASONING_CONSTRAINT, btn.dataset.reasoning === '1', 'reasoning', 'No reasoning', 'Has reasoning');
    if (set.has(btn.dataset.uuid)) {
      btn.textContent = 'Selected';
      btn.classList.add('selected-btn');
      btn.disabled = true;
      btn.title = '';
    } else if (block) {
      btn.textContent = block.text;
      btn.classList.remove('selected-btn');
      btn.disabled = true;
      btn.title = block.title;
    } else {
      btn.textContent = 'Select';
      btn.classList.remove('selected-btn');
      btn.disabled = false;
      btn.title = '';
    }
  });
}

function renderPriorityPanel() {
  const root = document.getElementById('priority-panel');
  if (!GROUP_ID) {
    root.innerHTML = '<p class="empty">No model group. Open one from <a href="' + MODELGROUPS_URL + '">Model groups</a> and click "Edit priority list".</p>';
    return;
  }
  if (selected.length === 0) {
    root.innerHTML = '<p class="empty">(no selected models yet — click <b>Select</b> on a row in the left panel)</p>';
    return;
  }
  const rows = selected.map((s, i) => {
    const atTop = i === 0;
    const atBottom = i === selected.length - 1;
    const href = MODELS_URL + '?id=' + encodeURIComponent(s.uuid);
    return `<tr>
      <td class="num">${i + 1}</td>
      <td>${renderModelCell(s)}</td>
      <td class="actions">
        <button class="move-btn" data-action="up" data-uuid="${escapeHtml(s.uuid)}" ${atTop ? 'disabled' : ''}>&uarr;</button>
        <button class="move-btn" data-action="down" data-uuid="${escapeHtml(s.uuid)}" ${atBottom ? 'disabled' : ''}>&darr;</button>
        <button class="deselect-btn" data-uuid="${escapeHtml(s.uuid)}">Deselect</button>
      </td>
      <td class="link"><a href="${href}" target="_blank" rel="noopener">model details &rarr;</a></td>
    </tr>`;
  }).join('');
  root.innerHTML = `<table class="priority-list">
    <thead><tr><th class="num">#</th><th>Model</th><th>Actions</th><th>Details</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function rerender() {
  renderSelectButtons();
  renderPriorityPanel();
}

async function persist() {
  try { await save(); } catch (e) { console.error(e); alert('Failed to save: ' + e); }
}

document.addEventListener('click', (ev) => {
  if (!GROUP_ID) return;
  const selectBtn = ev.target.closest('button.select-btn');
  if (selectBtn && !selectBtn.disabled) {
    const item = {
      uuid: selectBtn.dataset.uuid,
      kind: selectBtn.dataset.kind,
      provider: selectBtn.dataset.provider || '',
      model_name: selectBtn.dataset.modelName || '',
      model_display_name: selectBtn.dataset.modelDisplayName || '',
      display_name: selectBtn.dataset.displayName || '',
    };
    if (!selected.find(s => s.uuid === item.uuid)) {
      selected.push(item);
      rerender();
      persist();
    }
    return;
  }
  const move = ev.target.closest('button.move-btn');
  if (move && !move.disabled) {
    const uuid = move.dataset.uuid;
    const idx = selected.findIndex(s => s.uuid === uuid);
    if (idx < 0) return;
    if (move.dataset.action === 'up' && idx > 0) {
      [selected[idx - 1], selected[idx]] = [selected[idx], selected[idx - 1]];
    } else if (move.dataset.action === 'down' && idx < selected.length - 1) {
      [selected[idx], selected[idx + 1]] = [selected[idx + 1], selected[idx]];
    }
    rerender();
    persist();
    return;
  }
  const deselectBtn = ev.target.closest('button.deselect-btn');
  if (deselectBtn) {
    selected = selected.filter(s => s.uuid !== deselectBtn.dataset.uuid);
    rerender();
    persist();
    return;
  }
});

(async function init() {
  try { await loadFromDb(); } catch (e) { console.error(e); }
  rerender();
})();
</script>
"""


@app.route("/modelgrouppriorities")
def modelgrouppriorities_page() -> str:
    tree = [
        (cfg, overrides)
        for cfg, overrides in list_model_configs_with_overrides()
        if cfg.available
    ]
    # Which configs/overrides resolve to a function-calling model — used to
    # disable Select for non-function-calling models in a function-calling group.
    fc_uuids: list[str] = []
    struct_uuids: list[str] = []
    reasoning_uuids: list[str] = []
    for cfg, overrides in tree:
        if cfg.arguments.get("is_function_calling_model"):
            fc_uuids.append(str(cfg.uuid))
        if cfg.arguments.get("should_use_structured_outputs"):
            struct_uuids.append(str(cfg.uuid))
        if args_reasoning_on(cfg.arguments):
            reasoning_uuids.append(str(cfg.uuid))
        for ov in overrides:
            resolved = {**cfg.arguments, **ov.overrides}
            if resolved.get("is_function_calling_model"):
                fc_uuids.append(str(ov.uuid))
            if resolved.get("should_use_structured_outputs"):
                struct_uuids.append(str(ov.uuid))
            if args_reasoning_on(resolved):
                reasoning_uuids.append(str(ov.uuid))
    # When opened from /modelgroups via "Edit priority list", ?id carries the
    # group uuid; we use it to build the Close button's return URL, to swap
    # the nav for a single Close action, to show the group's name, and to apply
    # its function-calling membership constraint.
    editing_group_id = request.args.get("id") or ""
    editing_group_name = ""
    function_calling_constraint = "dont_care"
    structured_output_constraint = "dont_care"
    reasoning_constraint = "dont_care"
    if editing_group_id:
        try:
            g = get_model_group(UUID(editing_group_id))
        except (ValueError, TypeError):
            g = None
        editing_group_name = g.name if g is not None else "(unknown group)"
        if g is not None:
            function_calling_constraint = g.function_calling_constraint
            structured_output_constraint = g.structured_output_constraint
            reasoning_constraint = g.reasoning_constraint
    return render_template_string(
        MODELGROUPPRIORITIES_TEMPLATE,
        tree=tree,
        fc_uuids=fc_uuids,
        struct_uuids=struct_uuids,
        reasoning_uuids=reasoning_uuids,
        editing_group_id=editing_group_id,
        editing_group_name=editing_group_name,
        function_calling_constraint=function_calling_constraint,
        structured_output_constraint=structured_output_constraint,
        reasoning_constraint=reasoning_constraint,
    )
