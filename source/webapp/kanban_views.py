"""The /kanban page (HTML shell + CSS; logic in static/kanban.js).

Multiple kanban boards backed by Postgres (kanban_board/column/task/
task_event via db.kanban + webapp/kanban_api.py) — the database-backed
coordination primitive from docs/plan.md: agents track progress here because
markdown todo-list editing is too fragile for small models. Each task carries
a uuid and is assigned to an agent (agents/config.py uuid — stable across role
renames). A board serializes to markdown server-side (the page's "Markdown"
button; GET /kanban/api/board/<uuid>/markdown) so LLMs get context about what
they are working on, with tasks referenced by uuid. See docs/kanban-design.md
for the markdown contract, wire shapes, and agent operations.

The assignee picker is populated at render time from agent_config as
{name, uuid} pairs — names for display, uuids stored.

Pattern mirrors webapp/cron_views.py: shell + inline <style> here, page logic
in a static JS file with an mtime ?v= cache-buster, shared nav, desktop-first,
and no native prompt/confirm/alert (in-page overlays only).
"""

from pathlib import Path

from flask import render_template_string

from .core import app

_KANBAN_JS = Path(__file__).resolve().parent.parent / "static" / "kanban.js"


def _kanban_js_version() -> int:
    """mtime cache-buster (same trick as cron_views): edits to kanban.js change
    the URL, so the browser refetches instead of serving a stale copy."""
    try:
        return int(_KANBAN_JS.stat().st_mtime)
    except OSError:
        return 0


