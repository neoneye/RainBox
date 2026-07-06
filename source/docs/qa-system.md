# Q&A System (Seed Memory)

## Purpose

The Q&A system answers questions about the operator and the running system from
a curated knowledge base, separate from the dynamic memory-claim store. It backs
two things:

- The **assistant**'s `query_memory` read action (the ReAct loop in
  `agents/assistant.py`).
- The **chat** query agents that answer a message directly, plus the always-on
  "Curated facts" block injected into every chat turn.

The knowledge base is a JSONL registry of question/answer entries, embedded into
pgvector for semantic lookup and mirrored in an in-memory alias table for exact
lookup. Entries are either **static** (a fixed answer) or **dynamic** (a
read-only handler that computes a live answer, e.g. git status). Sensitive
entries can be hidden behind **shields**.

The module is `memory/seed_memory.py`; dynamic handlers live in
`agents/query_handlers.py`.

## Data

### Registry files

Entries are loaded from two JSONL files, merged by `id` (`_load_jsonl`):

- **Base** — `data/question_answer.jsonl` (`QA_JSONL_PATH`), tagged
  `_source="upstream"`.
- **Operator overlay** — `<customize.dir>/question_answer.jsonl`, tagged
  `_source="user-overlay"`. `customize.dir` is a setting pointing at the
  operator's private customizations (PII / persona). An overlay entry replaces a
  base entry with the same `id`.

### Entry schema

One JSON object per line:

- `id` — UUID; the `qa_id`. Required (id-less lines are dropped).
- `path` — dotted label grouping the entry, e.g. `identity.model` or
  `human.<person>.<topic>`.
- `kind` — `"static"` or `"dynamic"` (defaults to `static`).
- `questions` — list of phrasing alternates. Each becomes an exact-match alias
  and an embedded document.
