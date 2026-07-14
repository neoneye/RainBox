import json
import os
import subprocess
import sys
import time
from typing import Any
from uuid import UUID

from flask import (
    Response,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    url_for,
)

import llm
import providers
from db import (
    create_model_config_override,
    db,
    delete_model_config_override,
    get_model_config,
    get_model_config_override,
    list_model_configs_with_overrides,
    resolved_arguments,
)

from .core import app, sync_models_from_providers




MODELS_TEMPLATE: str = """
{% macro test_table(target, target_uuid, is_function_calling, is_struct, show_chat=true) %}
<table class="test-table" data-target="{{ target }}" data-target-uuid="{{ target_uuid }}">
  {% if show_chat %}
  <tr>
    <td class="test-actions">
      <button type="button" data-action="test_chat" onclick="ppRunTest(this)"
              title="plain completion: system &quot;answer with 'pong'&quot;, user &quot;ping&quot;">Test chat</button>
    </td>
    <td class="test-output" data-out="test_chat"><span class="empty">No test run yet.</span></td>
  </tr>
  {% endif %}
  <tr>
    <td class="test-actions">
      <button type="button" data-action="test_streaming" onclick="ppRunTest(this)"
              title="same prompt as Test reasoning but over a streaming response — reveals if reasoning_content arrives only as per-chunk deltas">Test streaming</button>
    </td>
    <td class="test-output" data-out="test_streaming"><span class="empty">No test run yet.</span></td>
  </tr>
  <tr>
    <td class="test-actions">
      <button type="button" data-action="test_structuredoutput" onclick="ppRunTest(this)"
              title="probe always forces should_use_structured_outputs=true so it can use structured-output mode; the saved config is untouched">Test structured output</button>
    </td>
    <td class="test-output" data-out="test_structuredoutput">
      {% if not is_struct %}<span class="empty">⚠ saved config has should_use_structured_outputs=false — probe will force it true; may fail if the model doesn't support structured output</span>{% else %}<span class="empty">No test run yet.</span>{% endif %}
    </td>
  </tr>
  <tr>
    <td class="test-actions">
      <button type="button" data-action="test_tool" onclick="ppRunTest(this)"
              title="probe always forces is_function_calling_model=true so it can construct the FunctionAgent; the saved config is untouched">Test function calling</button>
    </td>
    <td class="test-output" data-out="test_tool">
      {% if not is_function_calling %}<span class="empty">⚠ saved config has is_function_calling_model=false — probe will force it true; may fail if the model doesn't support tools</span>{% else %}<span class="empty">No test run yet.</span>{% endif %}
    </td>
  </tr>
</table>
{% endmacro %}
<!doctype html>
<title>Models &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{padding:0.6em 1em;border-bottom:1px solid #ddd;background:#fafafa}
  header a{margin-right:1em}
  .split{display:grid;grid-template-columns:380px 1fr;grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .pane{overflow:auto;min-height:0;padding:0.8em 1em}
  .pane.left{border-right:1px solid #ddd;background:#fbfbfb}
  ul.tree{list-style:none;margin:0;padding:0}
  ul.tree ul{list-style:none;margin:0;padding:0 0 0 1.2em}
  ul.tree li{margin:0.15em 0;line-height:1.3}
  ul.tree a{display:block;padding:0.2em 0.4em;border-radius:3px;text-decoration:none;color:inherit}
  ul.tree a:hover{background:#eef}
  ul.tree a.selected{background:#dde7ff;font-weight:600}
  ul.tree a.new-override{color:#0653a8;font-size:90%}
  .badge{display:inline-block;font-size:80%;padding:0 0.4em;border-radius:0.8em;background:#eee;color:#555;margin-left:0.3em}
  .badge.unavailable{background:#fdd;color:#900}
  .pp-provider-badge{display:inline-block;font-size:75%;padding:0 0.4em;
    border-radius:0.4em;margin-right:0.3em;background:#dbeafe;color:#1e40af;
    vertical-align:0.05em}
  pre{background:#f4f4f4;padding:0.6em;white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:90%}
  code{background:#eee;padding:1px 4px;border-radius:3px;font-family:ui-monospace,monospace}
  h2{margin-top:0}
  dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:0.25em 0.8em;margin:0.5em 0}
  dl.kv dt{font-weight:600;color:#555}
  dl.kv dd{margin:0}
  .empty{color:#888;font-style:italic}
  .ok{color:#137333}
  .err{color:#a00}
  table.test-table{border-collapse:collapse;margin:0.4em 0}
  table.test-table td{vertical-align:top;padding:0.5em 0.8em;border:1px solid #e3e3e3}
  table.test-table td.test-actions{white-space:nowrap}
  table.test-table td.test-output{min-width:20em}
  .reload-bar{display:flex;align-items:center;gap:0.6em;margin:0 0 0.6em 0}
  .pp-sort-picker{font-size:85%;color:#555}
  .pp-sort-picker select{font-size:inherit;margin-left:0.2em}
  /* Click-to-rename name display: doubles as the detail heading; clicking
     opens the rename modal (docs/ui-modal-rename.md). */
  .pp-rename-display{font:inherit;font-size:1.5em;font-weight:600;color:#1a1a2e;background:none;
    text-align:left;border:1px solid transparent;border-radius:6px;padding:0.15em 0.3em;margin-left:-0.3em;cursor:pointer}
  .pp-rename-display:hover{border-color:#cbd5e1;background:#f8fafc}
  .pp-rename-display .empty{font-weight:400}
  #pp-rename-modal input{font:inherit;width:100%;box-sizing:border-box;padding:5px 7px}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>

<div class="split">
  <div class="pane left">
    <div class="reload-bar">
      <button type="button" id="pp-reload-btn" onclick="ppReloadModels(this)">Reload model list</button>
      <span id="pp-reload-status" class="empty"></span>
      <label class="pp-sort-picker" title="Tree sort order">
        sort:
        <select onchange="ppChangeSort(this.value)">
          <option value="provider" {% if sort_by == 'provider' %}selected{% endif %}>provider, then model name</option>
          <option value="model_name" {% if sort_by == 'model_name' %}selected{% endif %}>model name</option>
        </select>
      </label>
    </div>
    <ul class="tree">
      {% for cfg, overrides in tree %}
      <li>
        <a href="{{ url_for('models_page', id=cfg.uuid) }}" class="{% if selected_kind == 'config' and selected_uuid == cfg.uuid %}selected{% endif %}">
          <span class="pp-provider-badge">{% if cfg.provider == 'lm_studio' %}LM Studio{% elif cfg.provider == 'jan' %}Jan{% elif cfg.provider == 'ollama' %}Ollama{% else %}{{ cfg.provider }}{% endif %}</span>
          <b>{{ cfg.effective_display_name }}</b>
          {% if not cfg.available %}<span class="badge unavailable">unavailable</span>{% endif %}
        </a>
        <ul>
          {% for ov in overrides %}
          <li>
            <a href="{{ url_for('models_page', id=ov.uuid) }}" class="{% if selected_kind == 'override' and selected_uuid == ov.uuid %}selected{% endif %}">
              {% set label = ov.effective_display_name %}
              {% if label %}{{ label }}{% else %}<span class="empty">(no name)</span>{% endif %}
            </a>
          </li>
          {% endfor %}
          <li>
            <a href="{{ url_for('models_page', new_override=cfg.uuid) }}" class="new-override {% if selected_kind == 'new_override' and selected_uuid == cfg.uuid %}selected{% endif %}">
              + New override
            </a>
          </li>
        </ul>
      </li>
      {% else %}
      <li class="empty">no model configs yet</li>
      {% endfor %}
    </ul>
  </div>

  <div class="pane right">
    {% if detail is none %}
    <p class="empty">Select a row on the left to see details.</p>
    {% elif detail.kind == 'config' %}
    <form method="post" action="{{ url_for('models_page') }}" style="margin:0 0 0.6em 0">
      <input type="hidden" name="action" value="rename_config">
      <input type="hidden" name="target_uuid" value="{{ detail.row.uuid }}">
      <input type="hidden" name="display_name" value="{{ detail.row.display_name }}">
      <button type="button" class="pp-rename-display" title="Click to rename"
              data-rename-title="Rename model config"
              data-rename-placeholder="{{ detail.row.model_name }}"
              onclick="ppOpenRenameModal(this)">{{ detail.row.effective_display_name }}</button>
      {% if detail.renamed %}<span class="ok">✓ renamed</span>{% endif %}
    </form>
    <dl class="kv">
      <dt>uuid</dt><dd><code>{{ detail.row.uuid }}</code></dd>
      <dt>model_name</dt><dd><code>{{ detail.row.model_name }}</code></dd>
      <dt>available</dt><dd>{{ 'yes' if detail.row.available else 'no' }}</dd>
      <dt>size_bytes</dt>
      <dd>
        {% if detail.row.size_bytes is not none %}
          {{ '{:,}'.format(detail.row.size_bytes) }} <span class="muted">({{ detail.size_human }})</span>
        {% else %}<span class="muted">unknown</span>{% endif %}
      </dd>
      <dt>created_at</dt><dd>{{ detail.row.created_at }}</dd>
      <dt>updated_at</dt><dd>{{ detail.row.updated_at }}</dd>
    </dl>
    <h3>Test connection</h3>
    {{ test_table('config', detail.row.uuid, detail.is_function_calling, detail.is_struct) }}

    <h3>arguments</h3>
    <pre>{{ detail.arguments_json }}</pre>

    <h3>Model info ({{ detail.provider_display_name }})</h3>
    {% if detail.model_info %}
    <dl class="kv">
      {% for k, v in detail.model_info.items() %}
      <dt>{{ k }}</dt><dd>{% if v is iterable and v is not string %}{{ v | join(', ') }}{% else %}{{ v }}{% endif %}</dd>
      {% endfor %}
    </dl>
    {% elif not detail.provider_reachable %}
    <p class="empty">Could not reach {{ detail.provider_display_name }} at <code>{{ detail.provider_base_url }}</code>.</p>
    {% else %}
    <p class="empty">This model is no longer available in {{ detail.provider_display_name }} &mdash; it was likely deleted or removed there. The saved config is kept for reference; re-add <code>{{ detail.row.model_name }}</code> in {{ detail.provider_display_name }} to test or use it again.</p>
    {% endif %}
    {% elif detail.kind == 'new_override' %}
    <h2>New override</h2>
    <p>Under base config: <a href="{{ url_for('models_page', id=detail.parent.uuid) }}"><b>{{ detail.parent.model_name }}</b></a></p>

    <form method="post" action="{{ url_for('models_page') }}">
      <input type="hidden" name="config_uuid" value="{{ detail.parent.uuid }}">

      <p>
        <span style="font-weight:600;color:#555">Capability flags:</span><br>
        <label style="display:block;margin:0.2em 0;font-weight:normal">
          <input type="checkbox" name="is_function_calling_model" value="1"
                 {% if detail.form_data.is_function_calling_model %}checked{% endif %}
                 onchange="ppFormChanged()">
          <code>is_function_calling_model</code>
        </label>
        <label style="display:block;margin:0.2em 0;font-weight:normal">
          <input type="checkbox" name="should_use_structured_outputs" value="1"
                 {% if detail.form_data.should_use_structured_outputs %}checked{% endif %}
                 onchange="ppFormChanged()">
          <code>should_use_structured_outputs</code>
        </label>
        {% if detail.parent.provider == 'ollama' %}
        <label style="display:block;margin:0.2em 0;font-weight:normal">
          <input type="checkbox" name="thinking" value="1"
                 {% if detail.form_data.thinking %}checked{% endif %}
                 onchange="ppFormChanged()">
          <code>thinking</code>
          <span class="empty" style="font-size:0.85em">&mdash; Ollama only: surface the model's chain-of-thought. Combine it with any other flag &mdash; the required tests below will fail if the model can't handle the combination (e.g. thinking together with structured output).</span>
        </label>
        {% endif %}
      </p>

      <p>
        <label>Temperature: <output id="temp_val">{{ '%.2f' % detail.form_data.temperature }}</output><br>
          <input type="range" name="temperature" min="0" max="1" step="0.05"
                 value="{{ detail.form_data.temperature }}"
                 oninput="document.getElementById('temp_val').value = parseFloat(this.value).toFixed(2); ppFormChanged();">
        </label>
      </p>

      <p>
        {% if detail.parent.provider == 'jan' %}
        <label>Context window (tokens):<br>
          <input type="number" name="context_window" min="1" step="1"
                 value="{{ detail.form_data.context_window }}"
                 style="width:8em;background:#eee;color:#888" readonly tabindex="-1">
          <span class="empty" style="font-size:0.85em">&mdash; set the context length in Jan's UI; Jan loads the model with that window and ignores any size sent from here.</span>
        </label>
        {% else %}
        <label>Context window (tokens):<br>
          <input type="number" name="context_window" min="1" step="1"
                 value="{{ detail.form_data.context_window }}"
                 style="width:8em"
                 oninput="ppFormChanged()">
          <span class="empty" style="font-size:0.85em">&mdash; tells llama-index how many tokens the model can handle.{% if detail.parent.provider == 'lm_studio' %} It's also the context length the agent loads the model at: if LM Studio currently has it loaded with a smaller window, the agent reloads it at this size before running &mdash; no need to set anything in LM Studio.{% endif %}</span>
        </label>
        {% endif %}
      </p>

      {% if detail.parent.provider != 'ollama' %}
      <p>
        <label>Reasoning effort:<br>
          <select name="reasoning_effort" onchange="ppFormChanged()">
            <option value="none" {% if detail.form_data.reasoning_effort == 'none' %}selected{% endif %}>none (don't pass reasoning.effort)</option>
            <option value="low" {% if detail.form_data.reasoning_effort == 'low' %}selected{% endif %}>low</option>
            <option value="medium" {% if detail.form_data.reasoning_effort == 'medium' %}selected{% endif %}>medium</option>
            <option value="high" {% if detail.form_data.reasoning_effort == 'high' %}selected{% endif %}>high</option>
          </select>
        </label>
      </p>
      {% endif %}

      <h3>Test connection</h3>
      {{ test_table('new_override', detail.parent.uuid, detail.is_function_calling, detail.is_struct) }}

      <p>
        <button type="submit" name="action" value="save" disabled>Save</button>
        <span id="pp-save-hint" class="empty">required tests must pass before saving</span>
      </p>
    </form>

    <script>
      // The save gate: capability flags imply *required* tests; everything else
      // is optional. Save is enabled iff every required test has passed since
      // the form last changed, OR — if no test is required — at least one
      // optional test has passed. PP_PASSED tracks which test_* actions have
      // succeeded under the current form state and gets cleared on any edit.
      const PP_BASE_ARGS = {{ detail.parent.arguments | tojson }};
      const PP_MODEL_NAME = {{ detail.parent.model_name | tojson }};
      const PP_PASSED = new Set();
      function ppReadFlags(form) {
        return {
          is_function_calling_model: form.querySelector('[name=is_function_calling_model]').checked,
          should_use_structured_outputs: form.querySelector('[name=should_use_structured_outputs]').checked,
        };
      }
      // The thinking checkbox only exists for Ollama configs; null means the
      // control isn't rendered (any other provider), so callers omit the key.
      function ppReadThinking(form) {
        const el = form.querySelector('[name=thinking]');
        return el ? el.checked : null;
      }
      // The reasoning-effort select is hidden for Ollama (which uses `thinking`
      // instead); treat its absence as 'none'.
      function ppReadReasoning(form) {
        const el = form.querySelector('[name=reasoning_effort]');
        return el ? el.value : 'none';
      }
      function ppRequiredTests(form) {
        const flags = ppReadFlags(form);
        const req = [];
        if (flags.should_use_structured_outputs) req.push('test_structuredoutput');
        if (flags.is_function_calling_model) req.push('test_tool');
        return req;
      }
      function ppUpdatePreview() {
        const form = document.querySelector('form');
        const flags = ppReadFlags(form);
        const reasoning = ppReadReasoning(form);
        const overrides = Object.assign({
          temperature: parseFloat(form.querySelector('[name=temperature]').value),
          context_window: parseInt(form.querySelector('[name=context_window]').value, 10),
        }, flags);
        const thinking = ppReadThinking(form);
        if (thinking !== null) overrides.thinking = thinking;
        if (reasoning !== 'none') {
          overrides.additional_kwargs = {extra_body: {reasoning: {effort: reasoning}}};
        }
        const merged = Object.assign({}, PP_BASE_ARGS, overrides, {model: PP_MODEL_NAME});
        const pre = document.getElementById('resolved-preview');
        if (pre) pre.textContent = JSON.stringify(merged, null, 2);
      }
      function ppSyncToolBtn() {
        // Probe always forces is_function_calling_model=true server-side so the
        // FunctionAgent can be constructed regardless of the checkbox. Keep
        // the button enabled either way; the cell shows a warning when the
        // current form flag is off so the user knows the probe doesn't
        // reflect the about-to-save config.
        const flags = ppReadFlags(document.querySelector('form'));
        const on = flags.is_function_calling_model;
        const cell = document.querySelector('.test-output[data-out="test_tool"]');
        if (cell) cell.innerHTML = on
          ? '<span class="empty">No test run yet.</span>'
          : '<span class="empty">\\u26a0 is_function_calling_model is off \\u2014 probe will force it true; may fail if the model doesn\\u2019t support tools</span>';
      }
      function ppSyncStructBtn() {
        // Same idea as ppSyncToolBtn: the probe always forces
        // should_use_structured_outputs=true server-side, so warn when the
        // current form flag is off.
        const flags = ppReadFlags(document.querySelector('form'));
        const on = flags.should_use_structured_outputs;
        const cell = document.querySelector('.test-output[data-out="test_structuredoutput"]');
        if (cell) cell.innerHTML = on
          ? '<span class="empty">No test run yet.</span>'
          : '<span class="empty">\\u26a0 should_use_structured_outputs is off \\u2014 probe will force it true; may fail if the model doesn\\u2019t support structured output</span>';
      }
      // Apply "Required: " prefix on the buttons whose action is in the
      // required set for the current flag combination, and strip it otherwise.
      // The base label is cached on first call so re-applying is idempotent.
      function ppUpdateTestLabels() {
        const form = document.querySelector('form');
        const required = new Set(ppRequiredTests(form));
        form.querySelectorAll('button[data-action]').forEach(btn => {
          if (!btn.dataset.baseLabel) {
            btn.dataset.baseLabel = btn.textContent.replace(/^Required:\\s*/, '').trim();
          }
          btn.textContent = required.has(btn.dataset.action)
            ? 'Required: ' + btn.dataset.baseLabel
            : btn.dataset.baseLabel;
        });
      }
      // Recompute save-enabled, the hidden "tested" marker the server checks,
      // and the helper text. Called after every test result and every form edit.
      function ppUpdateSaveGate() {
        const form = document.querySelector('form');
        const required = ppRequiredTests(form);
        const missing = required.filter(a => !PP_PASSED.has(a));
        let satisfied, hint;
        if (required.length === 0) {
          satisfied = PP_PASSED.size > 0;
          hint = satisfied
            ? '\\u2713 ready to save'
            : 'no required tests \\u2014 run any test to enable Save';
        } else {
          satisfied = missing.length === 0;
          hint = satisfied
            ? '\\u2713 all required tests passed'
            : 'still required: ' + missing.join(', ');
        }
        const save = form.querySelector('button[value=save]');
        if (save) save.disabled = !satisfied;
        let marker = form.querySelector('input[name=tested]');
        if (satisfied && !marker) {
          marker = document.createElement('input');
          marker.type = 'hidden'; marker.name = 'tested'; marker.value = '1';
          form.appendChild(marker);
        } else if (!satisfied && marker) {
          marker.remove();
        }
        const hintEl = document.getElementById('pp-save-hint');
        if (hintEl) {
          hintEl.textContent = hint;
          hintEl.className = satisfied ? 'ok' : 'empty';
        }
        ppUpdateTestLabels();
      }
      // Any argument change invalidates prior tests: clear pass set, reset
      // output cells, and rebuild the save gate.
      function ppFormChanged() {
        const form = document.querySelector('form');
        PP_PASSED.clear();
        form.querySelectorAll('.test-output').forEach(cell => {
          cell.innerHTML = '<span class="empty">No test run yet.</span>';
        });
        ppSyncToolBtn();
        ppSyncStructBtn();
        ppUpdatePreview();
        ppUpdateSaveGate();
      }
      ppSyncToolBtn();
      ppSyncStructBtn();
      ppUpdateSaveGate();
    </script>

    <h3>Resolved arguments (preview)</h3>
    <pre id="resolved-preview">{{ detail.resolved_preview }}</pre>
    {% elif detail.kind == 'override' %}
    <form method="post" action="{{ url_for('models_page') }}" style="margin:0 0 0.6em 0">
      <input type="hidden" name="action" value="rename_override">
      <input type="hidden" name="target_uuid" value="{{ detail.row.uuid }}">
      <input type="hidden" name="display_name" value="{{ detail.row.display_name }}">
      <button type="button" class="pp-rename-display" title="Click to rename"
              data-rename-title="Rename override"
              data-rename-placeholder="{{ detail.row.synthesized_label or '(no name)' }}"
              onclick="ppOpenRenameModal(this)">{% if detail.row.effective_display_name %}{{ detail.row.effective_display_name }}{% else %}<span class="empty">(no name)</span>{% endif %}</button>
      {% if detail.renamed %}<span class="ok">✓ renamed</span>{% endif %}
    </form>
    <p style="margin:0 0 0.6em 0">
      <a href="{{ url_for('models_page', new_override=detail.parent.uuid, clone_from=detail.row.uuid) }}"
         style="display:inline-block;padding:0.3em 0.8em;border:1px solid #ccc;border-radius:3px;background:#f7f7f7;text-decoration:none;color:inherit">
        Clone &raquo; new override
      </a>
    </p>
    <dl class="kv">
      <dt>uuid</dt><dd><code>{{ detail.row.uuid }}</code></dd>
      <dt>created_at</dt><dd>{{ detail.row.created_at }}</dd>
      <dt>updated_at</dt><dd>{{ detail.row.updated_at }}</dd>
    </dl>
    <h3>Test connection</h3>
    {{ test_table('override', detail.row.uuid, detail.is_function_calling, detail.is_struct) }}

    <h3>overrides</h3>
    <pre>{{ detail.overrides_json }}</pre>
    <h3>resolved arguments (base &laquo;merged with&raquo; overrides)</h3>
    <pre>{{ detail.resolved_json }}</pre>

    <form method="post" action="{{ url_for('models_page') }}" style="margin:2em 0 1em 0;border-top:1px solid #ddd;padding-top:1em">
      <input type="hidden" name="action" value="delete_override">
      <input type="hidden" name="target_uuid" value="{{ detail.row.uuid }}">
      <button type="submit"
              onclick="return confirm('Delete this override? This cannot be undone.');"
              style="color:#a00;border:1px solid #a00;background:#fff;padding:0.3em 0.8em">Delete this override</button>
    </form>
    {% endif %}
  </div>
</div>

<script>
  function ppEsc(s){ const d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }
  // Preserve the left-pane scroll position across in-page navigations
  // (clicking a model/override links to ?id=… which is a full page load).
  // Restore as soon as the DOM is parsed so the user doesn't see a flicker.
  (function ppRestoreTreeScroll(){
    const pane = document.querySelector('.pane.left');
    if (!pane) return;
    const saved = sessionStorage.getItem('pp-models-tree-scroll');
    if (saved !== null) pane.scrollTop = parseInt(saved, 10) || 0;
    pane.addEventListener('scroll', () => {
      sessionStorage.setItem('pp-models-tree-scroll', String(pane.scrollTop));
    });
  })();
  // Re-sync model_config rows with LM Studio without a server restart, then
  // reload the page so the tree reflects newly downloaded models.
  // Navigate to the same page with sort=<value>, preserving id= when
  // the user has a row selected. Default sort is "provider", so we omit
  // it from the URL in that case to keep the URL clean.
  function ppChangeSort(value){
    const params = new URLSearchParams(window.location.search);
    if (value === 'provider') {
      params.delete('sort');
    } else {
      params.set('sort', value);
    }
    const q = params.toString();
    window.location.href = window.location.pathname + (q ? '?' + q : '');
  }
  function ppReloadModels(btn){
    const status = document.getElementById('pp-reload-status');
    btn.disabled = true;
    status.className = 'empty';
    status.textContent = 'Reloading\\u2026';
    fetch('/model/api/reload', {method: 'POST'})
      .then(r => r.json())
      .then(d => {
        if (d.ok) { window.location.reload(); }
        else {
          status.className = 'err';
          status.textContent = '\\u2717 ' + (d.error || 'reload failed');
          btn.disabled = false;
        }
      })
      .catch(e => {
        status.className = 'err';
        status.textContent = '\\u2717 ' + String(e);
        btn.disabled = false;
      });
  }
  // Build the JSON body that the /model/api/test* endpoints expect. Shared
  // by ppRunTest (single response) and ppRunStreamingTest (NDJSON stream).
  function ppBuildTestBody(btn, action){
    const table = btn.closest('.test-table');
    if (table.dataset.target === 'new_override') {
      const form = btn.closest('form');
      const flags = ppReadFlags(form);
      const body = {
        target: 'new_override', action: action,
        config_uuid: form.querySelector('[name=config_uuid]').value,
        temperature: form.querySelector('[name=temperature]').value,
        context_window: form.querySelector('[name=context_window]').value,
        reasoning_effort: ppReadReasoning(form),
        is_function_calling_model: flags.is_function_calling_model,
        should_use_structured_outputs: flags.should_use_structured_outputs
      };
      const thinking = ppReadThinking(form);
      if (thinking !== null) body.thinking = thinking;
      return body;
    }
    return {target: table.dataset.target, target_uuid: table.dataset.targetUuid, action: action};
  }
  // Run a structured-output/tool test against the JSON endpoint and update the matching
  // output cell in place (no form POST, so the ?id= URL parameter is preserved).
  function ppRunTest(btn){
    const action = btn.dataset.action;
    if (action === 'test_streaming') return ppRunStreamingTest(btn);
    return ppRunCancellableTest(btn, action);
  }
  // Run a chat/structured/tool probe against the killable-subprocess endpoint.
  // The endpoint streams {running,elapsed} heartbeats then a final {ok,...,done}
  // line; the Stop button aborts the fetch, which makes the server SIGKILL the
  // worker process (freeing the provider). Mirrors ppRunStreamingTest's Stop UX.
  async function ppRunCancellableTest(btn, action){
    const table = btn.closest('.test-table');
    const out = table.querySelector('.test-output[data-out="' + action + '"]');
    const controller = new AbortController();
    btn.disabled = true;
    out.innerHTML = '';
    const statusDiv = document.createElement('div');
    statusDiv.innerHTML = '<span class="empty">testing\\u2026</span>';
    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.textContent = 'Stop';
    stopBtn.style.marginTop = '0.4em';
    stopBtn.onclick = () => controller.abort();
    out.appendChild(statusDiv);
    out.appendChild(stopBtn);

    let final = null;
    let aborted = false;
    try {
      const resp = await fetch('/model/api/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(ppBuildTestBody(btn, action)),
        signal: controller.signal,
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line) continue;
          const obj = JSON.parse(line);
          if (obj.done) final = obj;
          else if (obj.running) statusDiv.innerHTML = '<span class="empty">testing\\u2026 ' + Number(obj.elapsed || 0).toFixed(1) + 's</span>';
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') aborted = true;
      else statusDiv.innerHTML = '<span class="err">\\u2717 ' + ppEsc(String(e)) + '</span>';
    } finally {
      stopBtn.remove();
      btn.disabled = false;
    }
    if (aborted) {
      statusDiv.innerHTML = '<span class="err">\\u26a0 stopped</span>';
      if (table.dataset.target === 'new_override') { PP_PASSED.delete(action); ppUpdateSaveGate(); }
    } else if (final && final.ok) {
      const counts = (final.reasoning_chars != null || final.content_chars != null)
        ? ' <span class="empty">&middot; reasoning ' + (final.reasoning_chars ?? 0) + ' chars &middot; content ' + (final.content_chars ?? 0) + ' chars</span>'
        : '';
      statusDiv.innerHTML = '<span class="ok">\\u2713 passed in ' + Number(final.elapsed).toFixed(2) + 's</span>' + counts + '<br><code>' + ppEsc(final.message) + '</code>';
      if (table.dataset.target === 'new_override') { PP_PASSED.add(action); ppUpdateSaveGate(); }
    } else if (final) {
      statusDiv.innerHTML = '<span class="err">\\u2717 ' + ppEsc(final.error) + '</span>';
      if (table.dataset.target === 'new_override') { PP_PASSED.delete(action); ppUpdateSaveGate(); }
    }
  }
  // Render a stats object from the live NDJSON stream into the test_streaming
  // output cell. Numbers come from the server; the function keeps the markup
  // tight so each update is cheap to re-render.
  function ppRenderStreamingStats(stats){
    const ttft = stats.ttft != null ? Number(stats.ttft).toFixed(2) + 's' : 'n/a';
    const elapsed = Number(stats.elapsed || 0).toFixed(2);
    return (
      '<div>chunks: <b>' + stats.chunk + '</b> ' +
      '(content ' + stats.content_chunks + ', reasoning ' + stats.reasoning_chunks + ')</div>' +
      '<div>content: <b>' + stats.content_len + '</b> chars, ' +
      'reasoning_content: <b>' + stats.reasoning_len + '</b> chars</div>' +
      '<div>TTFT: <b>' + ttft + '</b>, elapsed: <b>' + elapsed + 's</b></div>'
    );
  }
  // Streaming probe: POST to the NDJSON endpoint, read the stream line-by-line,
  // render live stats into the cell, and offer a Stop button that aborts the
  // fetch (which closes the server generator and the upstream LM Studio HTTP
  // stream by GC).
  async function ppRunStreamingTest(btn){
    const table = btn.closest('.test-table');
    const out = table.querySelector('.test-output[data-out="test_streaming"]');
    const controller = new AbortController();
    btn.disabled = true;
    out.innerHTML = '';
    const statsDiv = document.createElement('div');
    statsDiv.innerHTML = '<span class="empty">starting\\u2026</span>';
    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.textContent = 'Stop';
    stopBtn.style.marginTop = '0.4em';
    stopBtn.onclick = () => controller.abort();
    out.appendChild(statsDiv);
    out.appendChild(stopBtn);

    let last = null;
    let aborted = false;
    try {
      const resp = await fetch('/model/api/test_streaming_live', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(ppBuildTestBody(btn, 'test_streaming')),
        signal: controller.signal,
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line) continue;
          last = JSON.parse(line);
          if (last.error) {
            statsDiv.innerHTML = '<span class="err">\\u2717 ' + ppEsc(last.error) + '</span>';
            continue;
          }
          statsDiv.innerHTML = ppRenderStreamingStats(last);
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        aborted = true;
      } else {
        statsDiv.innerHTML = '<span class="err">\\u2717 ' + ppEsc(String(e)) + '</span>';
      }
    } finally {
      stopBtn.remove();
      btn.disabled = false;
    }
    if (last && !last.error) {
      const tag = aborted
        ? '<span class="err">\\u26a0 stopped after ' + Number(last.elapsed || 0).toFixed(2) + 's</span>'
        : '<span class="ok">\\u2713 done in ' + Number(last.elapsed || 0).toFixed(2) + 's</span>';
      statsDiv.innerHTML = tag + '<br>' + ppRenderStreamingStats(last);
      if (table.dataset.target === 'new_override' && !aborted && last.done) {
        PP_PASSED.add('test_streaming');
        ppUpdateSaveGate();
      } else if (table.dataset.target === 'new_override') {
        PP_PASSED.delete('test_streaming');
        ppUpdateSaveGate();
      }
    } else if (aborted) {
      statsDiv.innerHTML = '<span class="err">\\u26a0 stopped before any chunks arrived</span>';
    }
  }
</script>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="pp-rename-modal" hidden>
  <h3 id="pp-rename-title">Rename</h3>
  <input type="text" id="pp-rename-input" autocomplete="off">
  <div class="modal-actions">
    <button type="button" class="btn-cancel" id="pp-rename-cancel">Cancel</button>
    <button type="button" class="btn-primary" id="pp-rename-confirm" disabled>Rename</button>
  </div>
</div>

<script>
  // ---- rename modal (docs/ui-modal-rename.md) ----
  // The display name is shown as a click-to-rename control; editing happens in
  // this modal, so a typed-but-unconfirmed name can't be silently lost.
  // Confirming fills the enclosing form's hidden display_name and submits it
  // (the server-side rename_config / rename_override handlers do the rest).
  let ppRenameForm = null, ppRenameOriginal = null;
  function ppOpenRenameModal(btn){
    ppRenameForm = btn.closest('form');
    ppRenameOriginal = ppRenameForm.querySelector('input[name="display_name"]').value;
    document.getElementById('pp-rename-title').textContent = btn.dataset.renameTitle || 'Rename';
    const input = document.getElementById('pp-rename-input');
    input.placeholder = btn.dataset.renamePlaceholder || '';
    input.value = ppRenameOriginal;
    ppSyncRenameConfirm();
    document.getElementById('ui-modal-backdrop').hidden = false;
    document.getElementById('pp-rename-modal').hidden = false;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
  function ppCloseRenameModal(){
    document.getElementById('pp-rename-modal').hidden = true;
    document.getElementById('ui-modal-backdrop').hidden = true;
    ppRenameForm = null;
    ppRenameOriginal = null;
  }
  // Unlike the tree pages, empty IS a valid name here: it clears display_name
  // and the label falls back to the model/synthesized name (the placeholder
  // shows which). So Rename enables on any change, including clearing.
  function ppSyncRenameConfirm(){
    document.getElementById('pp-rename-confirm').disabled =
      ppRenameForm === null
      || document.getElementById('pp-rename-input').value.trim() === (ppRenameOriginal || '');
  }
  function ppConfirmRenameModal(){
    if (!ppRenameForm) return;
    ppRenameForm.querySelector('input[name="display_name"]').value =
      document.getElementById('pp-rename-input').value.trim();
    ppRenameForm.submit();
  }
  // Backdrop / Esc dismiss only while the typed name still equals the stored
  // one (docs/ui-modals.md dirty guard); Cancel always closes.
  function ppDismissRenameIfClean(){
    if (document.getElementById('pp-rename-modal').hidden) return;
    if (document.getElementById('pp-rename-input').value === (ppRenameOriginal || '')) ppCloseRenameModal();
  }
  document.getElementById('pp-rename-cancel').addEventListener('click', ppCloseRenameModal);
  document.getElementById('pp-rename-confirm').addEventListener('click', ppConfirmRenameModal);
  document.getElementById('pp-rename-input').addEventListener('input', ppSyncRenameConfirm);
  document.getElementById('pp-rename-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !document.getElementById('pp-rename-confirm').disabled){
      e.preventDefault(); ppConfirmRenameModal();
    }
  });
  document.getElementById('ui-modal-backdrop').addEventListener('click', ppDismissRenameIfClean);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') ppDismissRenameIfClean(); });
</script>
"""


