"""The /persona page (HTML shell + CSS; the page logic lives in
static/persona.js).

Manages the assistant's personas as a folder tree of free-text bodies.
A persona's uuid is stable for its whole life (deep-linkable via
/persona?id=<uuid>) and every save that changes the text appends a
revision, so the History view can diff any earlier state against the current
one and restore it — by appending, never by rewinding. Persistence follows
docs/ui-tree-persistence.md: the tree PUT only updates existing rows, while
creation and deletion are their own endpoints (webapp/persona_api.py →
db/persona.py). Text is read-only until an explicit Edit → Save.
Mirrors the /prompt page; desktop-first.
"""
from pathlib import Path

from flask import render_template_string

from .core import app

_PERSONA_JS = Path(__file__).resolve().parent.parent / "static" / "persona.js"


def _persona_js_version() -> int:
    """mtime of persona.js as a cache-buster for the <script src> ?v=."""
    try:
        return int(_PERSONA_JS.stat().st_mtime)
    except OSError:
        return 0


PERSONA_TEMPLATE = """
<!doctype html>
<title>Persona &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<!-- CodeMirror 5: markdown-highlighted editor with line numbers + soft wrap. -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/lib/codemirror.min.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .persona-split{flex:1;display:grid;grid-template-columns:260px 1fr;min-height:0}
  .persona-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .persona-main{overflow:auto;padding:16px;display:flex;flex-direction:column;min-height:0}
  .persona-actions{display:flex;gap:6px}
  /* Small blue pill buttons, matching /cron's tree-action buttons. */
  .persona-actions button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer}
  .persona-actions button:hover{border-color:#2563eb;color:#2563eb}
  /* Hairline dividers between the root node, the actions, and the tree (like /cron). */
  .persona-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  /* Nested items indent past the parent's label with a guide line, like /cron. */
  .persona-tree-list,.persona-tree-list ul{list-style:none;margin:0;padding:0}
  .persona-tree-list ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  /* Tree node rows — folder + leaf — copied from /cron's .cron-node/.cron-job-node. */
  .persona-node,.persona-item-node{-webkit-user-select:none;user-select:none}
  /* Rows are anchors (CMD/Ctrl-click opens a new tab) — suppress link styling. */
  .persona-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;
               color:inherit;text-decoration:none}
  .persona-node:hover{background:#f1f5f9}
  .persona-node.sel{background:#dbeafe;font-weight:600}
  .persona-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .persona-ficon svg{width:15px;height:15px;display:block}
  .persona-item-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151;
                    text-decoration:none}
  .persona-item-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .persona-item-node:hover{background:#f1f5f9}
  .persona-item-node.sel{background:#dbeafe;font-weight:600}
  /* kebab (3-dot overflow) — hidden until the row is selected; rounded hover. */
  .persona-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .persona-node.sel .persona-kebab,.persona-item-node.sel .persona-kebab{visibility:visible}
  .persona-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .persona-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .persona-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .persona-menu[hidden]{display:none}
  .persona-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;color:#333;padding:0.45em 0.6em;border-radius:6px}
  .persona-menu .item:hover{background:#eef0f6}
  .persona-menu .item.danger{color:#b91c1c}
  /* Click-to-rename name display: reads as the node's name (it doubles as
     the pane heading); a hover border + tooltip reveal it opens the rename
     modal. */
  #persona-node-rename{margin:0 0 8px}
  #persona-node-rename button{font:inherit;font-size:1.1rem;font-weight:600;color:#1a1a2e;background:none;
    text-align:left;border:1px solid transparent;border-radius:6px;padding:4px 8px;margin-left:-8px;cursor:pointer}
  #persona-node-rename button:hover{border-color:#cbd5e1;background:#f8fafc}
  #persona-folder-desc{margin:8px 0;display:flex;gap:6px;align-items:center}
  .persona-table{border-collapse:collapse;width:100%;font-size:0.9rem}
  .persona-table th,.persona-table td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
  .persona-name-cell{white-space:nowrap}
  /* Folder rows carry the tree's folder icon in the Name cell (there is no
     Type column); align it with the text baseline. */
  .persona-name-cell .persona-ficon{vertical-align:text-bottom;margin-right:4px}
  /* Editor pane: meta line (dates + revision count), toolbar, then the
     monospace textarea filling the pane. */
  .persona-meta{margin:2px 0 8px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
  .persona-toolbar{margin:0 0 8px;display:flex;gap:6px;align-items:center}
  .persona-toolbar button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.3em 0.8em;font:inherit;font-size:0.85rem;cursor:pointer}
  .persona-toolbar button:hover{border-color:#2563eb;color:#2563eb}
  .persona-toolbar select{font:inherit;font-size:0.85rem;padding:0.25em}
  #persona-save-btn{background:#2563eb;border-color:#2563eb;color:#fff}
  #persona-save-btn:hover{background:#1d4ed8;color:#fff}
  #persona-editor{flex:1;display:flex;flex-direction:column;min-height:0}
  #persona-editor[hidden]{display:none}
  /* Edit mode: the editor (meta line + toolbar + CodeMirror) is raised above
     the shared modal backdrop, so everything else on the page is grayed out
     and non-clickable until Save or Cancel. */
  #persona-editor.editing{position:relative;z-index:1600;background:#fff;border-radius:8px}
  /* Read-only affordance: muted page background behind the text until Edit. */
  #persona-editor:not(.editing) .CodeMirror{background:#fbfbfb}
  /* The CodeMirror editor replaces the (hidden) #persona-content textarea. */
  #persona-editor .CodeMirror{flex:1;height:auto;min-height:16em;border:1px solid #d1d5db;border-radius:6px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.88rem;line-height:1.45}
  #persona-editor .CodeMirror-focused{outline:2px solid #93c5fd;outline-offset:-1px}
  #persona-editor .CodeMirror-lines{padding:10px 0}
  #persona-editor .CodeMirror-placeholder{color:#9ca3af}
  /* A muted return symbol marks every HARD line end; a break without it is a
     soft word-wrap (wrapped continuation rows also carry no line number). */
  #persona-editor .CodeMirror pre.CodeMirror-line::after{content:"⏎";color:#c7cdd6}
  /* Diff view: unified-diff lines in a monospace scroll box. */
  #persona-history-diff{flex:1;min-height:0;overflow:auto;border:1px solid #d1d5db;border-radius:6px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.85rem;line-height:1.45;background:#fff}
  #persona-history-diff[hidden]{display:none}
  #persona-history[hidden]{display:none}
  #persona-history-diff{margin-top:10px;max-height:50vh}
  .persona-diff-line{padding:0 10px;white-space:pre-wrap;word-break:break-word}
  .persona-diff-line.add{background:#ecfdf5;color:#065f46}
  .persona-diff-line.del{background:#fef2f2;color:#991b1b}
  .persona-diff-line.hunk{background:#eff6ff;color:#1d4ed8}
  .persona-diff-line.ctx{color:#374151}
  .persona-diff-line.hdr{color:#6b7280}
  /* Drag-only "move to top level" strip, sitting right under the tree (like /cron). */
  .persona-root-drop{display:none;margin-top:8px;padding:8px;border:1px dashed #93c5fd;border-radius:6px;color:#2563eb;font-size:0.82rem;text-align:center;-webkit-user-select:none;user-select:none}
  .persona-tree.persona-dragging-on .persona-root-drop{display:block}
  .persona-root-drop.over{background:#eff6ff;border-color:#2563eb}
  /* drag-and-drop affordances — children don't eat drag events; kebab/menu stay clickable. */
  .persona-node>*,.persona-item-node>*{pointer-events:none}
  .persona-node>.persona-kebab,.persona-node>.persona-menu,.persona-item-node>.persona-kebab,.persona-item-node>.persona-menu{pointer-events:auto}
  .persona-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .persona-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .persona-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .persona-dragging{opacity:0.4}
  .ui-modal label{display:flex;flex-direction:column;gap:3px;font-weight:600;font-size:0.9rem;margin:8px 0}
  .ui-modal input[type=text],.ui-modal textarea{font:inherit;font-weight:400;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:5em;resize:vertical}
  /* Button row + button colors come from the shared ui-modal.css
     (.modal-actions / .btn-primary / .btn-cancel). Only .err is page-local. */
  .ui-modal .err{color:#dc2626;font-size:0.85rem;min-height:1em;margin-top:6px}
  .persona-toast{position:fixed;bottom:20px;right:20px;background:#111827;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
  .persona-toast.show{opacity:1;transform:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="persona-split" id="persona-split">
  <div class="persona-tree" id="persona-tree">
    <a class="persona-node" id="persona-all" href="/persona">All personas</a>
    <hr class="persona-tree-sep">
    <div class="persona-actions">
      <button onclick="personaAddFolder(false)">+ Folder</button>
      <button onclick="personaAddPersona()">+ Persona</button>
    </div>
    <hr class="persona-tree-sep">
    <ul class="persona-tree-list" id="persona-tree-root"></ul>
    <div class="persona-root-drop" id="persona-root-drop">&#10515; Move to top level</div>
  </div>
  <div class="persona-main" id="persona-main">
    <div id="persona-node-rename" hidden></div>
    <div id="persona-folder-desc" hidden></div>
    <div class="persona-table-wrap" id="persona-table-wrap">
      <table class="persona-table">
        <thead><tr><th>Name</th><th>Revisions</th><th>Updated</th><th></th></tr></thead>
        <tbody id="persona-rows"></tbody>
      </table>
    </div>
    <div id="persona-editor" hidden>
      <div class="persona-meta">
        <span id="persona-dates" class="muted"></span>
        <span id="persona-revcount" class="muted"></span>
      </div>
      <div class="persona-toolbar">
        <button id="persona-edit-btn" onclick="personaStartEdit()">Edit</button>
        <button id="persona-save-btn" onclick="personaSaveEdit()" hidden>Save</button>
        <button id="persona-cancel-btn" onclick="personaCancelEdit()" hidden>Cancel</button>
        <button id="persona-history-btn" onclick="personaToggleHistory()">History</button>
      </div>
      <textarea id="persona-content" spellcheck="false"
                placeholder="Describe who the assistant is&hellip;"></textarea>
      <div id="persona-history" hidden>
        <table class="persona-table">
          <thead><tr><th>Saved</th><th>Size</th><th>First line</th><th></th></tr></thead>
          <tbody id="persona-history-rows"></tbody>
        </table>
        <div id="persona-history-diff" hidden></div>
      </div>
    </div>
  </div>
</div>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="persona-folder-modal" hidden>
  <h3 id="persona-folder-title">New folder</h3>
  <label>Name<input type="text" id="persona-folder-input" placeholder="Folder name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="persona-folder-create" onclick="personaAddFolderConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="personaCloseFolderModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="persona-new-modal" hidden>
  <h3>New persona</h3>
  <label>Name<input type="text" id="persona-new-input" placeholder="Persona name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="persona-new-create" onclick="personaAddPersonaConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="personaCloseNewModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="persona-rename-modal" hidden>
  <h3 id="persona-rename-title">Rename</h3>
  <label>Name<input type="text" id="persona-rename-input" autocomplete="off"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="persona-rename-confirm" onclick="personaConfirmRenameModal()" disabled>Rename</button>
    <button class="btn-cancel" onclick="personaCloseRenameModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="persona-desc-modal" hidden>
  <h3>Edit description</h3>
  <label>Description<textarea id="persona-desc-input"></textarea></label>
  <div class="modal-actions">
    <button class="btn-primary" onclick="personaSaveDescription()">Save</button>
    <button class="btn-cancel" onclick="personaCloseDescModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="persona-delete-modal" hidden>
  <h3 id="persona-delete-title">Delete</h3>
  <p id="persona-delete-msg"></p>
  <div id="persona-delete-name-row" hidden>
    <p style="margin-bottom:0.3em">Type <strong id="persona-delete-name"></strong> to confirm:</p>
    <input type="text" id="persona-delete-input" autocomplete="off">
  </div>
  <div class="modal-actions">
    <button type="button" class="btn-cancel" onclick="personaCloseDeleteModal()">Cancel</button>
    <button type="button" class="btn-danger" id="persona-delete-confirm">Delete</button>
  </div>
</div>

<div class="ui-modal" id="persona-restore-modal" hidden>
  <h3>Restore this version?</h3>
  <p id="persona-restore-msg"></p>
  <p class="muted">This appends a new version holding that text. Nothing in
     the history is deleted, so you can undo it the same way.</p>
  <div class="modal-actions">
    <button type="button" class="btn-primary" id="persona-restore-confirm">Restore</button>
    <button type="button" class="btn-cancel" onclick="personaCloseRestoreModal()">Cancel</button>
  </div>
</div>

<div class="persona-toast" id="persona-toast"></div>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/lib/codemirror.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/mode/markdown/markdown.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/addon/display/placeholder.min.js"></script>
<script src="/static/persona.js?v={{ persona_js_v }}"></script>
"""


@app.route("/persona")
def persona_page() -> str:
    return render_template_string(PERSONA_TEMPLATE, persona_js_v=_persona_js_version())
