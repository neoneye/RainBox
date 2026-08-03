"""The /personality page (HTML shell + CSS; the page logic lives in
static/personality.js).

Manages the assistant's personalities as a folder tree of free-text bodies.
A personality's uuid is stable for its whole life (deep-linkable via
/personality?id=<uuid>) and every save that changes the text appends a
revision, so the History view can diff any earlier state against the current
one and restore it — by appending, never by rewinding. Persistence follows
docs/ui-tree-persistence.md: the tree PUT only updates existing rows, while
creation and deletion are their own endpoints (webapp/personality_api.py →
db/personality.py). Text is read-only until an explicit Edit → Save.
Mirrors the /prompt page; desktop-first.
"""
from pathlib import Path

from flask import render_template_string

from .core import app

_PERSONALITY_JS = Path(__file__).resolve().parent.parent / "static" / "personality.js"


def _personality_js_version() -> int:
    """mtime of personality.js as a cache-buster for the <script src> ?v=."""
    try:
        return int(_PERSONALITY_JS.stat().st_mtime)
    except OSError:
        return 0


PERSONALITY_TEMPLATE = """
<!doctype html>
<title>Personality &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<!-- CodeMirror 5: markdown-highlighted editor with line numbers + soft wrap. -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/lib/codemirror.min.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .personality-split{flex:1;display:grid;grid-template-columns:260px 1fr;min-height:0}
  .personality-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .personality-main{overflow:auto;padding:16px;display:flex;flex-direction:column;min-height:0}
  .personality-actions{display:flex;gap:6px}
  /* Small blue pill buttons, matching /cron's tree-action buttons. */
  .personality-actions button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer}
  .personality-actions button:hover{border-color:#2563eb;color:#2563eb}
  /* Hairline dividers between the root node, the actions, and the tree (like /cron). */
  .personality-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  /* Nested items indent past the parent's label with a guide line, like /cron. */
  .personality-tree-list,.personality-tree-list ul{list-style:none;margin:0;padding:0}
  .personality-tree-list ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  /* Tree node rows — folder + leaf — copied from /cron's .cron-node/.cron-job-node. */
  .personality-node,.personality-item-node{-webkit-user-select:none;user-select:none}
  /* Rows are anchors (CMD/Ctrl-click opens a new tab) — suppress link styling. */
  .personality-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;
               color:inherit;text-decoration:none}
  .personality-node:hover{background:#f1f5f9}
  .personality-node.sel{background:#dbeafe;font-weight:600}
  .personality-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .personality-ficon svg{width:15px;height:15px;display:block}
  .personality-item-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151;
                    text-decoration:none}
  .personality-item-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .personality-item-node:hover{background:#f1f5f9}
  .personality-item-node.sel{background:#dbeafe;font-weight:600}
  /* kebab (3-dot overflow) — hidden until the row is selected; rounded hover. */
  .personality-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .personality-node.sel .personality-kebab,.personality-item-node.sel .personality-kebab{visibility:visible}
  .personality-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .personality-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .personality-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .personality-menu[hidden]{display:none}
  .personality-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;color:#333;padding:0.45em 0.6em;border-radius:6px}
  .personality-menu .item:hover{background:#eef0f6}
  .personality-menu .item.danger{color:#b91c1c}
  /* Click-to-rename name display: reads as the node's name (it doubles as
     the pane heading); a hover border + tooltip reveal it opens the rename
     modal. */
  #personality-node-rename{margin:0 0 8px}
  #personality-node-rename button{font:inherit;font-size:1.1rem;font-weight:600;color:#1a1a2e;background:none;
    text-align:left;border:1px solid transparent;border-radius:6px;padding:4px 8px;margin-left:-8px;cursor:pointer}
  #personality-node-rename button:hover{border-color:#cbd5e1;background:#f8fafc}
  #personality-folder-desc{margin:8px 0;display:flex;gap:6px;align-items:center}
  .personality-table{border-collapse:collapse;width:100%;font-size:0.9rem}
  .personality-table th,.personality-table td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
  .personality-name-cell{white-space:nowrap}
  /* Folder rows carry the tree's folder icon in the Name cell (there is no
     Type column); align it with the text baseline. */
  .personality-name-cell .personality-ficon{vertical-align:text-bottom;margin-right:4px}
  /* Editor pane: meta line (dates + revision count), toolbar, then the
     monospace textarea filling the pane. */
  .personality-meta{margin:2px 0 8px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
  .personality-toolbar{margin:0 0 8px;display:flex;gap:6px;align-items:center}
  .personality-toolbar button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.3em 0.8em;font:inherit;font-size:0.85rem;cursor:pointer}
  .personality-toolbar button:hover{border-color:#2563eb;color:#2563eb}
  .personality-toolbar select{font:inherit;font-size:0.85rem;padding:0.25em}
  #personality-save-btn{background:#2563eb;border-color:#2563eb;color:#fff}
  #personality-save-btn:hover{background:#1d4ed8;color:#fff}
  #personality-editor{flex:1;display:flex;flex-direction:column;min-height:0}
  #personality-editor[hidden]{display:none}
  /* Edit mode: the editor (meta line + toolbar + CodeMirror) is raised above
     the shared modal backdrop, so everything else on the page is grayed out
     and non-clickable until Save or Cancel. */
  #personality-editor.editing{position:relative;z-index:1600;background:#fff;border-radius:8px}
  /* Read-only affordance: muted page background behind the text until Edit. */
  #personality-editor:not(.editing) .CodeMirror{background:#fbfbfb}
  /* The CodeMirror editor replaces the (hidden) #personality-content textarea. */
  #personality-editor .CodeMirror{flex:1;height:auto;min-height:16em;border:1px solid #d1d5db;border-radius:6px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.88rem;line-height:1.45}
  #personality-editor .CodeMirror-focused{outline:2px solid #93c5fd;outline-offset:-1px}
  #personality-editor .CodeMirror-lines{padding:10px 0}
  #personality-editor .CodeMirror-placeholder{color:#9ca3af}
  /* A muted return symbol marks every HARD line end; a break without it is a
     soft word-wrap (wrapped continuation rows also carry no line number). */
  #personality-editor .CodeMirror pre.CodeMirror-line::after{content:"⏎";color:#c7cdd6}
  /* Diff view: unified-diff lines in a monospace scroll box. */
  #personality-history-diff{flex:1;min-height:0;overflow:auto;border:1px solid #d1d5db;border-radius:6px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.85rem;line-height:1.45;background:#fff}
  #personality-history-diff[hidden]{display:none}
  #personality-history[hidden]{display:none}
  #personality-history-diff{margin-top:10px;max-height:50vh}
  .personality-diff-line{padding:0 10px;white-space:pre-wrap;word-break:break-word}
  .personality-diff-line.add{background:#ecfdf5;color:#065f46}
  .personality-diff-line.del{background:#fef2f2;color:#991b1b}
  .personality-diff-line.hunk{background:#eff6ff;color:#1d4ed8}
  .personality-diff-line.ctx{color:#374151}
  .personality-diff-line.hdr{color:#6b7280}
  /* Drag-only "move to top level" strip, sitting right under the tree (like /cron). */
  .personality-root-drop{display:none;margin-top:8px;padding:8px;border:1px dashed #93c5fd;border-radius:6px;color:#2563eb;font-size:0.82rem;text-align:center;-webkit-user-select:none;user-select:none}
  .personality-tree.personality-dragging-on .personality-root-drop{display:block}
  .personality-root-drop.over{background:#eff6ff;border-color:#2563eb}
  /* drag-and-drop affordances — children don't eat drag events; kebab/menu stay clickable. */
  .personality-node>*,.personality-item-node>*{pointer-events:none}
  .personality-node>.personality-kebab,.personality-node>.personality-menu,.personality-item-node>.personality-kebab,.personality-item-node>.personality-menu{pointer-events:auto}
  .personality-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .personality-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .personality-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .personality-dragging{opacity:0.4}
  .ui-modal label{display:flex;flex-direction:column;gap:3px;font-weight:600;font-size:0.9rem;margin:8px 0}
  .ui-modal input[type=text],.ui-modal textarea{font:inherit;font-weight:400;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:5em;resize:vertical}
  /* Button row + button colors come from the shared ui-modal.css
     (.modal-actions / .btn-primary / .btn-cancel). Only .err is page-local. */
  .ui-modal .err{color:#dc2626;font-size:0.85rem;min-height:1em;margin-top:6px}
  .personality-toast{position:fixed;bottom:20px;right:20px;background:#111827;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
  .personality-toast.show{opacity:1;transform:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="personality-split" id="personality-split">
  <div class="personality-tree" id="personality-tree">
    <a class="personality-node" id="personality-all" href="/personality">All personalities</a>
    <hr class="personality-tree-sep">
    <div class="personality-actions">
      <button onclick="personalityAddFolder(false)">+ Folder</button>
      <button onclick="personalityAddPersonality()">+ Personality</button>
    </div>
    <hr class="personality-tree-sep">
    <ul class="personality-tree-list" id="personality-tree-root"></ul>
    <div class="personality-root-drop" id="personality-root-drop">&#10515; Move to top level</div>
  </div>
  <div class="personality-main" id="personality-main">
    <div id="personality-node-rename" hidden></div>
    <div id="personality-folder-desc" hidden></div>
    <div class="personality-table-wrap" id="personality-table-wrap">
      <table class="personality-table">
        <thead><tr><th>Name</th><th>Revisions</th><th>Updated</th><th></th></tr></thead>
        <tbody id="personality-rows"></tbody>
      </table>
    </div>
    <div id="personality-editor" hidden>
      <div class="personality-meta">
        <span id="personality-dates" class="muted"></span>
        <span id="personality-revcount" class="muted"></span>
      </div>
      <div class="personality-toolbar">
        <button id="personality-edit-btn" onclick="personalityStartEdit()">Edit</button>
        <button id="personality-save-btn" onclick="personalitySaveEdit()" hidden>Save</button>
        <button id="personality-cancel-btn" onclick="personalityCancelEdit()" hidden>Cancel</button>
        <button id="personality-history-btn" onclick="personalityToggleHistory()">History</button>
      </div>
      <textarea id="personality-content" spellcheck="false"
                placeholder="Describe who the assistant is&hellip;"></textarea>
      <div id="personality-history" hidden>
        <table class="personality-table">
          <thead><tr><th>Saved</th><th>Size</th><th>First line</th><th></th></tr></thead>
          <tbody id="personality-history-rows"></tbody>
        </table>
        <div id="personality-history-diff" hidden></div>
      </div>
    </div>
  </div>
</div>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="personality-folder-modal" hidden>
  <h3 id="personality-folder-title">New folder</h3>
  <label>Name<input type="text" id="personality-folder-input" placeholder="Folder name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="personality-folder-create" onclick="personalityAddFolderConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="personalityCloseFolderModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="personality-new-modal" hidden>
  <h3>New personality</h3>
  <label>Name<input type="text" id="personality-new-input" placeholder="Personality name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="personality-new-create" onclick="personalityAddPersonalityConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="personalityCloseNewModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="personality-rename-modal" hidden>
  <h3 id="personality-rename-title">Rename</h3>
  <label>Name<input type="text" id="personality-rename-input" autocomplete="off"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="personality-rename-confirm" onclick="personalityConfirmRenameModal()" disabled>Rename</button>
    <button class="btn-cancel" onclick="personalityCloseRenameModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="personality-desc-modal" hidden>
  <h3>Edit description</h3>
  <label>Description<textarea id="personality-desc-input"></textarea></label>
  <div class="modal-actions">
    <button class="btn-primary" onclick="personalitySaveDescription()">Save</button>
    <button class="btn-cancel" onclick="personalityCloseDescModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="personality-delete-modal" hidden>
  <h3 id="personality-delete-title">Delete</h3>
  <p id="personality-delete-msg"></p>
  <div id="personality-delete-name-row" hidden>
    <p style="margin-bottom:0.3em">Type <strong id="personality-delete-name"></strong> to confirm:</p>
    <input type="text" id="personality-delete-input" autocomplete="off">
  </div>
  <div class="modal-actions">
    <button type="button" class="btn-cancel" onclick="personalityCloseDeleteModal()">Cancel</button>
    <button type="button" class="btn-danger" id="personality-delete-confirm">Delete</button>
  </div>
</div>

<div class="ui-modal" id="personality-restore-modal" hidden>
  <h3>Restore this version?</h3>
  <p id="personality-restore-msg"></p>
  <p class="muted">This appends a new version holding that text. Nothing in
     the history is deleted, so you can undo it the same way.</p>
  <div class="modal-actions">
    <button type="button" class="btn-primary" id="personality-restore-confirm">Restore</button>
    <button type="button" class="btn-cancel" onclick="personalityCloseRestoreModal()">Cancel</button>
  </div>
</div>

<div class="personality-toast" id="personality-toast"></div>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/lib/codemirror.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/mode/markdown/markdown.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/codemirror@5.65.16/addon/display/placeholder.min.js"></script>
<script src="/static/personality.js?v={{ personality_js_v }}"></script>
"""


@app.route("/personality")
def personality_page() -> str:
    return render_template_string(PERSONALITY_TEMPLATE, personality_js_v=_personality_js_version())
