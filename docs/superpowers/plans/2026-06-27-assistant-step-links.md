# Direct links to an assistant step — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each assistant step addressable by uuid — a `#` permalink + anchor on `/assistant`, and a link from the flask-admin `AssistantStep` list to `/assistant?id=<run>#step-<uuid>`.

**Architecture:** Add `id="step-<uuid>"` + a permalink anchor + `:target` highlight + a scroll-on-load script to `ASSISTANT_TEMPLATE` in `webapp/assistant_views.py`; add an `AssistantStepView` with a `column_formatters` link in `webapp/core.py`. No new routes — the existing `?id=` run selection plus a URL fragment do the work.

**Tech Stack:** Flask / Jinja2 (`render_template_string`), flask-admin `ModelView` + `column_formatters` + `markupsafe.Markup`, vanilla JS, pytest (`source/venv/bin/python`).

## Global Constraints

- Tests run with `source/venv/bin/python -m pytest` from `source/` (conftest forces `rainbox_claude`).
- Anchor id format is exactly `step-<step.uuid>`; the admin link fragment must match it character-for-character.
- Admin formatter renders raw HTML via `markupsafe.Markup`, escaping interpolated values with `escape` (both already imported in `core.py:18`).
- Docs/comments describe current state, not change history.
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- Modify: `source/webapp/assistant_views.py` — step `id`, `.step-anchor` permalink, `.step:target`/`.step-anchor` CSS, scroll-on-load JS.
- Modify: `source/webapp/core.py` — `_format_step_trace_link` + `AssistantStepView`, swap the `add_view` line.
- Modify: `source/webapp/test_assistant_views.py` — assert step anchors render.
- Create: `source/webapp/test_assistant_step_admin_link.py` — unit-test the formatter.

---

### Task 1: Anchorable steps + permalink on `/assistant`

**Files:**
- Modify: `source/webapp/test_assistant_views.py` (test first)
- Modify: `source/webapp/assistant_views.py`

**Interfaces:**
- Consumes: the existing timeline loop `{% for step, intents in timeline %}` and `step.uuid`.
- Produces: each `.step` div carries `id="step-{{ step.uuid }}"` and a header
  `<a class="step-anchor" href="#step-{{ step.uuid }}">`; `.step:target` highlight;
  a script that `scrollIntoView()`s the `#step-…` element on load.

- [ ] **Step 1: Write the failing test**

In `source/webapp/test_assistant_views.py`, add after `test_timeline_shows_step_with_inline_intent_and_undo`:
```python
def test_step_is_anchored_and_has_permalink(app_ctx, client):
    room = _room()
    run = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=room.uuid, agent_uuid=uuid4())
    step = db.open_assistant_step(
        run_uuid=run.uuid, step_index=0, action="query_qa", reason="look")
    db.settle_assistant_step(step, phase="observed", observation_preview="ok")
    db.finish_run(run, "finished")
    try:
        body = client.get(f"/assistant?id={run.uuid}").get_data(as_text=True)
        assert f'id="step-{step.uuid}"' in body          # anchor target
        assert f'href="#step-{step.uuid}"' in body        # permalink
    finally:
        _cleanup(run.uuid, room.uuid)
```

- [ ] **Step 2: Run the test — it fails**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_views.py::test_step_is_anchored_and_has_permalink -q`
Expected: FAIL — the step div has no `id="step-…"` / permalink yet.

- [ ] **Step 3: Add the anchor id + permalink to the step header**

In `source/webapp/assistant_views.py`, change:
```jinja
      <div class="step phase-{{ step.phase }}">
        <div class="hd">
          <span class="ix" title="internal step index={{ step.step_index }}">Step {{ step.step_index + 1 }} of {{ timeline|length }}</span>
```
to:
```jinja
      <div class="step phase-{{ step.phase }}" id="step-{{ step.uuid }}">
        <div class="hd">
          <span class="ix" title="internal step index={{ step.step_index }}">Step {{ step.step_index + 1 }} of {{ timeline|length }}</span>
          <a class="step-anchor" href="#step-{{ step.uuid }}" title="Link to this step">#</a>
