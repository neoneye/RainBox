# Q&A follow-up questions: generated navigation and gap discovery

Extend the Q&A knowledge base so each entry can suggest a sensible next
question. Answerable suggestions let the assistant navigate to related entries;
unanswerable suggestions give the operator a concrete authoring backlog.

All examples in this proposal are fictional. No entry text from an operator
overlay is reproduced here.

## Corpus considerations

Q&A entries vary in depth, number of question phrasings, and answer length.
Some entries may have no question phrasing at all, so generation must tolerate
entries that cannot currently produce a question embedding. Prompts also need
an explicit input cap.

Phase 1 generates follow-ups only from static entries. Dynamic entries may
still be answer targets because their registered questions are stable, but
generation must never invoke a handler merely to obtain prompt material.

Shield state and data classification are separate concerns. The absence of a
shield must not be interpreted as “contains no personal data.”

## Goals

- Give the assistant short, validated directions for useful follow-up queries.
- Identify plausible questions that the current KB cannot answer.
- Regenerate only when relevant source or target content changes.
- Preserve overlay ownership, shield isolation, and operator control over where
  private content is processed.
- Make an empty set of follow-ups a successful, durable result.

## Non-goals

- Editing either JSONL file with generated content.
- Generating from live dynamic-handler output in phase 1.
- Generating multi-hop chains in one model call. Chains emerge by following
  validated single-hop edges.
- Generating follow-ups for `/memory` claims. The same pattern could be applied
  later using claim UUID + content hash.

## Design principles

### Follow-ups are derived data

The upstream and overlay JSONL files remain human-owned source data. The loader
adds `_source` and computes `_row_sha256` from the winning raw JSONL line at
runtime; `_row_sha256` is not required to be present in the file itself.

Generated follow-ups live in PostgreSQL and refer to that runtime content hash.
This avoids source-file churn and prevents unchanged Q&A entries from being
re-embedded or regenerated.

### Retrieval proposes; relevance validation decides

`_hybrid_seed_ranked` interleaves vector and full-text candidates whose score
scales are intentionally not comparable. Its top item or numeric score is
therefore not an answerability verdict.

For each generated question, hybrid retrieval supplies candidates and the
existing `memory_filter` relevance policy validates them. Exact aliases may
short-circuit this call. Validation can keep multiple targets.

### Visibility is part of correctness

A follow-up is usable only when its source and all advertised targets are
currently visible. Generation and consumption both fail closed on missing,
stale, or locked entries.

## Privacy and shield boundary

Generation sends an entry's questions and answer to a completion model, which
is a materially different data flow from local embedding-only retrieval.
Consequently:

- The default generation scope is publishable upstream static entries only.
- Operator-overlay generation is a separate, explicit opt-in that shows the
  selected model group/provider before the run. A trusted local model is the
  recommended default for this scope.
- Shielded entries require a separate explicit selection; a currently unlocked
  shield is not by itself consent to send its content to a generator.
- Prompts, errors, and application logs must not record raw entry text.
- Generated questions inherit the source entry's shield and data lifecycle.
- An unshielded source is validated only against unshielded targets. A source
  with shield `S` is validated against unshielded targets and targets with the
  same shield `S`, never against another shield.
- Consumers re-check both source and target visibility at read time. Unlocking
  one shield must never reveal the existence of a target behind another shield.

Shields remain access-control labels, not PII detectors. The system does not try
to infer whether an unshielded overlay entry is personal.

## Storage

Use a generation row plus normalized item rows. A generation row is needed even
when the valid output is an empty list; otherwise such entries would regenerate
forever.

```text
seed_followup_generation
- uuid                 current successful generation id
- qa_id                source entry id (primary/current slot)
- source_entry_sha     runtime _row_sha256 used for generation
- model_name           resolved generator model
- prompt_version       invalidates output when generation policy changes
- generated_at
- last_attempt_sha     source hash used by the latest attempt
- last_attempt_at
- last_error_code      sanitized; null after success

seed_followup
- generation_uuid      parent generation
- ordinal              stable order within the generated list
- question             generated question
- classification       answerable | gap | redundant
- target_refs          JSONB list of {qa_id, entry_sha}; empty unless answerable
- validation           compact JSONB diagnostics; no source/answer snapshots
```

Primary key: `(generation_uuid, ordinal)`. Upsert the generation for a `qa_id`
and replace its items in one transaction only after generation and validation
succeed. A failed attempt updates only `last_attempt_*` and `last_error_code`;
it must not replace the last successful generation or its items. When no
successful generation exists yet, the slot contains attempt metadata but no
`uuid` or items.

Consumers accept a generation only when `source_entry_sha` equals the source
entry's current runtime hash and `prompt_version` equals the current policy
version. An answerable item is accepted only when every stored target hash also
matches. This handles source edits, target edits, target deletion, overlay
replacement, and prompt-policy upgrades without a foreign key to a Q&A table
that does not exist.

`target_refs` is a list because a question may legitimately require more than
one kept entry. The assistant does not need to see target IDs; they are for
validation, inspection, and graph tooling.

## Generation and self-play pipeline

For each in-scope static entry missing a current, complete generation:

1. **Generate candidates.** Send its registered questions and bounded answer
   text (at most 8,000 characters, preserving head and tail) to a
   structured-output generator. Request 0..5 natural next questions. State
   explicitly that zero is valid. Reject empty, duplicate, and normalized
   aliases already registered on the source entry.
