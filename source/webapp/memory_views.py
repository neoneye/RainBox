"""The /memory review page (HTML shell + CSS; logic lives in static/memory.js).

Inspect the memory store and run provenance-safe lifecycle actions on claims.
Persistence is real: the page hydrates from `GET /memory/api/claims` and mutates
through `POST /memory/api/claims/<uuid>/<action>` (webapp/memory_api.py).

Chrome is copied from /cron (the mature left-panel reference) — split layout,
sidebar list, static "All memories" root node, select-first/toggle-expand,
kebab-on-selected, detail pane, shared ui-modal pattern, toast. The deliberate
divergence (see docs/ui-left-panel-tree.md and the design spec): the left panel
groups by **status facets**, not draggable user folders, so there is no
drag-drop and no whole-tree version-guarded save. The render layer is
grouping-agnostic so a user-created folder tree can be added later as an
additional grouping mode without a rewrite.
"""

from pathlib import Path

from flask import render_template_string

from .core import app

_MEMORY_JS = Path(__file__).resolve().parent.parent / "static" / "memory.js"


def _memory_js_version() -> int:
    """mtime of memory.js as a cache-buster (same trick as /cron)."""
    try:
        return int(_MEMORY_JS.stat().st_mtime)
    except OSError:
        return 0


MEMORY_TEMPLATE = """
<!doctype html>
<title>Memory &mdash; rainbox</title>
<link rel="stylesheet" href="/static/ui-modal.css">
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .muted{color:#6b7280;font-size:0.85rem}
  button{padding:6px 14px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.9rem}
  button:hover{background:#1d4ed8}
  code{font-family:ui-monospace,monospace;background:#eef;padding:1px 6px;border-radius:3px}
  table{border-collapse:collapse;width:100%;margin-top:0.5em}
  th,td{border:1px solid #ccc;padding:5px 9px;text-align:left;vertical-align:top;font-size:0.9rem}
  a.row-details{color:#2563eb;cursor:pointer}
  .err{color:#991b1b;font-weight:600}
  /* Split view: full-height grid (left facet tree | right panel). */
  .mem-split{display:grid;grid-template-columns:260px minmax(0,1fr);grid-template-rows:1fr;flex:1 1 auto;min-height:0}
  .mem-tree{overflow:auto;min-height:0;border-right:1px solid #e5e7eb;background:#fbfbfb;padding:10px;font-size:0.9rem}
  .mem-main{overflow:auto;min-height:0;min-width:0;padding:12px 16px}
  .mem-table-wrap{overflow-x:auto}
  .pane-title{font-weight:700;font-size:1.4rem;margin:0 0 0.6em}
  .pane-title[hidden]{display:none}
  /* Filter bar above the tree. */
  .mem-filters{display:flex;flex-direction:column;gap:6px;margin:2px 0}
  .mem-filters input,.mem-filters select{font:inherit;font-size:0.85rem;padding:4px 6px;border:1px solid #d1d5db;border-radius:6px}
  .mem-filter-row{display:flex;gap:6px}
  .mem-filter-row select{flex:1 1 0;min-width:0}
  /* Tree: nested lists with a left guide line on nested (claim) lists only. */
  .mem-tree-list,.mem-tree-list ul{list-style:none;margin:0;padding:0}
  .mem-tree-list ul{margin-left:0.85em;border-left:1px solid #e5e7eb;padding-left:0.35em}
  .mem-tree-sep{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
  .mem-node,.mem-claim-node{-webkit-user-select:none;user-select:none}
  /* Rows are anchors (CMD/Ctrl-click opens a new tab) — suppress link styling. */
  .mem-node{display:flex;align-items:center;gap:4px;padding:8px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;box-sizing:border-box;
            color:inherit;text-decoration:none}
  .mem-node:hover{background:#f1f5f9}
  .mem-node.sel{background:#dbeafe;font-weight:600}
  .mem-ficon{display:inline-flex;align-items:center;color:#6b7280}
  .mem-ficon svg{width:15px;height:15px;display:block}
  .mem-group-count{margin-left:6px;color:#6b7280;font-weight:400;font-size:0.82rem}
  .mem-claim-node{display:flex;align-items:center;gap:4px;padding:4px 4px;border-radius:4px;cursor:pointer;color:#374151;
                  text-decoration:none}
  .mem-claim-label{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mem-claim-node:hover{background:#f1f5f9}
  .mem-claim-node.sel{background:#dbeafe;font-weight:600}
  /* kebab (3-dot overflow) menu — visible only on the selected row. */
  .mem-kebab{margin-left:auto;flex:0 0 auto;border:none;background:none;cursor:pointer;color:#6b7280;
             width:1.4rem;height:1.4rem;padding:0;border-radius:5px;display:inline-flex;align-items:center;justify-content:center;visibility:hidden}
  .mem-node.sel .mem-kebab,.mem-claim-node.sel .mem-kebab{visibility:visible}
  .mem-kebab::before{content:"";width:3px;height:3px;border-radius:50%;background:currentColor;
                     box-shadow:-5px 0 0 currentColor,5px 0 0 currentColor}
  .mem-kebab:hover{background:#d2ddf6;color:#1a1a2e}
  .mem-menu{position:fixed;z-index:1000;min-width:160px;background:#fff;border:1px solid #d1d5db;border-radius:8px;
            box-shadow:0 6px 18px rgba(0,0,0,0.14);padding:0.25em;display:flex;flex-direction:column}
  .mem-menu[hidden]{display:none}
  .mem-menu .item{text-align:left;border:none;background:none;cursor:pointer;font:inherit;font-size:0.85rem;
                  color:#333;padding:0.45em 0.6em;border-radius:6px}
  .mem-menu .item:hover{background:#eef0f6}
  .mem-menu .item.danger{color:#b91c1c}
  /* Detail pane (right). */
  .mem-detail[hidden]{display:none}
  .mem-detail-text{font-size:1.15rem;font-weight:600;margin:0 0 0.5em;white-space:pre-wrap}
  .mem-badges{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 0.9em}
  .mem-badge{font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:0.03em;
             padding:2px 8px;border-radius:999px;background:#e5e7eb;color:#374151}
  .mem-badge.status-active{background:#dcfce7;color:#166534}
  .mem-badge.status-candidate{background:#fef9c3;color:#854d0e}
  .mem-badge.status-superseded{background:#e0e7ff;color:#3730a3}
  .mem-badge.status-rejected{background:#fee2e2;color:#991b1b}
  .mem-badge.status-expired{background:#f3f4f6;color:#6b7280}
  .mem-badge.sens-secret{background:#fee2e2;color:#991b1b}
  .mem-badge.stale{background:#fef3c7;color:#92400e}
  .mem-badge.conflict{background:#fce7f3;color:#9d174d}
  .mem-section{margin:0 0 0.9em;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;background:#fbfbfb}
  .mem-section-label{font-weight:700;font-size:0.8rem;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280;margin-bottom:6px}
  .mem-actions{display:flex;flex-wrap:wrap;gap:6px}
  .mem-actions button{padding:5px 11px;font-size:0.85rem}
  .mem-actions button.danger{background:#dc2626}
  .mem-actions button.danger:hover{background:#b91c1c}
  .mem-actions button.secondary{background:#6b7280}
  .mem-actions button.secondary:hover{background:#4b5563}
  .mem-evi-row,.mem-ret-row{font-size:0.85rem;padding:5px 0;border-top:1px solid #eef0f3}
  .mem-evi-row:first-child,.mem-ret-row:first-child{border-top:none}
  .mem-lineage a{color:#2563eb;cursor:pointer}
  .mem-reveal{margin-left:8px;font-size:0.78rem;padding:2px 8px;background:#6b7280}
  .mem-emb-fresh{color:#166534;font-weight:600}
  .mem-emb-stale{color:#92400e;font-weight:600}
  .mem-emb-absent{color:#6b7280}
  tr.row-secret td.mem-text-cell{color:#991b1b}
  /* Transient toast (e.g. "changed elsewhere — reloaded"). */
  .mem-toast{position:fixed;bottom:18px;right:18px;max-width:380px;background:#1f2937;color:#fff;
    padding:10px 14px;border-radius:8px;font-size:0.9rem;box-shadow:0 4px 14px rgba(0,0,0,0.3);
    z-index:2000;opacity:0;transition:opacity .25s;pointer-events:none}
  .mem-toast.show{opacity:1}
  .ui-modal textarea,.ui-modal input[type=text],.ui-modal input[type=datetime-local],.ui-modal select{
    font-family:inherit;font-size:0.9rem;padding:5px 7px;width:100%;box-sizing:border-box}
  .ui-modal textarea{min-height:4em;resize:vertical}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="mem-split">
<aside id="mem-tree" class="mem-tree">
  <a id="mem-all" class="mem-node" href="/memory"><span>All memories</span></a>
  <hr class="mem-tree-sep">
  <div class="mem-filters">
    <input type="text" id="mem-filter-text" placeholder="filter text / subject…" autocomplete="off">
    <div class="mem-filter-row">
      <select id="mem-filter-scope"><option value="">scope: any</option>
        <option value="global">global</option><option value="agent">agent</option>
        <option value="room">room</option><option value="project">project</option></select>
      <select id="mem-filter-kind"><option value="">kind: any</option>
        <option value="fact">fact</option><option value="preference">preference</option>
        <option value="project_decision">project_decision</option>
        <option value="procedure">procedure</option>
        <option value="episode_summary">episode_summary</option></select>
    </div>
    <div class="mem-filter-row">
      <select id="mem-filter-sens"><option value="">sensitivity: any</option>
        <option value="public">public</option><option value="private">private</option>
        <option value="secret">secret</option></select>
    </div>
  </div>
  <hr class="mem-tree-sep">
  <ul id="mem-tree-root" class="mem-tree-list"></ul>
</aside>
<section id="mem-main" class="mem-main">
  <div id="mem-pane-title" class="pane-title" hidden></div>
  <div id="mem-detail" class="mem-detail" hidden></div>
  <div class="mem-table-wrap" id="mem-table-wrap">
    <table>
      <thead><tr>
        <th>status</th><th>text</th><th>scope</th><th>kind</th><th>sensitivity</th>
        <th>used</th><th>embedding</th><th></th>
      </tr></thead>
      <tbody id="mem-rows"></tbody>
    </table>
  </div>
</section>
</div>
<!-- One shared backdrop; each card is a SIBLING of it (docs/ui-modals.md). -->
<div id="ui-modal-backdrop" class="ui-modal-backdrop" hidden></div>
<!-- Correct (= supersede): replace text, old kept as history. -->
<div id="mem-correct-modal" class="ui-modal" hidden>
  <h3>Correct memory</h3>
  <p class="muted">The current memory is superseded (kept as history) and a new active memory is created.</p>
  <textarea id="mem-correct-input" rows="3" placeholder="new text"></textarea>
  <div class="modal-actions">
    <span class="err" id="mem-correct-err"></span>
    <button class="btn-cancel" onclick="memCloseCorrect()">Cancel</button>
    <button id="mem-correct-save" class="btn-primary" onclick="memConfirmCorrect()" disabled>Save</button>
  </div>
</div>
<!-- Sensitivity change. -->
<div id="mem-sens-modal" class="ui-modal" hidden>
  <h3>Change sensitivity</h3>
  <select id="mem-sens-input">
    <option value="public">public</option><option value="private">private</option>
    <option value="secret">secret</option>
  </select>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="memCloseSens()">Cancel</button>
    <button class="btn-primary" onclick="memConfirmSens()">Save</button>
  </div>
</div>
<!-- Expiry set/clear. -->
<div id="mem-expiry-modal" class="ui-modal" hidden>
  <h3>Set expiry</h3>
  <input type="datetime-local" id="mem-expiry-input">
  <div class="modal-actions">
    <button class="btn-cancel" onclick="memCloseExpiry()">Cancel</button>
    <button class="btn-danger" onclick="memConfirmExpiry(true)">Clear expiry</button>
    <button class="btn-primary" onclick="memConfirmExpiry(false)">Save</button>
  </div>
</div>
<!-- Reject/forget confirmation. -->
<div id="mem-reject-modal" class="ui-modal" hidden>
  <h3>Forget this memory?</h3>
  <p class="muted" id="mem-reject-msg"></p>
  <div class="modal-actions">
    <button class="btn-cancel" onclick="memCloseReject()">Cancel</button>
    <button id="mem-reject-confirm" class="btn-danger" onclick="memConfirmReject()">Forget</button>
  </div>
</div>
<div id="mem-toast" class="mem-toast"></div>
<script src="/static/memory.js?v={{ memory_js_v }}"></script>
"""


@app.route("/memory")
def memory_page() -> str:
    return render_template_string(MEMORY_TEMPLATE, memory_js_v=_memory_js_version())
