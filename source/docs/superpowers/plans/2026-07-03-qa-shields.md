# Q&A Shields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator hide selected Q&A entries from the LLM by tagging them with a `shield`, kept locked by default and unlocked per-shield from the Settings page.

**Architecture:** A Q&A entry may carry one optional dotted-path `shield` string. A locked shield is excluded at two layers of the shared `memory/seed_memory.py` retrieval chokepoint — a pgvector metadata filter (so top-K never considers it) plus an in-memory backstop — so all three consuming agents (`query_filter_router`, `query_router`, `assistant`) inherit it. A new `qa.unlocked_shields` registry setting (JSON list, default empty) holds the unlocked names; the Settings page renders a clustered checklist over the discovered shields.

**Tech Stack:** Python, Flask, SQLAlchemy, LlamaIndex (`llama-index-core` 0.14.22 + `llama-index-vector-stores-postgres` 0.8.1), pgvector, pytest.

## Global Constraints

- **Neutral naming only.** No sensitive category names anywhere in code, tests, or commit messages — only neutral placeholders (`alice.travel`, `bob.notes`, `alpha.one`). This is a hard requirement copied from the spec.
- **Databases:** ad-hoc scripts/REPL target `rainbox_claude`, never `rainbox_production`. Tests are already forced onto `rainbox_claude` by `conftest.py` — no action needed for the pytest path.
- **Filter names/types are fixed by the spec:** setting key `qa.unlocked_shields` (type `json`, default `[]`); entry field `shield` (single string); shields matched **exactly** on the whole string (dots are for UI clustering, not prefix inheritance).
- **Design doc:** `docs/superpowers/specs/2026-07-03-qa-shields-design.md`.

---

### Task 1: Shield primitives — setting + pure helpers

Adds the `qa.unlocked_shields` registry setting and three pure helpers in `memory/seed_memory.py`: `_unlocked_shields()` (reads the setting, safe-empty default), `_entry_locked()` (the lock predicate), and `_drop_locked()` (filters a `Match` list). These carry all the lock *logic*; later tasks only wire them into retrieval.

**Files:**
- Modify: `db/settings.py` (add one entry to the `SETTINGS` dict, after `customize.dir`)
- Modify: `memory/seed_memory.py` (add three helpers just before `_exact_match`, ~line 369)
- Create: `memory/test_seed_shields.py`

**Interfaces:**
- Produces:
  - `db` setting key `"qa.unlocked_shields"` → `list[str]`, default `[]`.
  - `memory.seed_memory._unlocked_shields() -> set[str]`
  - `memory.seed_memory._entry_locked(entry: dict[str, Any], unlocked: set[str]) -> bool`
  - `memory.seed_memory._drop_locked(matches: list[Match], unlocked: set[str]) -> list[Match]`
- Consumes: existing `memory.seed_memory.Match`, module globals `_entries_by_id`, and `db.get_setting`.

- [ ] **Step 1: Write the failing tests**

Create `memory/test_seed_shields.py`:

