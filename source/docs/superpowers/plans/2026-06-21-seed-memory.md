# Seed Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make curated `question_answer.jsonl` entries first-class memories that the assistant's `query_memory` surfaces (so "what candy do I like" works), keeping LlamaIndex pgvector and decoupling the store from the throwaway QueryAgent.

**Architecture:** Two retrieval stores, one merge. The dynamic store (`memory_claim`/`memory_embedding`) is unchanged. The curated store (LlamaIndex pgvector, sourced from the jsonl) gains a `retrieve_seed_memories()` reader; `query_memory` fans out to both and merges results tiered by provenance. Seed memories are immutable (never in `memory_claim`).

**Tech Stack:** Python, SQLAlchemy/Flask, LlamaIndex `PGVectorStore` + Ollama `nomic-embed-text` (768-dim), pgvector, pytest.

## Global Constraints

- The seed (Q&A) store keeps LlamaIndex `PGVectorStore`; do NOT replace it with raw pgvector.
- jsonl entry shape: `id` is a **uuid** (canonical reference), `path` is a human label, `kind` is `static`|`dynamic`. The loader keys the registry on `id` (so `qa_id == uuid`).
- **Only `kind == "static"` entries become seed memories.** Dynamic/handler entries stay reachable via `query_qa` only.
- Seed memories are **fully immutable**: they live only in the LlamaIndex store, never in `memory_claim`. No code may write a seed entry into `memory_claim`.
- Retrieval merge order is **user-overlay seed → upstream seed → dynamic `memory_claim`**.
- Provenance values are exactly `"user-overlay"` and `"upstream"`.
- Tests must be deterministic and model-free: never call the real embedder. Inject the ranker / retrievers (the `_ranker=` / `seed_retriever=` seams below). The existing query_kb tests are "loader-level — no embedding, no pgvector"; follow that.
- Sandbox DB only (`conftest.py` forces `rainbox_claude`).
- Tests run from `source/`: `./venv/bin/python -m pytest <path> -v`.

---

### Task 1: Tag each loaded entry with its provenance source

**Files:**
- Modify: `agents/query_kb_helpers.py` (`_load_jsonl`, ~148-167)
- Test: `agents/test_query_kb_overlay.py` (add a test)

**Interfaces:**
- Produces: `_load_jsonl()` still returns `list[dict]`, but each entry dict now has a `"_source"` key = `"upstream"` (from the base `data/` file) or `"user-overlay"` (from the customize overlay). Overlay still overrides base by `id`. The registry `_entries_by_id` therefore carries `_source` per entry.

- [ ] **Step 1: Write the failing test** in `agents/test_query_kb_overlay.py`

```python
def test_load_jsonl_tags_source(customize_dir, monkeypatch):
    # base file (upstream) has one entry; overlay has another + an override.
    base = [{"id": "u1", "path": "p.u1", "kind": "static", "questions": ["qu"], "answer": "base-u1"},
            {"id": "shared", "path": "p.s", "kind": "static", "questions": ["qs"], "answer": "base-shared"}]
    monkeypatch.setattr(kb, "QA_JSONL_PATH", customize_dir / "base.jsonl")
    (customize_dir / "base.jsonl").write_text("\n".join(json.dumps(e) for e in base) + "\n")
    _write_overlay(customize_dir, [
        {"id": "o1", "path": "p.o1", "kind": "static", "questions": ["qo"], "answer": "overlay-o1"},
        {"id": "shared", "path": "p.s", "kind": "static", "questions": ["qs"], "answer": "overlay-shared"},
    ])
    by_id = {e["id"]: e for e in kb._load_jsonl()}
    assert by_id["u1"]["_source"] == "upstream"
    assert by_id["o1"]["_source"] == "user-overlay"
    assert by_id["shared"]["_source"] == "user-overlay"   # overlay overrides → its source wins
    assert by_id["shared"]["answer"] == "overlay-shared"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_query_kb_overlay.py::test_load_jsonl_tags_source -v`
Expected: FAIL with `KeyError: '_source'`.

- [ ] **Step 3: Implement** — in `_load_jsonl`, tag entries by which file they came from. Replace the loop body:

```python
def _load_jsonl() -> list[dict[str, Any]]:
    """Base entries merged with the operator overlay (see _overlay_path),
    keyed by id — an overlay entry with the same id replaces the base entry
    wholesale (base order is kept; overlay-only entries append). Each entry is
    tagged with `_source` ("upstream" for the base data/ file, "user-overlay"
    for the customize overlay) so retrieval can tier by provenance. Id-less
    entries are dropped here."""
    overlay = _overlay_path()
    sources: list[tuple[Path, str]] = [(QA_JSONL_PATH, "upstream")]
    if overlay is not None and overlay.exists():
        sources.append((overlay, "user-overlay"))
    merged: dict[str, dict[str, Any]] = {}
    for path, source in sources:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line:
                e = json.loads(line)
                if e.get("id"):
                    e["_source"] = source
                    merged[e["id"]] = e
    return list(merged.values())
```

- [ ] **Step 4: Run test to verify it passes** (and the existing overlay tests still pass)

Run: `./venv/bin/python -m pytest agents/test_query_kb_overlay.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/query_kb_helpers.py agents/test_query_kb_overlay.py
git commit -m "feat(seed-memory): tag loaded jsonl entries with provenance source"
```

---

### Task 2: `SeedMemory` + `retrieve_seed_memories()`

**Files:**
- Modify: `agents/query_kb_helpers.py` (add after `_resolve_match`, ~378)
- Test: `agents/test_seed_memory_retrieval.py` (create)

**Interfaces:**
- Consumes: `Match` (existing dataclass: `qa_id, method, score, matched_question, ...`), `_entries_by_id` (each entry has `_source`, `kind`, `answer`, `path`), `_semantic_ranked(query, vs) -> list[Match]`, `MIN_SCORE`, `TOP_K`.
- Produces:
  - `@dataclass class SeedMemory: uuid: str; path: str; source: str; answer: str; score: float`
  - `def retrieve_seed_memories(query: str, *, limit: int = 5, _ranker: Callable[[str], list[Match]] | None = None) -> list[SeedMemory]` — semantic-ranked static entries above `MIN_SCORE`, deduped by uuid (the ranker already aggregates per qa_id), capped at `limit`, in descending score order. `_ranker` defaults to `lambda q: _semantic_ranked(q, _vector_store())`; tests inject it.

- [ ] **Step 1: Write the failing test** in `agents/test_seed_memory_retrieval.py`

```python
import pytest
import db
import agents.query_kb_helpers as kb
from agents.query_kb_helpers import Match, SeedMemory


@pytest.fixture()
def app_ctx():
    app = db.make_app()
    ctx = app.app_context(); ctx.push()
    try:
        yield
    finally:
        db.db.session.rollback(); ctx.pop()


@pytest.fixture()
def registry(app_ctx, monkeypatch):
    # Seed the in-memory registry directly (no embeddings, no pgvector).
    entries = {
        "u-candy": {"id": "u-candy", "path": "food.candy", "kind": "static",
                    "answer": "Simon likes licorice.", "_source": "user-overlay"},
        "up-name": {"id": "up-name", "path": "identity.name", "kind": "static",
                    "answer": "EgonBot.", "_source": "upstream"},
        "dyn-git": {"id": "dyn-git", "path": "dev.git", "kind": "dynamic",
                    "handler": "git_status", "_source": "upstream"},
    }
    monkeypatch.setattr(kb, "_entries_by_id", entries)
    return entries


def test_retrieve_seed_memories_filters_static_and_tags(registry):
    ranked = [Match(qa_id="u-candy", method="semantic", score=0.81),
              Match(qa_id="dyn-git", method="semantic", score=0.79),   # dynamic → excluded
              Match(qa_id="up-name", method="semantic", score=0.70)]
    out = kb.retrieve_seed_memories("candy", _ranker=lambda q: ranked)
    assert [m.uuid for m in out] == ["u-candy", "up-name"]   # dynamic dropped, score order
    assert out[0].source == "user-overlay" and out[0].path == "food.candy"
    assert out[0].answer == "Simon likes licorice."


def test_retrieve_seed_memories_drops_below_min_score_and_caps(registry):
    ranked = [Match(qa_id="u-candy", method="semantic", score=0.50)]  # below MIN_SCORE (0.60)
    assert kb.retrieve_seed_memories("x", _ranker=lambda q: ranked) == []
    many = [Match(qa_id="up-name", method="semantic", score=0.9 - i*0.01) for i in range(10)]
    out = kb.retrieve_seed_memories("x", limit=2, _ranker=lambda q: many)
    assert len(out) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_seed_memory_retrieval.py -v`
Expected: FAIL with `ImportError: cannot import name 'SeedMemory'`.

- [ ] **Step 3: Implement** — append to `agents/query_kb_helpers.py` (and add `from collections.abc import Callable` to imports if absent):