- `answer` — the answer text (static entries).
- `handler` — a function name in `HANDLERS` (dynamic entries).
- `shield` — optional shield name; the entry is hidden from the LLM unless that
  shield is unlocked (see [Shields](#shields)).
- `_source` — injected at load time (`upstream` / `user-overlay`), not in the file.

### Storage

- **pgvector table** `data_seed_memory` (`QA_FULL_TABLE`) — one embedded node per
  question alternate, for semantic retrieval. Populated by `_ensure_populated`
  / `rebuild_kb`.
- **In-memory registry** (`_entries_by_id`, `_alias_table`) — built by
  `_load_kb`: `qa_id → entry` and normalized-question → `qa_id`. Required to
  resolve a match back to its answer/handler; a caller that retrieves without
  loading the registry gets nothing.

## Retrieval

Tuning constants (`memory/seed_memory.py`): `TOP_K = 5`, `MIN_SCORE = 0.60`,
`MIN_MARGIN = 0.05`.

- **Exact alias** (`_exact_match`) — normalize the query, look it up in
  `_alias_table`. No embedding call; deterministic.
- **Semantic, ungated** (`_semantic_ranked`) — pgvector top-K, aggregated to the
  max score per `qa_id`, returned ranked descending. No score gate — the caller
  decides.
- **Semantic, gated top-1** (`_semantic_match`) — requires the best score
  `>= MIN_SCORE` and a margin `>= MIN_MARGIN` over the runner-up. Returns `None`
  when too weak or ambiguous — a clean "no" over a confident wrong answer.

Resolving a match to text is `_resolve_match`: static → `answer`; dynamic → run
the handler.

### Dynamic handlers

Dynamic entries name a read-only function in `HANDLERS` (`agents/query_handlers.py`),
called with a `QueryContext` (room, query, agent). Handlers cover identity
(`get_capabilities`, `get_model_info`), system (`get_system_health`,
`get_system_resources`, `get_host_info`), dev (`get_git_status`,
`get_last_git_commit`, `get_test_status`, `get_cron_overview`,
`get_kanban_overview`), and project (`get_current_chatroom`, …) facts. Because
they compute a live value, their answers change between calls.

### Shields

A shield hides sensitive entries from the LLM until the operator unlocks them.

- An entry with no `shield` is always visible. An entry with a `shield` reaches
  the LLM only when that shield name is in the `qa.unlocked_shields` setting
  (empty by default — everything shielded stays hidden).
- The `shield` value must be a **string** (the shield name). A non-string value
  (`"shield": 5`, `["a","b"]`, …) is a data error: `_load_jsonl` rejects it, so
  **repopulate fails hard** with a `file:line` message (surfaced by the
  /settings repopulate result). As a runtime backstop, any non-string shield
  that still reaches a lookup is treated as locked — fail closed, never revealed
  (`_entry_locked`).
- Enforced in two layers: at the pgvector query via a metadata filter
  (`_shield_filters` — keep entries whose `shield` is empty OR in the unlocked
  set, so locked entries never occupy a top-K slot) and as an in-memory backstop
  (`_entry_locked` / `_drop_locked`) that also catches cross-process staleness
  and Settings toggles with no repopulate.
- `available_qa_shields()` lists the shield names for the /settings UI. The
  unlocked set comes from `_unlocked_shields()` (the `qa.unlocked_shields`
  setting; empty outside an app context — the safe default).

## Consumers

### Assistant `query_memory`

`_action_query_memory` (`agents/assistant.py`) is the assistant's single read
action for facts. It:

1. Loads the registry (`_load_kb`) and ensures the table is populated
   (`_ensure_populated`) — the assistant loop does not otherwise load the KB.
2. Retrieves **static and dynamic** seed answers, top-N, gated at `MIN_SCORE`
   (`retrieve_seed_answers`), resolving dynamic handlers on demand.
3. Retrieves dynamic memory claims (`retrieve_memories_hybrid`).
4. Tiers the result — user-overlay seed, then upstream seed, then dynamic
   claims — and wraps it in a `<recalled_memory>` fence (untrusted-data
   framing; angle brackets sanitized). The fence holds only bare fact lines
   (`{uuid}, {tags}: {text}`); the format legend and the truncation note live
   *outside* it (they are the assistant's own instructions, not recalled data).

Each fact is capped to `QUERY_MEMORY_PER_FACT_CHARS` (1200) — longer facts are
shortened and tagged `truncate1200` — and the whole block to
`QUERY_MEMORY_TOTAL_CHARS` (11000); lower-ranked facts past the budget are
dropped at a fact boundary (never mid-word) and counted in a note appended
outside the fence. This keeps one large overlay entry (some are >5000 chars)
from crowding out every other fact.

To read a shortened or omitted fact in full, the model calls `query_memory`
again with `{"uuid": "<the fact's uuid>"}` instead of `{"query": ...}`
(`_query_memory_full`): the uuid mode returns that single entry untruncated —
seed entries still respect shields, claims never return secrets. The system
prompt tells the model about the `truncate` tags and this uuid escape hatch.

It also posts a one-time re-check notice when facts were invalidated — see
[Facts-invalidation notice](#facts-invalidation-notice).

### Chat "Curated facts" (always-on)

`chat_context.py` injects a "Curated facts" block into every chat turn via
`retrieve_seed_memories` — **static entries only**. Dynamic handlers are not
resolved here: running them on every turn regardless of relevance would be
expensive and noisy. (`retrieve_seed_answers`, above, is the on-demand
static+dynamic counterpart.)

### Chat query agents

Three agents answer a chat message directly from the same registry (registered
in `agents/__main__.py`):

- `query` — `QueryAgent`: exact alias, then gated semantic match; posts the
  resolved answer.
- `query_router` — `QueryRouterAgent`: exact alias, then a single LLM call that
  both judges relevance and routes/answers.
- `query_filter_router` — `QueryFilterRouterAgent`: exact alias, then a
  two-stage LLM pipeline — a relevance **filter** over the ungated top-K
  candidates (`_semantic_ranked(...)[:TOP_K_FILTER]`, `TOP_K_FILTER = 5`),
  then a **route** call that produces the reply. Memory commands short-circuit
  before any Q&A retrieval.

All share the seed-memory matching functions; they differ in how much LLM
judgment sits between retrieval and reply.

## Operator operations

- **Add/edit facts** — edit the overlay `question_answer.jsonl` under
  `customize.dir` (or the base file), then repopulate.
- **Repopulate** — the /settings "Repopulate Q&A memory" button
  (`POST /settings/api/repopulate_memory` → `rebuild_kb`) re-reads the merged
  JSONL and re-embeds it without a restart. Equivalent to setting
  `QUERY_AGENT_REBUILD_KB=1` (`REBUILD_ENV`) and restarting. A failure (e.g. the
  embedding backend down, or a JSONL parse error carrying `file:line:column`)
  leaves the table empty/partial; the next successful run heals it.
- **Unlock a shield** — check it on /settings and Save; this writes
  `qa.unlocked_shields`. Shielded entries become visible to the LLM immediately
  (the in-memory backstop applies on the next query; no repopulate needed).
- **Troubleshooting** retrieval failures: see `operator-guide.md`
  ("Seed-memory / QueryAgent retrieval fails").

### Facts-invalidation notice

Changing a shield or repopulating the Q&A can stale facts the assistant already
answered earlier in a conversation. `query_memory` filters correctly, but a prior
answer still sits in the chat transcript, so the model can reuse it. To counter
this:

- A shield change (`qa.unlocked_shields`, when the value actually changes) or a
  repopulate stamps `qa.facts_invalidated_at` (`db.mark_facts_invalidated`).
- The next time the assistant runs in a room, it posts a one-time visible notice
  telling the model that earlier answers may be out of date and to re-check via
  `query_memory` (`_maybe_post_facts_marker`). It is deduped per invalidation via
  the marker's `meta.facts_invalidation` timestamp, and does not remove any
  history — the operator's message stays the current one.

This is a **soft** signal by design, not a hard boundary: it nudges the model to
re-query but leaves the earlier answer in history. A hard guarantee would mean
removing or redacting prior answers from the transcript — but that strips the
assistant's conversational memory. Full history is kept on purpose: a
"lobotomized" assistant that starts each session with wiped context forces the
operator to re-explain everything upfront before they can even ask their real
question. Preserving context is worth more than a hard block on this edge case.

## Telemetry

Retrieval events (candidates, scores in permille, the chosen match) are recorded
via `db.record_retrieval_event`; see `relevance-telemetry.md`.

## Reference

| Thing | Where |
|-------|-------|
| Registry + retrieval | `memory/seed_memory.py` |
| Dynamic handlers | `agents/query_handlers.py` (`HANDLERS`) |
| Assistant read action | `agents/assistant.py` (`_action_query_memory`) |
| Always-on chat facts | `agents/chat_context.py` (`retrieve_seed_memories`) |
| Chat query agents | `agents/query.py`, `agents/query_router.py`, `agents/query_filter_router.py` |
| Base data | `data/question_answer.jsonl` |
| pgvector table | `data_seed_memory` |
| Settings | `qa.unlocked_shields`, `customize.dir`, `qa.facts_invalidated_at` |
| Constants | `TOP_K=5`, `MIN_SCORE=0.60`, `MIN_MARGIN=0.05`, `TOP_K_FILTER=5` |