```python
"""Shield primitives: the pure lock predicate + candidate filter, and the
qa.unlocked_shields registry setting. Neutral placeholder shield names only."""
import db
import memory.seed_memory as kb
from memory.seed_memory import Match


def test_entry_with_no_shield_is_never_locked():
    assert kb._entry_locked({"id": "a"}, set()) is False
    assert kb._entry_locked({"id": "a", "shield": ""}, set()) is False


def test_entry_locked_when_shield_not_unlocked():
    assert kb._entry_locked({"id": "a", "shield": "alice.travel"}, set()) is True
    assert kb._entry_locked({"id": "a", "shield": "alice.travel"}, {"bob.notes"}) is True


def test_entry_unlocked_when_shield_present_in_set():
    assert kb._entry_locked({"id": "a", "shield": "alice.travel"}, {"alice.travel"}) is False


def test_drop_locked_removes_locked_and_preserves_order(monkeypatch):
    entries = {
        "u1": {"id": "u1", "shield": "alice.travel"},
        "u2": {"id": "u2"},                       # unshielded
        "u3": {"id": "u3", "shield": "bob.notes"},
    }
    monkeypatch.setattr(kb, "_entries_by_id", entries)
    matches = [Match(qa_id="u1", method="semantic", score=0.9),
               Match(qa_id="u2", method="semantic", score=0.8),
               Match(qa_id="u3", method="semantic", score=0.7)]
    kept = kb._drop_locked(matches, {"bob.notes"})   # only bob.notes unlocked
    assert [m.qa_id for m in kept] == ["u2", "u3"]    # u1 locked out, order kept


def test_unlocked_shields_setting_defaults_to_empty_list():
    app = db.make_app()
    with app.app_context():
        assert db.get_setting("qa.unlocked_shields") == []


def test_unlocked_shields_helper_reads_setting():
    app = db.make_app()
    with app.app_context():
        assert kb._unlocked_shields() == set()


def test_unlocked_shields_helper_empty_outside_app_context():
    # No app context -> get_setting raises -> safe empty default (all hidden).
    assert kb._unlocked_shields() == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest memory/test_seed_shields.py -q` (use the repo's venv: `source venv/bin/activate` first, or `venv/bin/pytest`)
Expected: FAIL — `AttributeError: module 'memory.seed_memory' has no attribute '_entry_locked'` and `UnknownSetting: qa.unlocked_shields`.

- [ ] **Step 3: Add the registry setting**

In `db/settings.py`, inside the `SETTINGS` dict, immediately after the `"customize.dir"` entry, add:

```python
    "qa.unlocked_shields": Setting(
        "qa.unlocked_shields", None, "json", [],
        description="Names of Q&A shields the operator has unlocked. A Q&A entry "
                    "carrying a shield reaches the LLM only when that shield is "
                    "in this list; an entry with no shield is always visible. "
                    "Empty (the default) keeps every shielded entry hidden.",
    ),
```

- [ ] **Step 4: Add the helpers**

In `memory/seed_memory.py`, immediately before `def _exact_match(` (~line 369), add:

```python
def _unlocked_shields() -> set[str]:
    """Shields the operator has unlocked (the qa.unlocked_shields setting).
    Empty when unset or when called outside a Flask app context — the safe
    default, which keeps every shielded entry hidden."""
    try:
        return set(db.get_setting("qa.unlocked_shields") or [])
    except Exception:
        return set()


def _entry_locked(entry: dict[str, Any], unlocked: set[str]) -> bool:
    """True if `entry` is hidden from the LLM: it carries a shield that is not
    in `unlocked`. An entry with no shield is always visible."""
    shield = entry.get("shield")
    return bool(shield) and shield not in unlocked


def _drop_locked(matches: list[Match], unlocked: set[str]) -> list[Match]:
    """Layer-2 backstop: drop matches whose current in-memory entry is locked,
    order preserved. Pure over `_entries_by_id`, so unit-testable with no DB —
    and correct even when the pgvector metadata is stale (pre-repopulate)."""
    return [
        m for m in matches
        if not _entry_locked(_entries_by_id.get(m.qa_id) or {}, unlocked)
    ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/pytest memory/test_seed_shields.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add db/settings.py memory/seed_memory.py memory/test_seed_shields.py
git commit -m "feat(seed-memory): shield lock predicate + qa.unlocked_shields setting"
```

---

### Task 2: Store the shield in node metadata

`_build_documents` must write the `shield` onto each node's pgvector metadata — but only when the entry has one, so unshielded entries keep no `shield` key (the `IS_EMPTY` filter in Task 3 depends on the key being absent).

**Files:**
- Modify: `memory/seed_memory.py` — `_build_documents`, inside the `for q in ...` loop (~line 251)
- Modify: `memory/test_seed_documents.py` (append two tests)

**Interfaces:**
- Consumes: entry dicts with an optional `"shield"` key.
- Produces: nodes whose `metadata["shield"]` is set iff the entry has a shield; the key is included in `excluded_embed_metadata_keys` / `excluded_llm_metadata_keys`.

- [ ] **Step 1: Write the failing tests**

Append to `memory/test_seed_documents.py`:

```python
def test_shield_present_is_metadata_and_excluded_from_embed():
    entries = [{"id": "s", "kind": "static", "questions": ["what is it?"],
                "answer": "a", "shield": "alice.travel"}]
    doc = seed_memory._build_documents(entries)[0]
    assert doc.metadata["shield"] == "alice.travel"
    # Excluded from the embedded/LLM text, like the other metadata keys.
    assert "shield" in doc.excluded_embed_metadata_keys
    assert "shield" in doc.excluded_llm_metadata_keys


def test_no_shield_means_no_shield_metadata_key():
    entries = [{"id": "s", "kind": "static", "questions": ["what is it?"],
                "answer": "a"}]
    doc = seed_memory._build_documents(entries)[0]
    assert "shield" not in doc.metadata
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest memory/test_seed_documents.py -q`
Expected: FAIL — `KeyError: 'shield'` / assertion that `shield` is in metadata.

- [ ] **Step 3: Add the shield to metadata**

In `memory/seed_memory.py`, inside `_build_documents`'s `for q in e.get("questions") or []:` loop, after the `if kind == "static": ... elif kind == "dynamic": ...` block and **before** `keys = list(md.keys())`, add:

```python
            shield = e.get("shield")
            if shield:
                md["shield"] = shield
```

(Placing it before `keys = list(md.keys())` means the existing exclusion lines automatically cover `shield`, so it never pollutes the question-only embedding.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest memory/test_seed_documents.py -q`
Expected: PASS (existing tests + 2 new).

- [ ] **Step 5: Commit**

```bash
git add memory/seed_memory.py memory/test_seed_documents.py
git commit -m "feat(seed-memory): carry entry shield in node metadata"
```

---

### Task 3: Filter locked shields at retrieval

Wire the helpers into the three retrieval functions so all three agents inherit the lock. `_semantic_ranked` gains the pgvector metadata filter (layer 1) plus `_drop_locked` (layer 2); `_exact_match` post-filters its single hit; `retrieve_seed_memories` skips locked static entries. A new `_shield_filters` helper builds the `MetadataFilters`.

**Files:**
- Modify: `memory/seed_memory.py` — add `_shield_filters`; edit `_exact_match` (~369), `_semantic_ranked` (~378), `retrieve_seed_memories` (~491)
- Modify: `memory/test_seed_shields.py` (append tests)

**Interfaces:**
- Consumes: `_unlocked_shields`, `_entry_locked`, `_drop_locked` (Task 1); `Match`, `_entries_by_id`, `_alias_table`, `_vector_store`, `_normalize_query`.
- Produces (updated signatures — existing positional callers unchanged):
  - `_shield_filters(unlocked: set[str]) -> MetadataFilters`
  - `_exact_match(query: str, *, unlocked_shields: set[str] | None = None) -> Match | None`
  - `_semantic_ranked(query: str, vs: PGVectorStore, *, unlocked_shields: set[str] | None = None) -> list[Match]`
  - `retrieve_seed_memories(query: str, *, limit: int = 5, _ranker=None, unlocked_shields: set[str] | None = None) -> list[SeedMemory]`

- [ ] **Step 1: Write the failing tests**

Append to `memory/test_seed_shields.py`:

```python
from llama_index.core.vector_stores import FilterCondition, FilterOperator


def test_shield_filters_is_empty_only_when_nothing_unlocked():
    f = kb._shield_filters(set())
    assert len(f.filters) == 1
    assert f.filters[0].key == "shield"
    assert f.filters[0].operator == FilterOperator.IS_EMPTY


def test_shield_filters_adds_sorted_in_clause_when_unlocked():
    f = kb._shield_filters({"bob.notes", "alice.travel"})
    assert f.condition == FilterCondition.OR
    ops = {flt.operator for flt in f.filters}
    assert FilterOperator.IS_EMPTY in ops and FilterOperator.IN in ops
    in_flt = next(flt for flt in f.filters if flt.operator == FilterOperator.IN)
    assert in_flt.key == "shield"
    assert in_flt.value == ["alice.travel", "bob.notes"]   # sorted


def test_exact_match_hidden_when_locked_and_shown_when_unlocked(monkeypatch):
    monkeypatch.setattr(kb, "_alias_table", {"who is alice": "u1"})
    monkeypatch.setattr(kb, "_entries_by_id",
                        {"u1": {"id": "u1", "shield": "alice.travel"}})
    assert kb._exact_match("Who is alice?", unlocked_shields=set()) is None
    m = kb._exact_match("Who is alice?", unlocked_shields={"alice.travel"})
    assert m is not None and m.qa_id == "u1"


def test_exact_match_unshielded_entry_unaffected(monkeypatch):
    monkeypatch.setattr(kb, "_alias_table", {"hello": "u2"})
    monkeypatch.setattr(kb, "_entries_by_id", {"u2": {"id": "u2"}})
    m = kb._exact_match("Hello?", unlocked_shields=set())
    assert m is not None and m.qa_id == "u2"


def test_retrieve_seed_memories_skips_locked(monkeypatch):
    app = db.make_app()
    with app.app_context():
        entries = {
            "u1": {"id": "u1", "path": "p.a", "kind": "static", "answer": "A",
                   "shield": "alice.travel", "_source": "upstream"},
            "u2": {"id": "u2", "path": "p.b", "kind": "static", "answer": "B",
                   "_source": "upstream"},
        }
        monkeypatch.setattr(kb, "_entries_by_id", entries)
        ranked = [Match(qa_id="u1", method="semantic", score=0.9),
                  Match(qa_id="u2", method="semantic", score=0.8)]
        out = kb.retrieve_seed_memories("x", _ranker=lambda q: ranked,
                                        unlocked_shields=set())
        assert [m.uuid for m in out] == ["u2"]          # u1 locked out
        out2 = kb.retrieve_seed_memories("x", _ranker=lambda q: ranked,
                                         unlocked_shields={"alice.travel"})
        assert [m.uuid for m in out2] == ["u1", "u2"]   # unlocked -> both
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest memory/test_seed_shields.py -q`
Expected: FAIL — `AttributeError: ... '_shield_filters'`, and `_exact_match()`/`retrieve_seed_memories()` got an unexpected keyword argument `unlocked_shields`.

- [ ] **Step 3: Add `_shield_filters` and the import**

At the top of `memory/seed_memory.py`, extend the existing `from llama_index.core import ...` area with a new import line (below line 26):

```python
from llama_index.core.vector_stores import (
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)
```

Then, just after `_drop_locked` (from Task 1) and before `_exact_match`, add:

```python
def _shield_filters(unlocked: set[str]) -> MetadataFilters:
    """pgvector metadata filter that keeps only retrievable nodes: unshielded
    ones (no `shield` metadata key -> IS_EMPTY) plus any whose shield is
    unlocked. Locked shields are excluded in SQL, so they never occupy a top-K
    slot."""
    keep: list[MetadataFilter] = [
        MetadataFilter(key="shield", operator=FilterOperator.IS_EMPTY),
    ]
    if unlocked:
        keep.append(MetadataFilter(
            key="shield", value=sorted(unlocked), operator=FilterOperator.IN))
    return MetadataFilters(filters=keep, condition=FilterCondition.OR)
```

- [ ] **Step 4: Wire `_exact_match`**

Replace `_exact_match` with:

```python
def _exact_match(query: str, *, unlocked_shields: set[str] | None = None) -> Match | None:
    norm = _normalize_query(query)
    qa_id = _alias_table.get(norm)
    if qa_id is None:
        return None
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    if _entry_locked(_entries_by_id.get(qa_id) or {}, unlocked):
        return None
    return Match(qa_id=qa_id, method="exact", score=1.0, matched_question=norm)
```

- [ ] **Step 5: Wire `_semantic_ranked`**

Change its signature and add the filter + backstop. The body up to building `ranked` is unchanged except the two marked lines:

```python
def _semantic_ranked(query: str, vs: PGVectorStore, *,
                     unlocked_shields: set[str] | None = None) -> list[Match]:
    """Top-K retrieve, aggregate by qa_id (max score per qa_id), return them
    ranked descending by score. Locked shields are excluded at the vector query
    (so they never occupy a top-K slot) and again as an in-memory backstop.
    **No** MIN_SCORE/MIN_MARGIN gating — for the caller to apply."""
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    index = VectorStoreIndex.from_vector_store(vs, embed_model=_embed_model())
    nodes = index.as_retriever(
        similarity_top_k=TOP_K, filters=_shield_filters(unlocked),
    ).retrieve(query)
    if not nodes:
        return []
    by_qa: dict[str, tuple[float, str]] = {}   # qa_id -> (best_score, matched_question)
    for n in nodes:
        md = n.metadata or {}
        qa_id = md.get("qa_id") or ""
        if not qa_id:
            continue
        score = float(n.score) if n.score is not None else 0.0
        cur = by_qa.get(qa_id)
        if cur is None or score > cur[0]:
            by_qa[qa_id] = (score, md.get("question") or "")
    ranked = sorted(by_qa.items(), key=lambda kv: kv[1][0], reverse=True)
    matches = [
        Match(qa_id=qa, method="semantic", score=s, matched_question=q)
        for qa, (s, q) in ranked
    ]
    return _drop_locked(matches, unlocked)
```

- [ ] **Step 6: Wire `retrieve_seed_memories`**

Change its signature and loop:

```python
def retrieve_seed_memories(
    query: str, *, limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
    unlocked_shields: set[str] | None = None,
) -> list[SeedMemory]:
    """Curated static Q&A entries relevant to `query`, as memories. Ranked by the
    seed store's question-embedding similarity (>= MIN_SCORE), deduped by uuid,
    capped at `limit`. Dynamic/handler entries and locked-shield entries are
    excluded. `_ranker` is injected by tests; in production it runs the semantic
    ranker (which itself applies the shield filter)."""
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    rank = _ranker or (lambda q: _semantic_ranked(q, _vector_store(),
                                                   unlocked_shields=unlocked))
    out: list[SeedMemory] = []
    for m in rank(query):
        if m.score < MIN_SCORE:
            continue
        entry = _entries_by_id.get(m.qa_id)
        if entry is None or entry.get("kind") != "static":
            continue
        if _entry_locked(entry, unlocked):
            continue
        out.append(SeedMemory(
            uuid=m.qa_id,
            path=str(entry.get("path", "")),
            source=str(entry.get("_source", "upstream")),
            answer=str(entry.get("answer", "")),
            score=m.score,
        ))
        if len(out) >= limit:
            break
    return out
```

- [ ] **Step 7: Run the shield + existing seed-memory tests**

Run: `venv/bin/pytest memory/test_seed_shields.py agents/test_seed_memory_retrieval.py memory/test_seed_documents.py -q`
Expected: PASS (new shield tests + unchanged existing retrieval tests still green).

- [ ] **Step 8: Commit**

```bash
git add memory/seed_memory.py memory/test_seed_shields.py
git commit -m "feat(seed-memory): exclude locked shields at retrieval (query filter + backstop)"
```

---

### Task 4: Discover available shields

Add `available_qa_shields()` so the Settings UI can list the distinct shield names actually present in the loaded registry (base + overlay).

**Files:**
- Modify: `memory/seed_memory.py` (add helper near `get_entry`, ~line 240)
- Modify: `memory/test_seed_shields.py` (append tests)

**Interfaces:**
- Produces: `available_qa_shields() -> list[str]` — sorted, distinct, non-empty shield strings across `_entries_by_id`. Calls `_load_kb()` first so it works before any retrieval has populated the registry.
- Consumes: `_load_kb`, `_entries_by_id`.

- [ ] **Step 1: Write the failing tests**

Append to `memory/test_seed_shields.py`:

```python
def test_available_qa_shields_sorted_distinct(monkeypatch):
    monkeypatch.setattr(kb, "_load_kb", lambda: None)   # registry pre-seeded below
    monkeypatch.setattr(kb, "_entries_by_id", {
        "a": {"id": "a", "shield": "bob.notes"},
        "b": {"id": "b", "shield": "alice.travel"},
        "c": {"id": "c", "shield": "alice.travel"},     # duplicate
        "d": {"id": "d"},                               # unshielded -> ignored
        "e": {"id": "e", "shield": ""},                 # empty -> ignored
    })
    assert kb.available_qa_shields() == ["alice.travel", "bob.notes"]


def test_available_qa_shields_empty_when_none(monkeypatch):
    monkeypatch.setattr(kb, "_load_kb", lambda: None)
    monkeypatch.setattr(kb, "_entries_by_id", {"a": {"id": "a"}})
    assert kb.available_qa_shields() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest memory/test_seed_shields.py -k available -q`
Expected: FAIL — `AttributeError: ... 'available_qa_shields'`.

- [ ] **Step 3: Add the helper**

In `memory/seed_memory.py`, after `def get_entry(...)` (~line 244), add:

```python
def available_qa_shields() -> list[str]:
    """Sorted, distinct shield names present in the loaded registry (base +
    overlay). Drives the Settings checklist. Loads the KB first so it is
    correct before any retrieval has run."""
    _load_kb()
    shields = {
        s for e in _entries_by_id.values()
        if (s := e.get("shield"))
    }
    return sorted(shields)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest memory/test_seed_shields.py -k available -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add memory/seed_memory.py memory/test_seed_shields.py
git commit -m "feat(seed-memory): available_qa_shields() for the settings checklist"
```

---

### Task 5: Settings page — clustered shield checklist

Inject the available shields into the `/settings` page and render a clustered checkbox list for `qa.unlocked_shields` (grouped by dotted-path prefix), saving the checked full names through the existing `/settings/api/set`. This mirrors the existing `customize.dir` special-case in the same template.

**Files:**
- Modify: `webapp/settings_views.py` — `settings_page()` (~276) to inject shields; `SETTINGS_TEMPLATE` `render()` (~131) to special-case the key
- Modify: `webapp/test_settings_views.py` (append tests)

**Interfaces:**
- Consumes: `memory.seed_memory.available_qa_shields()`; existing `db.all_settings()`, `/settings/api/set`.
- Produces: the page carries `const QA_SHIELDS = [...]` and renders a `#qa-shields` checklist; saving posts `{key: "qa.unlocked_shields", value: [<checked names>]}`.

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_settings_views.py`:

```python
def test_page_injects_available_shields(client, monkeypatch):
    import memory.seed_memory as kb
    monkeypatch.setattr(kb, "available_qa_shields",
                        lambda: ["alice.travel", "bob.notes"])
    body = client.get("/settings").get_data(as_text=True)
    assert "const QA_SHIELDS =" in body
    assert "alice.travel" in body and "bob.notes" in body
    # The special-case checklist container is rendered for the shields key.
    assert "qa-shields" in body


def test_unlocked_shields_roundtrips_through_api(client):
    r = client.post("/settings/api/set", json={
        "key": "qa.unlocked_shields", "value": ["alice.travel"]})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert db_settings.get_setting("qa.unlocked_shields") == ["alice.travel"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest webapp/test_settings_views.py -k "shields" -q`
Expected: FAIL — `const QA_SHIELDS =` not found (page not injecting yet). The roundtrip test may already pass once Task 1's setting exists; keep it — it guards the API contract.

- [ ] **Step 3: Inject the shields into the page**

In `webapp/settings_views.py`, replace `settings_page()` with:

```python
@app.route("/settings")
def settings_page() -> str:
    import json

    import memory.seed_memory as seed_memory

    # ensure_ascii=False so redaction bullets / unicode render literally (the
    # page is served UTF-8). Escape <>& to \uXXXX so a value containing
    # "</script>" can't break out of the inline <script> block.
    def _js(obj: object) -> str:
        s = json.dumps(obj, ensure_ascii=False)
        return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    return render_template_string(
        SETTINGS_TEMPLATE,
        settings_json=_js(db.all_settings()),
        qa_shields_json=_js(seed_memory.available_qa_shields()),
    )
```

- [ ] **Step 4: Declare `QA_SHIELDS` and render the checklist**

In `SETTINGS_TEMPLATE`, just below `const SETTINGS = {{ settings_json|safe }};` (line 109), add:

```javascript
const QA_SHIELDS = {{ qa_shields_json|safe }};
```

Then in `render()`, replace the `else { body = ... }` branch's assignment so the shields key gets the checklist instead of the value+Edit row. Locate this block:

```javascript
    } else {
      body = '<div class="s-row">' + displayValue(s) + ' ' + badge(s.source)
```

and change it to:

```javascript
    } else if (s.key === 'qa.unlocked_shields'){
      body = renderShieldChecklist(s);
    } else {
      body = '<div class="s-row">' + displayValue(s) + ' ' + badge(s.source)
```

Add the `renderShieldChecklist` function and its wiring just above `function render(){` (line 131):

```javascript
// Cluster the discovered shields by their dotted-path prefix and render a
// checkbox per full shield string. Checked = unlocked (shown to the LLM),
// unchecked = locked (hidden, the default). Saving posts the checked names.
function shieldGroups(shields){
  const groups = {};
  shields.forEach(name => {
    const head = name.includes('.') ? name.slice(0, name.indexOf('.')) : name;
    (groups[head] = groups[head] || []).push(name);
  });
  return groups;
}
function renderShieldChecklist(s){
  const unlocked = new Set(Array.isArray(s.value) ? s.value : []);
  if (!QA_SHIELDS.length){
    return '<div class="s-env">No shields defined. Add a "shield" field to an '
      + 'entry in your Q&A overlay, then press "Repopulate Q&A memory".</div>';
  }
  const groups = shieldGroups(QA_SHIELDS);
  let html = '<div id="qa-shields" class="qa-shields">';
  Object.keys(groups).sort().forEach(head => {
    html += '<div class="qa-shield-group"><div class="qa-shield-head">'
      + escapeHtml(head) + '</div>';
    groups[head].sort().forEach(name => {
      const id = 'sh-' + name.replace(/[^a-zA-Z0-9]/g, '-');
      html += '<label class="qa-shield-item"><input type="checkbox" data-shield="'
        + escapeHtml(name) + '" id="' + id + '"'
        + (unlocked.has(name) ? ' checked' : '') + '> ' + escapeHtml(name)
        + '</label>';
    });
    html += '</div>';
  });
  html += '</div><div class="s-row"><button data-save-shields>Save shields</button> '
    + badge(s.source) + ' <span class="s-env" data-shields-result></span></div>';
  return html;
}
```

Wire the Save button inside `render()`, next to the existing `list.querySelectorAll('[data-edit]')...` block, add:

```javascript
  list.querySelectorAll('[data-save-shields]').forEach(btn =>
    btn.addEventListener('click', async () => {
      const out = btn.parentElement.querySelector('[data-shields-result]');
      const names = Array.from(document.querySelectorAll('#qa-shields [data-shield]'))
        .filter(cb => cb.checked).map(cb => cb.getAttribute('data-shield'));
      btn.disabled = true; if (out) out.textContent = 'saving…';
      try {
        const r = await fetch('/settings/api/set', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({key: 'qa.unlocked_shields', value: names}),
        });
        const d = await r.json();
        if (d.ok){
          const i = SETTINGS.findIndex(x => x.key === 'qa.unlocked_shields');
          if (i >= 0) SETTINGS[i] = d.setting;
          render();
        } else if (out){ out.textContent = 'failed: ' + (d.error || 'save failed'); }
      } catch (e) { if (out) out.textContent = 'network error'; }
      finally { btn.disabled = false; }
    }));
```

Add minimal styles to the `<style>` block (anywhere before `</style>`):

```css
  .qa-shields{margin:6px 0 10px}
  .qa-shield-group{margin:0 0 8px}
  .qa-shield-head{font-family:ui-monospace,monospace;font-weight:700;font-size:0.82rem;color:#374151;margin:0 0 3px}
  .qa-shield-item{display:flex;align-items:center;gap:7px;font-size:0.9rem;margin:2px 0 2px 10px}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/pytest webapp/test_settings_views.py -q`
Expected: PASS (existing settings tests + 2 new; the injected shields render, roundtrip persists).

- [ ] **Step 6: Manually verify layer 1 (the pgvector metadata filter) end-to-end**

This is the one path unit tests can't exercise (it needs the live store). With Ollama running and a shielded overlay entry:

```bash
# rainbox_claude only — never production.
DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude venv/bin/python - <<'PY'
import db, memory.seed_memory as kb
app = db.make_app()
with app.app_context():
    kb.rebuild_kb()  # embed base + overlay so the shield lands in metadata
    vs = kb._vector_store()
    # A query that would otherwise hit a shielded entry:
    locked = kb._semantic_ranked("<a question your shielded entry answers>", vs, unlocked_shields=set())
    shown = kb._semantic_ranked("<same question>", vs, unlocked_shields={"<that.shield>"})
    print("locked ->", [m.qa_id for m in locked])
    print("unlocked ->", [m.qa_id for m in shown])
PY
```

Expected: the shielded qa_id is **absent** from `locked` and **present** in `shown`; unshielded entries appear in both. Confirm an entry embedded before this feature (no `shield` key) still appears (IS_EMPTY keeps it).

- [ ] **Step 7: Commit**

```bash
git add webapp/settings_views.py webapp/test_settings_views.py
git commit -m "feat(settings): clustered shield checklist to unlock Q&A shields"
```

---

## Self-Review

**Spec coverage:**
- Entry `shield` field, unshielded always visible → Task 2 (metadata) + Task 1/3 (`_entry_locked` treats no-shield as visible). ✓
- `qa.unlocked_shields` setting, json, default `[]` → Task 1. ✓
- Independent, multi-unlock switches → Task 1 (`_entry_locked` per-entry) + Task 5 (checkbox per name, posts full list). ✓
- Exact match, not prefix inheritance → Task 1 `_entry_locked` compares whole string; Task 5 groups only for display, stores full names. ✓
- Layer 1 pgvector metadata filter (IS_EMPTY OR IN) → Task 3 `_shield_filters` + `_semantic_ranked`. ✓
- Layer 2 in-memory backstop → Task 1 `_drop_locked`, applied in Task 3. ✓
- `_exact_match` post-filter; `retrieve_seed_memories` skip; `_semantic_match` inherits (calls `_semantic_ranked`) → Task 3. ✓
- Shield stored only when present (no key otherwise) → Task 2. ✓
- `available_qa_shields()` discovery → Task 4. ✓
- Settings checklist clustered by dotted prefix → Task 5. ✓
- All three agents covered: they call `_exact_match` / `_semantic_ranked` / `_semantic_match` / `retrieve_seed_memories`, all filtered in Task 3 — no per-agent edits needed. ✓
- Non-goal (not encryption-at-rest; global; one shield) → no task needed. ✓
- Future work (incremental repopulate) → explicitly out of scope. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; shield names are neutral placeholders throughout. ✓

**Type consistency:** `unlocked_shields: set[str] | None` param name identical across `_exact_match`, `_semantic_ranked`, `retrieve_seed_memories`; `_shield_filters(set[str]) -> MetadataFilters`; `available_qa_shields() -> list[str]`; setting key `qa.unlocked_shields` and JS `QA_SHIELDS` consistent between Task 4 and Task 5. ✓
