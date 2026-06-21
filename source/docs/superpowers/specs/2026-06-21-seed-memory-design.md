# Seed memory — unify curated Q&A into the memory system (design)

**Status:** approved design, ready for an implementation plan.
**Date:** 2026-06-21

## Goal

Make the curated `question_answer.jsonl` entries first-class **memories**: the
assistant's `query_memory` should surface them, decoupled from the throwaway
QueryAgent, while keeping LlamaIndex's pgvector for the vector search. A "what
candy do I like" question must return the operator's curated answer.

## Background — why this is needed

There are two separate retrieval stores today, and the assistant only sees one:

| | dynamic memories | curated Q&A (seed) |
|---|---|---|
| store | `memory_claim` + rainbox-owned `memory_embedding` | LlamaIndex `PGVectorStore` → table `data_query_agent_kb` |
| embeds | the claim/fact text | the **question phrasings** (an answer can have many) |
| reached by | `query_memory` | `query_qa` (and QueryAgent) |
| source | `remember`ed at runtime | `question_answer.jsonl` (upstream `data/` + operator overlay `rainbox_customize/`) |

A real failure (observed): "what candy do I like" → the assistant called
`query_memory`, which searched only `memory_claim` and missed the candy answer
that lives in the Q&A store. The Q&A store is also named/structured around
`QueryAgent`, a temporary troubleshooting agent, even though the operator wants
these pairs available everywhere as memories.

The Q&A store's retrieval model is actually *better* for this: it embeds the
question phrasings ("What candy does Simon like"), so a natural question matches
even when the answer text never says "candy". We keep that.

## jsonl entry shape (already updated by the operator)

```json
{"id": "229a2a6d-…uuid…", "path": "identity.name", "kind": "static",
 "questions": ["What is your name?", "Are you Egon?"],
 "answer": "My name is EgonBot. …"}
```

- **`id`** — a **uuid**, the single canonical reference (shown in `query_memory`
  output, greppable in logs).
- **`path`** — a human label (`identity.name`); descriptive only, not a search key.
- **`kind`** — `static` (a fact) or `dynamic` (handler-computed, e.g. git status).
- The loader already keys its registry on `id`, so the uuid flows through as the
  existing `qa_id` for free.

## Architecture

Approach **A — two stores, one retrieval** (chosen over folding everything into
`memory_claim`, which would abandon LlamaIndex and lose the question-phrasing
embeddings).

```
                       ┌── retrieve_memories_hybrid → memory_claim/_embedding (dynamic)
query_memory ──fan-out─┤
                       └── retrieve_seed_memories ──→ seed_memory store (LlamaIndex)
                                  │ merge, tiered
                                  ▼
        user-overlay seed  >  upstream seed  >  dynamic   (each line shows its uuid + source)
```

### Component 1 — `seed_memory` module (reframed from `query_kb_helpers`)

The current `agents/query_kb_helpers.py` is repurposed as the **seed memory**
store and renamed to `memory/seed_memory.py` (signals it is part of the memory
system; both the assistant and QueryAgent import from it). Responsibilities,
mostly unchanged:

- Load the jsonl (upstream `data/` + operator overlay), **tagging each entry with
  its `source`** (`upstream` | `user-overlay` — which file it came from). Overlay
  still overrides upstream by `id`.
- Embed question phrasings into LlamaIndex pgvector (kept).
- Provide `retrieve_seed_memories(query, *, limit) -> list[SeedMemory]`:
  - semantic match over the question embeddings, **deduped by `id` (uuid)** so an
    entry isn't returned once per question phrasing;
  - **`kind == "static"` only** — dynamic/handler entries are computed answers,
    not facts, and are excluded here (they remain reachable via `query_qa`);
  - filtered by the store's existing relevance threshold (~0.66) so unrelated
    entries don't flood the block;
  - capped at `limit` (default **5**, matching the dynamic side);
  - each `SeedMemory` carries `{uuid, path, source, answer, score}`.

The existing `query_qa` / QueryAgent paths keep working through the same module
(including dynamic handlers) — this is an additive consumer, not a rewrite.

### Component 2 — `query_memory` fan-out + tiered merge

`agents/assistant.py::_action_query_memory` (and the shared chat memory block in
`agents/chat_context.py`) call both retrievers and merge:

1. **user-overlay seed** (operator's own jsonl) — always first.
2. **upstream seed** (shipped with RainBox).
3. **dynamic `memory_claim`** — ordered by similarity, as today.

Tiering by provenance avoids comparing raw similarity scores across two different
embedding spaces (question-vs-question in the seed store, query-vs-fact in the
claim store). Output reuses the existing uuid-first line format:

```
Relevant remembered facts
- {memory_uuid}, {tags}: {text}
- 229a2a6d-…, seed/user-overlay: My name is EgonBot. …
- 25c4dcc8-…, fact, private, confirmed_by_user: Simon prefers python+postgres+…
```

Seed lines tag `seed/<source>` so the operator can see a fact came from the
curated jsonl vs a runtime `remember`.

### Immutability (falls out for free)

Seed memories live **only** in the LlamaIndex store, never in `memory_claim`.
`remember` and `forget_memory` only ever touch `memory_claim`, so the model
**cannot** forget or overwrite a seed memory. Changing one means editing the
jsonl and re-seeding (the existing "repopulate" path). No new enforcement code.

### Table rename

`data_query_agent_kb` → `data_seed_memory` (constant `QA_TABLE_NAME` →
`seed_memory`) to finish decoupling from QueryAgent. The operator's repopulate
button re-embeds, so this is a clean swap, not a data migration. The Flask-Admin
view (already under the **Memory** menu) follows the rename.

## Out of scope (deferred, by decision)

- **Answer normalization / "Table B".** The answer stays in the LlamaIndex node
  metadata for now; a multi-question entry still repeats its answer across its
  question rows in the raw table. The normalized uuid→answer table rides along
  with `/search` later — the uuid identity the operator needs is already provided
  by the `id` field, so nothing is blocked.
- **`/search` page** (paste a uuid → resolve any entity). Explicitly deferred.
- **Dynamic/handler entries as memories.** They stay computed-on-demand via
  `query_qa`.

## Testing

- `retrieve_seed_memories("what candy do i like")` returns the candy entry with
  its uuid + `source=user-overlay`; a model-free embedder seam (as memory tests
  use) keeps it deterministic.
- only `kind=static` entries are returned (a `dynamic` entry is never a seed
  memory).
- dedup: an entry with multiple question phrasings appears once.
- `query_memory` merge ordering: user-overlay seed before upstream seed before
  dynamic; the candy answer now appears for "what candy do i like".
- immutability: `forget_memory` / `remember` leave the seed store untouched (they
  operate on `memory_claim` only) — a seed uuid can't be forgotten.
- the rename: the store populates/reads `data_seed_memory`; QueryAgent + `query_qa`
  still resolve answers.

## Success criteria

Asking the assistant "what candy do I like" returns the operator's curated candy
answer, surfaced through `query_memory` with the entry's uuid and a
`seed/user-overlay` tag — without the operator having `remember`ed anything, and
with the entry un-forgettable by the model.