```python
@dataclass
class SeedMemory:
    """A curated Q&A entry surfaced as a memory. `uuid` is the jsonl `id`."""
    uuid: str
    path: str
    source: str   # "user-overlay" | "upstream"
    answer: str
    score: float


def retrieve_seed_memories(
    query: str, *, limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
) -> list[SeedMemory]:
    """Curated static Q&A entries relevant to `query`, as memories. Ranked by the
    seed store's question-embedding similarity (>= MIN_SCORE), deduped by uuid
    (the ranker aggregates per qa_id), capped at `limit`. Dynamic/handler entries
    are excluded — they are computed answers, not facts. `_ranker` is injected by
    tests; in production it runs the LlamaIndex semantic ranker."""
    rank = _ranker or (lambda q: _semantic_ranked(q, _vector_store()))
    out: list[SeedMemory] = []
    for m in rank(query):
        if m.score < MIN_SCORE:
            continue
        entry = _entries_by_id.get(m.qa_id)
        if entry is None or entry.get("kind") != "static":
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

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest agents/test_seed_memory_retrieval.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/query_kb_helpers.py agents/test_seed_memory_retrieval.py
git commit -m "feat(seed-memory): retrieve_seed_memories (static-only, tiered-ready)"
```

---

### Task 3: `query_memory` fans out to seed memories + tiered merge

**Files:**
- Modify: `agents/assistant.py` (`_action_query_memory`, ~156-175)
- Test: `agents/test_assistant_actions.py` (add a test)

**Interfaces:**
- Consumes: `retrieve_seed_memories(query, ...) -> list[SeedMemory]` (Task 2); `retrieve_memories_hybrid(...)` and `format_memory_context(memories, include_uuid=True)` (existing, `memory/retrieval.py`).
- Produces: `_action_query_memory` observation text now lists seed memories first (user-overlay before upstream), then dynamic memories. Adds a private `_seed_retriever` seam: `_action_query_memory(ctx, args, *, _seed_retriever=retrieve_seed_memories)` so tests inject seed results.

- [ ] **Step 1: Write the failing test** in `agents/test_assistant_actions.py`

```python
def test_query_memory_includes_seed_memories_tiered(app_ctx):
    from agents.assistant import _action_query_memory
    from agents.query_kb_helpers import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="up-1", path="p.up", source="upstream", answer="upstream fact", score=0.7),
                SeedMemory(uuid="ov-1", path="p.ov", source="user-overlay", answer="overlay fact", score=0.65)]
    ctx = AssistantActionContext(journal_id=None, room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID, step_index=0)
    obs = _action_query_memory(ctx, {"query": "anything unrelated zzz"}, _seed_retriever=fake_seed)
    assert obs.ok is True
    # user-overlay seed appears before upstream seed
    assert obs.text.index("overlay fact") < obs.text.index("upstream fact")
    # the seed uuids are present (greppable)
    assert "ov-1" in obs.text and "up-1" in obs.text
    # source tag is shown
    assert "user-overlay" in obs.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_assistant_actions.py::test_query_memory_includes_seed_memories_tiered -v`
Expected: FAIL (`_action_query_memory` has no `_seed_retriever` kwarg / no seed lines).

- [ ] **Step 3: Implement** — rewrite `_action_query_memory`:

```python
def _action_query_memory(
    ctx: AssistantActionContext, args: dict[str, Any], *, _seed_retriever=None
) -> AssistantObservation:
    """Hybrid memory retrieval over dynamic claims PLUS curated seed memories.
    Results are tiered: user-overlay seed, then upstream seed, then dynamic
    claims. Secrets are never returned (include_secret stays False)."""
    from memory.retrieval import format_memory_context, retrieve_memories_hybrid
    from agents.query_kb_helpers import retrieve_seed_memories

    query = str(args.get("query", "")).strip()
    seed_fn = _seed_retriever or retrieve_seed_memories
    seeds = []
    try:
        seeds = seed_fn(query)
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
        # dynamic_block already has its own header line; append its fact lines.
        text += "\n" + "\n".join(dynamic_block.split("\n")[1:])
    return AssistantObservation(
        ok=True, text=text,
        data={"seed_count": len(seeds), "dynamic_count": len(memories),
              "memory_uuids": [s.uuid for s in seeds] + [str(m.uuid) for m in memories]},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest agents/test_assistant_actions.py -v`
