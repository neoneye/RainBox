# Assistant Page As A Log — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render an assistant run as a flat stream of typed events — gantt on top, split view below — with one detail component per event kind.

**Architecture:** A new read model `db/assistant_log.py` derives typed `RunEvent`s from records that already exist (step rows and the JSON inside them), so every historical run renders without a migration. `llm_call` gains a run linkage so the `llm` component can show prefill/decode and cache reuse. The view splits into a shell, a log view, and one renderer per kind.

**Tech Stack:** Python 3, Flask, SQLAlchemy + Postgres (JSONB), Jinja `render_template_string`, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-08-24-assistant-log-design.md`

## Global Constraints

- All commands run from `/Users/neoneye/git/rainbox/source`.
- Test runner is `./venv/bin/python -m pytest`. Never a bare `pytest`.
- Never run ad-hoc scripts against `rainbox_production`. Tests are forced onto `rainbox_claude` by `rainbox/conftest.py`.
- **No event contains another.** The staircase property is what makes the gantt readable; it is asserted directly and must survive every task.
- **Deriving only.** No new table, and no writer change beyond `llm_call`'s two columns. Historical runs must render.
- `assistant_run_stats` totals must not change. `activity`, `unaccounted` and `embedding` are not LLM calls.
- Templates are non-raw Python strings: a `\n` inside inline JS is eaten by Python. Use string concatenation in JS, never backslash escapes.
- Comments describe how the code works now — no "previously", no migration notes.
- Commit after every task. Never amend.

---

### Task 1: The read model — `llm` and `action` events

**Files:**
- Create: `db/assistant_log.py`
- Modify: `db/assistant.py` (re-point `assistant_llm_calls` at the new module)
- Modify: `db/__init__.py` (re-export)
- Test: `db/test_assistant_log.py` (create)

**Interfaces:**
- Produces: `db.run_events(run, steps, reviews=None) -> list[dict]`. Each event is `{"uuid", "kind", "label", "start", "duration_ms", "anchor", "kpis", "payload"}` where `kind` is one of `llm|embedding|action|activity|control|unaccounted`. Tasks 2–8 consume this.
- Produces: `db.EVENT_KINDS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
def test_one_step_becomes_a_call_and_an_action():
    """The split the whole rework rests on. A step row is a model call AND the
    action it chose; rendering them as one row is why neither has a home."""
    step = _step("memory_query", at=0, ms=11800, observation={"text": "facts"})

    events = db.run_events(_run(finished=30), [step])
    kinds = [(e["kind"], e["label"]) for e in events]

    assert ("llm", "decide → memory_query") in kinds
    assert ("action", "memory_query") in kinds
```

Plus: an action event carries `args` and the observation in `payload`; a
`code_driven` step's llm label is the action name itself (no "decide →"); a
step with no action yields only the llm event.

- [ ] **Step 2: Run it and watch it fail**

Run: `./venv/bin/python -m pytest db/test_assistant_log.py -q`
Expected: `AttributeError: module 'db' has no attribute 'run_events'`.

- [ ] **Step 3: Create the module with the interval machinery moved in**

Move `_parse_ts`, `_call`, `_rejected_calls`, `embed_call_label`,
`_embedding_calls`, `_phase_calls`, `_end_of`, `_span`, `_subtract`,
`_activity_rows`, `_unaccounted_rows`, `_inner_calls` from `db/assistant.py`
into `db/assistant_log.py` unchanged, then add:

```python
def _event(kind, label, *, start, duration_ms, anchor="", uuid="",
           kpis=None, payload=None) -> dict:
    return {"uuid": uuid, "kind": kind, "label": label, "start": start,
            "duration_ms": duration_ms, "anchor": anchor,
            "kpis": kpis or {}, "payload": payload or {}}
```

`run_events` walks the steps and, per step, emits the llm event (label
`f"decide → {action}"` for a model-chosen step, the action name for a
`code_driven` one) and, when the step has an action, the action event placed
at the end of the call.

- [ ] **Step 4: Keep `assistant_llm_calls` working as a projection**

In `db/assistant.py`, replace the body with a filter over the new model so the
two enumerations cannot disagree:

```python
def assistant_llm_calls(steps, reviews=None, run=None) -> list[dict]:
    """The LLM-shaped projection of the run's events, for the stats rollup and
    the in-chat progress row. One enumeration, filtered — two that could
    disagree about a run is the bug this module exists against."""
    from db.assistant_log import run_events
    return [e for e in run_events(run, steps, reviews)
            if e["kind"] not in ("action", "control")]
```

- [ ] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest db/ -q`
Expected: PASS, including the existing `db/test_assistant_timeline.py`.

- [ ] **Step 6: Commit**

