# Merge `query_qa` into `query_memory` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the assistant's two overlapping read actions (`query_qa`, `query_memory`) into one `query_memory` that also resolves the live dynamic handlers `query_qa` used to own.

**Architecture:** Add `retrieve_seed_answers` (static + dynamic seed, top-N, resolves handlers) in `seed_memory.py`; rewire `_action_query_memory` to call it with a `QueryContext`; migrate every `query_qa` reference to `query_memory`; then delete the `query_qa` enum member, action, and capability. `retrieve_seed_memories` stays for the always-on chat block. `QueryFilterRouterAgent` is untouched.

**Tech Stack:** Python, pytest, LlamaIndex + pgvector (seed store), Flask app context in tests.

## Global Constraints

- Ad-hoc DB work targets `rainbox_claude`, never `rainbox_production` (`source/CLAUDE.md`). Tests are already forced onto `rainbox_claude` by `conftest.py` — no action needed for the test path.
- Docs/comments describe current state, not change history (no "renamed from", "PR N", "Run N").
- `MIN_SCORE = 0.60` is the retained score floor; the `MIN_MARGIN` gate is intentionally NOT applied by the new path.
- Seed line render shape stays `- {uuid}, seed/{source}: {answer}`.
- The `<recalled_memory>` fence, the hybrid claim ranker, and the shield system are unchanged.
- Every task ends on a green `pytest source` run — the removal of `query_qa` is deferred to the last task so no intermediate commit references a deleted enum member.

**Run tests with:** `python -m pytest source/<path> -q` from `/Users/neoneye/git/rainbox`.

---

### Task 1: `retrieve_seed_answers` — top-N static + dynamic seed

**Files:**
- Modify: `source/memory/seed_memory.py` (add `kind` to `SeedMemory` ~line 560; add `retrieve_seed_answers` after `retrieve_seed_memories` ~line 601)
- Test: `source/agents/test_seed_memory_retrieval.py`

**Interfaces:**
- Consumes: existing `_semantic_ranked`, `_resolve_match`, `_entries_by_id`, `_entry_locked`, `_unlocked_shields`, `_vector_store`, `HANDLERS`, `MIN_SCORE`, `Match`, `SeedMemory`, and `QueryContext` (already imported in this module).
- Produces: `retrieve_seed_answers(query: str, *, qctx: QueryContext, limit: int = 5, _ranker: Callable[[str], list[Match]] | None = None, unlocked_shields: set[str] | None = None) -> list[SeedMemory]` and `SeedMemory.kind: str` (default `"static"`).

- [ ] **Step 1: Write the failing tests**

Append to `source/agents/test_seed_memory_retrieval.py`:

```python
def _qctx():
    from uuid import uuid4
    from agents.query_handlers import QueryContext
    return QueryContext(room_uuid=uuid4(), query="git", payload={}, agent_uuid=uuid4())


def test_retrieve_seed_answers_resolves_dynamic_and_static(registry, monkeypatch):
    monkeypatch.setattr(kb, "HANDLERS", {"git_status": lambda ctx: "Working tree clean."})
    ranked = [Match(qa_id="dyn-git", method="semantic", score=0.81),   # dynamic → resolved
              Match(qa_id="up-name", method="semantic", score=0.70)]   # static
    out = kb.retrieve_seed_answers("git", qctx=_qctx(), _ranker=lambda q: ranked)
    assert [m.uuid for m in out] == ["dyn-git", "up-name"]             # both kept, score order
    assert out[0].kind == "dynamic" and out[0].answer == "Working tree clean."
    assert out[1].kind == "static" and out[1].answer == "EgonBot."


def test_retrieve_seed_answers_gates_min_score_and_caps(registry, monkeypatch):
    monkeypatch.setattr(kb, "HANDLERS", {"git_status": lambda ctx: "x"})
    ranked = [Match(qa_id="up-name", method="semantic", score=0.50)]   # below MIN_SCORE
    assert kb.retrieve_seed_answers("x", qctx=_qctx(), _ranker=lambda q: ranked) == []
    many = [Match(qa_id="up-name", method="semantic", score=0.9 - i * 0.01) for i in range(10)]
    out = kb.retrieve_seed_answers("x", qctx=_qctx(), limit=2, _ranker=lambda q: many)
    assert len(out) == 2


def test_retrieve_seed_answers_excludes_locked(registry, monkeypatch):
    monkeypatch.setattr(kb, "HANDLERS", {"git_status": lambda ctx: "secret status"})
    registry["dyn-git"]["shield"] = "ops"                              # locked (not unlocked)
    ranked = [Match(qa_id="dyn-git", method="semantic", score=0.9)]
    out = kb.retrieve_seed_answers("git", qctx=_qctx(), _ranker=lambda q: ranked,
                                   unlocked_shields=set())
    assert out == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest source/agents/test_seed_memory_retrieval.py -q`