Expected: PASS (existing `query_memory` tests still pass — when `_seed_retriever` returns `[]` the dynamic path is unchanged; verify the no-memories test still returns "No relevant remembered facts.").

- [ ] **Step 5: Commit**

```bash
git add agents/assistant.py agents/test_assistant_actions.py
git commit -m "feat(seed-memory): query_memory surfaces seed memories, tiered"
```

---

### Task 4: Always-on chat memory block also includes seed memories

**Files:**
- Modify: `agents/chat_context.py` (`build_chat_context_block`, ~20)
- Test: `agents/test_chat_context.py` (add a test)

**Interfaces:**
- Consumes: `retrieve_seed_memories` (Task 2). `build_chat_context_block` gains a `_seed_retriever=retrieve_seed_memories` seam.
- Produces: the chat context block prepends a "Curated facts" section (user-overlay then upstream) above the existing dynamic memory block, each line `- <uuid>, seed/<source>: <answer>`.

- [ ] **Step 1: Write the failing test** in `agents/test_chat_context.py`

```python
def test_chat_context_includes_seed_memories(app_ctx):
    from agents.chat_context import build_chat_context_block
    from agents.query_kb_helpers import SeedMemory
    def fake_seed(query, **_):
        return [SeedMemory(uuid="s-1", path="p", source="user-overlay", answer="curated answer", score=0.7)]
    block = build_chat_context_block(
        query="zzz unrelated", room_uuid=uuid4(), agent_uuid=ASSISTANT_UUID,
        journal_id=uuid4(), _seed_retriever=fake_seed)
    assert "curated answer" in block and "s-1" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_chat_context.py::test_chat_context_includes_seed_memories -v`
Expected: FAIL (no `_seed_retriever` kwarg / no seed line).

- [ ] **Step 3: Implement** — read the current `build_chat_context_block` body and add the seam + a seed section above the dynamic memory block, mirroring Task 3's tiering (user-overlay before upstream). Build the seed lines as `f"- {s.uuid}, seed/{s.source}: {s.answer}"` under a `"Curated facts"` header, prepended to the existing block; an empty seed list adds nothing (no stray header). Wrap the `seed_fn(query)` call in try/except logging like Task 3.

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python -m pytest agents/test_chat_context.py -v`
Expected: PASS (existing chat-context tests unchanged when seeds are empty).

- [ ] **Step 5: Commit**

```bash
git add agents/chat_context.py agents/test_chat_context.py
git commit -m "feat(seed-memory): chat context block includes curated seed memories"
```

---

### Task 5: Rename the table `data_query_agent_kb` → `data_seed_memory`

**Files:**
- Modify: `agents/query_kb_helpers.py:34` (`QA_TABLE_NAME`)
- Modify: `db/models.py` (`QueryAgentKb.__tablename__`, ~1147; rename class `QueryAgentKb` → `SeedMemoryKb`)
- Modify: `webapp/core.py` (import + `QueryAgentKbView` registration, ~533/602; comments)
- Test: `webapp/test_admin_model_coverage.py` (the coverage test already guards this; add a name assertion)

**Interfaces:**
- Produces: `QA_TABLE_NAME = "seed_memory"` → PGVectorStore table `data_seed_memory`. Model `SeedMemoryKb(__tablename__="data_seed_memory")` registered in admin under **Memory**. The operator's "Repopulate Q&A memory" button (calls `rebuild_kb`) re-embeds into the new table.

- [ ] **Step 1: Write the failing test** in `webapp/test_admin_model_coverage.py`

```python
def test_seed_memory_table_renamed():
    import db
    assert db.SeedMemoryKb.__tablename__ == "data_seed_memory"
    import agents.query_kb_helpers as kb
    assert kb.QA_FULL_TABLE == "data_seed_memory"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_admin_model_coverage.py::test_seed_memory_table_renamed -v`
Expected: FAIL (`SeedMemoryKb` undefined / table name still `data_query_agent_kb`).

- [ ] **Step 3: Implement**
  - `agents/query_kb_helpers.py:34`: `QA_TABLE_NAME: str = "seed_memory"` (comment: PGVectorStore creates `data_seed_memory`).
  - `db/models.py`: rename class `QueryAgentKb` → `SeedMemoryKb`, set `__tablename__ = "data_seed_memory"`, update its docstring. Re-export under both names in `db/__init__.py` is NOT needed — update all importers (next).
  - `db/__init__.py`: export `SeedMemoryKb` (replace the `QueryAgentKb` export).
  - `webapp/core.py`: import `SeedMemoryKb`; rename `QueryAgentKbView` → `SeedMemoryKbView`; register `admin.add_view(SeedMemoryKbView(SeedMemoryKb, db, category="Memory"))`.
  - Grep for remaining `QueryAgentKb` references: `grep -rn QueryAgentKb source/` and update.

- [ ] **Step 4: Run tests**

Run: `./venv/bin/python -m pytest webapp/test_admin_model_coverage.py webapp/ -v`
Expected: PASS. Then manually: `./venv/bin/python -c "import db,webapp; from webapp.core import app as a; ap=db.make_app(); db.init_db(ap); ap.app_context().push(); print(a.test_client().get('/admin/seedmemorykb/').status_code)"` → `200`.

- [ ] **Step 5: Commit**

```bash
git add agents/query_kb_helpers.py db/ webapp/ 
git commit -m "refactor(seed-memory): rename table/model data_query_agent_kb -> data_seed_memory"
```

NOTE: after deploying, the operator must click **Repopulate Q&A memory** (or set `QUERY_AGENT_REBUILD_KB=1`) once to embed into the new table. `log()`/note this in the handoff.

---

### Task 6 (mechanical, lowest priority): Rename the module to `memory/seed_memory.py`

**Files:**
- Move: `agents/query_kb_helpers.py` → `memory/seed_memory.py`
- Modify importers: `agents/query.py`, `agents/query_router.py`, `agents/query_filter_router.py`, `agents/assistant.py`, `webapp/settings_views.py`, and tests (`agents/test_query_kb_overlay.py`, `agents/test_seed_memory_retrieval.py`, others found by grep).

**Interfaces:**
- Produces: the seed store module lives at `memory/seed_memory.py` (signals it is part of the memory system). All `import agents.query_kb_helpers as kb` / `from agents import query_kb_helpers as qkb` become `import memory.seed_memory as kb` (keep the local alias names to minimize diff).

- [ ] **Step 1:** `git mv source/agents/query_kb_helpers.py source/memory/seed_memory.py`.
- [ ] **Step 2:** `grep -rn "query_kb_helpers" source/` — update every import to `memory.seed_memory` (preserve the `kb`/`qkb` aliases so call sites are unchanged). Fix the `QA_JSONL_PATH` base path in the moved file: it uses `Path(__file__).resolve().parent.parent / "data" / ...`; from `memory/` that still resolves to `source/data/`, so it is unchanged — verify.
- [ ] **Step 3:** Run the full suites: `./venv/bin/python -m pytest agents/ memory/ webapp/ -q`. Expected: all pass.
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(seed-memory): move query_kb_helpers -> memory/seed_memory"
```

