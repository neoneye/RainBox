# Merge `query_qa` into `query_memory`

## Problem

The assistant (the ReAct loop in `source/agents/assistant.py`) exposes two
overlapping read actions:

- `query_memory` — hybrid retrieval over dynamic claims **plus** curated static
  seed Q&A, tiered and wrapped in a `<recalled_memory>` fence.
- `query_qa` — a gated **top-1** match over the same seed Q&A store that also
  resolves **dynamic handlers** (live git status, project status, capabilities,
  model info), returning a single answer or `"No confident Q&A match."`.

Observed behavior: the model rarely picks `query_qa`; it reaches for
`query_memory` the majority of the time. When `query_qa` *is* used, its gated
top-1 result returns too little — it does not surface the top-N relevant
entries. The root cause is a routing ambiguity between two near-identical read
actions over the same data. Fewer choices means less confusion, so the fix is to
collapse the two into one action.

The wrinkle: `query_memory`'s seed path (`retrieve_seed_memories`,
`seed_memory.py`) is **static-only** — it explicitly excludes dynamic/handler
entries (`entry.get("kind") != "static": continue`). So a naive "delete
`query_qa`" would silently drop the live handlers that were its whole purpose.
The merged action must carry three source types: static seed answers, live
dynamic handlers, and dynamic claims.

## Approach

Collapse `query_qa` into `query_memory`. The single surviving action performs
the full retrieval on every call — static seed, dynamic handlers, and dynamic
claims — ranks a combined top-N, and fences everything in `<recalled_memory>`.

`QueryFilterRouterAgent` (the chat route at `source/agents/query_filter_router.py`)
is **out of scope** and untouched: it already does ungated top-K + LLM filter,
works well, and modifying it carries regression risk this change does not need.

Decisions locked in:

- **Keep the name `query_memory`** (not `recall`). The model already gravitates
  to it, `forget_memory`'s description already references it, and tests/docs
  point at it — keeping the name means the action the model already picks now
  returns everything, with no new routing to learn.
- **Drop the margin gate, keep the 0.60 score floor.** The `MIN_MARGIN` gate
  existed to avoid a confident-wrong *single* answer. Returning top-N candidates
  makes ambiguity harmless, so only `MIN_SCORE` remains.

## Changes

### 1. Action surface (`source/agents/assistant.py`)

- Delete `AssistantActionName.QUERY_QA`.
- Delete `_action_query_qa`.
- Delete the `query_qa` entry from the `CAPABILITIES` table.

### 2. New retrieval over static + dynamic seed (`source/memory/seed_memory.py`)

Add `retrieve_seed_answers` as a **new** function. `retrieve_seed_memories`
**stays** — it has a second caller, `chat_context.py:44`, which builds the
always-on "Curated facts" block injected into every chat turn. That block must
remain static-only: running dynamic handlers on every turn regardless of
relevance would be expensive and noisy. So `retrieve_seed_answers` (static +
dynamic, on-demand) and `retrieve_seed_memories` (static-only, always-on) coexist
and share the same ranker.

```python
def retrieve_seed_answers(
    query: str,
    qctx: QueryContext,
    *,
    limit: int = 5,
    _ranker: Callable[[str], list[Match]] | None = None,
    unlocked_shields: set[str] | None = None,
) -> list[SeedMemory]:
    """Top-N curated Q&A entries (static AND dynamic) relevant to `query`, as
    SeedMemory. Static entries carry their answer text; dynamic entries carry
    their handler's resolved output (handlers are read-only). Ranked by the seed
    store's question-embedding similarity (>= MIN_SCORE), deduped by uuid, capped
    at `limit`. Locked-shield entries are excluded. `_ranker` is injected by
    tests; in production it runs the semantic ranker (which itself applies the
    shield filter)."""
```

Behavior:

- Rank via the ungated `_semantic_ranked` top-K.
- Keep matches with `score >= MIN_SCORE` (0.60). No margin gate.
- For each kept match, resolve by kind:
  - `static` → `entry["answer"]`.
  - `dynamic` → `_resolve_match(match, qctx)` (runs the read-only handler).
- Dedup by uuid, cap at `limit`, drop locked shields.

`SeedMemory` gains a `kind: str` field (`"static" | "dynamic"`) for the trace and
tagging; its `answer` field now holds resolved handler output for dynamic
entries.

