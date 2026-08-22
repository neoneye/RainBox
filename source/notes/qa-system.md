# Q&A System (Seed Memory)

## Purpose

The Q&A system answers questions about the operator and the running system from
a curated knowledge base, separate from the dynamic memory-claim store. It backs
two things:

- The **assistant**'s `memory_query` read action (the ReAct loop in
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

Entries are loaded from two JSONL files, merged by `id` (`_load_jsonl`), plus
one optional JSON file declaring **relations** (see
[Derived rosters](#derived-rosters)):

- **Base** — `data/question_answer.jsonl` (`QA_JSONL_PATH`), tagged
  `_source="upstream"`. Stays publishable — no PII.
- **Operator overlay** — `<customize.dir>/question_answer.jsonl`, tagged
  `_source="user-overlay"`. `customize.dir` is a setting pointing at the
  operator's private customizations (PII / persona). An overlay entry replaces a
  base entry with the same `id` wholesale.

- **Relations** — `<customize.dir>/relations.json` (`RELATIONS_FILENAME`),
  optional. Each declaration turns a path prefix into one synthesised roster
  entry. Absent file means no rosters and is not an error.

Within one file, a duplicate `id` or a duplicate `path` is an operator mistake
and is rejected — repopulate fails hard with a `file:line` message naming the
first occurrence. Reuse *across* files is the override mechanism and is fine.

Three keys are **reserved for the loader** and are rejected in a source file
with a `file:line` (`_RESERVED_ENTRY_KEYS`): `_source` and `_row_sha256`, both
injected into every entry during loading, and `_derived`, which is set only on
synthesised rosters. Other `_`-prefixed keys are not policed. An authored
`_derived` is the one that matters — it would let an entry suppress full-text
indexing of its own answer.
(The overlay schema is under active design — see the
`notes/proposals/2026-07-*-qa-overlay-*` proposals.)

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
- `label` — optional display name, used when the entry is listed as a roster
  member. Must be a non-empty string with no newline. Without it a roster falls
  back to the entry's final path segment, which is never prettified: casing and
  diacritics are unrecoverable from a slug.
- `_source` — injected at load time (`upstream` / `user-overlay`), not in the file.
- `_row_sha256`, `_derived` — injected at load time; see above.

### `label`, and why there are no tags

`label` reads like a tag, and it is not one. It is worth being precise about,
because the field it *looks* like is the field this schema does not have.

**What it is.** One string, one entry, purely presentational: the name printed
for this entry when a roster lists it as a member.

```json
{"path": "human.<subject>.friend.alberteinstein", "label": "Albert Einstein", ...}
{"path": "human.<subject>.friend.nielsbohr", ...}
```

```text
recorded friends (2):
- Albert Einstein  [<qa_id>]      ← with a label
- nielsbohr        [<qa_id>]      ← without one: the raw final path segment
```

The fallback is never prettified. Capitalisation, spacing and diacritics are
unrecoverable from a slug, and a wrong guess misspells somebody's name — so the
slug is printed exactly as filed, and `label` is how an operator overrides it.

**Choosing one.** Four rules, each learned from getting it wrong:

- *Read the name off the entry, never off the slug.* A slug has already lost
  the word boundaries, so reconstructing one guesses where they were — and a
  two-word given name compressed into a slug reads convincingly as a different
  one-word name. The entry's own `questions` and `answer` spell it correctly;
  take it from there.
- *Do not repeat what the roster already says.* A roster titled `families`
  renders `recorded families (4):` directly above its members, so labelling a
  member `<name>'s family` says "family" twice and adds nothing. The label only
  has to say **which one**.
- *Prefer names over relational words.* Relational words are language-specific,
  and a possessive-plus-noun construction is English — it reads as an
  intrusion in a registry written in another language. Names joined by a
  separator carry across languages unchanged.
- *Informative beats defensive.* A label naming somebody other than the entry's
  own subject — both partners of a couple, say — goes stale if that
  relationship changes. That is a smaller cost than it looks: the entry's
  `answer` names them too, so the same change already forces an edit there, and
  the label is one more field in a pass you are making anyway. Prefer the label
  that tells the reader more.

A label is only worth adding to an entry that a roster will list. Everywhere
else it is inert.

**Where it is read.** In exactly one place: `_member_label`, called only from
`_render_roster`. An entry that is never a roster member never has its `label`
read, and it is validated only at that point (non-empty string, no `\n` or
`\r`; a number, `""` or `null` is a configuration error, not something to
coerce).

**What it is not.**

- *Not a list.* One entry, one label. `["a", "b"]` is rejected.
- *Not searchable.* The label is not embedded and not lexically indexed. In
  fact the roster answer carrying it is **deliberately excluded** from the
  full-text index (see [Derived rosters](#derived-rosters)), so a label
  contributes nothing to retrieval anywhere. **Adding a label does not make a
  person findable by that name.**
- *Not a category, tag, or second path.* It groups nothing and is queried by
  nothing.

Where each concern actually lives:

| field | job | cardinality |
| --- | --- | --- |
| `path` | where the entry is filed; what makes it a roster member | one |
| `questions` | how the entry is **found** | many |
| `label` | how the entry is **printed** in a roster | one |
| `answer` | what is read once it is found | one |

So the field closest to "many handles on one row" is `questions`: every
phrasing there becomes an alias and an embedded document pointing at the same
entry. When something cannot be found, a phrasing in `questions` is the fix —
never a label.

**Why there is no tag field.** `path` gives each entry exactly one home in a
tree, and that is the whole grouping mechanism. A person filed under
`human.<subject>.friend.nielsbohr` is *a friend*; if the same person is also a
former colleague, a neighbour, and connected to a third party, none of that is
expressible as structure. It survives only as prose inside the answer and as
phrasings in `questions`, and nothing can enumerate either.

This is why `who are all my <relation>` is answerable and
`who did I meet through <context>` is not: the first is a path prefix, the
second is a cross-cutting set. Rosters deliberately solve only the prefix case.

Tags — or typed edges between entries — are the design that would close the
gap, and neither exists. See the tree-versus-graph note at the end of
`notes/proposals/2026-08-21-set-valued-questions-and-derived-rosters.md`, and
the separate `qa_edge` design in
`notes/proposals/2026-08-08-qa-navigation-routes.md`, which is unbuilt.

### Derived rosters

A question like "who are all my X" has an N-entry answer set, while the
candidate budget is a fixed top-k — so members past k are unreachable however
good the ranker. A **roster** collapses those N entries into one, and one
candidate slot then carries the whole set.

Each declaration in `relations.json` becomes one synthesised entry:

```json
{
  "relations": [
    {
      "prefix": "human.<subject>.friend",
      "title": "friends",
      "complete": false,
      "shield": null,
      "questions": ["who are my friends", "my friends", "list my friends"]
    }
  ]
}
```

- `prefix` — the path prefix whose children are the members. Members are
  entries at exactly `prefix + "." + <one non-empty segment>`; a member's own
  subtree belongs to that member, not the roster.
- `title` — the noun in the rendered answer.
- `complete` — whether the operator asserts the prefix holds *everyone*.
  Default `false`, which renders the qualifier **and an explicit caveat line**;
  `true` renders neither. The count is always the number of *entries*, never a
  claim about the world.

  ```text
  recorded friends (6):
  (Recorded entries only, not necessarily everyone — absence from this list is not evidence.)
  - Albert Einstein  [<qa_id>]
  ```

  The caveat is a sentence rather than the adjective alone because a hedge is
  the first thing a model drops when it summarises: "recorded" relies on the
  connotation surviving, while the caveat states the inference not to make.
  This exists because a personal-memory store must never read "never written
  down" as "false" — the same reasoning that rules out a Datalog-style design,
  which a roster claiming exhaustiveness would have reintroduced.

  **How well it works is measured, not assumed** —
  `benchmarks/roster_completeness.py`, and the honest answer is *sometimes*.
  Asked about somebody absent from the list, with the caveat present versus
  absent:

  | model | `complete: false` | `complete: true` |
  | --- | --- | --- |
  | `llama3.2:3b` | flat answer | flat answer |
  | `granite3.3:8b` | flat answer | flat answer |
  | `qwen3.5:9b` | **names the limit** | flat answer |

  So the caveat changes behaviour on the 9B and is ignored by the smaller two,
  which answer "no, not on the list" either way. What `complete` guarantees
  unconditionally is that the **stored artifact** stops over-claiming; that a
  given model acts on it is a property of the model, and small local models
  should be assumed not to. Re-run the benchmark after changing the wording or
  the model.
- `shield` — **required**, `null` or a name. Declared rather than derived: a
  roster may have zero members, and defaulting that to unshielded would publish
  the declaration's own title and questions. Synthesis verifies every member
  carries the same shield class (*unshielded* is a class of its own) and
  otherwise produces **no roster for that prefix**, silently — shielding a
  member is data evolution, not malformed configuration, and must not take the
  registry down. Every other validation failure raises before any write.
- `questions` — authored aliases, exactly like an entry's. Phrasings are
  language- and instance-specific, so no predicate→phrasing table ships in the
  repository.

**How a rejected declaration is reported.** JSON has no line numbers, so an
error names the declaration by its `prefix` — a string the operator can search
for, which is the affordance `file:line` provides for the JSONL loader. When
`prefix` is itself the broken field the message falls back to `title`, and to
the ordinal only when neither is usable. The path is the resolved one, not the
bare filename, so it is clear which file to open:

```text
…/relations.json: relation 1 (prefix 'human.<subject>.colleague'): 'shield' is required — …
…/relations.json: relation 1 (title 'neighbours'): 'prefix' must be a non-empty string
```

Malformed JSON is reported with the decoder's own line number instead.

A roster is an ordinary `kind: "static"` entry tagged `_derived: "roster"` and
`_source: "user-overlay"`, with a `uuid5(_ROSTER_NS, prefix)` id. It embeds,
ranks, obeys shields and renders like any entry: apart from the full-text
exclusion below, existing consumers otherwise treat it like any static
entry. Rendering is bounded at `ROSTER_ANSWER_MAX_CHARS`
(1100, under `memory_query`'s 1200-char per-fact cap, so the uncapped chat
routes are covered too) and truncates only at member boundaries:

```text
recorded friends (6):
- Alpha  [<qa_id>]
- … 3 additional recorded members omitted
```

The header reports total membership and the marker the omitted count; a label
or id is never split. Each line keeps its `qa_id` so a member can be read in
full via `memory_query`'s uuid mode — the roster is an index card, not a
display list.

Three rules the rest of the system depends on:

- **A roster's answer is excluded from the lexical index.** It holds every
  member label, so indexing it would surface the roster on single-person
  queries and hand the rarest name tokens an extra document. `_fulltext_index`
  skips the answer when `_derived == "roster"`. This is the only retrieval-side
  change rosters make.
- **Synthesis runs over frozen authored entries** and appends rosters
  afterwards, so a roster at `<p>.<q>` can never become a member of a
  declaration for `<p>` depending on declaration order.
- **Collisions raise, naming both sides**: an authored entry at the roster's
  path, an authored entry holding its generated id (the registry is keyed by
  id, so this would silently replace the authored entry), or two members
  sharing a path (under a declared prefix the path is the member's identity).

`_ROSTER_NS` and the canonical JSON encoding (`sort_keys`, compact separators,
`ensure_ascii=False`, UTF-8) are pinned: rows outlive the code that wrote them,
so changing either orphans every embedded roster instead of updating it.

### Storage

- **pgvector table** `data_seed_memory` (`QA_FULL_TABLE`) — one embedded node per
  question alternate, for semantic retrieval. Kept in sync by `sync_kb` /
  `_ensure_populated` (with `rebuild_kb` as the full-wipe path). Only the
  **question text** is embedded: answer/handler/shield ride along as metadata
  excluded from the vector (`_build_documents`), so a long answer neither
  pollutes the question vector nor trips the chunk-size guard. Every node also
  carries the row's sync stamp — `row_sha256` (SHA-256 of the entry's raw JSONL
  line) and `kb_epoch` (`EMBED_MODEL_NAME|KB_SCHEMA_VERSION`) — which is what
  makes incremental reconciling possible (see
  [Sync (incremental reconcile)](#sync-incremental-reconcile)).

  A roster has no source line, so its `row_sha256` is a digest over its
  **complete synthesised representation** — prefix, title, questions,
  `complete`, `shield`, the render version and budget, and each member's
  `(qa_id, row_sha256)` in order. Hashing members alone would leave an alias,
  title or format edit embedded as stale text; and because `sync_kb` clears the
  registry caches only when a row actually changed, a digest missing an input
  also misses the invalidation.
- **In-memory registry** (`_entries_by_id`, `_alias_table`) — built by
  `_load_kb`: `qa_id → entry`, and normalized-question → **the list of
  `qa_id`s claiming it** (`_build_alias_table`, distinct and in first-seen
  order). Required to resolve a match back to its answer/handler; a caller
  that retrieves without loading the registry gets nothing.

  The alias list is not cosmetic. Two entries may legitimately carry the same
  question text, and mapping the alias to one id silently discarded every other
  claimant while `_exact_match` answered one of them at `score=1.0`. It is also
  deduplicated **by `qa_id`**, because one entry may carry several questions
  that collapse under `_normalize_query` (casing, a trailing `?`) — the base
  registry has such entries, and treating them as two claimants would stop a
  working lookup.

## Retrieval

Tuning constants (`memory/seed_memory.py`): `TOP_K_NODES = 50` (question
*nodes* fetched from pgvector — entries carry many question alternates, so a
small node budget collapses to very few unique entries after by-`qa_id`
aggregation), `TOP_K_VECTOR = 5` / `TOP_K_FULLTEXT = 5` (per-signal candidate
budgets for the filter pipelines), `MIN_SCORE = 0.60`, `MIN_MARGIN = 0.05`
(gates for the legacy/gated paths only).

- **Exact alias** (`_exact_match`) — normalize the query, look it up in
  `_alias_table`, drop locked claimants, and answer **only if exactly one
  visible entry remains**. Two surviving claimants mean the alias is
  ambiguous, and an arbitrary pick at `score=1.0` is not an answer: exact
  matching declines and the caller falls through to its own retrieval path
  (four callers, three different fallbacks — `agents/query.py` gated
  `_semantic_match`, `agents/query_router.py` ungated top-1 into an LLM,
  `agents/query_filter_router.py` the relevance filter, and
  `webapp/memory_developer_views.py` a debug view). No embedding call;
  deterministic. Note the assistant's `memory_query` is **not** among them —
  it never consults the alias table.
- **Semantic, ungated** (`_semantic_ranked`) — pgvector top-`TOP_K_NODES`
  nodes, aggregated to the max score per `qa_id`, returned ranked descending.
  No score gate — the caller decides. The query vector comes from
  `embed_query` (below) and is handed to the retriever in the `QueryBundle`,
  so LlamaIndex does not embed the string a second time.
- **Lexical full-text** (`_fulltext_ranked`) — IDF-weighted token overlap over
  every entry's questions (double weight) AND answer text, scored in Python
  against the in-memory registry (no embedding server needed). The signal
  question embeddings miss: exact content words ("demoscene") and
  answer-only tokens. `Match.score` is query *coverage* (1.0 only when every
  query token matches in the questions; answer-only matches count half;
  KB-unknown query tokens count against coverage). No stemming; English
  stopwords only.
- **Hybrid fusion** (`_hybrid_seed_ranked`) — the filter pipelines' candidate
  set: the top `top_k_vector` entries by embedding similarity plus the top
  `top_k_fulltext` by full-text, deduplicated, presented as a neutral
  interleave (best vector, best full-text, second vector, ...). The signals
  are deliberately NOT score-blended — the scales aren't commensurable, and a
  weighted blend buried perfect rare-token matches; relevance judgment
  belongs to the filter LLM. A budget of 0 disables its signal; degrades to
  full-text-only when the embedding server is down.
- **Semantic, gated top-1** (`_semantic_match`) — requires the best score
  `>= MIN_SCORE` and a margin `>= MIN_MARGIN` over the runner-up. Returns `None`
  when too weak or ambiguous — a clean "no" over a confident wrong answer.

**`embed_query`** is the one place a search query is embedded, for both vector
stores: this KB and the memory-claim store (`memory/retrieval.py::_vector_sims`
defaults to it). One `memory_query` searches both, and each store embedding the
query for itself put two identical requests on embeddinggemma — on the same
local runtime the assistant's own model is waiting for. The memo is bounded and
process-lifetime: an embedding is a pure function of (model, text) and the
embedder is a process singleton, so a hit is never stale, and the same question
asked twice costs one embedding. Tests get an empty cache per test (the
autouse fixture in `conftest.py`), since they swap in fake embedders.

Resolving a match to text is `_resolve_match`: static → `answer`; dynamic → run
the handler.

### Dynamic handlers

Dynamic entries name a read-only function in `HANDLERS` (`agents/query_handlers.py`),
called with a `QueryContext` (room, query, agent). Handlers (~40) cover identity
(`get_capabilities`, `get_version`, `get_model_info`), system health and host
facts (`get_system_health`, `get_system_resources`, `get_host_info`,
`get_gpu_info`, `get_connectivity`, `get_local_ip`, uptimes, datetime), dev/git
(`get_git_status`, `get_git_overview`, `get_last_git_commit`, `get_test_status`,
`get_outdated_dependencies`), subsystem overviews (`get_cron_overview`,
`get_kanban_overview`, `get_todo_list`), chat (`get_current_chatroom`,
`list_chatrooms`), and memory introspection (`get_memory_stats`,
`get_last_match_explanation`). Because they compute a live value, their answers
change between calls.

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

> **Control-plane caveat.** `qa.unlocked_shields` is an ordinary setting, and
> the settings API has no authentication — any local HTTP caller can unlock
> every shield with one `POST /settings/api/set`. Shields gate what reaches
> the *LLM*, not what a local attacker can read. This is Finding 8a of
> `notes/proposals/2026-06-25-security-review-mitigations.md` (open); the plan
> is auth plus treating shield changes as high-sensitivity, audited actions.

## Consumers

### Assistant `memory_query`

`_action_query_memory` (`agents/assistant.py`) is the assistant's single read
action for facts. It:

1. Loads the registry (`_load_kb`) and ensures the table is populated
   (`_ensure_populated`) — the assistant loop does not otherwise load the KB.
2. Retrieves memory-claim candidates (`retrieve_memories_hybrid`) and seed
   candidates (`_hybrid_seed_ranked`, ungated, per-signal budgets).
3. Runs ONE shared **recall filter** call over both candidate kinds
   (`_filter_recalled_candidates`): the scorer LLM (the
   `assistant.memory_filter` slot) rates every candidate on the Likert scales, code keeps/drops via
   `apply_filter_scores`, kept seed entries are resolved (dynamic handlers run
   only for kept candidates), kept claims are injected. In a live run the
   scoring call is made by the loop (`AssistantAgent._recall_filter_call`)
   through the same `_structured_completion` as every other call of the turn,
   on the filter's model group, and lands as its own code-driven `recall_filter`
   step row — prompts, scores, model link, cost and retries included. Live runs
   also record one RetrievalEvent verdict per candidate (see
   `relevance-telemetry.md`).
   Fallback when no scorer group is bound or the LLM fails: `MIN_SCORE`-gated
   `retrieve_seed_answers` plus unfiltered claims — recall degrades, never
   dies with the scorer.
4. Tiers the result — user-overlay seed, then upstream seed, then claims —
   and wraps it in a `<recalled_memory>` fence (untrusted-data framing; angle
   brackets sanitized). The fence holds only bare fact lines
   (`{uuid}, {tags}: {text}`); the format legend and the truncation note live
   *outside* it (they are the assistant's own instructions, not recalled
   data). The scorer's think-before-scoring note is NOT part of the
   observation text: it is one model's summary of a candidate set, and a
   traced run had it asserting that no candidate held an answer that was in
   the candidate list. It stays in `observation.data["recall_filter"]`, on the
   step row and in the inspector.

Seed fact tags are, in order: `seed/<source>` (`seed/user-overlay` or
`seed/upstream`), `dynamic` for handler entries, the entry's `path` (omitted
when it has none), and `truncateN` when the fact's middle was dropped to fit. The path is what makes
answers whose text alone is ambiguous tellable apart, e.g.:

```text
013d…, seed/upstream, dynamic, system.uptime_host: 2:33  up 22 days, …
e6d8…, seed/upstream, dynamic, system.uptime_process: 1m 35s (since …)
```

The uuid full-fetch mode (below) renders the same tag shape.

Each fact's text is capped to `MEMORY_QUERY_PER_FACT_CHARS` (1200) — longer
facts have their MIDDLE dropped, keeping both ends, and are tagged
`truncate1200`. The cap is on the RENDERED text, marker included, so a
shortened fact cannot displace another through marker overhead.

`MEMORY_QUERY_FACT_PAYLOAD_CHARS` (11000) then governs which further facts are
admitted, counted over the format legend, the per-line newlines and the
retained fact lines — not the fence and not the notes after it. It is a
threshold, not a ceiling: the first fact is admitted whatever its size, because
one over-long line is better than returning nothing, and the payload is then
larger than the number. Facts after it are admitted only while they fit;
lower-ranked ones past that point are dropped at a fact boundary (never
mid-word) and counted in a note appended outside the fence. This keeps one
large overlay entry (some are >5000 chars) from crowding out every other fact.

To read a shortened or omitted fact in full, the model calls `memory_query`
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
  two-stage LLM pipeline over the hybrid candidates (`_hybrid_seed_ranked`,
  5 vector + 5 full-text): the **filter** LLM scores every candidate on three
  anchored Likert scales — writing a short `reasoning` self-calibration
  *before* the score rows (schema field order = generation order) — and the
  keep/drop decision is made in code (`apply_filter_scores`): fewer than
  `TOP_K_FILTER` candidates → keep all; a full list → the top
  `FILTER_KEEP_TOP_N` ranked survive on relative merit (unless pure 1/1/1
  noise) plus anything with a scale at `FILTER_KEEP_THRESHOLD`. Then a
  **route** call produces the reply from the kept candidates. Memory commands
  short-circuit before any Q&A retrieval.

Each pipeline resolves its own scorer model. The assistant's `memory_query`
(and the `/memory/developer` panel that reproduces it) uses the
**`assistant.memory_filter`** slot on `/agentmodel`, else `assistant.default`;
the router uses its own binding. Different model groups calibrate Likert
scales very differently, so a keep/drop comparison across the two pipelines is
only meaningful when they are pointed at the same group deliberately.

All share the seed-memory matching functions; they differ in how much LLM
judgment sits between retrieval and reply.

## Sync (incremental reconcile)

`sync_kb()` reconciles the pgvector table with the merged JSONL instead of
wiping it. Each entry's raw line is hashed at load time (`_row_sha256` on the
entry — for an id in both base and overlay, the winning file's line is the one
hashed), and each stored node carries that hash plus the current `KB_EPOCH`.
One `SELECT DISTINCT` reads the table's stamps; `_diff_rows` classifies every
file row:

- **new** (id not in the table) → embed its questions, insert.
- **dirty** (stamp differs) → re-sync the row (fast paths below).
- **deleted** (id in the table, not in the file) → delete its nodes.
- **unchanged** → skip. The common case; costs nothing.

Because the vector derives from the question text alone, a dirty row whose
question set is unchanged — an answer, shield, path, or handler edit — gets a
metadata-only UPDATE in place: **zero embedding calls** (`_sync_row` /
`_update_node_metadata`, which rewrites both the top-level metadata the shield
SQL filter reads and the copy nested in `_node_content` that retrieval
deserializes). When questions did change, only the new/changed strings embed;
unchanged strings keep their stored vectors, and new nodes are inserted
*before* the old ones are deleted, so retrieval sees old-or-new per row, never
an absent row or an empty table.

`KB_EPOCH` folds the embedding model and `KB_SCHEMA_VERSION` into the stamp: a
model swap or a metadata-shape change dirties every row automatically (stored
vectors are then treated as unusable — no metadata-only shortcut, no vector
reuse). Tables written before stamps existed look all-dirty and re-embed once.

Failures are isolated per row: a row that fails to embed keeps its old nodes,
stays dirty, and retries on the next sync; the other rows land. Loader
validation errors (duplicate id/path, bad JSON, non-string shield) raise with
`file:line` *before* any write — the table is left untouched, not emptied.

`sync_kb` runs from two places:

- **Automatically** — `_ensure_populated` (called by the assistant's
  `memory_query` and the chat query agents) syncs on the first call of each
  process and re-checks an `(mtime_ns, size)` snapshot of the source files on
  later calls, so an unchanged corpus costs one `stat()` per file. Agents run
  in freshly spawned processes, so a JSONL edit is picked up on the next
  message with no button press. A sync failure here (e.g. Ollama down) is
  fatal only when the table is empty; with existing rows it is logged and
  retrieval keeps serving them.
- **On demand** — the /settings button (below).

## Operator operations

- **Add/edit facts** — edit the overlay `question_answer.jsonl` under
  `customize.dir` (or the base file). The edit is picked up on the next
  message (see [Sync](#sync-incremental-reconcile)); the /settings button
  forces it immediately.
- **Add/edit relations** — edit `relations.json` in the same directory. It
  joins the same mtime/size snapshot, so a declaration edit is picked up on the
  next message like a JSONL edit, and the roster's digest covers every field
  that changes what is stored or rendered. Adding a member to a declared prefix
  needs no edit here at all — membership is derived.
- **Repopulate** — the /settings "Repopulate Q&A memory" button
  (`POST /settings/api/repopulate_memory` → `sync_kb`) reconciles without a
  restart and reports `{unchanged, updated, embedded, deleted}` row counts. A
  failure (embedding backend down, a JSONL parse error carrying
  `file:line:column`, or a `relations.json` validation error naming the
  declaration) leaves synced rows intact; pressing again retries the stale
  ones.
- **Rebuild (full)** — the escape hatch next to it
  (`POST /settings/api/rebuild_memory` → `rebuild_kb`) keeps the TRUNCATE +
  re-embed-everything semantics for genuine table corruption. Equivalent to
  setting `QUERY_AGENT_REBUILD_KB=1` (`REBUILD_ENV`) and restarting. A failure
  here can leave the table empty/partial; the next successful run heals it.
  Sources are parsed **before** the TRUNCATE, so a malformed JSONL or
  `relations.json` is a no-op rather than an emptied table.
- **Unlock a shield** — check it on /settings and Save; this writes
  `qa.unlocked_shields`. Shielded entries become visible to the LLM immediately
  (the in-memory backstop applies on the next query; no repopulate needed).
- **Troubleshooting** retrieval failures: see `operator-guide.md`
  ("Seed-memory / QueryAgent retrieval fails").

### Facts-invalidation notice

Changing a shield or repopulating the Q&A can stale facts the assistant already
answered earlier in a conversation. `memory_query` filters correctly, but a prior
answer still sits in the chat transcript, so the model can reuse it. To counter
this:

- A shield change (`qa.unlocked_shields`, when the value actually changes)
  stamps `qa.facts_invalidated_at` (`db.mark_facts_invalidated`). A sync stamps
  it only when it actually changed rows (a clean reconcile stays silent, so a
  no-op button press posts no notice); a full rebuild always stamps.
- The next time the assistant runs in a room, it posts a one-time visible notice
  telling the model that earlier answers may be out of date and to re-check via
  `memory_query` (`_maybe_post_context_marker` — the generalized context
  marker, which also announces `profile.current` switches via the independent
  `profile.current_changed_at` stamp and posts one combined notice when both
  causes are pending; see `assistant-design.md`). Each cause is deduped per
  room via its exact stamp in the marker's `meta` (`facts_invalidation` /
  `profile_context_changed`), and no history is removed — the operator's
  message stays the current one.

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

## Proposed maintenance and navigation

The current Q&A system does not generate aliases, follow-up questions, or
knowledge-gap rows. The design proposal
[`proposals/2026-07-21-qa-followup-questions.md`](proposals/2026-07-21-qa-followup-questions.md)
keeps those artifacts outside the human-owned JSONL and treats them as
revocable derived projections.

Its recommended delivery order is observed unanswered-query reporting first,
derived alias enrichment second, synthetic gap discovery third, and
user/model-facing hints last. Generated navigation must independently pass
source-grounding and target-answerability checks; dynamic targets are
exact-alias-only in phase 1. Operator-overlay or shielded generation remains
blocked until an authenticated operator control plane exists.

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
| Constants | `TOP_K_NODES=50`, `TOP_K_VECTOR=5`, `TOP_K_FULLTEXT=5`, `MIN_SCORE=0.60`, `MIN_MARGIN=0.05`, `TOP_K_FILTER=5`, `FILTER_KEEP_THRESHOLD=4`, `FILTER_KEEP_TOP_N=2`, `FILTER_KEEP_TOP_FLOOR=2` |
| Inspection page | `/memory/developer` (`webapp/memory_developer_views.py`, `static/memory_developer.js`) |
| Tests | `memory/test_seed_memory_errors.py`, `memory/test_seed_shields.py`, `memory/test_seed_documents.py`, `memory/test_seed_sync.py` |
| Overlay schema proposals | `notes/proposals/2026-07-04-qa-overlay-person-schema.md`, `notes/proposals/2026-07-07-qa-overlay-first-person-voice.md` |
| Security review | `notes/proposals/2026-06-25-security-review-mitigations.md` (Finding 8a: shields) |
