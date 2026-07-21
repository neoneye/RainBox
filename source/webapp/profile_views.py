"""The /profile page (HTML shell + CSS; the page logic lives in static/profile.js).

Manages person profiles — the structured record of a human (name, locale,
formats, contact) — as a folder tree whose leaf detail pane is a form. The
form's fieldsets are generated server-side from profile_fields.PROFILE_FIELDS,
so the page can never drift from the validator. Persistence is real: the
browser-side state hydrates from and saves to GET/PUT /profile/api/tree
(webapp/profile_api.py → db.profile_load_tree/profile_save_tree); field edits
autosave through a per-profile PUT. The built-in locale templates render
read-only from the same tree GET. Mirrors the /prompt page; desktop-first.
"""
from pathlib import Path

from flask import render_template_string
from markupsafe import Markup, escape

from profile_fields import FIELD_GROUPS, PROFILE_FIELDS

from .core import app

_PROFILE_JS = Path(__file__).resolve().parent.parent / "static" / "profile.js"


def _profile_js_version() -> int:
    """mtime of profile.js as a cache-buster for the <script src> ?v=."""
    try:
        return int(_PROFILE_JS.stat().st_mtime)
    except OSError:
        return 0


def _form_fields_html() -> str:
    """The registry's groups as <fieldset>s in registry order, one label +
    input per field. Inputs carry data-key; profile.js fills, reads, and
    autosaves them. Hints ride as title tooltips (and, on datalist-backed
    fields, as visible placeholders); enum selects get a leading blank option
    (a form affordance — blank means the field is unset). Datalist-backed
    fields also get an advisory warning line profile.js fills when the typed
    value is provably invalid — advisory only, saving is never blocked."""
    parts = []
    for group in FIELD_GROUPS:
        parts.append(f'<fieldset class="profile-fieldset"><legend>{escape(group)}</legend>')
        for f in (x for x in PROFILE_FIELDS if x.group == group):
            fid = f"pf-{f.key}"
            hint = f' title="{escape(f.hint)}"' if f.hint else ""
            parts.append(f'<label for="{fid}"{hint}>{escape(f.label)}</label>')
            if f.kind == "enum":
                opts = ['<option value=""></option>'] + [
                    f'<option value="{escape(c)}">{escape(c)}</option>' for c in f.choices]
                parts.append(f'<select id="{fid}" data-key="{f.key}">{"".join(opts)}</select>')
            elif f.multiline:
                parts.append(f'<textarea id="{fid}" data-key="{f.key}" rows="3"{hint}></textarea>')
            else:
                itype = {"date": "date", "email": "email"}.get(f.kind, "text")
                dl = f' list="profile-dl-{f.datalist}"' if f.datalist else ""
                ph = f' placeholder="{escape(f.hint)}"' if f.datalist and f.hint else ""
                inp = f'<input id="{fid}" data-key="{f.key}" type="{itype}"{dl}{ph}{hint}>'
                if f.key == "timezone":
                    # One-click fill from the browser — non-developers should
                    # not have to know their IANA zone name.
                    inp = ('<div class="pf-inline">' + inp +
                           '<button type="button" id="profile-tz-mine">Use my timezone</button></div>')
                parts.append(inp)
            if f.datalist:
                parts.append(f'<div class="pf-warn" id="pf-warn-{f.key}" hidden></div>')
        if group == "Locale & formats":
            # Live datetime preview from timezone + both format selects — the
            # preview is the documentation for the format enums.
            parts.append('<div id="profile-preview" class="muted"></div>')
        parts.append("</fieldset>")
    return "".join(parts)