def _format_size(n: int | None) -> str:
    if n is None:
        return "unknown"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} TB"


def _is_function_calling(arguments: dict[str, Any]) -> bool:
    """Whether these resolved arguments let the model do tool calling (gates the
    'Test tool' button warning)."""
    return bool(arguments.get("is_function_calling_model"))


def _is_struct(arguments: dict[str, Any]) -> bool:
    """Whether these resolved arguments enable structured-output mode (gates the
    'Test structured output' button warning)."""
    return bool(arguments.get("should_use_structured_outputs"))


def _parse_checkbox(form: Any, name: str) -> bool:
    """Accepts either an HTML form (checkbox sends a string when checked, missing
    when unchecked) or a JSON dict (true/false bool). Missing/empty/false-y →
    False; truthy → True."""
    v = form.get(name)
    if isinstance(v, bool):
        return v
    return v not in (None, "", "0", "false", "False")


def _new_override_form_data(form: Any) -> dict[str, Any]:
    try:
        temperature = float(form.get("temperature", "0.5"))
    except ValueError:
        temperature = 0.5
    temperature = max(0.0, min(1.0, temperature))
    try:
        context_window = int(form.get("context_window", "3900"))
    except (TypeError, ValueError):
        context_window = 3900
    context_window = max(1, context_window)
    reasoning_effort = form.get("reasoning_effort", "none")
    if reasoning_effort not in ("none", "low", "medium", "high"):
        reasoning_effort = "none"
    return {
        "display_name": (form.get("display_name") or "").strip(),
        "temperature": temperature,
        "context_window": context_window,
        "reasoning_effort": reasoning_effort,
        "is_function_calling_model": _parse_checkbox(form, "is_function_calling_model"),
        "should_use_structured_outputs": _parse_checkbox(form, "should_use_structured_outputs"),
        # Ollama-only chain-of-thought toggle; the checkbox is rendered only
        # for Ollama configs, so it's absent (→ False) for other providers.
        "thinking": _parse_checkbox(form, "thinking"),
    }


