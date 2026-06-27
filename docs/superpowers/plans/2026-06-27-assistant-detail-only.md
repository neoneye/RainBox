# Detail-only `/assistant` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strip the left tree from `/assistant`, making it a full-width single-run detail view driven by `?id=`, with the run's actions kebab moved into a header bar at the top of the detail pane.

**Architecture:** Edit the inline `ASSISTANT_TEMPLATE` and `assistant_page()` in `webapp/assistant_views.py`. Remove the `.as-tree` aside, the `run_leaf` macro, the folder icons/CSS/JS, and `_bucket_runs`. Reuse the existing `asKebab`/`#as-menu` JS unchanged, invoked from a new `.as-main-head` button. Update `test_assistant_views.py` to the new behavior.

**Tech Stack:** Python 3 / Flask (`render_template_string`), Jinja2, vanilla JS, pytest (`app.test_client()` via `source/venv/bin/python`).

## Global Constraints

- Tests run with `source/venv/bin/python -m pytest` from `source/` (conftest forces `rainbox_claude`).
- Docs/comments describe current state, not change history.
- Kebab actions and the markdown/control APIs are unchanged; only where the kebab lives changes.
- Render user text via Jinja autoescaping as today (no new `| safe`).
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- Modify: `source/webapp/assistant_views.py` — constants (remove folder icons), template (remove tree, add header bar, full-width main, new empty state), `assistant_page()` handler, remove `_bucket_runs`.
- Modify: `source/webapp/test_assistant_views.py` — drop the `_bucket_runs` test + import + `_FakeRun`; rewrite the folder-render test into a no-tree test; update the empty-state assertions.

---

### Task 1: Make `/assistant` a detail-only view with a header kebab

**Files:**
- Modify: `source/webapp/test_assistant_views.py` (tests first)
- Modify: `source/webapp/assistant_views.py`

**Interfaces:**
- Consumes: existing `_selected_run()`, `_load_run_detail()`, `_format_duration()`, the `asKebab`/`#as-menu` JS, `url_for('assistant_overview_page')`.
- Produces: `assistant_page()` rendering the template with only `selected` + detail context (no `runs`/`folders`/`icon_*`). Template shows `.as-main-head` with `.as-runid` + `.as-kebab` when a run is selected, an overview-pointer empty state otherwise, and no `.as-tree`/`.as-folder`.

- [ ] **Step 1: Rewrite the affected tests to the new behavior**

In `source/webapp/test_assistant_views.py`:

(a) Remove `_bucket_runs` from the import on line 17 — change:
```python
from webapp.assistant_views import _bucket_runs, _format_duration
```
to:
```python
from webapp.assistant_views import _format_duration
```

(b) Delete the `_FakeRun` class and `test_bucket_runs_files_each_run_under_matching_facets` (the whole block from `class _FakeRun:` through `assert running in f["Recent"]["runs"]`).

(c) Replace `test_runs_list_renders` (the `def test_runs_list_renders(...)` body that asserts the folders render) with a no-tree + empty-state test:
```python
def test_assistant_page_has_no_tree_and_points_to_overview(app_ctx, client):
    resp = client.get("/assistant")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The left tree is gone…
    assert "as-tree" not in body
    assert "as-folder" not in body
    # …and the empty state points at the overview (the run finder).
    assert "/assistant-overview" in body
    assert "No run selected" in body
```

(d) Update `test_run_is_addressable_and_shown_by_uuid` (lines ~255-273): the page no longer lists runs, and the empty-state copy changed. Replace its body with:
```python
def test_run_is_addressable_and_shown_by_uuid(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    db.finish_run(run, "finished")
    try:
        # Addressable only by uuid via ?id=; the header kebab offers Copy run id.
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert str(run.uuid) in body
        assert "Copy run id" in body
        assert "as-main-head" in body                 # header bar with the kebab
        assert f"asKebab(event, '{run.uuid}'" in body  # kebab wired to this run
        assert "No run selected" not in body           # a run is selected
        # Only a uuid ?id= resolves: a non-uuid value and the old ?run= don't.
        assert "No run selected" in client.get(
            "/assistant?id=not-a-uuid").get_data(as_text=True)
        assert "No run selected" in client.get(
            f"/assistant?run={run.uuid}").get_data(as_text=True)
    finally:
        _cleanup(run.uuid, room.uuid)
```

