"""The /git page (HTML shell + CSS; the page logic lives in static/git.js).

Organizes git repositories into a folder tree. Persistence is real: the
browser-side state (gitFolders / gitRepos) hydrates from and saves to
GET/PUT /git/api/tree (webapp/git_api.py → db.git_load_tree/git_save_tree), so
edits survive a refresh. Repos are added by pointing at an existing repo path on
disk (no cloning). Mirrors the /cron page; desktop-first.
"""
from pathlib import Path

from flask import render_template_string

from .core import app

_GIT_JS = Path(__file__).resolve().parent.parent / "static" / "git.js"


def _git_js_version() -> int:
    """mtime of git.js as a cache-buster for the <script src> ?v=."""
    try:
        return int(_GIT_JS.stat().st_mtime)
    except OSError:
        return 0


GIT_TEMPLATE = """
<!doctype html>
<title>Git &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .git-split{flex:1;display:grid;grid-template-columns:260px 1fr;min-height:0}
  .git-tree{border-right:1px solid #e5e7eb;background:#fbfbfb;overflow:auto;padding:10px;display:flex;flex-direction:column;gap:6px}
  .git-main{overflow:auto;padding:16px 20px}
  .git-actions{display:flex;gap:6px}
  /* Small blue pill buttons, matching /cron's tree-action buttons. */
  .git-actions button{padding:3px 8px;font-size:0.75rem;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}
  .git-actions button:hover{background:#1d4ed8}
  /* Hairline dividers between the root node, the actions, and the tree (like /cron). */
  .git-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:0}
  .git-tree-list,.git-tree-list ul{list-style:none;margin:0;padding-left:14px}
  #git-tree-root{padding-left:0}
  .git-node,.git-repo-node{display:flex;align-items:center;gap:6px;padding:3px 6px;border-radius:5px;cursor:pointer;position:relative}
  .git-node:hover,.git-repo-node:hover{background:#f3f4f6}
  .git-node.sel,.git-repo-node.sel{background:#dbeafe}
  .git-ficon{display:inline-flex;color:#6b7280}
  .git-ficon svg{width:16px;height:16px;vertical-align:middle}
  .git-kebab{margin-left:auto;border:0;background:transparent;cursor:pointer;font-size:1.1rem;line-height:1;padding:0 4px;color:#6b7280}
  .git-kebab::before{content:"\\22EF"}
  .git-menu{position:fixed;z-index:50;background:#fff;border:1px solid #d1d5db;border-radius:6px;box-shadow:0 4px 12px rgba(0,0,0,.12);min-width:140px;padding:4px}
  .git-menu .item{display:block;width:100%;text-align:left;border:0;background:transparent;padding:6px 10px;cursor:pointer;font:inherit;border-radius:4px}
  .git-menu .item:hover{background:#f3f4f6}
  .git-pane-title{font-weight:600;font-size:1.1rem;margin-bottom:8px}
  #git-node-rename{margin:8px 0;display:flex;gap:6px}
  #git-node-rename input{font:inherit;padding:4px 6px}
  #git-folder-desc{margin:8px 0;display:flex;gap:6px;align-items:center}
  .git-table{border-collapse:collapse;width:100%;font-size:0.9rem}
  .git-table th,.git-table td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
  .git-name-cell{white-space:nowrap}
  .git-repo-head{margin:10px 0;display:flex;flex-direction:column;gap:4px}
  .git-flist{list-style:none;margin:6px 0;padding:0}
  .git-flist li{padding:2px 0;display:flex;align-items:center;gap:6px}
  .git-root-drop{margin-top:auto;border:1px dashed #cbd5e1;border-radius:6px;padding:8px;text-align:center;color:#94a3b8;font-size:0.85rem;display:none}
  .git-tree.git-dragging-on .git-root-drop{display:block}
  .git-root-drop.over{background:#eff6ff;border-color:#3b82f6;color:#3b82f6}
  .git-dragging{opacity:0.5}
  .git-drop-target{outline:2px solid #3b82f6;outline-offset:-2px}
  .git-drop-before{box-shadow:inset 0 2px 0 #3b82f6}
  .git-drop-after{box-shadow:inset 0 -2px 0 #3b82f6}
  .ui-modal label{display:flex;flex-direction:column;gap:3px;font-weight:600;font-size:0.9rem;margin:8px 0}
  .ui-modal input[type=text],.ui-modal textarea{font:inherit;font-weight:400;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:5em;resize:vertical}
  /* Button row + button colors come from the shared ui-modal.css
     (.modal-actions / .btn-primary / .btn-cancel). Only .err is page-local. */
  .ui-modal .err{color:#dc2626;font-size:0.85rem;min-height:1em;margin-top:6px}
  .git-toast{position:fixed;bottom:20px;right:20px;background:#111827;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
  .git-toast.show{opacity:1;transform:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="git-split" id="git-split">
  <div class="git-tree" id="git-tree">
    <div class="git-node" id="git-all">All repositories</div>
    <hr class="git-tree-sep">
    <div class="git-actions">
      <button onclick="gitAddFolder(false)">+ Folder</button>
      <button onclick="gitAddRepo()">+ Repo</button>
    </div>
    <hr class="git-tree-sep">
    <ul class="git-tree-list" id="git-tree-root"></ul>
    <div class="git-root-drop" id="git-root-drop">Move to top level</div>
  </div>
  <div class="git-main" id="git-main">
    <div class="git-pane-title" id="git-pane-title"></div>
    <div id="git-node-rename" hidden></div>
    <div id="git-folder-desc" hidden></div>
    <div class="git-table-wrap" id="git-table-wrap">
      <table class="git-table">
        <thead><tr><th>Name</th><th>Type</th><th>Path</th><th>Description</th><th></th></tr></thead>
        <tbody id="git-rows"></tbody>
      </table>
    </div>
    <div id="git-repo-detail" hidden></div>
  </div>
</div>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="git-folder-modal" hidden>
  <h3 id="git-folder-title">New folder</h3>
  <label>Name<input type="text" id="git-folder-input" placeholder="Folder name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="git-folder-create" onclick="gitAddFolderConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="gitCloseFolderModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="git-repo-modal" hidden>
  <h3>Add repository</h3>
  <label>Name (optional)<input type="text" id="git-repo-name" placeholder="display name"></label>
  <label>Path<input type="text" id="git-repo-path" placeholder="/path/to/existing/repo"></label>
  <div class="err" id="git-repo-err"></div>
  <div class="modal-actions">
    <button class="btn-primary" id="git-repo-create" onclick="gitAddRepoConfirm()">Add</button>
    <button class="btn-cancel" onclick="gitCloseRepoModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="git-desc-modal" hidden>
  <h3>Edit description</h3>
  <label>Description<textarea id="git-desc-input"></textarea></label>
  <div class="modal-actions">
    <button class="btn-primary" onclick="gitSaveDescription()">Save</button>
    <button class="btn-cancel" onclick="gitCloseDescModal()">Cancel</button>
  </div>
</div>

<div class="git-toast" id="git-toast"></div>
<script src="/static/git.js?v={{ git_js_v }}"></script>
"""


@app.route("/git")
def git_page() -> str:
    return render_template_string(GIT_TEMPLATE, git_js_v=_git_js_version())