def _build_overrides_dict(form_data: dict[str, Any], provider: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "temperature": form_data["temperature"],
        "context_window": form_data["context_window"],
        "is_function_calling_model": form_data["is_function_calling_model"],
        "should_use_structured_outputs": form_data["should_use_structured_outputs"],
    }
    # `thinking` is a native-Ollama-only knob (prepare_llm strips it elsewhere);
    # only persist/send it for Ollama so other providers' configs stay clean.
    # Any combination is allowed — the required tests validate whether the model
    # actually handles it (e.g. thinking + structured output).
    if provider == "ollama":
        overrides["thinking"] = bool(form_data.get("thinking", False))
    if form_data["reasoning_effort"] != "none":
        overrides["additional_kwargs"] = {
            "extra_body": {"reasoning": {"effort": form_data["reasoning_effort"]}}
        }
    return overrides


@app.route("/model/api/reload", methods=["POST"])
def models_reload_api() -> Response:
    """Re-sync model_config rows with every registered provider's current
    model list. Used by the Reload button on /model so newly added models
    show up without a server restart."""
    summary = sync_models_from_providers()
    return jsonify({"ok": True, "summary": summary})


def _resolve_test_target(
    data: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Turn a /model/api/* JSON body into (provider_id, model_name,
    arguments). Three targets:
      'new_override' — args come from the unsaved form (no DB row exists yet);
      'config'/'override' — args come from the saved row identified by
        target_uuid. The provider id is read from the underlying
        ModelConfig (overrides inherit their parent config's provider).
    Aborts 400/404 on bad inputs."""
    target = data.get("target")
    if target == "new_override":
        try:
            cfg_uuid = UUID(str(data.get("config_uuid", "")))
        except ValueError:
            abort(400, "invalid config_uuid")
        cfg = get_model_config(cfg_uuid)
        if cfg is None:
            abort(404)
        overrides = _build_overrides_dict(_new_override_form_data(data), cfg.provider)
        return cfg.provider, cfg.model_name, {**cfg.arguments, **overrides}
    if target in ("config", "override"):
        try:
            target_uuid = UUID(str(data.get("target_uuid", "")))
        except ValueError:
            abort(400, "invalid target_uuid")
        if target == "config":
            cfg = get_model_config(target_uuid)
            if cfg is None:
                abort(404)
            return cfg.provider, cfg.model_name, dict(cfg.arguments)
        ov = get_model_config_override(target_uuid)
        if ov is None:
            abort(404)
        parent = get_model_config(ov.model_config_uuid)
        if parent is None:
            abort(404, "override references missing base config")
        return (
            parent.provider,
            parent.model_name,
            {**parent.arguments, **ov.overrides},
        )
    abort(400, "unknown target")


_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _worker_env() -> dict[str, str]:
    """Env for spawned worker subprocesses: make the source root importable
    regardless of the parent's CWD."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT_DIR + (
        os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )
    return env


@app.route("/model/api/test", methods=["POST"])
def models_test_api() -> Response:
    """Run a structured-output/tool/chat probe and stream NDJSON so the /model
    page can show a live elapsed counter and a Stop button.

    The probe is one blocking LLM call, which can't be cancelled in-process — so
    it runs in a throwaway subprocess (llm.models_test_worker). While it runs we
    emit `{"running": true, "elapsed": s}` heartbeats; the final line is the
    worker's result tagged `done`. Each heartbeat yield is where a client
    disconnect surfaces as GeneratorExit, which runs the finally below and
    SIGKILLs the worker — closing its HTTP socket so the provider (e.g. Ollama)
    stops generating. Mirrors the Stop UX of /model/api/test_streaming_live."""
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("test_structuredoutput", "test_tool", "test_chat"):
        abort(400, "unknown action")
    provider_id, model, args = _resolve_test_target(data)
    req_line = json.dumps(
        {"action": action, "provider_id": provider_id, "model": model, "arguments": args}
    )

    def generate():
        proc = subprocess.Popen(
            [sys.executable, "-m", "llm.models_test_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_worker_env(),
        )
        t0 = time.monotonic()
        try:
            assert proc.stdin is not None
            proc.stdin.write((req_line + "\n").encode())
            proc.stdin.close()
            while proc.poll() is None:
                yield json.dumps(
                    {"running": True, "elapsed": round(time.monotonic() - t0, 2)}
                ) + "\n"
                time.sleep(0.2)
            raw = proc.stdout.read() if proc.stdout else b""
            lines = [ln for ln in raw.decode(errors="replace").splitlines() if ln.strip()]
            try:
                result = json.loads(lines[-1])
            except (ValueError, IndexError):
                result = {"ok": False, "error": "test worker produced no result", "kind": action}
            result["done"] = True
            yield json.dumps(result) + "\n"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/model/api/test_streaming_live", methods=["POST"])
def models_test_streaming_live_api() -> Response:
    """Run the streaming probe and stream incremental stat updates back as
    NDJSON (one JSON object per line, throttled to ~100ms). Lets the /model
    UI render a live progress display and offer a Stop button (the client
    aborts the fetch, which closes this generator)."""
    data = request.get_json(silent=True) or {}
    provider_id, model, args = _resolve_test_target(data)

    def generate():
        try:
            for stats in llm.stream_test_streaming(provider_id, model, args):
                yield json.dumps(stats) + "\n"
        except Exception as e:
            yield json.dumps({"error": f"{type(e).__name__}: {e}", "done": True}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/models", methods=["GET"])
def models_legacy_redirect():
    """The page's old plural path — singular now, like /cron and /kanban.
    Kept as a redirect so old bookmarks/links (incl. ?id=) keep working."""
    query = request.query_string.decode()
    return redirect(url_for("models_page") + (f"?{query}" if query else ""))


@app.route("/model", methods=["GET", "POST"])
def models_page() -> str | Response:
    sort_by = request.args.get("sort", "provider")
    if sort_by not in ("provider", "model_name"):
        sort_by = "provider"
    tree = list_model_configs_with_overrides(sort_by=sort_by)
    id_arg = request.args.get("id")
    new_override_arg = request.args.get("new_override")
    detail: dict | None = None
    selected_kind: str | None = None
    selected_uuid = None

    if request.method == "POST" and request.form.get("action") == "delete_override":
        try:
            target_uuid = UUID(request.form.get("target_uuid", ""))
        except ValueError:
            abort(400)
        ov_for_parent = get_model_config_override(target_uuid)
        if ov_for_parent is None:
            abort(404)
        parent_uuid = ov_for_parent.model_config_uuid
        delete_model_config_override(target_uuid)
        return redirect(url_for("models_page", id=parent_uuid))

    if request.method == "POST" and request.form.get("action") == "rename_override":
        try:
            target_uuid = UUID(request.form.get("target_uuid", ""))
        except ValueError:
            abort(400)
        ov = get_model_config_override(target_uuid)
        if ov is None:
            abort(404)
        ov.display_name = (request.form.get("display_name") or "").strip()
        db.session.commit()
        return redirect(url_for("models_page", id=ov.uuid, renamed=1))

    if request.method == "POST" and request.form.get("action") == "rename_config":
        try:
            target_uuid = UUID(request.form.get("target_uuid", ""))
        except ValueError:
            abort(400)
        cfg = get_model_config(target_uuid)
        if cfg is None:
            abort(404)
        cfg.display_name = (request.form.get("display_name") or "").strip()
        db.session.commit()
        return redirect(url_for("models_page", id=cfg.uuid, renamed=1))

    # (Config/override tests run via the JSON endpoint /model/api/test, so they
    # update the page in place without a form POST that would drop the ?id= URL.)

    # New-override save. Tests run via the JSON endpoint (see models_test_api),
    # so the only POST from this form is the save; a passing test sets the
    # `tested` marker client-side.
    if request.method == "POST":
        try:
            cfg_uuid = UUID(request.form.get("config_uuid", ""))
        except ValueError:
            abort(400)
        cfg = get_model_config(cfg_uuid)
        if cfg is None:
            abort(404)
        if not request.form.get("tested"):
            abort(400, "a test must succeed before saving")
        form_data = _new_override_form_data(request.form)
        overrides_dict = _build_overrides_dict(form_data, cfg.provider)
        # Save display_name as-is (possibly empty); the tree falls back to
        # `effective_display_name` which derives a summary from overrides.
        new_ov = create_model_config_override(
            model_config_uuid=cfg.uuid,
            overrides=overrides_dict,
            display_name=form_data["display_name"],
        )
        return redirect(url_for("models_page", id=new_ov.uuid))

    if new_override_arg:
        try:
            c_uuid = UUID(new_override_arg)
        except ValueError:
            abort(400)
        cfg = get_model_config(c_uuid)
        if cfg is None:
            abort(404)
        # ?clone_from=<override_uuid> prepopulates the form from an existing
        # override (the Clone button on the override detail page). We only honor
        # the clone source if it sits under this same parent config — otherwise
        # the user pasted/edited a mismatched URL.
        clone_overrides: dict[str, Any] | None = None
        clone_from_arg = request.args.get("clone_from")
        if clone_from_arg:
            try:
                src_uuid = UUID(clone_from_arg)
            except ValueError:
                abort(400, "invalid clone_from")
            src_ov = get_model_config_override(src_uuid)
            if src_ov is None:
                abort(404, "clone source not found")
            if src_ov.model_config_uuid != cfg.uuid:
                abort(400, "clone source belongs to a different model config")
            clone_overrides = src_ov.overrides or {}
        if clone_overrides is not None:
            effort = (
                ((clone_overrides.get("additional_kwargs") or {}).get("extra_body") or {})
                .get("reasoning", {})
                .get("effort")
            )
            form_data = {
                "display_name": "",
                "temperature": float(clone_overrides.get("temperature", 0.5)),
                "context_window": int(
                    clone_overrides.get("context_window")
                    or cfg.arguments.get("context_window")
                    or 3900
                ),
                "reasoning_effort": effort if effort in ("low", "medium", "high") else "none",
                "is_function_calling_model": bool(clone_overrides.get("is_function_calling_model", False)),
                "should_use_structured_outputs": bool(clone_overrides.get("should_use_structured_outputs", True)),
                "thinking": bool(clone_overrides.get("thinking", False)),
            }
        else:
            form_data = {
                "display_name": "",
                "temperature": 0.5,
                "context_window": int(cfg.arguments.get("context_window") or 3900),
                "reasoning_effort": "none",
                "is_function_calling_model": False,
                "should_use_structured_outputs": True,
                "thinking": False,
            }
        overrides_dict = _build_overrides_dict(form_data, cfg.provider)
        resolved_preview = json.dumps(
            {**cfg.arguments, **overrides_dict, "model": cfg.model_name},
            indent=2,
            default=str,
        )
        detail = {
            "kind": "new_override",
            "parent": cfg,
            "form_data": form_data,
            "test_result": None,
            "resolved_preview": resolved_preview,
            "is_function_calling": _is_function_calling(
                {**cfg.arguments, **overrides_dict}
            ),
            "is_struct": _is_struct({**cfg.arguments, **overrides_dict}),
        }
        selected_kind, selected_uuid = "new_override", c_uuid
    elif id_arg:
        try:
            the_uuid = UUID(id_arg)
        except ValueError:
            abort(400)
        # Resolve the id: try the override table first, then model_config.
        ov = get_model_config_override(the_uuid)
        if ov is not None:
            parent = get_model_config(ov.model_config_uuid)
            try:
                resolved = resolved_arguments(ov.uuid)
            except LookupError:
                resolved = {"_error": "base config missing"}
            detail = {
                "kind": "override",
                "row": ov,
                "parent": parent,
                "overrides_json": json.dumps(ov.overrides, indent=2, default=str),
                "resolved_json": json.dumps(resolved, indent=2, default=str),
                "renamed": bool(request.args.get("renamed")),
                "is_function_calling": _is_function_calling(resolved),
                "is_struct": _is_struct(resolved),
            }
            selected_kind, selected_uuid = "override", ov.uuid
        else:
            cfg = get_model_config(the_uuid)
            if cfg is None:
                abort(404)
            prov = providers.get(cfg.provider)
            native = prov.fetch_native_models()
            model_info = (
                next((m for m in native if m.get("id") == cfg.model_name), None)
                if native is not None
                else None
            )
            detail = {
                "kind": "config",
                "row": cfg,
                "renamed": bool(request.args.get("renamed")),
                "arguments_json": json.dumps(cfg.arguments, indent=2, default=str),
                "size_human": _format_size(cfg.size_bytes),
                "model_info": model_info,
                "provider_reachable": native is not None,
                "is_function_calling": _is_function_calling(cfg.arguments),
                "is_struct": _is_struct(cfg.arguments),
                "provider_display_name": prov.display_name,
                "provider_base_url": prov.base_url(),
            }
            selected_kind, selected_uuid = "config", cfg.uuid

    return render_template_string(
        MODELS_TEMPLATE,
        tree=tree,
        detail=detail,
        selected_kind=selected_kind,
        selected_uuid=selected_uuid,
        sort_by=sort_by,
    )
