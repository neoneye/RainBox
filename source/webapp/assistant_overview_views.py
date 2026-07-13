"""The /assistant-overview page — a searchable, sortable, paginated table of
all Assistant ReAct loops, a roomier replacement for the cramped /assistant
left panel. The shell is server-rendered; static/assistant-overview.js hydrates
it from /assistant-overview/api/runs and links each row to the inspector at
/assistant?id=<uuid>."""
from pathlib import Path

from flask import render_template_string

from .core import app

_JS = Path(__file__).resolve().parent.parent / "static" / "assistant-overview.js"


def _js_version() -> int:
    """mtime of assistant-overview.js as a cache-buster: the <script src>
    carries ?v=<this>, so an edit changes the URL and the browser refetches
    instead of serving a stale copy against a newer API."""
    try:
        return int(_JS.stat().st_mtime)
    except OSError:
        return 0


OVERVIEW_TEMPLATE = """
<!doctype html>
<title>Assistant overview &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#fbfbfb;color:#374151}
  .ao-wrap{max-width:1320px;margin:0 auto;padding:24px 28px 56px}
  .ao-filters{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:18px}
  .ao-search{flex:1 1 260px;min-width:200px;padding:8px 12px;border:1px solid #e5e7eb;
    border-radius:6px;font:inherit;font-size:0.9rem;background:#fff;color:#1a1a2e}
  .ao-search:focus{outline:none;border-color:#2563eb}
  .ao-select{padding:8px 11px;border:1px solid #e5e7eb;border-radius:6px;background:#fff;
    font:inherit;font-size:0.9rem;color:#1a1a2e;cursor:pointer}
  .ao-select:focus{outline:none;border-color:#2563eb}
  .ao-tabs{display:flex;gap:2px}
  .ao-tab{appearance:none;background:none;border:none;border-bottom:2px solid transparent;
    cursor:pointer;padding:9px 15px;font:inherit;font-size:0.9rem;font-weight:500;
    color:#6c757d;display:flex;align-items:center;gap:8px}
  .ao-tab.sel{border-bottom-color:#2563eb;font-weight:700;color:#1a1a2e}
  .ao-tab .ct{font-size:0.72rem;font-weight:600;color:#6b7280;background:#f3f4f6;
    padding:1px 8px;border-radius:999px;min-width:20px;text-align:center}
  .ao-tab.sel .ct{color:#2563eb;background:#dbeafe}
  /* Auto layout: every column shrinks to its content (white-space:nowrap on
     all cells) except Summary, which is told to absorb the remaining width and
     ellipsis-truncate. No per-column pixel widths. */
  .ao-table{width:100%;border-collapse:collapse;border:1px solid #e5e7eb;
    border-radius:8px;overflow:hidden;background:#fff}
  .ao-table th,.ao-table td{white-space:nowrap}
  .ao-table th:nth-child(3),.ao-table td:nth-child(3){width:100%;max-width:0}
  /* Numeric columns (Steps, Duration) read better right-aligned. */
  .ao-table th:nth-child(4),.ao-table td:nth-child(4),
  .ao-table th:nth-child(5),.ao-table td:nth-child(5){text-align:right}
  .ao-table th{background:#fbfbfb;border-bottom:1px solid #e5e7eb;text-align:left;
    padding:11px 14px;font-size:0.72rem;font-weight:700;text-transform:uppercase;
    letter-spacing:0.03em;color:#9ca3af;user-select:none}
  .ao-table th.sortable{cursor:pointer}
  /* Cell padding lives on the .ao-cell anchor (not the td) so the row's link
     covers the full clickable area — CMD/Ctrl-click opens the run in a new tab. */
  .ao-table td{padding:0;border-bottom:1px solid #e5e7eb;font-size:0.9rem}
  .ao-table td>.ao-cell{display:block;padding:12px 14px;color:inherit;text-decoration:none}
  .ao-table tbody tr{cursor:pointer}
  .ao-table tbody tr:hover{background:#f1f5f9}
  .ao-date{font-size:0.8rem;color:#374151}
  .ao-time{font-size:0.72rem;color:#9ca3af;font-family:ui-monospace,Menlo,monospace;margin-top:2px}
  .ao-sum{font-weight:600;color:#1a1a2e;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .ao-sum.pending{font-weight:400;color:#98a2b3;font-style:italic}
  .ao-mono{font-family:ui-monospace,Menlo,monospace;font-size:0.8rem;color:#374151}
  .ao-chip{display:inline-flex;align-items:center;gap:6px;font-size:0.72rem;
    font-weight:600;padding:4px 10px;border-radius:999px;white-space:nowrap}
  .ao-chip.running{color:#1d4ed8;background:#dbeafe}
  .ao-chip.resolved{color:#16a34a;background:#dcfce7}
  .ao-chip.unresolved{color:#b91c1c;background:#fee2e2}
  .ao-chip.stopped{color:#6b7280;background:#e5e7eb}
  .ao-chip.pending{color:#9ca3af;background:#e5e7eb}
  .ao-dot{width:7px;height:7px;border-radius:999px;background:#2563eb;
    animation:aopulse 1.5s ease-in-out infinite}
  @keyframes aopulse{0%,100%{opacity:1}50%{opacity:0.3}}
  .ao-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;
    margin-top:18px;flex-wrap:wrap}
  .ao-range{font-size:0.8rem;color:#6b7280}
  .ao-pager{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .ao-pg{min-width:34px;padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;
    background:#fff;color:#374151;font:inherit;font-size:0.8rem;cursor:pointer}
  .ao-pg.sel{border-color:#2563eb;background:#2563eb;color:#fff;font-weight:700}
  .ao-pg:disabled{opacity:0.4;cursor:default}
  .ao-empty{border:1px dashed #d1d5db;border-radius:8px;padding:52px 24px;
    text-align:center;background:#fff}
  .ao-empty .t{font-size:0.95rem;color:#1a1a2e;font-weight:600;margin-bottom:6px}
  .ao-empty .s{font-size:0.8rem;color:#6b7280}
  [hidden]{display:none!important}
</style>
{% include "_nav.html" %}
<style>.pp-nav{margin-bottom:0}</style>
<div class="ao-wrap">
  <div class="ao-filters">
    <input id="ao-search" class="ao-search" type="search" placeholder="Search summary&hellip;">
    <select id="ao-range-select" class="ao-select" aria-label="Time range">
      <option value="all">Any time</option>
      <option value="3h">Last 3 hours</option>
      <option value="6h">Last 6 hours</option>
      <option value="12h">Last 12 hours</option>
      <option value="24h">Last 24 hours</option>
      <option value="48h">Last 48 hours</option>
      <option value="7d">Last 7 days</option>
      <option value="30d">Last 30 days</option>
    </select>
    <div id="ao-tabs" class="ao-tabs"></div>
  </div>
  <table class="ao-table" id="ao-table">
    <thead>
      <tr>
        <th class="sortable" data-sort="started">Date</th>
        <th>Status</th>
        <th class="sortable" data-sort="summary">Summary</th>
        <th class="sortable" data-sort="steps">Steps</th>
        <th class="sortable" data-sort="duration">Duration</th>
      </tr>
    </thead>
    <tbody id="ao-body"></tbody>
  </table>
  <div id="ao-empty" class="ao-empty" hidden>
    <div class="t">No runs match these filters</div>
    <div class="s">Try a different status or search.</div>
  </div>
  <div class="ao-foot" id="ao-foot" hidden>
    <div id="ao-range" class="ao-range"></div>
    <div id="ao-pager" class="ao-pager"></div>
  </div>
</div>
<script src="/static/assistant-overview.js?v={{ js_v }}"></script>
"""


@app.route("/assistant-overview")
def assistant_overview_page() -> str:
    return render_template_string(OVERVIEW_TEMPLATE, js_v=_js_version())