### 3. `_action_query_memory` (`source/agents/assistant.py`)

- Build `QueryContext(room_uuid=ctx.room_uuid, query=query, payload={},
  agent_uuid=ctx.agent_uuid)` (as `query_qa` did) so dynamic handlers can
  resolve.
- Call `retrieve_seed_answers(query, qctx, ...)` in place of
  `retrieve_seed_memories(query)`.
- Blend unchanged: **user-overlay seed → upstream seed → dynamic claims**
  (`retrieve_memories_hybrid`), rendered into the existing `<recalled_memory>`
  fence via `fence_recalled_memory`.
- Seed lines keep the `- {uuid}, seed/{source}: {answer}` shape. Multi-line
  handler output sits inline after the colon — cosmetically loose in a bulleted
  list but content-safe (sanitized by the fence; `query_qa` already returned
  multi-line text).
- Preserve the injected test seam (`_seed_retriever`), retyped to the
  `retrieve_seed_answers` shape.
- The empty path (`"No relevant remembered facts."`) is unchanged.

### 4. Prompt + capability copy (`source/agents/assistant.py`)

- `query_memory` capability `description`:

  > recall stored facts AND answer general questions (project status, git
  > status, capabilities, model info) from the knowledge base. NOT for kanban or
  > files — use kanban_read / workspace_read_command. args: {"query": "..."}

- `ASSISTANT_SYSTEM_PROMPT` routing guidance: drop the two `query_qa` sentences;
  `query_memory` now covers "remembered facts and general questions (project/git
  status)".

### 5. Migrate lingering `query_qa` references

Removing the enum member breaks any code that names it and orphans test fixtures
that use `"query_qa"` as a sample action string. All of these migrate to
`query_memory`:

- **Breaks on removal (enum member gone):**
  - `assistant_fakes.py:15` — a fake emits `AssistantActionName.QUERY_QA`.
  - `test_assistant_fakes.py:67,69,87` — parses `"query_qa"` into `QUERY_QA`.
  - `test_capability_registry.py:80,90,99–111,137` — uses `query_qa` as the
    sample disabled / removed-from-dispatch capability and asserts
    `family == "query"`. Retarget to `query_memory` (still `family="query"`).
- **Module docstring / copy:**
  - `assistant.py:7` — the "Actions are read-only (query_memory, query_qa, …)"
    docstring line.
  - `db/settings.py:89` — docstring example list
    `["query_qa","workspace_read_command"]`.
- **Orphaned literal-string fixtures (DB stores arbitrary strings, so these
  pass today but reference a removed action — update for correctness):**
  - `db/test_assistant_trace.py:59,70,87,215,222`
  - `webapp/test_assistant_run_api.py:42,46,58`
  - `webapp/test_assistant_views.py:104`
  - `test_assistant_control.py:133,135` (`_activity = "running query_qa"`).
- **Evals:** `evals/test_acceptance_spine.py:13,91` — migrate the `query_qa`
  acceptance assertion onto `query_memory`.
- **Existing `query_qa` action tests** (`test_assistant_actions.py:31–36`
  disambiguation, `:181–216` pipeline + no-match) — rewrite as `query_memory`
  assertions (see Testing).

`test_seed_memory_retrieval.py` and `test_seed_shields.py` exercise
`retrieve_seed_memories`, which stays — they need no migration; new
`retrieve_seed_answers` tests are added alongside.

## Testing

- `retrieve_seed_answers`:
  - static and dynamic entries both surface;
  - `MIN_SCORE` gate excludes low-score matches;
  - `limit` cap honored;
  - locked-shield entries excluded;
  - a dynamic entry's handler is actually invoked (stub `HANDLERS`), its output
    carried in `SeedMemory.answer`.
- `_action_query_memory`:
  - a dynamic-handler answer (e.g. git status) now appears inside the fenced
    block;
  - the empty path (`"No relevant remembered facts."`) still holds;
  - user-overlay seed is tiered before upstream before dynamic claims.
- Migrate `query_qa`'s existing dynamic-handler assertions onto `query_memory`.
- Assert the `query_qa` action name is gone — the enum no longer defines it, so
  the structured-output surface can't emit it.

## Out of scope

- `QueryFilterRouterAgent` and the chat route — untouched.
- Renaming `query_memory` to `recall`.
- Changing the `<recalled_memory>` fence, the hybrid claim ranker, or the shield
  system.
