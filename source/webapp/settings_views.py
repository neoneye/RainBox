"""The /settings page + its JSON API.

Operator-editable configuration backed by the `db_settings` registry (the source
of truth) and the `app_setting` table (persisted values). The page renders one
row per registry setting with its effective value, provenance (DB / env /
default), and a typed editor; secret settings are shown env-managed/read-only
(they are never persisted to the DB — see the threat model in docs/backup.md).

Writes go through `db.set_setting`, so the registry's coercion/validation runs and
a bad value (or an attempt to store a secret) is rejected with a 400 carrying the
error message. This is the editable counterpart to the read-only Flask-Admin
AppSetting view. See docs/proposals/2026-06-07-user-configuration-in-postgres.md.
"""
from flask import Response, jsonify, render_template_string, request

import db

from .core import app


def _setting_row(key: str) -> dict | None:
    """The all_settings() entry for one key (effective value + provenance,
    secrets redacted), or None if the key isn't in the registry."""
    return next((s for s in db.all_settings() if s["key"] == key), None)


SETTINGS_TEMPLATE = """
<!doctype html>
<title>Settings &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .pp-content{max-width:900px}
  h1{font-size:1.6rem;margin:0.2em 0 0.1em}
  .muted{color:#6b7280;font-size:0.85rem}
  .s-card{border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin:0 0 14px;background:#fff}
  .s-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
  .s-key{font-family:ui-monospace,monospace;font-weight:700;font-size:0.98rem}
  .s-type{font-size:0.72rem;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280;border:1px solid #e5e7eb;border-radius:4px;padding:1px 5px}
  .s-desc{color:#374151;font-size:0.9rem;margin:5px 0 9px}
  .s-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .s-val{font-family:ui-monospace,monospace;font-size:0.9rem;background:#f8fafc;border:1px solid #eef2f7;border-radius:6px;padding:3px 8px}
  .s-val.unset{font-family:inherit;color:#6b7280;background:none;border:none;padding:0;font-style:italic}
  button{padding:6px 14px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.88rem}
  button:hover{background:#1d4ed8}
  button.s-cancel{background:#6b7280}
  button.s-cancel:hover{background:#4b5563}
  button:disabled{opacity:0.45;cursor:not-allowed}
  button:disabled:hover{background:#2563eb}
  .badge{font-size:0.72rem;font-weight:600;border-radius:999px;padding:2px 9px}
  .badge.db{background:#dbeafe;color:#1e40af}
  .badge.env{background:#fef3c7;color:#92400e}
  .badge.default{background:#f1f5f9;color:#475569}
  .s-env{color:#6b7280;font-size:0.82rem;margin-top:6px}
  .s-secret{color:#92400e;font-size:0.85rem;font-weight:600}
  /* Edit overlay (mirrors the /cron edit modals). */
  .s-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:1500}
  .s-backdrop[hidden]{display:none}
  .s-modal{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1600;
    width:min(560px,92vw);max-height:90vh;overflow:auto;background:#fff;border-radius:10px;
    box-shadow:0 12px 40px rgba(0,0,0,0.3);padding:22px 24px}
  .s-modal[hidden]{display:none}
  .s-modal-title{font-weight:700;font-size:1.15rem;font-family:ui-monospace,monospace;margin:0 0 0.3em}
  .s-modal label{display:flex;flex-direction:column;gap:5px;font-weight:600;font-size:0.9rem;margin:0.7em 0}
  .s-modal input[type=text],.s-modal input[type=number],.s-modal select{font-family:inherit;font-size:0.95rem;font-weight:400;padding:6px 9px;border:1px solid #ccc;border-radius:6px;width:100%;box-sizing:border-box}
  .s-modal .brow{display:flex;gap:10px;align-items:center;margin-top:1em}
  .s-err{color:#b91c1c;font-size:0.85rem;min-height:1.2em;margin-top:0.4em}
  .s-legend{font-size:0.82rem;color:#6b7280;line-height:1.9;margin:0 0 1.2em;
    border:1px solid #e5e7eb;border-radius:8px;padding:9px 12px;background:#fbfbfb}
  .s-legend .badge{margin:0 2px}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Settings</h1>
<p class="muted">Operator configuration stored in Postgres. Each value resolves
in priority order: <b>DB &rarr; environment&nbsp;variable &rarr; built-in
default</b>. Click <b>Edit</b> to change it; clearing the field (or choosing
&ldquo;unset&rdquo;) drops the DB value and falls back. Secret settings are
environment-managed and never stored here.</p>

<div class="s-legend">
  Each value shows where it currently comes from:
  <span class="badge db">from db</span> stored in the database (overrides the rest) &middot;
  <span class="badge env">from env</span> no DB value &mdash; using an environment variable &middot;
  <span class="badge default">from default</span> no DB value or env var &mdash; using the built-in default
</div>

<div id="s-list"></div>
</div>

<!-- Edit overlay -->
<div id="s-backdrop" class="s-backdrop" hidden></div>
<div id="s-modal" class="s-modal" hidden>
  <div class="s-modal-title" id="s-modal-key"></div>
  <div class="muted" id="s-modal-desc"></div>
  <label id="s-modal-field"></label>
  <div class="s-env" id="s-modal-current"></div>
  <div class="s-err" id="s-modal-err"></div>
  <div class="brow">
    <button id="s-save" disabled>Save</button>
    <button class="s-cancel" id="s-cancel">Cancel</button>
  </div>
</div>

<script>
const SETTINGS = {{ settings_json|safe }};

const SOURCE_HELP = {
  db: 'Stored in the database — overrides the environment variable and the default.',
  env: 'No value in the database — using the environment variable fallback.',
  default: 'No database value or environment variable — using the built-in default.',
};
function badge(source){
  return '<span class="badge ' + source + '" title="' + (SOURCE_HELP[source] || '')
    + '">from ' + source + '</span>';
}
function escapeHtml(s){
  return (s == null ? '' : String(s)).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
// Effective value for display ("(unset)" when empty/null).
function displayValue(s){
  if (s.value === null || s.value === '') return '<span class="s-val unset">(unset)</span>';
  return '<span class="s-val">' + escapeHtml(String(s.value)) + '</span>';
}

// ---- card list -------------------------------------------------------------
function render(){
  const list = document.getElementById('s-list');
  list.innerHTML = '';
  SETTINGS.forEach(s => {
    const card = document.createElement('div');
    card.className = 's-card';
    let body;
    if (s.secret){
      body = '<div class="s-row"><span class="s-secret">&#128274; environment-managed (read-only)</span> '
        + badge(s.source) + '</div><div class="s-env">value: ' + escapeHtml(s.value) + '</div>';
    } else {
      body = '<div class="s-row">' + displayValue(s) + ' ' + badge(s.source)
        + ' <button data-edit="' + escapeHtml(s.key) + '">Edit</button>'
        + (s.key === 'customize.dir'
            ? ' <button data-repopulate>Repopulate Q&A memory</button>'
              + ' <span class="s-env" data-repopulate-result></span>'
            : '')
        + '</div>'
        + (s.env ? '<div class="s-env">env fallback: <code>' + escapeHtml(s.env) + '</code></div>' : '');
    }
    card.innerHTML =
      '<div class="s-head"><span class="s-key">' + escapeHtml(s.key) + '</span>'
      + '<span class="s-type">' + escapeHtml(s.value_type) + '</span></div>'
      + (s.description ? '<div class="s-desc">' + escapeHtml(s.description) + '</div>' : '')
      + body;
    list.appendChild(card);
  });
  list.querySelectorAll('[data-edit]').forEach(btn =>
    btn.addEventListener('click', () => openEdit(btn.getAttribute('data-edit'))));
  list.querySelectorAll('[data-repopulate]').forEach(btn =>
    btn.addEventListener('click', async () => {
      const out = btn.parentElement.querySelector('[data-repopulate-result]');
      if (!out) return;
      btn.disabled = true;
      out.textContent = 'embedding…';
      try {
        const r = await fetch('/settings/api/repopulate_memory', {method: 'POST'});
        const d = await r.json();
        out.textContent = d.ok
          ? 're-embedded ' + d.entries + ' entries / ' + d.documents + ' questions'
          : 'failed: ' + d.error;
      } catch (e) {
        out.textContent = 'failed: ' + e;
      } finally {
        btn.disabled = false;
      }
    }));
}

// ---- edit overlay ----------------------------------------------------------
let editKey = null;
let origControl = '';   // the DB-layer value when the overlay opened

// The control's current string value (selects: 'true'/'false'/''; inputs: text).
function controlValue(){
  const el = document.getElementById('s-edit-input');
  return el ? el.value.trim() : '';
}
// The DB-layer baseline: the stored value if it comes from the DB, else unset.
function dbBaseline(s){
  if (s.source !== 'db') return '';
  if (s.value_type === 'bool') return s.value === true ? 'true' : 'false';
  return s.value == null ? '' : String(s.value);
}
function updateSaveState(){
  document.getElementById('s-save').disabled = (controlValue() === origControl);
  document.getElementById('s-modal-err').textContent = '';
}
function openEdit(key){
  const s = SETTINGS.find(x => x.key === key);
  if (!s || s.secret) return;
  editKey = key;
  document.getElementById('s-modal-key').textContent = key;
  document.getElementById('s-modal-desc').textContent = s.description || '';
  origControl = dbBaseline(s);

  let field;
  if (s.value_type === 'bool'){
    field = 'Value <select id="s-edit-input">'
      + '<option value="">(unset &mdash; use env/default)</option>'
      + '<option value="true">true</option>'
      + '<option value="false">false</option></select>';
  } else {
    const t = s.value_type === 'int' ? 'number' : 'text';
    field = 'Value <input type="' + t + '" id="s-edit-input" placeholder="(unset &mdash; uses env/default)">';
  }
  document.getElementById('s-modal-field').innerHTML = field;
  const input = document.getElementById('s-edit-input');
  input.value = origControl;
  input.addEventListener(s.value_type === 'bool' ? 'change' : 'input', updateSaveState);

  // Show where the live value currently comes from, so an empty DB field isn't
  // mistaken for "no value".
  let cur = 'Effective: ' + (s.value === null || s.value === '' ? '(unset)' : escapeHtml(String(s.value)))
    + ' \\u00b7 from ' + s.source;
  document.getElementById('s-modal-current').textContent = cur;

  document.getElementById('s-modal-err').textContent = '';
  document.getElementById('s-save').disabled = true;
  document.getElementById('s-backdrop').hidden = false;
  document.getElementById('s-modal').hidden = false;
  input.focus();
}
function closeEdit(){
  editKey = null;
  document.getElementById('s-backdrop').hidden = true;
  document.getElementById('s-modal').hidden = true;
}
function typedValue(s){
  const raw = controlValue();
  if (raw === '') return null;                 // empty/unset -> NULL (fallback)
  if (s.value_type === 'bool') return raw === 'true';
  if (s.value_type === 'int') return Number(raw);
  return raw;
}
function saveEdit(){
  if (!editKey) return;
  const s = SETTINGS.find(x => x.key === editKey);
  fetch('/settings/api/set', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: editKey, value: typedValue(s)}),
  }).then(r => r.json().then(d => ({status: r.status, d})))
    .then(({status, d}) => {
      if (status === 200 && d.ok){
        const i = SETTINGS.findIndex(x => x.key === d.setting.key);
        if (i >= 0) SETTINGS[i] = d.setting;
        closeEdit();
        render();
      } else {
        document.getElementById('s-modal-err').textContent = d.error || 'save failed';
      }
    }).catch(() => { document.getElementById('s-modal-err').textContent = 'network error'; });
}

document.getElementById('s-save').addEventListener('click', saveEdit);
document.getElementById('s-cancel').addEventListener('click', closeEdit);
document.getElementById('s-backdrop').addEventListener('click', closeEdit);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && editKey) closeEdit(); });

render();
</script>
"""