Expected: FAIL — `AttributeError: module 'memory.seed_memory' has no attribute 'retrieve_seed_answers'` (and `SeedMemory.__init__` has no `kind`).

- [ ] **Step 3: Add the `kind` field to `SeedMemory`**

In `source/memory/seed_memory.py`, the `SeedMemory` dataclass (~line 560) becomes:

```python
@dataclass
class SeedMemory:
    """A curated Q&A entry surfaced as a memory. `uuid` is the jsonl `id`.
    `answer` holds the static answer text, or a dynamic handler's resolved
    output. `kind` is "static" or "dynamic"."""
    uuid: str
    path: str
    source: str   # "user-overlay" | "upstream"
    answer: str
    score: float
    kind: str = "static"
```

- [ ] **Step 4: Add `retrieve_seed_answers`**

In `source/memory/seed_memory.py`, immediately after `retrieve_seed_memories` (after ~line 601):

```python
def retrieve_seed_answers(
    query: str, *, qctx: QueryContext, limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
    unlocked_shields: set[str] | None = None,
) -> list[SeedMemory]:
    """Top-N curated Q&A entries (static AND dynamic) relevant to `query`, as
    SeedMemory. Static entries carry their answer text; dynamic entries carry
    their handler's resolved output (handlers are read-only, resolved via
    `_resolve_match`). Ranked by question-embedding similarity (>= MIN_SCORE, no
    margin gate), deduped by uuid, capped at `limit`. Locked-shield entries are
    excluded. `_ranker` is injected by tests; in production it runs the semantic
    ranker (which itself applies the shield filter).

    Unlike `retrieve_seed_memories` (static-only, for the always-on chat block),
    this resolves dynamic handlers on demand for the assistant's `query_memory`
    action."""
    unlocked = _unlocked_shields() if unlocked_shields is None else unlocked_shields
    rank = _ranker or (lambda q: _semantic_ranked(q, _vector_store(),
                                                   unlocked_shields=unlocked))
    out: list[SeedMemory] = []
    seen: set[str] = set()
    for m in rank(query):
        if m.score < MIN_SCORE or m.qa_id in seen:
            continue
        entry = _entries_by_id.get(m.qa_id)
        if entry is None or _entry_locked(entry, unlocked):
            continue
        kind = str(entry.get("kind", "static"))
        answer = (str(entry.get("answer", "")) if kind == "static"
                  else _resolve_match(m, qctx))
        seen.add(m.qa_id)
        out.append(SeedMemory(
            uuid=m.qa_id,
            path=str(entry.get("path", "")),
            source=str(entry.get("_source", "upstream")),
            answer=answer,
            score=m.score,
            kind=kind,
        ))
        if len(out) >= limit:
            break
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest source/agents/test_seed_memory_retrieval.py -q`
Expected: PASS (all tests, including the pre-existing `retrieve_seed_memories` ones).

- [ ] **Step 6: Commit**

```bash
git add source/memory/seed_memory.py source/agents/test_seed_memory_retrieval.py
git commit -m "feat(seed-memory): add retrieve_seed_answers (static + dynamic, top-N)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Rewire `_action_query_memory` to fold in dynamic handlers

**Files:**
- Modify: `source/agents/assistant.py:157-198` (`_action_query_memory`) and the `query_memory` `CAPABILITIES` entry (~line 1021-1026)
- Test: `source/agents/test_assistant_actions.py`

**Interfaces:**
- Consumes: `retrieve_seed_answers` (Task 1), `QueryContext`, `fence_recalled_memory`, `format_memory_context`, `retrieve_memories_hybrid`.
- Produces: `_action_query_memory` now surfaces resolved dynamic-handler answers; the injected `_seed_retriever` seam is called as `seed_fn(query, qctx=qctx)`.

- [ ] **Step 1: Write the failing test**

Add to `source/agents/test_assistant_actions.py` (in the `# --- query_memory` section, after `test_query_memory_includes_seed_memories_tiered`):