---

## Self-Review

**Spec coverage:**
- Two stores, one retrieval / fan-out → Tasks 3, 4. ✓
- `seed_memory` module decoupled from QueryAgent → Tasks 2 (retrieval added), 6 (module move). ✓
- Keep LlamaIndex → Global Constraints + Task 2 uses `_semantic_ranked`. ✓
- uuid identity (`id`) surfaced + greppable → Tasks 2, 3 (uuid in line + data). ✓
- `path` = label only → Task 2 carries it but retrieval keys on uuid. ✓
- static-only → Global Constraints + Task 2 filter + test. ✓
- tiered merge (user-overlay > upstream > dynamic) → Task 3 + test. ✓
- fully immutable (never in `memory_claim`) → Global Constraints; seeds are read-only `SeedMemory`s, never written to `memory_claim`; `remember`/`forget_memory` untouched (no task modifies them — confirm none do). ✓
- table rename → Task 5. ✓
- Deferred (Table B, /search, dynamic-as-memory) → not in any task. ✓

**Placeholder scan:** Task 4 Step 3 describes the change in prose rather than full code because it depends on the current `build_chat_context_block` body (which the implementer must read first); the shape, line format, tiering, and try/except are fully specified. All other code steps are concrete.

**Type consistency:** `SeedMemory(uuid, path, source, answer, score)` is used identically in Tasks 2, 3, 4. `retrieve_seed_memories(query, *, limit, _ranker)` signature matches between definition (Task 2) and the `_seed_retriever` injection points (Tasks 3, 4). `_source`/`"_source"` key is written in Task 1 and read in Task 2.

**Immutability check:** no task adds a seed entry to `memory_claim`; `forget_memory`/`remember` are not modified. A reviewer should confirm in Task 3 review that seeds flow only into observation text, never into a write.
