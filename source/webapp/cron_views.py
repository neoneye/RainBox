"""The /cron page (HTML shell + CSS; the page logic lives in static/cron.js).

Defines cron-scheduled jobs that either send a message to an agent/chatroom or
execute a command, organized in a folder tree. This module only renders the
page shell; **persistence is real** — the browser-side state (`cronFolders` /
`cronRowsState`) hydrates from and saves to `GET`/`PUT /cron/api/tree`
(`webapp/cron_api.py` → `db.cron_load_tree`/`cron_save_tree`), so edits survive
a refresh. Scheduled firing is the supervisor loop's cron tick (db.cron).

UI scope is **desktop-first** (tablet acceptable); small-phone layouts are a
non-goal for now.

The template keeps the inline <style> block + shared nav (like the other view
modules), but the JS outgrew the inline pattern (~1.5k lines, no linting or
syntax checking inside a Python string) and moved to **static/cron.js**, served
with an mtime-based ?v= cache-buster so edits show up on reload.
"""

from pathlib import Path

from flask import render_template_string

from .core import app

_CRON_JS = Path(__file__).resolve().parent.parent / "static" / "cron.js"


def _cron_js_version() -> int:
    """mtime of cron.js as a cache-buster: the <script src> carries ?v=<this>,
    so a deploy/edit changes the URL and the browser refetches instead of
    serving a stale cached copy against a newer HTML shell/API."""
    try:
        return int(_CRON_JS.stat().st_mtime)
    except OSError:
        return 0