```bash
git add db/assistant_log.py db/assistant.py db/__init__.py db/test_assistant_log.py
git commit -m "feat(assistant): derive a typed event stream from a run's records"
```

---

### Task 2: The remaining event kinds

**Files:**
- Modify: `db/assistant_log.py`
- Test: `db/test_assistant_log.py`

**Interfaces:**
- Consumes: `run_events` from Task 1.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_kind_appears_for_a_rich_step():
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4)],
                 embeds=[("what do I like", 12, 0.5)])
    control = _step("stop", at=40, ms=0, phase="control")

    kinds = {e["kind"] for e in db.run_events(_run(finished=60),
                                              [step, control])}

    assert {"llm", "action", "activity", "embedding",
            "control", "unaccounted"} <= kinds


def test_no_event_contains_another():
    """The staircase property. It moved modules; it did not become optional."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("recall filter", 11.8, 22.8)])
    inner = _step("recall_filter", at=21.8, ms=12700)

    events = db.run_events(_run(finished=34.5), [step, inner])
    spans = [(e["start"], e["start"] + timedelta(milliseconds=e["duration_ms"]))
             for e in events if e["start"] and e["duration_ms"]]
    for i, (a0, a1) in enumerate(spans):
        for j, (b0, b1) in enumerate(spans):
            assert i == j or not (a0 <= b0 and b1 <= a1)
```

- [ ] **Step 2: Run and watch fail**

Run: `./venv/bin/python -m pytest db/test_assistant_log.py -q -k "every_kind or contains_another"`
Expected: FAIL — the extra kinds are absent.

- [ ] **Step 3: Emit them**

In `run_events`, after the per-step llm/action events, extend with
`_embedding_calls(step, data)`, `_rejected_calls(step)` and `_inner_calls(step,
data)` (all already `kind="llm"` shaped — map them through `_event`), emit
`control` for `step.phase == "control"`, then apply the phase subtraction and
gap synthesis exactly as `assistant_llm_calls` did:

```python
    phases = [e for e in events if e["kind"] == "phase"]
    rows = [e for e in events if e["kind"] != "phase"]
    rows.extend(_activity_rows(phases, rows))
    rows.extend(_unaccounted_rows(rows, run))
    rows.sort(key=lambda e: (e["start"] is None, e["start"] or datetime.min))
```

The action event must be excluded from the `occupied` set passed to
`_activity_rows`, or an action spanning its own phases would zero them out.

- [ ] **Step 4: Run the tests**

Run: `./venv/bin/python -m pytest db/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add db/assistant_log.py db/test_assistant_log.py
git commit -m "feat(assistant): add embedding, activity, control and gap events"
```

---

### Task 3: `llm_call` run linkage

**Files:**
- Modify: `db/models.py` (two columns), `db/__init__.py` (two `_add_column_if_missing`)
- Modify: `llm/activity.py` (read the tags), `agents/base.py` (set them)
- Test: `llm/test_activity_recorder.py`

**Interfaces:**
- Produces: `llm_call.run_uuid` and `llm_call.step_uuid`, nullable.

- [ ] **Step 1: Write the failing test**

```python
def test_a_tagged_call_records_its_run_and_step():
    """Without this the assistant page cannot reach prefill/decode or cache
    reuse, which is the data that explains a slow call."""
    run_uuid, step_uuid = uuid4(), uuid4()
    rows = _record_one_call(tags={"caller": "assistant.decide",
                                 "run_uuid": str(run_uuid),
                                 "step_uuid": str(step_uuid)})

    assert rows[0]["run_uuid"] == run_uuid
    assert rows[0]["step_uuid"] == step_uuid


def test_an_untagged_call_records_nulls():
    """Every non-assistant call still records; the columns are simply empty."""
    rows = _record_one_call(tags={"caller": "benchmark.story"})
    assert rows[0]["run_uuid"] is None
```

- [ ] **Step 2: Run and watch fail**

Run: `./venv/bin/python -m pytest llm/test_activity_recorder.py -q -k run_and_step`
Expected: FAIL — no such key on the recorded row.

- [ ] **Step 3: Add the columns**

In `db/models.py`, on `LlmCall`:

```python
    # Which assistant run and step this call belongs to, from the
    # instrumentation tags the call site sets alongside `caller`. NULL for
    # every call made outside an assistant turn, and on rows recorded before
    # the tags existed. What lets /assistant show a call's prefill/decode
    # split and cache reuse, which the step row has never carried.
    run_uuid: Mapped[UUID | None] = mapped_column(index=True)
    step_uuid: Mapped[UUID | None] = mapped_column(index=True)
```

In `db/__init__.py`, beside the other idempotent additions:

```python
        _add_column_if_missing("llm_call", "run_uuid", "run_uuid UUID")
        _add_column_if_missing("llm_call", "step_uuid", "step_uuid UUID")
```

- [ ] **Step 4: Record them**

In `llm/activity.py`'s `_on_start`, beside `"caller"`:

```python
            "run_uuid": _uuid_tag(event.tags, "run_uuid"),
            "step_uuid": _uuid_tag(event.tags, "step_uuid"),
```

with a helper that never raises on a malformed tag:

```python
def _uuid_tag(tags: Any, key: str):
    """A UUID tag, or None. Total: a bad tag must not break an inference call."""
    try:
        raw = (tags or {}).get(key)
        return UUID(str(raw)) if raw else None
    except (ValueError, AttributeError, TypeError):
        return None
```

Carry both through `_on_end` the way `caller` and `origin` are carried.

- [ ] **Step 5: Set them at the assistant's call site**

`agents/base.py` builds `instrument_tags({"caller": caller_tag})`. Extend it
with the run and step when the agent has them — `StructuredLLMAgent` gains two
optional attributes (`_log_run_uuid`, `_log_step_uuid`) that the assistant sets
around a call, defaulting to `None` so every other agent is unaffected:

```python
                    tags = {"caller": caller_tag}
                    if getattr(self, "_log_run_uuid", None):
                        tags["run_uuid"] = str(self._log_run_uuid)
                    if getattr(self, "_log_step_uuid", None):
                        tags["step_uuid"] = str(self._log_step_uuid)
                    with instrument_tags(tags), capture_reasoning() as tally:
```

- [ ] **Step 6: Run the tests**

Run: `./venv/bin/python -m pytest llm/ db/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add db/models.py db/__init__.py llm/activity.py agents/base.py llm/test_activity_recorder.py
git commit -m "feat(activity): tie an llm_call row to the assistant run that made it"
```

---

### Task 4: Join the richer KPIs onto `llm` events

**Files:**
- Modify: `db/assistant_log.py`
- Test: `db/test_assistant_log.py`

- [ ] **Step 1: Write the failing test**

```python
def test_an_llm_event_takes_prefill_and_cache_from_its_llm_call_row():
    step = _step("reply", at=0, ms=16200)
    _record_llm_call(step_uuid=step.uuid, prefill_ms=14000, decode_ms=2200,
                     cached_tokens_estimated=8100)

    event = _first(db.run_events(_run(finished=20), [step]), kind="llm")

    assert event["kpis"]["prefill_ms"] == 14000
    assert event["kpis"]["cached_tokens"] == 8100


def test_an_event_without_an_llm_call_row_still_renders():
    """Runs predating the linkage keep the KPIs the step row carries."""
    step = _step("reply", at=0, ms=16200)

    event = _first(db.run_events(_run(finished=20), [step]), kind="llm")

    assert event["kpis"]["prefill_ms"] is None
    assert event["kpis"]["input_tokens"] == 10
```

- [ ] **Step 2: Run and watch fail**

Run: `./venv/bin/python -m pytest db/test_assistant_log.py -q -k prefill`
Expected: FAIL — no such KPI.

- [ ] **Step 3: Implement the join**

One query per run, not per event:

```python
def _llm_call_kpis(steps) -> dict:
    """llm_call rows for these steps, keyed by step uuid. One query for the
    run: a lookup per event would be a query per bar on the page."""
    ids = [s.uuid for s in steps]
    if not ids:
        return {}
    rows = (db.session.query(LlmCall)
            .filter(LlmCall.step_uuid.in_(ids)).all())
    return {r.step_uuid: r for r in rows}
```

Populate `kpis` from the row when present, else from the step's own fields,
with `prefill_ms`/`decode_ms`/`cached_tokens` as `None`.

- [ ] **Step 4: Run and commit**

Run: `./venv/bin/python -m pytest db/ -q` — expect PASS.

```bash
git add db/assistant_log.py db/test_assistant_log.py
git commit -m "feat(assistant): show a call's prefill, decode and cache reuse"
```

---

### Task 5: One component per kind

**Files:**
- Create: `webapp/assistant_components.py`
- Test: `webapp/test_assistant_components.py` (create)

**Interfaces:**
- Produces: `render_event_detail(event) -> str` (HTML), `event_kpis(event) -> list[tuple[str, str]]`, `EVENT_GLYPH: dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_kind_renders_without_a_bespoke_component():
    """The property that makes this scale: a kind with no special case still
    produces a usable pane."""
    for kind in db.EVENT_KINDS:
        html = render_event_detail({"kind": kind, "label": "x", "kpis": {},
                                    "payload": {}, "duration_ms": 1000})
        assert html and "x" in html


def test_an_unknown_action_needs_no_code():
    """32 actions today and more later; a new one must cost nothing."""
    html = render_event_detail({
        "kind": "action", "label": "kanban_task_teleport", "kpis": {},
        "payload": {"args": {"id": "7"}, "observation": {"text": "ok"}},
        "duration_ms": 1200})

    assert "kanban_task_teleport" in html and "ok" in html


def test_python_run_shows_its_code_and_output():
    html = render_event_detail({
        "kind": "action", "label": "python_run", "kpis": {},
        "payload": {"args": {"code": "print(2+2)"},
                    "observation": {"text": "4"}},
        "duration_ms": 900})

    assert "print(2+2)" in html
```

Plus: an `llm` pane shows the model and token KPIs and escapes its prompt; an
`unaccounted` pane says nothing measured it.

- [ ] **Step 2: Run and watch fail** — module does not exist.

- [ ] **Step 3: Implement**

A dispatch dict from kind to renderer, with `action` consulting a second dict
keyed by action name and falling back to the generic renderer. Every renderer
returns escaped HTML built with `markupsafe.escape`, never string
concatenation of untrusted text.

- [ ] **Step 4: Run and commit**

```bash
git add webapp/assistant_components.py webapp/test_assistant_components.py
git commit -m "feat(assistant): one detail component per event kind"
```

---

### Task 6: The log view — gantt and list

**Files:**
- Create: `webapp/assistant_log_view.py`
- Test: `webapp/test_assistant_log_view.py` (create)

**Interfaces:**
- Consumes: `db.run_events`, `render_event_detail`.
- Produces: `log_view(run, steps, reviews) -> dict` with `events` (view-models carrying `offset_pct`, `width_pct`, `seconds`, `glyph`, `detail_html`) and `span_seconds`.

- [ ] **Step 1: Write the failing tests**

```python
def test_bars_and_rows_are_the_same_events():
    """One stream rendered twice. Two lists that could diverge is the bug."""
    view = log_view(run, steps, [])
    assert len(view["events"]) == len(db.run_events(run, steps, []))


def test_a_bar_is_never_wider_than_the_span():
    for e in log_view(run, steps, [])["events"]:
        assert e["offset_pct"] + e["width_pct"] <= 100.5
```

- [ ] **Step 2–4: Implement, run, commit**

The percentage arithmetic is `_waterfall`'s, moved. Commit:

```bash
git commit -m "feat(assistant): lay the event stream out as a gantt and a list"
```

---

### Task 7: The page

**Files:**
- Modify: `webapp/assistant_views.py` — replace the step sections with the three bands
- Test: `webapp/test_assistant_views.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_page_renders_the_log_not_step_sections(app_ctx, client):
    page, _ = _rendered(client, run)

    assert 'class="log-list"' in page and 'class="log-detail"' in page
    assert "Step 1 of" not in page
```

- [ ] **Step 2–5: Implement, run the whole webapp suite, commit**

Selecting a row swaps the detail pane client-side from data already in the
page — no round trip, because a run's events are all rendered once. Keep the
existing `#step-<uuid>` ids as anchors so old links still land.

```bash
git commit -m "feat(assistant): render a run as a gantt over a split view"
```

---

### Task 8: The Markdown export follows

**Files:**
- Modify: `webapp/assistant_views.py` (the markdown builder)
- Test: `webapp/test_assistant_views.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_export_lists_the_same_events_as_the_page(app_ctx, client):
    page, md = _rendered(client, run)
    for e in db.run_events(run, steps, []):
        assert e["label"] in md
```

- [ ] **Step 2–4: Implement, run, commit**

```bash
git commit -m "docs(assistant): export the same event stream the page shows"
```

---

### Task 9: Verify

- [ ] **Step 1:** `./venv/bin/python -m pytest db/ webapp/ agents/ llm/ -q` — no failures.
- [ ] **Step 2:** Render a real run through `tools.serve_ui` on 5055 against `rainbox_production` (read-only) and confirm in a browser: gantt contiguous, list and gantt agree, selecting a row changes the pane, no console errors.
- [ ] **Step 3:** Confirm `git diff --stat main -- '*editdocument*'` is empty and `assistant_run_stats` totals match the pre-change values for the same run.
- [ ] **Step 4:** Commit any doc correction.

---

## Notes for the implementer

**The staircase is the contract.** No event contains another. It is asserted in
`db/test_assistant_log.py` and every task must leave it true — an action event
that spans its own phases would silently swallow them.

**Deriving, not emitting.** No new table. If a KPI is not already recorded, the
pane shows nothing rather than a guess — `claim retrieval` will show only a
duration, and that is the honest result of this slice.

**One enumeration.** `assistant_llm_calls` is a filter over `run_events`.
Anything that needs a run's calls goes through it, so the page, the export, the
stats and the chat progress row cannot disagree.