```

- [ ] **Step 4: Add `.step-anchor` + `.step:target` CSS and `scroll-margin`**

In `source/webapp/assistant_views.py`, find the rule
`.as-main .step, .as-main .card { border:1px solid #e5e7eb; border-radius:8px;`
and immediately AFTER that rule's closing `}` add:
```css
  .as-main .step { scroll-margin-top:14px; }
  .as-main .step:target { border-color:#2563eb; box-shadow:0 0 0 2px rgba(37,99,235,0.25); }
  .as-main .step-anchor { color:#b6bdc8; text-decoration:none; font-weight:700;
             padding:0 0.3rem; border-radius:4px; }
  .as-main .step-anchor:hover { color:#2563eb; background:#eef2ff; }
  .as-main .step:target .step-anchor { color:#2563eb; }
```

- [ ] **Step 5: Add the scroll-on-load script**

In `source/webapp/assistant_views.py`, just before the closing `</script>` (the last line of the script block), add:
```javascript
  // Deep-link to a step: #step-<uuid> scrolls the .as-main pane to it on load.
  // (.as-main is the scroll container, so a bare fragment isn't reliable here.)
  (function () {
    var h = location.hash;
    if (h.indexOf('#step-') === 0) {
      var el = document.getElementById(h.slice(1));
      if (el) el.scrollIntoView();
    }
  })();
```

- [ ] **Step 6: Run the test — it passes**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_views.py -q`
Expected: PASS (the new anchor test plus the existing suite).

- [ ] **Step 7: Commit**

```bash
git add source/webapp/assistant_views.py source/webapp/test_assistant_views.py
git commit -m "feat(assistant): anchor + permalink each step on /assistant

Each timeline step gets id=step-<uuid> and a # permalink; a :target highlight
plus a scroll-on-load script make /assistant?id=<run>#step-<uuid> open the run
scrolled to that step.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: flask-admin AssistantStep → trace link

**Files:**
- Create: `source/webapp/test_assistant_step_admin_link.py`
- Modify: `source/webapp/core.py`

**Interfaces:**
- Consumes: `markupsafe.Markup`/`escape` (imported in `core.py:18`); `AssistantStep` (imported); the Part-1 anchor id format `step-<uuid>`.
- Produces: module-level `_format_step_trace_link(view, context, model, name) -> Markup` and `class AssistantStepView(ModelView)`; the registered admin view links each step's `uuid` cell to `/assistant?id=<run_uuid>#step-<uuid>`.

- [ ] **Step 1: Write the failing test**

```python
# source/webapp/test_assistant_step_admin_link.py
"""The flask-admin AssistantStep list links each step's uuid to its /assistant
trace location (?id=<run>#step-<uuid>)."""
from uuid import uuid4

from webapp.core import _format_step_trace_link


class _FakeStep:
    def __init__(self, run_uuid, uuid):
        self.run_uuid = run_uuid
        self.uuid = uuid


def test_trace_link_points_at_run_and_step():
    run_uuid, step_uuid = uuid4(), uuid4()
    html = str(_format_step_trace_link(None, None,
                                       _FakeStep(run_uuid, step_uuid), "uuid"))
    assert f"/assistant?id={run_uuid}#step-{step_uuid}" in html
    assert str(step_uuid) in html      # uuid still shown as the link text
    assert html.startswith("<a ")
```

- [ ] **Step 2: Run the test — it fails**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_step_admin_link.py -q`
Expected: FAIL — `ImportError: cannot import name '_format_step_trace_link'`.

- [ ] **Step 3: Add the formatter + view and register it**

In `source/webapp/core.py`, replace:
```python
# Assistant ReAct loop: runs, per-step trace, control channel, write intents.
class AssistantRunView(ModelView):
    # Newest first — the integer id is internal plumbing, not the sort the
    # operator cares about.
    column_default_sort = ("started_at", True)


admin.add_view(AssistantRunView(AssistantRun, db, category="Assistant"))
admin.add_view(ModelView(AssistantStep, db, category="Assistant"))
```
with:
```python
# Assistant ReAct loop: runs, per-step trace, control channel, write intents.
class AssistantRunView(ModelView):
    # Newest first — the integer id is internal plumbing, not the sort the
    # operator cares about.
    column_default_sort = ("started_at", True)


def _format_step_trace_link(view, context, model, name):
    """Render a step's uuid cell as a link to its /assistant trace location —
    the run, scrolled to this step's anchor (id="step-<uuid>")."""
    href = f"/assistant?id={escape(model.run_uuid)}#step-{escape(model.uuid)}"
    return Markup(f'<a href="{href}"><code>{escape(model.uuid)}</code> ↗</a>')


class AssistantStepView(ModelView):
    # Newest first; the uuid links to the run's trace, scrolled to this step.
    column_default_sort = ("id", True)
    column_formatters = {"uuid": _format_step_trace_link}


admin.add_view(AssistantRunView(AssistantRun, db, category="Assistant"))
admin.add_view(AssistantStepView(AssistantStep, db, category="Assistant"))
```

- [ ] **Step 4: Run the test — it passes**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_step_admin_link.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add source/webapp/core.py source/webapp/test_assistant_step_admin_link.py
git commit -m "feat(admin): link AssistantStep uuid to its /assistant trace

The AssistantStep list view formats each uuid as a link to
/assistant?id=<run>#step-<uuid>, opening the run scrolled to that step.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Verify end-to-end

**Files:** none (verification only).

- [ ] **Step 1: Full related suites green**

Run: `cd source && ./venv/bin/python -m pytest webapp/test_assistant_views.py webapp/test_assistant_step_admin_link.py -q`
Expected: all PASS.

- [ ] **Step 2: App imports cleanly (admin view registered without error)**

Run: `cd source && PYTHONPATH=. ./venv/bin/python -c "import webapp; print('ok')"`
Expected: prints `ok` (no flask-admin column error).

- [ ] **Step 3: Real-server smoke (boot Flask, read-only)**

Boot the app, then:
- `GET /assistant?id=<a real run uuid with steps>` → body contains
  `id="step-<a step uuid>"` and `href="#step-<that uuid>"`.
- `GET /admin/assistantstep/` → 200, body contains `/assistant?id=` and `#step-`.

- [ ] **Step 4: Commit (only if fixups were needed)**

```bash
git add -A
git commit -m "test(assistant): verify step deep-links end-to-end

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** step anchor id ✓(T1 S3) · `#` permalink ✓(T1 S3) · `:target` highlight + scroll-margin ✓(T1 S4) · scroll-on-load JS ✓(T1 S5) · shareable `/assistant?id=…#step-…` ✓(T1+T2) · admin `AssistantStepView` formatting uuid → link ✓(T2 S3) · cross-run not handled (acceptable per spec) ✓ · tests for anchors ✓(T1 S1) and formatter ✓(T2 S1) · verification incl. admin import + smoke ✓(T3).

**Placeholder scan:** none — every code step shows exact old/new text; commands have expected output.

**Type consistency:** the anchor id is `step-<uuid>` in the template (T1 S3) and the formatter builds `#step-{escape(model.uuid)}` (T2 S3) — same format. `_format_step_trace_link(view, context, model, name)` signature matches the flask-admin formatter contract and the test call (T2 S1). `escape`/`Markup` are the names already imported in `core.py:18`. The new test imports `_format_step_trace_link` from `webapp.core`, which T2 S3 defines at module level.
