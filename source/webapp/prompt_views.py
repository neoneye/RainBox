"""The /prompt page (HTML shell + CSS; the page logic lives in static/prompt.js).

Manages system prompts for LLM personas as a folder tree of versioned prompts.
Every prompt row is one version with its own uuid (deep-linkable via
/prompt?id=<uuid>); cloning is the only way to make a new version, and each
clone links back to the prompt it was based on, so the ancestor chain is the
edit history and the editor pane can 2-way diff against any ancestor.
Persistence is real: the browser-side state (promptFolders / promptItems)
hydrates from and saves to GET/PUT /prompt/api/tree (webapp/prompt_api.py →
db.prompt_load_tree/prompt_save_tree); prompt content autosaves per-prompt.
Mirrors the /git page; desktop-first.
"""
from pathlib import Path

from flask import render_template_string

from .core import app

_PROMPT_JS = Path(__file__).resolve().parent.parent / "static" / "prompt.js"


def _prompt_js_version() -> int:
    """mtime of prompt.js as a cache-buster for the <script src> ?v=."""
    try:
        return int(_PROMPT_JS.stat().st_mtime)
    except OSError:
        return 0


PROMPT_TEMPLATE = """
<!doctype html>
<title>Prompt &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .prompt-split{flex:1;display:grid;grid-template-columns:260px 1fr;min-height:0}
  .prompt-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .prompt-main{overflow:auto;padding:16px 20px;display:flex;flex-direction:column;min-height:0}
  .prompt-actions{display:flex;gap:6px}
  /* Small blue pill buttons, matching /cron's tree-action buttons. */
  .prompt-actions button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer}
  .prompt-actions button:hover{border-color:#2563eb;color:#2563eb}
  /* Hairline dividers between the root node, the actions, and the tree (like /cron). */
  .prompt-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  /* Nested items indent past the parent's label with a guide line, like /cron. */
  .prompt-tree-list,.prompt-tree-list ul{list-style:none;margin:0;padding:0}
  .prompt-tree-list ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  /* Tree node rows — folder + leaf — copied from /cron's .cron-node/.cron-job-node. */
  .prompt-node,.prompt-item-node{-webkit-user-select:none;user-select:none}
  .prompt-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap}
  .prompt-node:hover{background:#f1f5f9}
  .prompt-node.sel{background:#dbeafe;font-weight:600}
  .prompt-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .prompt-ficon svg{width:15px;height:15px;display:block}
  .prompt-item-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151}
  .prompt-item-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .prompt-item-node:hover{background:#f1f5f9}
  .prompt-item-node.sel{background:#dbeafe;font-weight:600}
  /* kebab (3-dot overflow) — hidden until the row is selected; rounded hover. */
  .prompt-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .prompt-node.sel .prompt-kebab,.prompt-item-node.sel .prompt-kebab{visibility:visible}
  .prompt-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .prompt-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .prompt-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .prompt-menu[hidden]{display:none}
  .prompt-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;color:#333;padding:0.45em 0.6em;border-radius:6px}
  .prompt-menu .item:hover{background:#eef0f6}
  .prompt-menu .item.danger{color:#b91c1c}
  .prompt-pane-title{font-weight:600;font-size:1.1rem;margin-bottom:8px}
  #prompt-node-rename{margin:8px 0;display:flex;gap:6px}
  #prompt-node-rename input{font:inherit;padding:4px 6px;flex:0 1 26em;min-width:10em}
  #prompt-folder-desc{margin:8px 0;display:flex;gap:6px;align-items:center}
  .prompt-table{border-collapse:collapse;width:100%;font-size:0.9rem}
  .prompt-table th,.prompt-table td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
  .prompt-name-cell{white-space:nowrap}
  /* Folder rows carry the tree's folder icon in the Name cell (there is no
     Type column); align it with the text baseline. */
  .prompt-name-cell .prompt-ficon{vertical-align:text-bottom;margin-right:4px}
  /* Editor pane: based-on line, toolbar, then the monospace textarea filling the pane. */
  .prompt-meta{margin:2px 0 8px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
  .prompt-toolbar{margin:0 0 8px;display:flex;gap:6px;align-items:center}
  .prompt-toolbar button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.3em 0.8em;font:inherit;font-size:0.85rem;cursor:pointer}
  .prompt-toolbar button:hover{border-color:#2563eb;color:#2563eb}
  .prompt-toolbar select{font:inherit;font-size:0.85rem;padding:0.25em}
  #prompt-save-state{margin-left:auto}
  #prompt-editor{flex:1;display:flex;flex-direction:column;min-height:0}
  #prompt-editor[hidden]{display:none}
  #prompt-content{flex:1;min-height:16em;width:100%;box-sizing:border-box;resize:none;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.88rem;
    line-height:1.45;padding:10px;border:1px solid #d1d5db;border-radius:6px;overflow:auto}
  #prompt-content:focus{outline:2px solid #93c5fd;outline-offset:-1px}
  /* Diff view: unified-diff lines in a monospace scroll box. */
  #prompt-diff{flex:1;min-height:0;overflow:auto;border:1px solid #d1d5db;border-radius:6px;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:0.85rem;line-height:1.45;background:#fff}
  #prompt-diff[hidden]{display:none}
  .prompt-diff-line{padding:0 10px;white-space:pre-wrap;word-break:break-word}
  .prompt-diff-line.add{background:#ecfdf5;color:#065f46}
  .prompt-diff-line.del{background:#fef2f2;color:#991b1b}
  .prompt-diff-line.hunk{background:#eff6ff;color:#1d4ed8}
  .prompt-diff-line.ctx{color:#374151}
  .prompt-diff-line.hdr{color:#6b7280}
  /* Drag-only "move to top level" strip, sitting right under the tree (like /cron). */
  .prompt-root-drop{display:none;margin-top:8px;padding:8px;border:1px dashed #93c5fd;border-radius:6px;color:#2563eb;font-size:0.82rem;text-align:center;-webkit-user-select:none;user-select:none}
  .prompt-tree.prompt-dragging-on .prompt-root-drop{display:block}
  .prompt-root-drop.over{background:#eff6ff;border-color:#2563eb}
  /* drag-and-drop affordances — children don't eat drag events; kebab/menu stay clickable. */
  .prompt-node>*,.prompt-item-node>*{pointer-events:none}
  .prompt-node>.prompt-kebab,.prompt-node>.prompt-menu,.prompt-item-node>.prompt-kebab,.prompt-item-node>.prompt-menu{pointer-events:auto}
  .prompt-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .prompt-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .prompt-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .prompt-dragging{opacity:0.4}
  .ui-modal label{display:flex;flex-direction:column;gap:3px;font-weight:600;font-size:0.9rem;margin:8px 0}
  .ui-modal input[type=text],.ui-modal textarea{font:inherit;font-weight:400;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:5em;resize:vertical}
  /* Button row + button colors come from the shared ui-modal.css
     (.modal-actions / .btn-primary / .btn-cancel). Only .err is page-local. */
  .ui-modal .err{color:#dc2626;font-size:0.85rem;min-height:1em;margin-top:6px}
  .prompt-toast{position:fixed;bottom:20px;right:20px;background:#111827;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
  .prompt-toast.show{opacity:1;transform:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="prompt-split" id="prompt-split">
  <div class="prompt-tree" id="prompt-tree">
    <div class="prompt-node" id="prompt-all">All prompts</div>
    <hr class="prompt-tree-sep">
    <div class="prompt-actions">
      <button onclick="promptAddFolder(false)">+ Folder</button>
      <button onclick="promptAddPrompt()">+ Prompt</button>
    </div>
    <hr class="prompt-tree-sep">
    <ul class="prompt-tree-list" id="prompt-tree-root"></ul>
    <div class="prompt-root-drop" id="prompt-root-drop">&#10515; Move to top level</div>
  </div>
  <div class="prompt-main" id="prompt-main">
    <div class="prompt-pane-title" id="prompt-pane-title"></div>
    <div id="prompt-node-rename" hidden></div>
    <div id="prompt-folder-desc" hidden></div>
    <div class="prompt-table-wrap" id="prompt-table-wrap">
      <table class="prompt-table">
        <thead><tr><th>Name</th><th>Based on</th><th>Updated</th><th></th></tr></thead>
        <tbody id="prompt-rows"></tbody>
      </table>
    </div>
    <div id="prompt-editor" hidden>
      <div class="prompt-meta">
        <span id="prompt-based-on" class="muted"></span>
        <span id="prompt-dates" class="muted"></span>
      </div>
      <div class="prompt-toolbar">
        <button id="prompt-newchat-btn" onclick="promptNewChat()">New chat</button>
        <button id="prompt-clone-btn" onclick="promptCloneCurrent()">Clone</button>
        <button id="prompt-diff-btn" onclick="promptToggleDiff()">Diff against parent</button>
        <select id="prompt-diff-against" hidden onchange="promptDiffAgainstChanged()"></select>
        <span id="prompt-save-state" class="muted"></span>
      </div>
      <textarea id="prompt-content" spellcheck="false"
                placeholder="Write the system prompt here&hellip;"></textarea>
      <div id="prompt-diff" hidden></div>
    </div>
  </div>
</div>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="prompt-folder-modal" hidden>
  <h3 id="prompt-folder-title">New folder</h3>
  <label>Name<input type="text" id="prompt-folder-input" placeholder="Folder name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="prompt-folder-create" onclick="promptAddFolderConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="promptCloseFolderModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="prompt-new-modal" hidden>
  <h3>New prompt</h3>
  <label>Name<input type="text" id="prompt-new-input" placeholder="Prompt name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="prompt-new-create" onclick="promptAddPromptConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="promptCloseNewModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="prompt-desc-modal" hidden>
  <h3>Edit description</h3>
  <label>Description<textarea id="prompt-desc-input"></textarea></label>
  <div class="modal-actions">
    <button class="btn-primary" onclick="promptSaveDescription()">Save</button>
    <button class="btn-cancel" onclick="promptCloseDescModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="prompt-delete-modal" hidden>
  <h3 id="prompt-delete-title">Delete</h3>
  <p id="prompt-delete-msg"></p>
  <div id="prompt-delete-name-row" hidden>
    <p style="margin-bottom:0.3em">Type <strong id="prompt-delete-name"></strong> to confirm:</p>
    <input type="text" id="prompt-delete-input" autocomplete="off">
  </div>
  <div class="modal-actions">
    <button type="button" class="btn-cancel" onclick="promptCloseDeleteModal()">Cancel</button>
    <button type="button" class="btn-danger" id="prompt-delete-confirm">Delete</button>
  </div>
</div>

<div class="prompt-toast" id="prompt-toast"></div>
<script src="/static/prompt.js?v={{ prompt_js_v }}"></script>
"""


@app.route("/prompt")
def prompt_page() -> str:
    return render_template_string(PROMPT_TEMPLATE, prompt_js_v=_prompt_js_version())