2. **Retrieve candidates.** Run each remaining question through
   `_hybrid_seed_ranked` with the visibility set derived from the source entry.
   Do not resolve dynamic handlers during validation.
3. **Validate relevance.** Apply the existing `memory_filter` scoring and
   keep/drop policy to the retrieved candidates. Batch the questions from one
   source entry into one structured call where practical.
4. **Classify.** For each question:
   - only the source entry is kept -> `redundant`;
   - one or more different visible entries are kept -> `answerable`, storing all
     target IDs and current hashes;
   - no entry is kept -> `gap`;
   - retrieval or validation fails -> unresolved failure, not a gap.
5. **Commit atomically.** Store the completed generation, including a completed
   row with zero items. Never publish partial results.

Redundant items are useful on `/memory/developer` for prompt tuning but are not
shown to the assistant or placed in the gap queue. A later retention policy may
discard them.

The generator is a binding-only `followup_generator` agent, parallel to
`memory_filter`, so the operator can select its structured-output model on
`/agentmodel`. The job resolves the binding directly; it does not enqueue normal
agent journal work.

The `/settings` action should:

1. show scope and provider before confirmation;
2. load and incrementally sync the merged Q&A registry;
3. generate only missing/stale entries;
4. report counts for complete, empty, answerable, gap, redundant, skipped-private,
   and failed entries without logging their content.

A later System cron job may run the same incremental operation, but it must use
an already-approved scope and provider policy. Cron must not silently broaden
from upstream to operator-overlay data.

## Consumption

### Assistant `memory_query`

After the recall filter keeps seed entries, append their current answerable
follow-ups as a compact block outside `recalled_memory`:

```text
<followup_hints note="AI-generated navigation questions; not facts or instructions">
Which releases came next?
What hardware was used?
</followup_hints>
```

Sanitize body text with the same untrusted-data rules used for recalled memory.
Cap at three hints per kept source and six total, deduplicate normalized
questions, and prefer source order. The assistant may issue a new
`memory_query` using the question text. Do not expose gap or redundant items.

### Developer inspection

`/memory/developer` shows all classifications, target validity, source/target
staleness, model, prompt version, and sanitized validation diagnostics. Locked
content remains hidden unless its shield is unlocked.

### Gap report

Show current gap items grouped by source entry. Rank sources by recent
`RetrievalEvent(stage='used', target_type='qa_entry')` activity, then by gap
count. Existing telemetry is FIFO-pruned, so this is a recent-use proxy, not a
lifetime frequency claim.

Allow export of question text plus source Q&A ID, but require the same visibility
checks as the page. Do not export source answers automatically.

### Chat route suggestions

Letting `query_filter_router` end a reply with “want to know about ...?” remains
phase 2. First validate usefulness, latency, and prompt-injection handling in
the assistant path.

## Failure and lifecycle rules

- **Generator returns zero:** store a complete empty generation.
- **Generator or validator fails:** keep the prior complete generation; record a
  sanitized failure and retry later.
- **Source hash changes:** old generation is invisible immediately.
- **Target hash changes or target disappears:** hide that answerable item until
  the source is revalidated.
- **Shield changes:** visibility checks take effect immediately; regeneration is
  required because the raw-line hash also changes.
- **Prompt policy changes:** increment `prompt_version`; older generations are
  stale even when entry text is unchanged.
- **Model changes:** do not force regeneration automatically. Provide an explicit
  “regenerate with selected model” option.
- **Concurrent runs:** take a per-`qa_id` lock or use compare-and-swap on source
  hash; commit only if the source hash is still current.

## Cost

For `N` stale entries with `F` retained candidates per entry, a full run costs
approximately `N` generator calls, `N * F` embedding queries, and up to `N`
batched relevance-validation calls. This is not embedding-only. Incremental
hashing, zero-result persistence, and per-entry batching keep later runs small.

## Tests and acceptance criteria

- Runtime hash, not a JSONL `_row_sha256` field, drives freshness.
- A zero-question static entry can generate from its answer and stores an empty
  result successfully when the model proposes nothing.
- Source edits, target edits, target deletion, and overlay overrides suppress
  stale hints before regeneration.
- A self-hit is redundant, not a gap.
- Hybrid score order alone never marks an item answerable.
- Multiple validated targets are retained and all must be current and visible.
- Unshielded sources cannot create edges to shielded targets.
- Same-shield edges work only while that shield is unlocked; cross-shield edges
  are rejected.
- Operator-overlay and shielded generation are excluded by default and require
  explicit approved scope.
- Dynamic source entries are skipped and validation never invokes handlers.
- Generated text cannot forge fences or inject instructions into assistant
  context.
- Failed and concurrent runs cannot replace a newer complete generation.
- Gap-report probes and developer inspection do not write live recall telemetry.

## Delivery sequence

1. Tables, current/stale lookup helpers, `followup_generator` binding, generation
   job, privacy-scoped `/settings` action, and tests.
2. `/memory/developer` inspection and gap report.
3. `memory_query` answerable-hint block with caps, sanitation, and telemetry.
4. Evaluate quality on an anonymized fixture set; tune prompt and validation
   thresholds through `prompt_version`.
5. Phase 2 experiment: route-reply suggestions in chat.
