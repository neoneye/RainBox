"""The /bridges page (HTML shell + CSS; the page logic lives in static/bridges.js).

Chat-bridge connectors, their folders, and room-to-remote-address bindings in
a left-panel tree (notes/ui-left-panel-tree.md), ported rule-for-rule from
/git. The right pane shows the selected node: a connector's fixed fields,
enabled flag, launch mode, launcher status with Restart and a copyable
manual launch command; a folder's enabled flag and policy; a binding's room,
address, enabled flags (local and effective), and policy with the effective
value and the level that supplied it. Persistence follows
notes/ui-tree-persistence.md against webapp/bridges_api.py. Design:
docs/superpowers/specs/2026-09-09-bridge-settings-design.md.
"""
from pathlib import Path

from flask import render_template_string

from .core import app

_BRIDGES_JS = Path(__file__).resolve().parent.parent / "static" / "bridges.js"


def _bridges_js_version() -> int:
    """mtime of bridges.js as a cache-buster for the <script src> ?v=."""
    try:
        return int(_BRIDGES_JS.stat().st_mtime)
    except OSError:
        return 0


BRIDGES_TEMPLATE = """
<!doctype html>
<title>Bridges &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  .br-split{flex:1;display:grid;grid-template-columns:300px 1fr;min-height:0}
  .br-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .br-main{overflow:auto;padding:16px}
  .br-actions{display:flex;gap:6px;flex-wrap:wrap}
  /* Small pill buttons, matching /git's tree-action buttons. */
  .br-actions button,.br-btn{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;
    padding:0.25em 0.6em;font:inherit;font-size:0.78rem;cursor:pointer}
  .br-actions button:hover,.br-btn:hover{border-color:#2563eb;color:#2563eb}
  .br-actions button:disabled,.br-btn:disabled{opacity:0.5;cursor:default;border-color:#cbd5e1;color:#374151}
  .br-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  .br-tree-list,.br-tree-list ul{list-style:none;margin:0;padding:0}
  .br-tree-list ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .br-node,.br-leaf-node{-webkit-user-select:none;user-select:none}
  /* Rows are anchors (CMD/Ctrl-click opens a new tab) — suppress link styling. */
  .br-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;
           color:inherit;text-decoration:none}
  .br-node:hover{background:#f1f5f9}
  .br-node.sel{background:#dbeafe;font-weight:600}
  .br-node.br-connector{font-weight:600}
  .br-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .br-ficon svg{width:15px;height:15px;display:block}
  .br-leaf-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151;
                text-decoration:none}
  .br-leaf-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .br-leaf-addr{color:#6b7280;font-size:0.78rem;margin-left:4px}
  .br-leaf-node:hover{background:#f1f5f9}
  .br-leaf-node.sel{background:#dbeafe;font-weight:600}
  /* Effectively-disabled nodes are dimmed; their own flag shows in the pane. */
  .br-node.br-off .br-folder-label,.br-leaf-node.br-off .br-leaf-label,.br-leaf-node.br-off .br-leaf-addr{opacity:0.5}
  .br-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .br-node.sel .br-kebab,.br-leaf-node.sel .br-kebab{visibility:visible}
  .br-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .br-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .br-menu{position:fixed;z-index:1000;min-width:150px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .br-menu[hidden]{display:none}
  .br-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;color:#333;padding:0.45em 0.6em;border-radius:6px}
  .br-menu .item:hover{background:#eef0f6}
  .br-menu .item.danger{color:#b91c1c}
  /* Click-to-rename name display doubling as the pane heading (notes/ui-modal-rename.md). */
  #br-node-rename{margin:0 0 8px}
  #br-node-rename button,#br-node-rename .br-heading{font:inherit;font-size:1.1rem;font-weight:600;color:#1a1a2e;background:none;
    text-align:left;border:1px solid transparent;border-radius:6px;padding:4px 8px;margin-left:-8px;cursor:pointer;display:inline-block}
  #br-node-rename button:hover{border-color:#cbd5e1;background:#f8fafc}
  #br-node-rename .br-heading{cursor:default}
  .br-table{border-collapse:collapse;width:100%;font-size:0.9rem}
  .br-table th,.br-table td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
  .br-name-cell{white-space:nowrap}
  /* Detail pane: a key/value grid, then sections. */
  .br-detail{display:flex;flex-direction:column;gap:14px;max-width:900px}
  .br-kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:0.9rem;align-items:baseline}
  .br-kv dt{color:#6b7280;margin:0}
  .br-kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
  .br-section h4,.br-contents-head{margin:0 0 6px;font-size:0.95rem}
  .br-contents-head{margin-top:14px}
  .br-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .br-state{display:inline-block;padding:1px 7px;border-radius:999px;font-size:0.78rem;background:#e5e7eb;color:#374151}
  .br-state.running{background:#dcfce7;color:#166534}
  .br-state.failed,.br-state.credential-missing,.br-state.not-installed{background:#fee2e2;color:#991b1b}
  .br-state.backoff,.br-state.starting,.br-state.stopping{background:#fef3c7;color:#92400e}
  .br-cmd{background:#111827;color:#e5e7eb;padding:10px 12px;border-radius:6px;font-size:0.8rem;overflow:auto;white-space:pre;margin:0}
  .br-policy{border-collapse:collapse;width:100%;font-size:0.88rem}
  .br-policy th,.br-policy td{text-align:left;padding:5px 8px;border-bottom:1px solid #eee;vertical-align:top}
  .br-policy code{font-size:0.82rem}
  .br-src{color:#6b7280;font-size:0.78rem}
  .br-warn{color:#b45309;font-size:0.85rem}
  .br-err{color:#b91c1c;font-size:0.85rem}
  /* drag-and-drop affordances — children don't eat drag events; kebab/menu stay clickable. */
  .br-node>*,.br-leaf-node>*{pointer-events:none}
  .br-node>.br-kebab,.br-node>.br-menu,.br-leaf-node>.br-kebab,.br-leaf-node>.br-menu{pointer-events:auto}
  .br-drop-target{outline:2px solid #2563eb;outline-offset:-2px}
  .br-drop-before{box-shadow:inset 0 2px 0 0 #2563eb}
  .br-drop-after{box-shadow:inset 0 -2px 0 0 #2563eb}
  .br-dragging{opacity:0.4}
  .ui-modal{width:min(520px,92vw)}
  .ui-modal label{display:flex;flex-direction:column;gap:3px;font-weight:600;font-size:0.9rem;margin:8px 0}
  .ui-modal label[hidden]{display:none}
  .ui-modal label.br-check{flex-direction:row;align-items:center;gap:8px;font-weight:400}
  .ui-modal input[type=text],.ui-modal input[type=password],.ui-modal input[type=number],.ui-modal input[type=url],.ui-modal textarea,.ui-modal select{
    font:inherit;font-weight:400;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:5em;resize:vertical}
  .ui-modal .err{color:#dc2626;font-size:0.85rem;min-height:1em;margin-top:6px}
  .ui-modal .hint{color:#6b7280;font-size:0.8rem;font-weight:400}
  .br-toast{position:fixed;bottom:20px;right:20px;background:#111827;color:#fff;padding:10px 14px;border-radius:6px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
  .br-toast.show{opacity:1;transform:none}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="br-split" id="br-split">
  <div class="br-tree" id="br-tree">
    <a class="br-node" id="br-all" href="/bridges">All bridges</a>
    <hr class="br-tree-sep">
    <div class="br-actions">
      <button onclick="brAddConnector()">+ Connector</button>
      <button id="br-add-folder-btn" onclick="brAddFolder()">+ Folder</button>
      <button id="br-add-binding-btn" onclick="brAddBinding()">+ Binding</button>
    </div>
    <hr class="br-tree-sep">
    <ul class="br-tree-list" id="br-tree-root"></ul>
  </div>
  <div class="br-main" id="br-main">
    <div id="br-node-rename" hidden></div>
    <div id="br-detail" class="br-detail" hidden></div>
    <div class="br-table-wrap" id="br-table-wrap">
      <p class="muted" id="br-intro">Connectors are bot identities; folders group bindings; a binding ties one chatroom to one remote channel. Traffic flows only where the connector, every folder above, and the binding itself are enabled.</p>
      <h4 class="br-contents-head" id="br-contents-head" hidden>Contents</h4>
      <table class="br-table">
        <thead><tr><th>Name</th><th>Type</th><th>Details</th><th>Enabled</th><th></th></tr></thead>
        <tbody id="br-rows"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="br-connector-modal" hidden>
  <h3>New connector</h3>
  <label>Name<input type="text" id="br-conn-name" placeholder="e.g. Main Bot" autocomplete="off"></label>
  <label>Platform<select id="br-conn-platform"></select></label>
  <label>Credential variable name<input type="text" id="br-conn-token-env" placeholder="e.g. DISCORD_TOKEN_MAINBOT" autocomplete="off">
    <span class="hint">The variable the bridge process reads. You paste the token itself on the connector pane afterwards; it is stored sealed and never shown again.</span></label>
  <label id="br-conn-base-url-row" hidden>Realm URL<input type="url" id="br-conn-base-url" placeholder="https://chat.example.org"></label>
  <label id="br-conn-identity-row" hidden>Bot identity<input type="text" id="br-conn-identity" placeholder="bot@example.org"></label>
  <div class="err" id="br-conn-err"></div>
  <div class="modal-actions">
    <button class="btn-primary" id="br-conn-create" onclick="brAddConnectorConfirm()">Create</button>
    <button class="btn-cancel" onclick="brCloseConnectorModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="br-credential-modal" hidden>
  <h3 id="br-credential-title">Set token</h3>
  <p class="muted" id="br-credential-desc"></p>
  <label>Token<input type="password" id="br-credential-input" autocomplete="off" spellcheck="false">
    <span class="hint">Sealed with RAINBOX_CREDENTIAL_KEY before it is stored; no page or API ever returns it. Saving replaces the previous value and restarts a running connector.</span></label>
  <div class="err" id="br-credential-err"></div>
  <div class="modal-actions">
    <button class="btn-primary" id="br-credential-save" onclick="brSaveCredential()" disabled>Save</button>
    <button class="btn-cancel" onclick="brCloseCredentialModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="br-folder-modal" hidden>
  <h3 id="br-folder-title">New folder</h3>
  <p class="muted" id="br-folder-where"></p>
  <label>Name<input type="text" id="br-folder-input" placeholder="Folder name"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="br-folder-create" onclick="brAddFolderConfirm()" disabled>Create</button>
    <button class="btn-cancel" onclick="brCloseFolderModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="br-binding-modal" hidden>
  <h3>New binding</h3>
  <p class="muted" id="br-binding-where"></p>
  <label>Chatroom<select id="br-binding-room"></select></label>
  <div id="br-binding-fields"></div>
  <div class="err" id="br-binding-err"></div>
  <div class="modal-actions">
    <button class="btn-primary" id="br-binding-create" onclick="brAddBindingConfirm()">Create</button>
    <button class="btn-cancel" onclick="brCloseBindingModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="br-rename-modal" hidden>
  <h3 id="br-rename-title">Rename</h3>
  <label>Name<input type="text" id="br-rename-input" autocomplete="off"></label>
  <div class="modal-actions">
    <button class="btn-primary" id="br-rename-confirm" onclick="brConfirmRenameModal()" disabled>Rename</button>
    <button class="btn-cancel" onclick="brCloseRenameModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="br-policy-modal" hidden>
  <h3 id="br-policy-title">Policy</h3>
  <p class="muted" id="br-policy-desc"></p>
  <label class="br-check"><input type="checkbox" id="br-policy-inherit"> Inherit (no value at this level)</label>
  <div id="br-policy-control"></div>
  <p class="muted" id="br-policy-effective"></p>
  <div class="err" id="br-policy-err"></div>
  <div class="modal-actions">
    <button class="btn-primary" id="br-policy-save" onclick="brSavePolicy()">Save</button>
    <button class="btn-cancel" onclick="brClosePolicyModal()">Cancel</button>
  </div>
</div>

<div class="ui-modal" id="br-delete-modal" hidden>
  <h3 id="br-delete-title">Delete</h3>
  <p id="br-delete-msg"></p>
  <div id="br-delete-name-row" hidden>
    <p style="margin-bottom:0.3em">Type <strong id="br-delete-name"></strong> to confirm:</p>
    <input type="text" id="br-delete-input" autocomplete="off">
  </div>
  <div class="modal-actions">
    <button type="button" class="btn-cancel" onclick="brCloseDeleteModal()">Cancel</button>
    <button type="button" class="btn-danger" id="br-delete-confirm">Delete</button>
  </div>
</div>

<div class="br-toast" id="br-toast"></div>
<script src="/static/bridges.js?v={{ bridges_js_v }}"></script>
"""


@app.route("/bridges")
def bridges_page() -> str:
    return render_template_string(BRIDGES_TEMPLATE, bridges_js_v=_bridges_js_version())
