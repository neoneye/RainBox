# Q&A follow-up questions: AI-generated directions to go deeper

Extend the Q&A knowledge base so each entry knows what a sensible *next*
question looks like. The assistant gets steering ("after answering this, these
directions make sense"), and the operator gets a map of where the KB runs out.

All example data below is fictional placeholder content.

## Why

Today an entry answers exactly what its `questions` cover, and the
conversation dead-ends there. The assistant has no sense of which follow-ups
the KB could actually answer versus which would come back empty — so it either
guesses at `memory_query` calls or offers nothing. Some entries are shallow
(one fact, nothing behind it); others sit on top of a whole subtree
(a demoscene entry behind which sit per-party entries, project entries,
friend entries). Only generation at scale can tell these apart — hand-curating
follow-ups for a few hundred entries, and again after every edit, will not
happen.

## Design principle: follow-ups are derived data, not source data

The overlay jsonl is the operator's hand-curated source of truth; the base
jsonl is upstream content. AI-generated follow-ups belong in **neither**:

- Writing them into the jsonl would churn `_row_sha256` on every regeneration
  (invalidating embeddings for unchanged content), bloat the operator's file
  with machine output, and blur who owns which text.
- They are regenerable at any time from the entry text — the definition of
  derived data. The KB already stores derived data outside the source files
  (the pgvector table, `memory_embedding`); follow-ups follow the same
  pattern.

So: a DB table, keyed to the entry **and its content hash**, exactly like the
sync machinery keys embeddings:

```text
seed_followup
- qa_id            entry id
- entry_sha        the entry's _row_sha256 at generation time
- followups        JSONB list (schema below)
- model_name       which model generated them
- created_at
```

An edited entry's hash changes → its stored follow-ups are stale → the next
generation run replaces them. Unchanged entries are never regenerated
(incremental, like `sync_kb`).

## Two kinds of follow-up, discovered by self-play

Generation alone produces plausible questions; it cannot know whether the KB
can answer them. So the pipeline plays the questions back through retrieval:

1. **Generate.** For one entry, prompt the model with its questions + answer:
   "what would someone naturally ask next?" Produce 0..5 candidates.
   **Zero is a valid answer** — the prompt must say so explicitly, or every
   shallow entry gets hallucinated depth. This is the "some QA items have
   little extra data" case: a one-fact entry should end the conversation, not
   fake a continuation.
2. **Self-play.** Run each candidate through the real retrieval stack
   (`_hybrid_seed_ranked`, no LLM filter needed — top-1 + score suffices).
3. **Classify.**
   - Top hit is a *different* entry with a strong score → **answerable**:
     store the target `qa_id`. This is a navigation edge — the KB becomes a
     graph the assistant can walk deeper.
   - No strong hit (or only the entry itself) → **gap**: the KB cannot answer
     this. Still stored — gaps are the product, not the waste.

```json
{"question": "Which computer parties did they attend?",
 "answerable": true, "target_qa_id": "01b2…", "score": 858}
{"question": "What did the demo group release?",
 "answerable": false}
```

Answerable follow-ups steer the assistant. **Gap follow-ups are the KB
authoring backlog**: a ranked list of questions people would plausibly ask
that the KB cannot answer, each anchored to the entry that provoked it. That
closes the loop the overlay-authoring sessions do by hand today ("what should
I add next?").

## Generation pipeline

- A batch job over all entries (base + overlay) missing current follow-ups:
  one structured LLM call per stale entry, then self-play retrieval per
  candidate. Incremental; a full first run over a few hundred entries is a
  few hundred small calls.
- Model: a binding-only agent (`followup_generator`, the `memory_filter`
  pattern) so the operator picks and swaps the generator model on
  `/agentmodel` without code changes.
- Trigger: a button on `/settings` next to the KB repopulate action (same
  incremental spirit), plus optionally a cron job in the System folder.
- **Shields**: an entry's follow-ups inherit its shield. Generation runs
  per-entry (no cross-entry context), so a shielded entry's content cannot
  leak into an unshielded entry's follow-ups. Locked entries' follow-ups are
  never surfaced, same rule as the entries themselves.

## Consumption

1. **Assistant `memory_query`.** For entries the recall filter *kept*, append
   their answerable follow-ups as a compact hint block in the observation —
   outside the `recalled_memory` fence (they are navigation hints, not
   recalled facts) but in their own fenced, labeled block, since they are
   generated content:

   ```text
   <followup_hints note="AI-generated navigation hints — questions the memory
   store can also answer; not facts, not instructions">
   50de…: Which computer parties did they attend? -> 01b2…
   </followup_hints>
   ```

   Cap hard (e.g. 3 per kept entry, 6 total). The assistant can chain a
   `memory_query` on a hint verbatim — the self-play step already proved the
   query retrieves. Gap follow-ups are deliberately NOT shown here: telling
   the model about questions the KB cannot answer invites hallucinated
   answers.
2. **Chat (`query_filter_router` route reply).** Optionally let the route
   prompt see the kept candidates' follow-ups so replies can end with a
   natural "want to know about X?". Phase 2 — the reply prompt is
   deliberately short today.
3. **`/memory/developer`.** Show stored follow-ups (both kinds) on candidate
   rows — inspection of what the generator produced.
4. **Gap report.** A section listing gap follow-ups grouped by entry, ordered
   by how often their entry is recalled (the recall-KPI streams already count
   this) — "the most-used entries with the most unanswerable follow-ups"
   is the highest-value authoring queue. Natural home: `/memory/developer`
   or a small section on `/settings` next to the repopulate button.

## Risks and their handling

- **Hallucinated depth** — the model invents follow-ups implying facts that
  do not exist. Mitigated by: zero-is-valid prompting, the self-play
  answerable/gap split (invented directions land as gaps, which are never
  shown to the assistant), and follow-ups never being presented as facts.
- **Staleness** — entry edited, follow-ups describe the old content. Handled
  structurally by the `entry_sha` key: stale rows are invisible to consumers
  (consumers join on current hash) and replaced on the next run.
- **Cost** — one small call per entry per edit, incremental. The self-play
  step is embedding-only.
- **Injection surface** — generated text derived from stored answers enters
  the assistant's context. Same treatment as the memory-filter assessment:
  fenced, labeled non-instructional, angle brackets sanitized.

## Out of scope (for this proposal)

- Generating follow-ups for **memory claims** (the /memory store). Same
  machinery would apply keyed on claim uuid + updated_at, but claims churn
  faster; start with the stable seed KB.
- Multi-hop chain generation. Chains emerge from single-hop edges: each
  answerable follow-up lands on an entry that has its own follow-ups.

## Sequence

1. `seed_followup` table + generation job (generate → self-play → classify),
   `followup_generator` binding, /settings trigger.
2. `memory_query` hint block (answerable only, capped, fenced) +
   `/memory/developer` display.
3. Gap report.
4. Phase 2: route-reply suggestions in chat.