```python
def test_query_memory_surfaces_dynamic_handler_answer(app_ctx):
    """query_memory now resolves dynamic seed handlers (formerly query_qa's job):
    a git-status handler answer must appear in the fenced block."""
    from memory.seed_memory import SeedMemory
    def fake_seed(query, *, qctx, **_):
        return [SeedMemory(uuid="dyn-git", path="dev.git", source="upstream",
                           answer="Working tree clean.", score=0.82, kind="dynamic")]
    obs = _action_query_memory(_ctx(), {"query": "what is the git status"},
                               _seed_retriever=fake_seed)
    assert obs.ok
    assert "Working tree clean." in obs.text
    assert "<recalled_memory" in obs.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest source/agents/test_assistant_actions.py::test_query_memory_surfaces_dynamic_handler_answer -q`
Expected: FAIL — `TypeError` (current `_action_query_memory` calls `seed_fn(query)` with no `qctx`, and the default retriever is the static-only one).

- [ ] **Step 3: Rewire the action**

Replace the body of `_action_query_memory` (`source/agents/assistant.py:157-198`). Change the import line, the retriever default + call, and build a `QueryContext`:

```python
def _action_query_memory(
    ctx: AssistantActionContext, args: dict[str, Any], *, _seed_retriever=None
) -> AssistantObservation:
    """Hybrid retrieval over dynamic claims, curated static seed answers, AND
    live dynamic seed handlers (project status, git status, capabilities, model
    info). Results are tiered: user-overlay seed, then upstream seed, then
    dynamic claims. Secrets are never returned (include_secret stays False)."""
    from memory.retrieval import fence_recalled_memory, format_memory_context, retrieve_memories_hybrid
    from memory.seed_memory import retrieve_seed_answers
    from agents.query_handlers import QueryContext

    query = str(args.get("query", "")).strip()
    qctx = QueryContext(
        room_uuid=ctx.room_uuid, query=query, payload={}, agent_uuid=ctx.agent_uuid
    )
    seed_fn = _seed_retriever or retrieve_seed_answers
    seeds = []
    try:
        seeds = seed_fn(query, qctx=qctx)
    except Exception:
        logger.warning("assistant: seed memory retrieval failed", exc_info=True)
    # Tier seeds: user-overlay first, then upstream; preserve score order within tier.
    overlay = [s for s in seeds if s.source == "user-overlay"]
    upstream = [s for s in seeds if s.source != "user-overlay"]
    memories = retrieve_memories_hybrid(
        query, agent_uuid=ctx.agent_uuid, room_uuid=ctx.room_uuid,
        include_secret=False, journal_id=ctx.journal_id,
    )
    dynamic_block = format_memory_context(memories, include_uuid=True) if memories else ""

    if not (overlay or upstream or memories):
        return AssistantObservation(ok=True, text="No relevant remembered facts.")
    lines = ["Relevant remembered facts",
             "- {memory_uuid}, {memory_tags}: {memory_text}"]
    for s in overlay + upstream:
        lines.append(f"- {s.uuid}, seed/{s.source}: {s.answer}")
    text = "\n".join(lines)
    if dynamic_block:
        # format_memory_context(include_uuid=True) emits TWO header lines (title +
        # the "{memory_uuid}, ..." legend); skip both and append only its fact lines.
        text += "\n" + "\n".join(dynamic_block.split("\n")[2:])
    text, _ = fence_recalled_memory(text)
    return AssistantObservation(
        ok=True, text=text,
        data={"seed_count": len(seeds), "dynamic_count": len(memories),
              "memory_uuids": [s.uuid for s in seeds] + [str(m.uuid) for m in memories]},
    )
```

- [ ] **Step 4: Bump the `query_memory` output cap**

The action now returns handler output (git status can be long); give it the cap `query_qa` had. In the `AssistantActionName.QUERY_MEMORY` `CAPABILITIES` entry (`source/agents/assistant.py:1021-1026`), add `output_cap_chars=6000`:

```python
    AssistantActionName.QUERY_MEMORY: Capability(
        name=AssistantActionName.QUERY_MEMORY, family="memory",
        description='search remembered facts. args: {"query": "..."}',
        summary="search remembered facts",
        required_args=("query",), action=_action_query_memory, output_cap_chars=6000,
    ),
```

(The description text is rewritten in Task 4, alongside removing `query_qa`.)

- [ ] **Step 5: Run the query_memory tests to verify they pass**

Run: `python -m pytest source/agents/test_assistant_actions.py -k query_memory -q`
Expected: PASS. The pre-existing `test_query_memory_includes_seed_memories_tiered` and `..._merges_seed_and_dynamic...` fakes use `def fake_seed(query, **_)`, so the new `qctx=` keyword lands in `**_` and they keep passing.