CRON_TEMPLATE = """
<!doctype html>
<title>Cron &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .builder{margin:1em 0;max-width:900px}
  .builder-title{font-weight:700;font-size:1.5rem;margin:0 0 0.6em}
  /* App-wide modal pattern (docs/ui-modals.md): one shared backdrop + centered
     "card" overlays that are siblings of it. The New-job builder and the
     job-details edit overlays (Edit schedule / Edit action / description /
     delete / folder) are all .ui-modal cards over the single backdrop. */
  .ui-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.35);z-index:1500}
  .ui-modal-backdrop[hidden]{display:none}
  .ui-modal{position:fixed;z-index:1600;left:50%;top:50%;transform:translate(-50%,-50%);
    background:#fff;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,0.25);
    padding:1.2em 1.3em;width:min(560px,92vw);max-height:90vh;overflow:auto}
  .ui-modal[hidden]{display:none}
  /* The New-job builder is a wider card than the canonical 420px default; keep
     its original width so its multi-column schedule rows don't get cramped. */
  .builder.ui-modal{margin:0;max-width:none;width:min(640px,92vw);padding:22px 24px}
  .ui-modal .brow{margin:0.6em 0;display:flex;flex-wrap:wrap;gap:14px;align-items:center}
  .ui-modal label{font-weight:600;font-size:0.9rem;display:inline-flex;flex-direction:column;gap:3px}
  .ui-modal select,.ui-modal input[type=text]{font-family:inherit;font-size:0.9rem;padding:5px 7px;font-weight:400}
  .ui-modal input[type=text]{min-width:220px}
  .ui-modal textarea{font-family:inherit;font-size:0.9rem;font-weight:400;padding:5px 7px;width:100%;min-height:5em;resize:vertical;box-sizing:border-box}
  .ui-modal button:disabled{opacity:0.45;cursor:not-allowed}
  /* Job details: read-only schedule/action summaries with an Edit button, plus
     an inline description editor. */
  .cron-job-detail[hidden]{display:none}
  .cjd-section{margin:0 0 0.9em;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;background:#fbfbfb}
  .cjd-label{font-weight:700;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280;margin-bottom:5px}
  .cjd-value{font-size:0.95rem}
  .cjd-section button{margin-top:9px}
  .cjd-section textarea{font-family:inherit;font-size:0.9rem;font-weight:400;padding:5px 7px;width:360px;max-width:100%;min-height:3.4em;resize:vertical;box-sizing:border-box;display:block}
  .builder .brow{margin:0.6em 0;display:flex;flex-wrap:wrap;gap:14px;align-items:center}
  #cron-name-row[hidden]{display:none}
  .builder label{font-weight:600;font-size:0.9rem;display:inline-flex;flex-direction:column;gap:3px}
  .builder select,.builder input[type=text],.builder textarea{font-family:inherit;font-size:0.9rem;padding:5px 7px;font-weight:400}
  .builder input[type=text],.builder textarea{min-width:220px}
  .builder textarea{width:360px;max-width:100%;min-height:3.4em;resize:vertical;box-sizing:border-box}
  code{font-family:ui-monospace,monospace;background:#eef;padding:1px 6px;border-radius:3px}
  #cron-string{font-size:1.05rem;font-weight:700}
  button{padding:6px 14px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.9rem}
  button:hover{background:#1d4ed8}
  table{border-collapse:collapse;width:100%;margin-top:0.5em}
  th,td{border:1px solid #ccc;padding:5px 9px;text-align:left;vertical-align:top;font-size:0.9rem}
  td button{padding:3px 9px;font-size:0.8rem}
  a.row-details{color:#2563eb;cursor:pointer}
  tr.cron-off{opacity:0.45}
  td.cron-active-cell{white-space:nowrap}
  td.cron-health-cell{white-space:nowrap}
  td.cron-nextrun-cell{white-space:nowrap}
  /* Schedule column: a cron string is short — never let it wrap onto two
     lines (the muted explanation underneath may still wrap). */
  td.cron-sched-cell > code{white-space:nowrap}
  td .cron-ficon{vertical-align:middle}
  /* Name column: keep icon + name on one line; truncate long names with an
     ellipsis (full name shown via the cell's title tooltip). */
  td.cron-name-cell{white-space:nowrap}
  .cron-name{display:inline-block;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle}
  .err{color:#991b1b;font-weight:600}
  /* Transient save-status toast (e.g. "tree changed elsewhere — reloaded").
     The page bans native dialogs, so save failures need an in-page surface. */
  .cron-toast{position:fixed;bottom:18px;right:18px;max-width:380px;background:#1f2937;color:#fff;
    padding:10px 14px;border-radius:8px;font-size:0.9rem;box-shadow:0 4px 14px rgba(0,0,0,0.3);
    z-index:2000;opacity:0;transition:opacity .25s;pointer-events:none}
  .cron-toast.show{opacity:1}
  /* Global-pause banner (top of the right pane) + the Draft badge for jobs
     whose action is still empty (the scheduler skips those). */
  .cron-paused-banner{background:#fef3c7;border:1px solid #f59e0b;color:#92400e;border-radius:8px;
    padding:8px 12px;margin:0 0 0.9em;font-size:0.9rem;font-weight:600}
  .cron-paused-banner[hidden]{display:none}
  .cron-draft{color:#b45309;font-weight:600}
  /* Job-details Health: recent-runs mini table + per-status colors. */
  #cjd-health table{margin-top:0.5em}
  #cjd-health td,#cjd-health th{font-size:0.82rem;padding:3px 7px}
  .crh-ok{color:#15803d;font-weight:600}
  .crh-error{color:#b91c1c;font-weight:600}
  .crh-pending{color:#92400e;font-weight:600}
  /* Split view: full-height grid (left folder tree | right panel), each pane
     scrolls independently — consistent with /chat and /modelgroups. */
  .cron-split{display:grid;grid-template-columns:250px minmax(0,1fr);grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .cron-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .cron-main{overflow:auto;min-height:0;min-width:0;padding:12px 16px}
  .cron-main .builder{margin-top:0}
  .cron-table-wrap{overflow-x:auto}
  .pane-title{font-weight:700;font-size:1.4rem;margin:0 0 0.6em}
  .pane-title[hidden]{display:none}
  /* Rename field at the top of the right pane for the selected node. */
  .node-rename{display:flex;align-items:center;gap:8px;margin:0 0 0.8em}
  .node-rename[hidden]{display:none}
  .node-rename input{font-size:1.1rem;font-weight:600;padding:0.25em 0.45em;border:1px solid #ccc;border-radius:6px;min-width:240px}
  .node-rename input:focus{border-color:#2563eb;outline:none}
  /* Folder detail header (right pane) with the activate/deactivate toggle. */
  .folder-detail{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;margin:0 0 0.9em;padding:9px 12px;border:1px solid #e5e7eb;border-radius:8px;background:#fbfbfb}
  .folder-desc{margin:0 0 0.9em}
  .folder-desc[hidden]{display:none}
  .folder-desc label{display:flex;flex-direction:column;gap:4px;font-weight:600;font-size:0.9rem;color:#374151}
  .folder-desc textarea{font-family:inherit;font-size:0.9rem;font-weight:400;padding:5px 7px;width:360px;max-width:100%;min-height:3.4em;resize:vertical;box-sizing:border-box}
  .folder-detail[hidden]{display:none}
  .folder-detail-name{font-weight:600}
  .folder-active{display:inline-flex;align-items:center;gap:6px;font-size:0.9rem;color:#374151;cursor:pointer;-webkit-user-select:none;user-select:none}
  .folder-active-note{color:#6b7280;font-size:0.85rem}
  .cron-timestamps{color:#6b7280;font-size:0.8rem;flex-basis:100%;margin-top:2px}
  .folder-detail .hint{color:#991b1b;font-size:0.8rem}
  .cron-tree-actions{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
  .cron-tree-actions button{padding:3px 8px;font-size:0.75rem}
  .cron-tree-list,.cron-tree-list ul{list-style:none;margin:0;padding:0}
  .cron-tree-list ul{padding-left:20px}
  .cron-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  /* Only visible while a node is being dragged — an explicit root drop target. */
  .cron-root-drop{display:none;margin-top:8px;padding:8px;border:1px dashed #93c5fd;border-radius:6px;color:#2563eb;font-size:0.82rem;text-align:center;-webkit-user-select:none;user-select:none}
  .cron-tree.cron-dragging-on .cron-root-drop{display:block}
  .cron-root-drop.over{background:#eff6ff;border-color:#2563eb}
  .cron-node,.cron-job-node{-webkit-user-select:none;user-select:none}
  .cron-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap}
  .cron-node:hover{background:#f1f5f9}
  .cron-node.sel{background:#dbeafe;font-weight:600}
  .cron-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .cron-ficon svg{width:15px;height:15px;display:block}
  .cron-job-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151}
  .cron-job-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cron-job-node:hover{background:#f1f5f9}
  .cron-job-node.sel{background:#dbeafe}
  /* Dim only the label (not the whole node) so the kebab menu, which is a
     child of the node, isn't rendered semi-transparent via inherited opacity. */
  .cron-job-node.off .cron-job-label{opacity:0.5}
  .cron-node.off .cron-folder-label,.cron-node.off .cron-ficon{opacity:0.5}
  /* kebab (3-dot overflow) menu on tree items */
  .cron-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;
              width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .cron-node.sel .cron-kebab,.cron-job-node.sel .cron-kebab{visibility:visible}
  .cron-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;
                      box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .cron-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .cron-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;
             box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .cron-menu[hidden]{display:none}
  .cron-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;
                   color:#333;padding:0.45em 0.6em;border-radius:6px}
  .cron-menu .item:hover{background:#eef0f6}
  .cron-menu .item.danger{color:#b91c1c}
  /* drag-and-drop affordances */
  .cron-node>*{pointer-events:none}
  .cron-node>.cron-kebab,.cron-node>.cron-menu{pointer-events:auto}
  .cron-node.cron-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .cron-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .cron-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .cron-dragging{opacity:0.4}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="cron-split">
<aside id="cron-tree" class="cron-tree">
  <div id="cron-all-jobs" class="cron-node"><span>All jobs</span></div>
  <hr class="cron-tree-sep">
  <div class="cron-tree-head">
    <div class="cron-tree-actions">
      <button onclick="cronAddFolder(false)">+ Folder</button>
      <button onclick="cronNewJob()">+ Job</button>
      <button id="cron-pause-btn" onclick="cronTogglePause()"
        title="Global pause: stop all scheduled firing without touching any job/folder toggle">Pause all</button>
    </div>
  </div>
  <hr class="cron-tree-sep">
  <ul id="cron-tree-root" class="cron-tree-list"></ul>
  <div id="cron-root-drop" class="cron-root-drop">&#10515; Move to top level</div>
</aside>
<section id="cron-main" class="cron-main">
<div id="cron-paused-banner" class="cron-paused-banner" hidden>
  &#9208; All cron firing is paused — no job will run until resumed (job/folder toggles are untouched).
</div>
<div id="cron-pane-title" class="pane-title" hidden></div>
<div id="cron-node-rename" class="node-rename" hidden></div>
<div id="cron-folder-detail" class="folder-detail" hidden></div>
<div id="cron-folder-desc" class="folder-desc" hidden></div>
<div id="cron-job-detail" class="cron-job-detail" hidden>
  <div class="cjd-section">
    <div class="cjd-label">Run</div>
    <button onclick="cronRunNow(false)">Run now</button>
    <button onclick="cronRunNow(true)" style="background:#6b7280"
      title="Dry-run: report what the fire would do (message + destination, backup destination, validated command) without doing it">Run debug</button>
    <span class="muted" id="cjd-run-status"></span>
  </div>
  <div class="cjd-section">
    <div class="cjd-label">Schedule</div>
    <div class="cjd-value"><code id="cjd-cron">* * * * *</code> <span class="muted" id="cjd-cron-desc"></span></div>
    <button onclick="cronEditSchedule()">Edit schedule</button>
  </div>
  <div class="cjd-section">
    <div class="cjd-label">Action</div>
    <div class="cjd-value" id="cjd-action"></div>
    <button onclick="cronEditAction()">Edit action</button>
  </div>
  <div class="cjd-section">
    <div class="cjd-label">Description</div>
    <div class="cjd-value" id="cjd-desc-value"></div>
    <button onclick="cronEditDescription()">Edit description</button>
  </div>
  <div class="cjd-section">
    <div class="cjd-label">Health</div>
    <div class="cjd-value" id="cjd-health"></div>
  </div>
</div>
<div class="builder ui-modal" id="cron-builder">
  <div class="builder-title" id="cron-builder-title"></div>
  <div class="brow" id="cron-name-row">
    <label>Name <input type="text" id="f-name" placeholder="short title (required)"></label>
  </div>
  <div class="brow">
    <label>Description <textarea id="f-desc" rows="3" placeholder="optional notes"></textarea></label>
  </div>
  <div class="brow">
    <label>Minute <select id="f-min"></select></label>
    <label>Hour <select id="f-hour"></select></label>
    <label>Day of month <select id="f-dom"></select></label>
    <label>Month <select id="f-mon"></select></label>
    <label>Weekday <select id="f-dow"></select></label>
    <label>Time zone <select id="f-tz">
      <option value="localtime">Local time</option>
      <option value="UTC">UTC</option>
    </select></label>
  </div>
  <div class="brow">
    Schedule: <code id="cron-string">* * * * *</code>
    <span class="muted" id="cron-hint"></span>
  </div>
  <div class="brow">
    <label>Folder <select id="f-folder"></select></label>
  </div>
  <div class="brow">
    <span style="font-weight:600">Action type:</span>
    <label style="flex-direction:row;align-items:center;gap:5px;font-weight:400">
      <input type="radio" name="atype" value="message" checked> Message</label>
    <label style="flex-direction:row;align-items:center;gap:5px;font-weight:400">
      <input type="radio" name="atype" value="command"> Command</label>
  </div>
  <div class="brow" id="msg-fields">
    <label>Target <select id="f-target"></select></label>
    <label>Message <input type="text" id="f-message" placeholder="text to send"></label>
  </div>
  <div class="brow" id="cmd-fields" style="display:none">
    <label>Command <input type="text" id="f-command" placeholder="git pull"></label>
  </div>
  <div class="brow">
    <label>Retry on failure <select id="f-retries">
      <option value="0">off</option><option value="1">1&times;</option>
      <option value="2">2&times;</option><option value="3">3&times;</option>
      <option value="5">5&times;</option><option value="10">10&times;</option>
    </select></label>
  </div>
  <div class="brow">
    <button id="add-btn" onclick="cronAddOrUpdate()">Create job</button>
    <button id="cancel-btn" onclick="cronCancelEdit()" style="display:none;background:#6b7280">Cancel</button>
    <span class="err" id="form-err"></span>
  </div>
</div>

<div class="cron-table-wrap" id="cron-table-wrap">
<table>
  <thead><tr>
    <th>Active</th><th>uuid</th><th>name</th><th>schedule</th><th>next run</th><th>health</th><th>command</th><th>description</th><th></th>
  </tr></thead>
  <tbody id="cron-rows"></tbody>
</table>
</div>
</section>
</div>
<!-- One shared backdrop for every modal on the page (the New-job builder above
     and all the edit overlays below); each card is a SIBLING of it so in-card
     clicks don't bubble to the backdrop's dismiss handler (docs/ui-modals.md). -->
<div id="ui-modal-backdrop" class="ui-modal-backdrop" hidden></div>
<!-- Job-details edit overlays. -->
<div id="cron-sched-modal" class="ui-modal" hidden>
  <div class="builder-title">Edit schedule</div>
  <div class="brow">
    <label>Minute <select id="es-min"></select></label>
    <label>Hour <select id="es-hour"></select></label>
    <label>Day of month <select id="es-dom"></select></label>
    <label>Month <select id="es-mon"></select></label>
    <label>Weekday <select id="es-dow"></select></label>
    <label>Time zone <select id="es-tz">
      <option value="localtime">Local time</option>
      <option value="UTC">UTC</option>
    </select></label>
  </div>
  <div class="brow">Schedule: <code id="es-cron-string">* * * * *</code> <span class="muted" id="es-cron-hint"></span></div>
  <div class="brow">
    <button id="es-save" onclick="cronSaveSchedule()">Save</button>
    <button onclick="cronCloseEditModals()" style="background:#6b7280">Cancel</button>
  </div>
</div>
<div id="cron-action-modal" class="ui-modal" hidden>
  <div class="builder-title">Edit action</div>
  <div class="brow">
    <span style="font-weight:600">Action type:</span>
    <label style="flex-direction:row;align-items:center;gap:5px;font-weight:400">
      <input type="radio" name="ea-atype" value="message" checked> Message</label>
    <label style="flex-direction:row;align-items:center;gap:5px;font-weight:400">
      <input type="radio" name="ea-atype" value="command"> Command</label>
  </div>
  <div class="brow" id="ea-msg-fields">
    <label>Target <select id="ea-target"></select></label>
    <label>Message <input type="text" id="ea-message" placeholder="text to send"></label>
  </div>
  <div class="brow" id="ea-cmd-fields" style="display:none">
    <label>Command <input type="text" id="ea-command" placeholder="git pull"></label>
  </div>
  <div class="brow">
    <label>Retry on failure <select id="ea-retries">
      <option value="0">off</option><option value="1">1&times;</option>
      <option value="2">2&times;</option><option value="3">3&times;</option>
      <option value="5">5&times;</option><option value="10">10&times;</option>
    </select></label>
  </div>
  <div class="brow">
    <button id="ea-save" onclick="cronSaveAction()">Save</button>
    <button onclick="cronCloseEditModals()" style="background:#6b7280">Cancel</button>
    <span class="err" id="ea-err"></span>
  </div>
</div>
<!-- Delete confirmation (custom overlay, not a native dialog so it can't be
     permanently suppressed by the browser). -->
<div id="cron-delete-modal" class="ui-modal" hidden>
  <div class="builder-title">Confirm delete</div>
  <div class="brow" id="cron-delete-msg" style="display:block"></div>
  <div class="brow" id="cron-delete-name-row" hidden>
    <label style="width:100%">Type the folder name to confirm
      <input type="text" id="cron-delete-input" autocomplete="off"></label>
  </div>
  <div class="brow">
    <button id="cron-delete-confirm" style="background:#b91c1c">Delete</button>
    <button onclick="cronCloseDeleteModal()" style="background:#6b7280">Cancel</button>
  </div>
</div>
<!-- Edit description (folder or job) overlay. -->
<div id="cron-desc-modal" class="ui-modal" hidden>
  <div class="builder-title">Edit description</div>
  <div class="brow">
    <label style="width:100%">Description
      <textarea id="cron-desc-input" rows="6" placeholder="optional notes"></textarea></label>
  </div>
  <div class="brow">
    <button onclick="cronSaveDescription()">Save</button>
    <button onclick="cronCloseDescModal()" style="background:#6b7280">Cancel</button>
  </div>
</div>
<!-- New folder / subfolder name (custom overlay, not a native prompt). -->
<div id="cron-folder-modal" class="ui-modal" hidden>
  <div class="builder-title" id="cron-folder-title">New folder</div>
  <div class="brow">
    <label style="width:100%">Folder name
      <input type="text" id="cron-folder-input" autocomplete="off" placeholder="folder name"></label>
  </div>
  <div class="brow">
    <button id="cron-folder-create" onclick="cronAddFolderConfirm()">Create</button>
    <button onclick="cronCloseFolderModal()" style="background:#6b7280">Cancel</button>
  </div>
</div>

<!-- Transient save-status messages (conflict reload, refused save). -->
<div id="cron-toast" class="cron-toast"></div>

<script src="/static/cron.js?v={{ cron_js_v }}"></script>
"""


@app.route("/cron")
def cron_page() -> str:
    return render_template_string(CRON_TEMPLATE, cron_js_v=_cron_js_version())