@app.route("/settings")
def settings_page() -> str:
    import json

    # ensure_ascii=False so redaction bullets / unicode render literally (the
    # page is served UTF-8). Escape <>& to \uXXXX so a value containing
    # "</script>" can't break out of the inline <script> block.
    payload = json.dumps(db.all_settings(), ensure_ascii=False)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return render_template_string(SETTINGS_TEMPLATE, settings_json=payload)


@app.route("/settings/api/set", methods=["POST"])
def settings_set_api() -> tuple[Response, int] | Response:
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "key" not in data:
        return jsonify({"ok": False, "error": "body must be a JSON object with a 'key'"}), 400
    key = data["key"]
    try:
        db.set_setting(key, data.get("value"))
    except db.UnknownSetting:
        return jsonify({"ok": False, "error": f"unknown setting: {key}"}), 400
    except (ValueError, TypeError) as exc:
        # Validation failure, env-only secret, or bad coercion.
        db.db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "setting": _setting_row(key)})


@app.route("/settings/api/repopulate_memory", methods=["POST"])
def settings_repopulate_memory() -> tuple[Response, int] | Response:
    """Re-embed the Q&A registry (base + customize.dir overlay) without a
    restart — the 'Repopulate Q&A memory' button. 502 carries the embedding
    error (typically Ollama being down); the table is left empty then, and
    clicking again after starting Ollama heals it."""
    import memory.seed_memory as seed_memory

    try:
        counts = seed_memory.rebuild_kb()
    except Exception as exc:  # noqa: BLE001 — any backend failure → 502 + message
        # Not dead code: rebuild_kb reads the customize.dir setting via db.session (get_setting); a failure there leaves the session in a failed state that must be rolled back before responding.
        db.db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True, **counts})