- [ ] **Step 6: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_actions.py
git commit -m "feat(assistant): query_memory resolves dynamic seed handlers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Migrate every `query_qa` reference off the enum

The enum member is deleted in Task 4; first repoint everything that names it (or uses it as a sample action string) to `query_memory`, so no commit references a deleted member. `query_memory` is `family="memory"` (not `"query"`) — assertions on family change accordingly.

**Files (all Modify):**
- `source/agents/assistant_fakes.py:14-19` (module docstring example)
- `source/agents/test_assistant_fakes.py:65-90`
- `source/agents/test_capability_registry.py:80-119` and `:137`
- `source/agents/test_assistant_control.py:133-135`
- `source/db/settings.py:89`
- `source/db/test_assistant_trace.py:59,70,87,215,222`
- `source/webapp/test_assistant_run_api.py:42,46,58`
- `source/webapp/test_assistant_views.py:104`
- `source/evals/test_acceptance_spine.py:13,91`

- [ ] **Step 1: Repoint the fixtures and docstrings**

Mechanical substitution in each location, `query_qa`/`QUERY_QA` → `query_memory`/`QUERY_MEMORY`:

- `assistant_fakes.py:15` docstring: `action=AssistantActionName.QUERY_QA` → `action=AssistantActionName.QUERY_MEMORY` (reason/args stay; they read fine for a memory lookup).
- `test_assistant_fakes.py`: line 67 `"action": "query_qa"` → `"query_memory"`; line 69 `AssistantActionName.QUERY_QA` → `QUERY_MEMORY`; line 87 the `"query_qa"` list item → `"query_memory"`.
- `test_capability_registry.py`:
  - line 80 `["query_qa"]` → `["query_memory"]`;
  - lines 90 `AssistantActionName.QUERY_QA` → `QUERY_MEMORY`;
  - lines 99–111 the four `"query_qa"` string literals → `"query_memory"`;
  - line 137 `report["query_qa"]["family"] == "query"` → `report["query_memory"]["family"] == "memory"`. Note line 135 already asserts `report["query_memory"]["enabled"] is True` — leave it; the two assertions on the same key are fine.
- `test_assistant_control.py:133,135`: `"running query_qa"` → `"running query_memory"` (both the assignment and the assertion).
- `db/settings.py:89` docstring example: `["query_qa","workspace_read_command"]` → `["query_memory","workspace_read_command"]`.
- `db/test_assistant_trace.py` lines 59,70,87,215,222: every `action="query_qa"` / `== "query_qa"` → `"query_memory"`.
- `webapp/test_assistant_run_api.py` lines 42,46,58: every `"query_qa"` → `"query_memory"`.
- `webapp/test_assistant_views.py:104`: `action="query_qa"` → `action="query_memory"`.
- `evals/test_acceptance_spine.py` lines 13,91: the `query_qa` references in the docstring/assertion → `query_memory`.

- [ ] **Step 2: Confirm no non-definition references remain**

Run:
```bash
grep -rn --include="*.py" "query_qa\|QUERY_QA" source | grep -v "site-packages" \
  | grep -vE "assistant\.py:(7|52|201|1027|1028|1035)" | grep -v "test_assistant_actions.py"
```
Expected: no output. (The only remaining hits are the `query_qa` *definition* in `assistant.py` and its dedicated tests in `test_assistant_actions.py`, both deleted in Task 4.)

- [ ] **Step 3: Run the migrated tests**