- [ ] **Step 2: Run the tests — they fail against the current page**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_views.py -q`
Expected: FAIL — `ImportError`/failures are gone for the bucket test, but the two rewritten tests fail (current page still renders `as-tree`/`as-folder` and the old "Select a run" copy; no `as-main-head`).

- [ ] **Step 3: Remove the folder-icon constants**

In `source/webapp/assistant_views.py`, delete the two assignments `_ICON_FOLDER = (...)` and `_ICON_FOLDER_OPEN = (...)` (the multi-line SVG string constants near the top, ~lines 31-41). They are used only by the tree.

- [ ] **Step 4: Remove the `run_leaf` macro**

Delete the macro block:
```jinja
{% macro run_leaf(r) %}
  <li>
    <div class="as-run-node {{ 'sel' if selected and r.uuid == selected.uuid }}">
      <a class="as-run-link" href="{{ url_for('assistant_page') }}?id={{ r.uuid }}">
        {% if r.status in ('running', 'stopping') %}<span class="as-ind run" title="running">⏳</span>
        {% elif r.status in ('failed', 'killed') %}<span class="as-ind fail" title="failed">✗</span>{% endif %}
        {% if r.summary %}<span class="rsum">{{ r.summary.trigger }}</span>
        {% else %}<span class="rsum pending">summarizing…</span>{% endif %}
      </a>
      <button class="as-kebab" title="actions"
              onclick="asKebab(event, '{{ r.uuid }}', '{{ r.status }}', '{{ r.journal_id or '' }}')"></button>
    </div>
  </li>
{% endmacro %}
```

- [ ] **Step 5: Replace the tree/layout CSS with detail-only + header CSS**

Replace the CSS block that starts at `/* Full-height split:` and ends at the `.as-kebab:hover { ... }` rule (the comment on ~line 100 through the `.as-kebab:hover` line ~143) with:
```css
  /* Full-height single-run detail pane; the run finder is /assistant-overview. */
  .as-main { overflow:auto; min-height:0; min-width:0; flex:1 1 auto;
             padding:12px 18px 3.5rem; }
  .as-empty { color:#667085; padding:1rem 0; }
  .as-empty a { color:#2563eb; }

  /* Detail header: run id + kebab actions menu. */
  .as-main-head { display:flex; align-items:center; gap:0.75rem; margin:0.2rem 0 0.7rem; }
  .as-main-head .as-runid { font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
             font-size:0.95rem; font-weight:600; color:#1a1a2e; word-break:break-all; }
  .as-kebab { margin-left:auto; flex:0 0 auto; border:none; background:none; cursor:pointer;
             color:#6b7280; width:1.9rem; height:1.9rem; padding:0; border-radius:6px;
             display:inline-flex; align-items:center; justify-content:center; }
  .as-kebab::before { content:""; width:3px; height:3px; border-radius:50%; background:currentColor;
             box-shadow:0 -5px 0 currentColor, 0 5px 0 currentColor; }
  .as-kebab:hover { background:#eef0f6; color:#1a1a2e; }
```
(The `.as-menu` / `.as-toast` rules immediately after stay unchanged.)

- [ ] **Step 6: Replace the split body with a detail-only section**

Replace this block:
```jinja
<div class="as-split">
  <aside class="as-tree">
    {% if not runs %}<div class="as-empty">No assistant runs yet.</div>{% endif %}
    {% for f in folders %}
    <details class="as-folder" data-folder="{{ f.name }}" {{ 'open' if f.default_open }}>
      <summary>
        <span class="as-ficon as-ficon-open">{{ icon_open | safe }}</span>
        <span class="as-ficon as-ficon-closed">{{ icon_closed | safe }}</span>
        <span class="as-fname">{{ f.name }}</span>
        <span class="as-group-count">{{ f.count }}</span>
      </summary>
      <ul class="as-tree-list">
        {% for r in f.runs %}{{ run_leaf(r) }}{% endfor %}
        {% if not f.runs %}<li class="as-none">none</li>{% endif %}
      </ul>
    </details>
    {% endfor %}
  </aside>

  {# The .as-main detail pane has a Markdown twin: _run_markdown() serializes the
     same sections (dashboard → summary → trigger → timeline → verdict) for the
     kebab's "View as markdown". Keep the two in sync when editing either. #}
  <section class="as-main">
    {% if not selected %}
      <h1>Timeline</h1>
      <div class="as-empty">Select a run on the left to see its summary and step timeline.</div>
    {% else %}
      <div class="dash">
```
with:
```jinja
  {# /assistant is a single-run detail view; the run finder is /assistant-overview.
     The .as-main detail pane has a Markdown twin: _run_markdown() serializes the
     same sections (dashboard → summary → trigger → timeline → verdict) for the
     kebab's "View as markdown". Keep the two in sync when editing either. #}
  <section class="as-main">
    {% if not selected %}
      <h1>Assistant run</h1>
      <div class="as-empty">No run selected — open the
        <a href="{{ url_for('assistant_overview_page') }}">Assistant overview</a>
        to pick a run.</div>
    {% else %}
      <div class="as-main-head">
        <span class="as-runid">Run {{ selected.uuid }}</span>
        <button class="as-kebab" title="actions"
                onclick="asKebab(event, '{{ selected.uuid }}', '{{ selected.status }}', '{{ selected.journal_id or '' }}')"></button>
      </div>
      <div class="dash">
```

- [ ] **Step 7: Remove the stray `.as-split` closing `</div>`**

After the detail `</section>` near the end of the template, the next line is the `</div>` that closed `.as-split`. Change:
```jinja
  </section>
</div>

<div id="as-menu" class="as-menu" hidden></div>
```
to:
```jinja
  </section>

<div id="as-menu" class="as-menu" hidden></div>
```

- [ ] **Step 8: Remove the folder expand/collapse persistence JS**

Delete this block in the `<script>`:
```javascript
  // --- folder expand/collapse persistence (localStorage, keyed by name) -------
  document.querySelectorAll('details.as-folder').forEach(function (d) {
    var key = 'as.folder.' + d.dataset.folder;
    var saved = localStorage.getItem(key);
    if (saved === 'open') d.open = true;
    else if (saved === 'closed') d.open = false;
    d.addEventListener('toggle', function () {
      localStorage.setItem(key, d.open ? 'open' : 'closed');
    });
  });

```

- [ ] **Step 9: Simplify the handler and remove `_bucket_runs`**

Replace `assistant_page()` (lines ~918-939) with:
```python
@app.route("/assistant")
def assistant_page() -> str:
    selected = _selected_run()
    ctx = _load_run_detail(selected) if selected is not None else {}
    duration = _format_duration(
        selected.started_at, selected.finished_at) if selected else None

    return render_template_string(
        ASSISTANT_TEMPLATE,
        selected=selected,
        trigger=ctx.get("trigger"),
        timeline=ctx.get("timeline", []),
        decision_json=ctx.get("decision_json", {}),
        action_descriptions=_ACTION_DESCRIPTIONS,
        unlinked=ctx.get("unlinked", []),
        pending_controls=ctx.get("pending_controls", []),
        duration=duration, model_names=ctx.get("model_names", {}),
        dash=ctx.get("dash"), verdict=ctx.get("verdict"), reply=ctx.get("reply"),
    )
```
Then delete the `_bucket_runs(runs)` function definition (the `def _bucket_runs(...)` through its `return [...]` list, ~lines 836-858).

- [ ] **Step 10: Run the tests — all pass**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_views.py -q`
Expected: PASS (the rewritten no-tree, empty-state, and addressable-by-uuid tests pass; the remaining detail/timeline/markdown tests still pass).

- [ ] **Step 11: Commit**

```bash
git add source/webapp/assistant_views.py source/webapp/test_assistant_views.py
git commit -m "feat(assistant): detail-only page with header kebab, drop left tree

The run finder is now /assistant-overview, so /assistant is a single-run detail
view driven by ?id=. The per-run kebab moves into a header bar (.as-main-head)
at the top of the detail pane; no-id shows a pointer to the overview. Removes
the tree macros/CSS/JS, the folder icons, and _bucket_runs.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Verify end-to-end

**Files:** none (verification only).

- [ ] **Step 1: Full assistant + overview suites green (no regression)**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_views.py webapp/test_assistant_overview_views.py webapp/test_assistant_overview_api.py db/test_assistant_overview_query.py -q`
Expected: all PASS.

- [ ] **Step 2: Grep for dangling tree references**

Run: `cd source && grep -nE "_bucket_runs|run_leaf|as-tree|as-folder|icon_open|icon_closed|_ICON_FOLDER" webapp/assistant_views.py`
Expected: no matches (all tree remnants removed).

- [ ] **Step 3: Real-server smoke (boot the Flask app, read-only)**

Boot the app, then:
- `GET /assistant` → 200, body contains `No run selected` and `/assistant-overview`, and NOT `as-tree`.
- `GET /assistant?id=<a real run uuid>` → 200, body contains `as-main-head`, `as-kebab`, and `asKebab(event, '<uuid>'`.

- [ ] **Step 4: Commit (only if fixups were needed)**

```bash
git add -A
git commit -m "test(assistant): verify detail-only page end-to-end

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** move kebab into main panel ✓(T1 S5/S6 header bar) · remove left tree ✓(T1 S4/S5/S6/S8) · full-width main ✓(T1 S5) · empty state → overview ✓(T1 S6) · handler stops calling list_assistant_runs/_bucket_runs ✓(T1 S9) · remove `_bucket_runs` + test ✓(T1 S9/S1b) · keep db.list_assistant_runs ✓(untouched) · remove folder icons (only-used-here, grep-confirmed) ✓(T1 S3) · markdown export unaffected ✓(not touched) · test updates ✓(T1 S1) · verification ✓(T2).

**Placeholder scan:** none — every step shows the exact old/new text or command.

**Type consistency:** the kebab call signature `asKebab(event, uuid, status, journalId)` is identical in the old leaf and the new header button (T1 S6) and in the JS (unchanged). The empty-state marker copy "No run selected" is used identically in the template (T1 S6) and all three test assertions (T1 S1c/S1d). `selected` is the only run variable passed to the template, matching the template's `{% if not selected %}`.