PROFILE_TEMPLATE = """
<!doctype html>
<title>Profile &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .profile-split{flex:1;display:grid;grid-template-columns:260px 1fr;min-height:0}
  .profile-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  /* 16px horizontal padding so the pane content starts at the same x as
     /chat's room title and log (260px panel + 1em). */
  .profile-main{overflow:auto;padding:16px;display:flex;flex-direction:column;min-height:0}
  .profile-actions{display:flex;gap:6px}
  /* Small pill buttons, matching /cron's tree-action buttons. */
  .profile-actions button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer}
  .profile-actions button:hover{border-color:#2563eb;color:#2563eb}
  /* Hairline dividers between the root node, the actions, and the tree (like /cron). */
  .profile-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  /* Nested items indent past the parent's label with a guide line, like /cron. */
  .profile-tree-list,.profile-tree-list ul{list-style:none;margin:0;padding:0}
  .profile-tree-list ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  /* Tree node rows — folder + leaf — copied from /cron's .cron-node/.cron-job-node. */
  .profile-node,.profile-item-node{-webkit-user-select:none;user-select:none}
  /* Rows are anchors (CMD/Ctrl-click opens a new tab) — suppress link styling. */
  .profile-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;
                color:inherit;text-decoration:none}
  .profile-node:hover{background:#f1f5f9}
  .profile-node.sel{background:#dbeafe;font-weight:600}
  .profile-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .profile-ficon svg{width:15px;height:15px;display:block}
  .profile-item-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151;
                     text-decoration:none}
  .profile-item-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .profile-item-node:hover{background:#f1f5f9}
  .profile-item-node.sel{background:#dbeafe;font-weight:600}
  /* Subtle read-only tag on built-in template rows. */
  .profile-builtin-tag{flex:0 0 auto;font-size:0.68rem;font-weight:400;color:#6b7280;border:1px solid #d1d5db;
    border-radius:4px;padding:0 4px;margin-left:4px}
  /* kebab (3-dot overflow) — hidden until the row is selected; rounded hover. */
  .profile-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .profile-node.sel .profile-kebab,.profile-item-node.sel .profile-kebab{visibility:visible}
  .profile-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .profile-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  /* A menu-less kebab (the Templates folder) keeps the element for constant
     row height but never becomes visible. */
  .profile-kebab-none{visibility:hidden !important}
  .profile-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .profile-menu[hidden]{display:none}
  .profile-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;color:#333;padding:0.45em 0.6em;border-radius:6px}
  .profile-menu .item:hover{background:#eef0f6}
  .profile-menu .item.danger{color:#b91c1c}
  /* Click-to-rename name display: reads as the node's name (it doubles as
     the pane heading); a hover border + tooltip reveal it opens the rename
     modal. Built-ins render a plain heading instead. */
  #profile-node-rename{margin:0 0 8px}
  #profile-node-rename button{font:inherit;font-size:1.1rem;font-weight:600;color:#1a1a2e;background:none;
    text-align:left;border:1px solid transparent;border-radius:6px;padding:4px 8px;margin-left:-8px;cursor:pointer}
  #profile-node-rename button:hover{border-color:#cbd5e1;background:#f8fafc}
  #profile-node-rename .profile-heading{display:inline-block;font-size:1.1rem;font-weight:600;color:#1a1a2e;padding:4px 8px;margin-left:-8px}
  #profile-folder-desc{margin:8px 0;display:flex;gap:6px;align-items:center}
  #profile-folder-desc button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer}
  #profile-folder-desc button:hover{border-color:#2563eb;color:#2563eb}
  .profile-table{border-collapse:collapse;width:100%;font-size:0.9rem}
  .profile-table th,.profile-table td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
  .profile-name-cell{white-space:nowrap}
  /* Folder rows carry the tree's folder icon in the Name cell (there is no
     Type column); align it with the text baseline. */
  .profile-name-cell .profile-ficon{vertical-align:text-bottom;margin-right:4px}
  /* Form pane: status line, then the registry fieldsets. */
  #profile-form{max-width:560px}
  #profile-form[hidden]{display:none}
  #profile-form-meta{margin:2px 0 8px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;min-height:1.2em}
  #profile-builtin-hint{margin:0 0 8px}
  .profile-fieldset{border:1px solid #e5e7eb;border-radius:8px;margin:0 0 14px;padding:6px 14px 12px;min-width:0}
  .profile-fieldset legend{font-size:0.85rem;font-weight:600;color:#374151;padding:0 4px}
  .profile-fieldset label{display:block;font-size:0.82rem;font-weight:600;color:#374151;margin:8px 0 2px;cursor:default}
  .profile-fieldset input,.profile-fieldset select,.profile-fieldset textarea{font:inherit;width:100%;box-sizing:border-box;
    padding:5px 7px;border:1px solid #d1d5db;border-radius:6px;background:#fff}
  .profile-fieldset textarea{resize:vertical}
  .profile-fieldset input:disabled,.profile-fieldset select:disabled,.profile-fieldset textarea:disabled{background:#f3f4f6;color:#6b7280}
  .profile-fieldset input:focus,.profile-fieldset select:focus,.profile-fieldset textarea:focus{outline:2px solid #93c5fd;outline-offset:-1px}
  #profile-preview{margin-top:8px}
  /* Advisory validation line under datalist-backed fields — amber, never blocking. */
  .pf-warn{color:#b45309;font-size:0.78rem;margin-top:2px}
  .pf-warn[hidden]{display:none}
  .pf-inline{display:flex;gap:6px;align-items:center}
  .pf-inline input{flex:1 1 auto}
  .pf-inline button{flex:0 0 auto;border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:5px 10px;font:inherit;font-size:0.78rem;cursor:pointer;white-space:nowrap}
  .pf-inline button:hover{border-color:#2563eb;color:#2563eb}
  .pf-inline button:disabled{color:#9ca3af;border-color:#e5e7eb;cursor:default}
  .profile-dynamic-row{padding:2px 0}
  /* Knowledge calibration rows: topic+enums on one grid line, note below,
     meta (age + reorder/remove pills) on the right. */
  .profile-cal-row{border:1px solid #eef2f7;border-radius:6px;padding:6px 8px;margin:6px 0}
  .profile-cal-main{display:grid;grid-template-columns:1fr 108px 92px 92px;gap:6px}
  /* Column headers naming the axes, aligned to the row grid (card padding
     8px + 1px border ≈ 9px). */
  .profile-cal-head{display:grid;grid-template-columns:1fr 108px 92px 92px;gap:6px;
    padding:0 9px;margin:8px 0 2px;font-size:0.75rem;font-weight:600;color:#374151}
  .profile-cal-note{margin-top:4px}
  .profile-cal-meta{display:flex;gap:6px;align-items:center;margin-top:4px}
  .profile-cal-age{color:#6b7280;font-size:0.75rem;margin-right:auto}
  .profile-cal-meta button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.1em 0.55em;font:inherit;font-size:0.75rem;cursor:pointer}
  .profile-cal-meta button:hover{border-color:#2563eb;color:#2563eb}
  .profile-cal-meta button:disabled{color:#9ca3af;border-color:#e5e7eb;cursor:default}
  .profile-cal-meta button.danger{color:#b91c1c}
  #profile-cal-add{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer;margin-top:6px}
  #profile-cal-add:hover{border-color:#2563eb;color:#2563eb}
  #profile-cal-status{min-height:1.1em}
  #profile-cal-error{color:#b91c1c;font-size:0.8rem;min-height:1em}
  /* Drag-only "move to top level" strip, sitting right under the tree (like /cron). */
  .profile-root-drop{display:none;margin-top:8px;padding:8px;border:1px dashed #93c5fd;border-radius:6px;color:#2563eb;font-size:0.82rem;text-align:center;-webkit-user-select:none;user-select:none}
  .profile-tree.profile-dragging-on .profile-root-drop{display:block}
  .profile-root-drop.over{background:#eff6ff;border-color:#2563eb}
  /* drag-and-drop affordances — children don't eat drag events; kebab/menu stay clickable. */
  .profile-node>*,.profile-item-node>*{pointer-events:none}
  .profile-node>.profile-kebab,.profile-node>.profile-menu,.profile-item-node>.profile-kebab,.profile-item-node>.profile-menu{pointer-events:auto}
  .profile-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .profile-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .profile-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .profile-dragging{opacity:0.4}
  .ui-modal label{display:flex;flex-direction:column;gap:3px;font-weight:600;font-size:0.9rem;margin:8px 0}
  .ui-modal input[type=text],.ui-modal textarea{font:inherit;font-weight:400;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:5em;resize:vertical}
  /* Button row + button colors come from the shared ui-modal.css
     (.modal-actions / .btn-primary / .btn-cancel). Only .err is page-local. */
  .ui-modal .err{color:#dc2626;font-size:0.85rem;min-height:1em;margin-top:6px}
  .profile-toast{position:fixed;bottom:20px;right:20px;background:#111827;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
  .profile-toast.show{opacity:1;transform:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="profile-split" id="profile-split">
  <div class="profile-tree" id="profile-tree">
    <a class="profile-node" id="profile-all" href="/profile">All profiles</a>
    <hr class="profile-tree-sep">
    <div class="profile-actions">
      <button onclick="profileAddFolder(false)">+ Folder</button>
      <button onclick="profileAddProfile()">+ Profile</button>
    </div>
    <hr class="profile-tree-sep">
    <ul class="profile-tree-list" id="profile-tree-root"></ul>
    <div class="profile-root-drop" id="profile-root-drop">&#10515; Move to top level</div>
  </div>
  <div class="profile-main" id="profile-main">
    <div id="profile-node-rename" hidden></div>
    <div id="profile-folder-desc" hidden></div>
    <div class="profile-table-wrap" id="profile-table-wrap">
      <table class="profile-table">
        <thead><tr><th>Name</th><th>Person</th><th>Language</th><th>Time</th><th>Country</th><th></th></tr></thead>
        <tbody id="profile-rows"></tbody>
      </table>
    </div>
    <div id="profile-form" hidden>
      <div id="profile-form-meta">
        <span id="profile-save-status" class="muted"></span>
      </div>
      <p id="profile-builtin-hint" class="muted" hidden>Built-in template &mdash; Duplicate to make an editable copy.</p>
      {{ form_fields }}
      <fieldset class="profile-fieldset" id="profile-calibration">
        <legend>Knowledge calibration</legend>
        <p class="muted">Self-declared familiarity per topic: level (how much
        they know), stance (prefer or avoid), depth (how much explanation
        they want), and an optional note. Row order is priority order. The
        assistant reads this as the operator's declaration &mdash; context,
        not proof.</p>
        <div id="profile-cal-status" class="muted"></div>
        <div id="profile-cal-error"></div>
        <div id="profile-cal-rows"></div>
        <button type="button" id="profile-cal-add">+ Topic</button>
      </fieldset>
      <fieldset class="profile-fieldset" id="profile-dynamic" hidden>
        <legend>Last seen</legend>
        <div id="profile-dynamic-rows"></div>
      </fieldset>
    </div>
  </div>
</div>

<datalist id="profile-dl-tz"></datalist>
<datalist id="profile-dl-lang"></datalist>
<datalist id="profile-dl-currency"></datalist>
<datalist id="profile-dl-country"></datalist>
<datalist id="profile-dl-topic"></datalist>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="profile-folder-modal" hidden>
  <h3 id="profile-folder-title">New folder</h3>
  <label>Name<input type="text" id="profile-folder-input" placeholder="Folder name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="profile-folder-create" onclick="profileAddFolderConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="profileCloseFolderModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="profile-new-modal" hidden>
  <h3>New profile</h3>
  <label>Name<input type="text" id="profile-new-input" placeholder="Profile name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="profile-new-create" onclick="profileAddProfileConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="profileCloseNewModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="profile-rename-modal" hidden>
  <h3 id="profile-rename-title">Rename</h3>
  <label>Name<input type="text" id="profile-rename-input" autocomplete="off"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="profile-rename-confirm" onclick="profileConfirmRenameModal()" disabled>Rename</button>
    <button class="btn-cancel" onclick="profileCloseRenameModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="profile-desc-modal" hidden>
  <h3>Edit description</h3>
  <label>Description<textarea id="profile-desc-input"></textarea></label>
  <div class="modal-actions">
    <button class="btn-primary" onclick="profileSaveDescription()">Save</button>
    <button class="btn-cancel" onclick="profileCloseDescModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="profile-delete-modal" hidden>
  <h3 id="profile-delete-title">Delete</h3>
  <p id="profile-delete-msg"></p>
  <div id="profile-delete-name-row" hidden>
    <p style="margin-bottom:0.3em">Type <strong id="profile-delete-name"></strong> to confirm:</p>
    <input type="text" id="profile-delete-input" autocomplete="off">
  </div>
  <div class="modal-actions">
    <button type="button" class="btn-cancel" onclick="profileCloseDeleteModal()">Cancel</button>
    <button type="button" class="btn-danger" id="profile-delete-confirm">Delete</button>
  </div>
</div>

<div class="profile-toast" id="profile-toast"></div>
<script src="/static/profile.js?v={{ profile_js_v }}"></script>
"""


@app.route("/profile")
def profile_page() -> str:
    return render_template_string(PROFILE_TEMPLATE,
                                  profile_js_v=_profile_js_version(),
                                  form_fields=Markup(_form_fields_html()))