KANBAN_TEMPLATE = """
<!doctype html>
<title>Kanban &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  button{padding:6px 14px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.9rem}
  button:hover{background:#1d4ed8}
  button.kb-secondary{background:#6b7280}
  button.kb-danger{background:#b91c1c}
  code{font-family:ui-monospace,monospace;background:#eef;padding:1px 6px;border-radius:3px}
  /* Split view: boards list | board canvas — same full-height grid as /cron.
     A right sidebar (picker: off / stats, like /chat's) adds a third column
     while open. */
  .kb-split{display:grid;grid-template-columns:230px minmax(0,1fr);grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .kb-split.kb-sidebar-open{grid-template-columns:230px minmax(0,1fr) 240px}
  .kb-sidebar{display:none;overflow:auto;min-height:0;border-left:1px solid #e5e7eb;background:#fbfbfb;padding:0.8em 1em}
  .kb-split.kb-sidebar-open .kb-sidebar{display:block}
  .kb-sidebar-title{margin:0 0 0.7em;font-size:0.95rem;color:#333}
  .kb-stat{display:flex;justify-content:space-between;gap:8px;padding:0.35em 0;font-size:0.9rem;border-bottom:1px solid #eee}
  .kb-stat span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* Developer mode: one row per serialization format with View/Copy actions. */
  .kb-dev-row{display:flex;align-items:center;gap:6px;padding:0.35em 0;font-size:0.9rem;border-bottom:1px solid #eee}
  .kb-dev-row .kb-dev-label{flex:1 1 auto;font-weight:600}
  .kb-dev-row button{padding:3px 10px;font-size:0.78rem}
  .kb-sidebar-mode{font:inherit;font-size:0.8rem;color:#6c757d;border:1px solid #ccc;border-radius:6px;
    padding:0.2em 0.4em;background:#fff;cursor:pointer;margin-left:auto}
  .kb-side{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .kb-side-head{display:flex;gap:6px;margin-bottom:8px}
  .kb-side-head button{padding:3px 10px;font-size:0.8rem}
  /* Tree: nested <ul>s. Indentation + guide line are pure CSS on NESTED lists
     only (the double-descendant selector skips the root list). */
  .kb-tree-list{list-style:none;margin:0;padding:0}
  .kb-tree-list ul{list-style:none;margin:0;padding:0}
  .kb-tree-list ul ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .kb-node{box-sizing:border-box;display:flex;align-items:center;gap:4px;padding:6px;border-radius:6px;
    cursor:pointer;-webkit-user-select:none;user-select:none;white-space:nowrap}
  .kb-node:hover{background:#f1f5f9}
  .kb-node.sel{background:#dbeafe;font-weight:600}
  .kb-node.kb-drop-into{outline:2px dashed #2563eb;outline-offset:-2px}
  .kb-node.kb-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .kb-node.kb-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  /* Folder icon (inline SVG), sized to the row's font — same as /chat's .chat-ficon. */
  .kb-ficon{display:inline-flex;width:1.05em;height:1.05em;color:#6b7280;flex:0 0 auto}
  .kb-ficon svg{width:100%;height:100%}
  .kb-node-name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* "All boards" root pseudo-node + the drag-only "move to top level" strip. */
  .kb-root-drop{margin:6px 0;padding:7px 6px;border:1px dashed #cbd5e1;border-radius:6px;color:#64748b;
    font-size:0.82rem;text-align:center;display:none}
  .kb-side.dragging-on .kb-root-drop{display:block}
  .kb-side.dragging-on .kb-root-drop.kb-drop-into{outline:2px dashed #2563eb;background:#eff6ff}
  /* Folder-contents detail table (shown in the main area when a folder is
     selected, instead of the board canvas). */
  .kb-folder-table{width:100%;border-collapse:collapse;font-size:0.88rem}
  .kb-folder-table th{text-align:left;color:#6b7280;font-weight:600;font-size:0.78rem;
    text-transform:uppercase;letter-spacing:0.03em;padding:6px 8px;border-bottom:1px solid #e5e7eb}
  .kb-folder-table td{padding:6px 8px;border-bottom:1px solid #f1f5f9}
  .kb-folder-table tr:hover td{background:#f8fafc}
  .kb-ft-name{display:flex;align-items:center;gap:6px}
  .kb-ft-link{color:#2563eb;cursor:pointer;background:none;border:none;font:inherit;padding:0}
  .kb-ft-link:hover{text-decoration:underline}
  /* The two main panes (board canvas vs folder table) toggle via `hidden`;
     this makes the bare attribute win even though panes set display. */
  .kb-main [hidden]{display:none}
  /* kebab (3-dot overflow) menu on the selected tree node — same pattern as
     the cron tree's and the chat room list's. */
  .kb-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;
    width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .kb-node.sel .kb-kebab{visibility:visible}
  .kb-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;
    box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .kb-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .kb-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;
    box-shadow:0 6px 18px rgba(0,0,0,0.12);padding:5px;display:flex;flex-direction:column}
  .kb-menu[hidden]{display:none}
  .kb-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;
    color:#1a1a2e;padding:7px 10px;border-radius:6px}
  .kb-menu .item:hover{background:#eef0f6}
  .kb-menu .item.danger{color:#b91c1c}
  .kb-main{overflow:auto;min-height:0;min-width:0;padding:14px 18px;display:flex;flex-direction:column}
  .kb-board-head{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:4px}
  .kb-board-title{font-weight:700;font-size:1.4rem;margin-right:6px}
  .kb-board-head button{padding:4px 11px;font-size:0.82rem}
  #kb-board-desc{margin:0 0 12px}
  /* Columns: a horizontal row of equal-width lanes; the row itself scrolls
     sideways if lanes outgrow the viewport (desktop-first). */
  .kb-columns{display:flex;gap:14px;align-items:flex-start;flex:1 1 auto;min-height:0;overflow-x:auto;padding-bottom:8px}
  .kb-col{flex:0 0 290px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:10px;
    display:flex;flex-direction:column;max-height:100%}
  .kb-col-head{display:flex;align-items:center;gap:8px;padding:10px 12px 6px;font-weight:700;font-size:0.9rem;color:#374151}
  .kb-col-count{font-weight:400;color:#6b7280;font-size:0.8rem}
  .kb-col-cards{flex:1 1 auto;overflow-y:auto;min-height:40px;padding:4px 10px;display:flex;flex-direction:column;gap:8px}
  /* Placeholder so an empty column reads as deliberately empty (it is not a
     drop obstacle: clicks/drags pass through to the column). */
  .kb-col-empty{color:#9ca3af;font-size:0.82rem;font-style:italic;text-align:center;
    padding:10px 0;border:1px dashed #e5e7eb;border-radius:8px;pointer-events:none}
  .kb-col.kb-drop{outline:2px dashed #2563eb;outline-offset:-4px}
  .kb-col-foot{padding:8px 10px 10px}
  .kb-col-foot button{width:100%;background:none;border:1px dashed #cbd5e1;color:#475569;font-size:0.82rem;padding:6px}
  .kb-col-foot button:hover{background:#eef2ff;border-color:#2563eb;color:#2563eb}
  /* Cards. */
  .kb-card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:9px 11px;cursor:grab;
    box-shadow:0 1px 2px rgba(0,0,0,0.05)}
  .kb-card:hover{border-color:#93c5fd}
  .kb-card.kb-dragging{opacity:0.4}
  .kb-card.kb-drop-before{box-shadow:inset 0 3px 0 0 #2563eb}
  .kb-card-title{font-weight:600;font-size:0.92rem;margin-bottom:5px;word-break:break-word}
  .kb-card-desc{font-size:0.8rem;color:#4b5563;margin-bottom:6px;white-space:pre-line;
    display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
  .kb-card-meta{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .kb-uuid{font-family:ui-monospace,monospace;font-size:0.72rem;color:#6b7280;background:#f3f4f6;
    border-radius:4px;padding:1px 5px}
  .kb-agent{font-size:0.74rem;font-weight:600;color:#3730a3;background:#e0e7ff;border-radius:999px;padding:2px 9px}
  .kb-agent.kb-unassigned{color:#6b7280;background:#f3f4f6;font-weight:400}
  /* Overlays — custom in-page modals; the page uses NO native prompt/confirm/
     alert (a browser can permanently suppress those). Base modal rules
     (backdrop, card, h3, buttons) come from the shared /static/ui-modal.css;
     only the page-specific form-field styling stays here. */
  /* Kanban cards are wider than the canonical 420px and scroll when tall (the
     task modal carries a history list), so keep that sizing as a page override. */
  .ui-modal{width:min(560px,92vw);max-height:90vh;overflow:auto}
  .ui-modal .kb-row{margin:0.6em 0;display:flex;flex-wrap:wrap;gap:14px;align-items:center}
  .ui-modal label{font-weight:600;font-size:0.9rem;display:inline-flex;flex-direction:column;gap:3px}
  .ui-modal input[type=text],.ui-modal select{font-family:inherit;font-size:0.9rem;padding:5px 7px;font-weight:400;min-width:260px}
  .ui-modal textarea{font-family:inherit;font-size:0.9rem;font-weight:400;padding:5px 7px;width:100%;
    min-height:5em;resize:vertical;box-sizing:border-box}
  .ui-modal .err{color:#991b1b;font-weight:600;font-size:0.85rem}
  /* Markdown view: monospace block + copy. */
  #kb-md-pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px;font-size:0.8rem;
    overflow:auto;max-height:55vh;white-space:pre-wrap;font-family:ui-monospace,monospace}
  /* Toast (same pattern as /cron). */
  .kb-toast{position:fixed;bottom:18px;right:18px;max-width:380px;background:#1f2937;color:#fff;
    padding:10px 14px;border-radius:8px;font-size:0.9rem;box-shadow:0 4px 14px rgba(0,0,0,0.3);
    z-index:2000;opacity:0;transition:opacity .25s;pointer-events:none}
  .kb-toast.show{opacity:1}
  /* Task history (the kanban_task_event audit trail) inside the edit modal. */
  #kb-t-events{margin-top:10px;border-top:1px solid #e5e7eb;padding-top:8px;max-height:180px;overflow:auto}
  .kb-events-title{font-weight:700;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280;margin-bottom:5px}
  .kb-event{font-size:0.8rem;margin:3px 0}
  .kb-event-kind{font-weight:600;color:#3730a3}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="kb-split">
<aside class="kb-side" id="kb-side">
  <div class="kb-side-head">
    <button onclick="kbNewBoard()">+ Board</button>
    <button class="kb-secondary" onclick="kbNewFolder()">+ Folder</button>
  </div>
  <div id="kb-tree-root" class="kb-tree-list"></div>
  <div id="kb-root-drop" class="kb-root-drop">Move to top level</div>
</aside>
<section class="kb-main">
  <div id="kb-empty" class="muted">No boards yet &mdash; create one with &ldquo;+ Board&rdquo;.</div>
  <div id="kb-folder-view" hidden>
    <div class="kb-board-head">
      <span id="kb-folder-view-name" class="kb-board-title"></span>
    </div>
    <div id="kb-folder-view-body"></div>
  </div>
  <div id="kb-board" hidden>
    <div class="kb-board-head">
      <span id="kb-board-name" class="kb-board-title"></span>
      <button class="kb-secondary" onclick="kbEditBoard()">Edit board</button>
      <select id="kb-sidebar-mode" class="kb-sidebar-mode" title="Right sidebar">
        <option value="hidden">Sidebar: off</option>
        <option value="stats">Stats</option>
        <option value="dev">Developer</option>
      </select>
    </div>
    <div id="kb-board-desc" class="muted"></div>
    <div id="kb-columns" class="kb-columns"></div>
  </div>
</section>
<aside id="kb-sidebar" class="kb-sidebar"></aside>
</div>

<div id="ui-modal-backdrop" class="ui-modal-backdrop" hidden></div>

<!-- Board create/edit -->
<div id="kb-board-modal" class="ui-modal" hidden>
  <h3 id="kb-board-modal-title">New board</h3>
  <div class="kb-row">
    <label style="width:100%">Name <input type="text" id="kb-b-name" autocomplete="off" placeholder="board name"></label>
  </div>
  <div class="kb-row">
    <label style="width:100%">Description <textarea id="kb-b-desc" rows="3"
      placeholder="optional — included in the markdown serialization as context for the LLM"></textarea></label>
  </div>
  <span class="err" id="kb-b-err"></span>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="kbCloseModals()">Cancel</button>
    <button id="kb-b-save" class="btn-primary" onclick="kbSaveBoardModal()">Create board</button>
  </div>
</div>

<!-- Folder create/rename -->
<div id="kb-folder-modal" class="ui-modal" hidden>
  <h3 id="kb-folder-modal-title">New folder</h3>
  <div class="kb-row">
    <label style="width:100%">Name <input type="text" id="kb-f-name" autocomplete="off" placeholder="folder name"></label>
  </div>
  <span class="err" id="kb-f-err"></span>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="kbCloseModals()">Cancel</button>
    <button id="kb-f-save" class="btn-primary" onclick="kbSaveFolderModal()">Create folder</button>
  </div>
</div>

<!-- Task create/edit -->
<div id="kb-task-modal" class="ui-modal" hidden>
  <h3 id="kb-task-modal-title">New task</h3>
  <div class="kb-row">
    <label style="width:100%">Title <input type="text" id="kb-t-title" autocomplete="off" placeholder="what needs doing (required)"></label>
  </div>
  <div class="kb-row">
    <label style="width:100%">Description <textarea id="kb-t-desc" rows="4"
      placeholder="optional details — serialized under the task in the markdown view"></textarea></label>
  </div>
  <div class="kb-row">
    <label>Assigned agent <select id="kb-t-agent"></select></label>
    <label>Column <select id="kb-t-col"></select></label>
  </div>
  <div class="kb-row muted" id="kb-t-uuid-row" hidden>Task uuid: <code id="kb-t-uuid"></code></div>
  <div class="kb-row muted" id="kb-t-claim" hidden></div>
  <span class="err" id="kb-t-err"></span>
  <div class="modal-actions">
    <button id="kb-t-delete" class="btn-danger" onclick="kbConfirmDeleteTask()" hidden>Delete</button>
    <button class="btn-cancel" onclick="kbCloseModals()">Cancel</button>
    <button id="kb-t-run" class="btn-primary" onclick="kbEnqueueTask()" hidden
      title="Enqueue the assigned agent to execute this task now">Run</button>
    <button id="kb-t-save" class="btn-primary" onclick="kbSaveTaskModal()">Create task</button>
  </div>
  <!-- Audit trail (kanban_task_event): UI saves + agent operations alike. -->
  <div id="kb-t-events" hidden></div>
</div>

<!-- Serialization view (markdown / json — the LLM-facing read views) -->
<div id="kb-md-modal" class="ui-modal" style="width:min(760px,94vw)" hidden>
  <h3 id="kb-md-title">Markdown</h3>
  <div class="muted" style="margin-bottom:8px">The LLM-facing serialization of this board; tasks carry their uuid.</div>
  <pre id="kb-md-pre"></pre>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="kbCloseModals()">Close</button>
    <button class="btn-primary" onclick="kbCopyShownSerialization()">Copy</button>
  </div>
</div>

<!-- Generic confirm overlay (the page bans native dialogs) -->
<div id="kb-confirm-modal" class="ui-modal" hidden>
  <h3 id="kb-confirm-title">Delete?</h3>
  <div class="kb-row" id="kb-confirm-text"></div>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="kbCloseModals()">Cancel</button>
    <button id="kb-confirm-yes" class="btn-danger">Delete</button>
  </div>
</div>

<div id="kb-toast" class="kb-toast"></div>

<script>
  // Assignee picker data, injected at render time from agent_config:
  // {name, uuid} pairs — names for display, uuids stored on the task.
  window.KANBAN_AGENTS = {{ agents | tojson }};
</script>
<script src="/static/kanban.js?v={{ kanban_js_v }}"></script>
"""


@app.route("/kanban")
def kanban_page() -> str:
    from agents.config import agent_config

    agents = [{"name": name, "uuid": str(entry["uuid"])}
              for name, entry in sorted(agent_config.items())]
    return render_template_string(
        KANBAN_TEMPLATE,
        agents=agents,
        kanban_js_v=_kanban_js_version(),
    )