Run:
```bash
python -m pytest source/agents/test_assistant_fakes.py source/agents/test_capability_registry.py \
  source/agents/test_assistant_control.py source/db/test_assistant_trace.py \
  source/webapp/test_assistant_run_api.py source/webapp/test_assistant_views.py \
  source/evals/test_acceptance_spine.py -q
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add source/agents/assistant_fakes.py source/agents/test_assistant_fakes.py \
  source/agents/test_capability_registry.py source/agents/test_assistant_control.py \
  source/db/settings.py source/db/test_assistant_trace.py \
  source/webapp/test_assistant_run_api.py source/webapp/test_assistant_views.py \
  source/evals/test_acceptance_spine.py
git commit -m "refactor(assistant): repoint query_qa references to query_memory

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Delete `query_qa`; finalize `query_memory` copy

**Files:**
- Modify: `source/agents/assistant.py` (module docstring line 7; enum line 52; delete `_action_query_qa` lines 201-228; delete the `QUERY_QA` `CAPABILITIES` entry lines 1027-1036; rewrite `query_memory` description ~line 1023; system prompt lines 108-110)
- Test: `source/agents/test_assistant_actions.py` (delete the two `query_qa` action tests; rewrite the disambiguation test; drop the `_action_query_qa` import)

**Interfaces:**
- Consumes: nothing new.
- Produces: `AssistantActionName` no longer defines `QUERY_QA`; `_action_query_qa` no longer exists; `query_memory`'s description advertises general-question coverage.

- [ ] **Step 1: Rewrite the disambiguation test onto `query_memory`**

In `source/agents/test_assistant_actions.py`, replace `test_read_action_descriptions_disambiguate_query_qa_from_kanban` (lines 31-38) with:

```python
def test_read_action_descriptions_disambiguate_query_memory_from_kanban():
    """The model once used the general Q&A action to 'query the kanban boards'.
    The catalog must steer inspecting a board to kanban_read, and mark
    query_memory as not-for-kanban."""
    qm = CAPABILITIES[AssistantActionName.QUERY_MEMORY].description.lower()
    kb = CAPABILITIES[AssistantActionName.KANBAN_READ].description.lower()
    assert "kanban" in qm and "not for" in qm          # query_memory says: not for kanban
    assert "column" in kb                              # kanban_read: look up a board's columns
    assert "kanban_read" in ASSISTANT_SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Delete the two `query_qa` action tests and the import**

In `source/agents/test_assistant_actions.py`:
- Delete `test_query_qa_reuses_query_pipeline_and_resolves_match` and `test_query_qa_reports_no_confident_match` (lines 181-218, including the `# --- query_qa ...` banner). Their behavior — resolving a dynamic handler — is now covered by `test_query_memory_surfaces_dynamic_handler_answer` (Task 2).
- Remove `_action_query_qa,` from the import block (line 23).

- [ ] **Step 3: Run those tests to verify they fail**

Run: `python -m pytest source/agents/test_assistant_actions.py -q`
Expected: collection succeeds (`_action_query_qa` still exists in `assistant.py`; only its import and the two tests were removed), but `test_read_action_descriptions_disambiguate_query_memory_from_kanban` FAILS its `"not for" in qm` assertion because `query_memory`'s description is still `"search remembered facts."`. (This drives Step 4.)

- [ ] **Step 4: Rewrite `query_memory`'s description and delete `query_qa`**

In `source/agents/assistant.py`:

(a) `query_memory` `CAPABILITIES` description (~line 1021-1026) becomes:

```python
    AssistantActionName.QUERY_MEMORY: Capability(
        name=AssistantActionName.QUERY_MEMORY, family="memory",
        description=('recall stored facts AND answer general questions (project '
                     'status, git status, capabilities, model info) from the '
                     'knowledge base. NOT for kanban or files — use kanban_read / '
                     'workspace_read_command. args: {"query": "..."}'),
        summary="recall facts and answer general questions",
        required_args=("query",), action=_action_query_memory, output_cap_chars=6000,
    ),
```

(b) Delete the entire `AssistantActionName.QUERY_QA: Capability(...)` block (lines 1027-1036).

(c) Delete the `QUERY_QA = "query_qa"` enum line (line 52).

(d) Delete the `_action_query_qa` function (lines 201-228).

(e) Module docstring (line 7): `Actions are read-only (query_memory, query_qa,` → `Actions are read-only (query_memory,`.

(f) System prompt routing lines (108-110) become:

```python
Match the read action to the data you need: `kanban_read` for boards/tasks,
`query_memory` for remembered facts and general questions (project/git status,
capabilities). Do not use `query_memory` to inspect kanban or files.
```

- [ ] **Step 5: Run the assistant action tests to verify they pass**

Run: `python -m pytest source/agents/test_assistant_actions.py -q`
Expected: PASS.

- [ ] **Step 6: Full grep guard — no `query_qa` anywhere**

Run: `grep -rn --include="*.py" "query_qa\|QUERY_QA" source | grep -v "site-packages"`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_actions.py
git commit -m "feat(assistant): remove query_qa; query_memory answers general questions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the assistant + memory + webapp + evals suites**

Run:
```bash
python -m pytest source/agents source/memory source/db source/webapp source/evals -q
```
Expected: PASS (no `query_qa` collection errors; new behavior green). If a failure is pre-existing and unrelated (see the restructure-packages notes), confirm it fails identically on `git stash` before accepting it.

- [ ] **Step 2: Sanity-check the merged action end to end (optional manual)**

With the app running (`http://127.0.0.1:5000`), send the assistant a general question ("what's the git status?") and a memory question in one session; confirm it picks `query_memory` for both and the observation is fenced in `<recalled_memory>`.
