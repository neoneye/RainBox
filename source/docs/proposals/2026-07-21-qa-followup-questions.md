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
existing `memory_filter` scorer rates them. Follow-up validation must **not**
reuse `apply_filter_scores`: the live recall policy intentionally keeps every
candidate when the list is short, which is appropriate for conversational
recall but would manufacture false “answerable” edges here. Instead, a
follow-up-specific, absolute code policy accepts only candidates with
`direct >= 4` and `relevancy >= 4`. The initial thresholds are named constants
covered by `policy_version`; `indirect` alone never proves that the candidate
can answer the question.

An exact alias match may short-circuit the scoring call — but only when the
alias belongs to a *different* visible entry (→ `answerable`); an alias
registered on the source entry itself is a self-hit (→ `redundant`). Validation
can accept multiple targets.

### Visibility is part of correctness

A follow-up is usable only when its source and all advertised targets are
currently visible. Generation and consumption both fail closed on missing,
stale, or locked entries.

## Privacy and shield boundary

Generation sends an entry's questions and answer to a completion model, which
is a materially different data flow from local embedding-only retrieval.
Validation can also send candidate paths, questions, and static answers to a
second completion model. Both calls are therefore inside the same privacy and
provider-consent boundary. Consequently:

- The default generation scope is publishable upstream static entries only.
- Operator-overlay generation is a separate, explicit opt-in. Before the run,
  the UI shows the resolved generator and validator model groups, including
  every fallback member and provider. A trusted local model is the recommended
  default for both roles.
- Shielded entries require a separate explicit selection; a currently unlocked
  shield is not by itself consent to send its content to a generator.
- Private generation never inherits the validator's normal agent-binding
  fallback chain. Both generator and validator bindings must be explicitly
  configured and pinned for the duration of the run.
- Retrieval candidates are restricted by the approved data scope as well as by
  shield visibility. The default upstream-only run must not retrieve, send, or
  create edges to operator-overlay entries. An approved overlay run may target
  upstream entries and only the overlay scope approved for that run.
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
- qa_id                source entry id (primary key/current slot)
- source_entry_sha     runtime _row_sha256 used by the current success; nullable
- generator_model      resolved generator model used by the current success
- validator_model      resolved validator model used by the current success
- scope_fingerprint    approved provenance/shield scope used by the run
- policy_version       prompt, input-limit, and validation-policy version
- generated_at
- last_attempt_sha     source hash used by the latest attempt
- last_attempt_at
- last_error_code      sanitized; null after success

seed_followup
- source_qa_id         parent source entry id
- ordinal              stable order within the generated list
- question             generated question
- classification       answerable | gap | redundant
- target_refs          JSONB list of {qa_id, entry_sha}; empty unless answerable
- validation           numeric ratings and retrieval signals only
```

Keying: `seed_followup_generation` is one slot per source entry, keyed by
`qa_id`. `seed_followup` rows are keyed `(source_qa_id, ordinal)` and reference
that slot with `ON DELETE CASCADE`. No separate generation UUID is needed
because only the current successful result is retained. Upsert the slot and
replace its items in one transaction only after generation and validation
succeed. A failed attempt updates only `last_attempt_*` and `last_error_code`;
it must not replace the last successful fields or items. Before the first
success, the slot may contain attempt metadata while `source_entry_sha` and
the successful-model fields remain null.

Consumers accept a generation only when `source_entry_sha` equals the source
entry's current runtime hash, `policy_version` equals the current policy
version, and `scope_fingerprint` still matches the active approved scope. An
answerable item is accepted only when every stored target hash also matches.
This handles source edits, target edits, target deletion, overlay replacement,
scope revocation, and policy upgrades without a foreign key to a Q&A table that
does not exist.

`scope_fingerprint` is a digest of a canonical scope description (allowed
provenance classes and shield set), not a copy of potentially sensitive scope
labels.

The scorer schema requires a free-form calibration note, but the batch job
discards it immediately. It stores only numeric ratings, retrieval signals, and
model identifiers; validation diagnostics must not retain generated reasoning
that could paraphrase source content.

`target_refs` is a list because a question may legitimately require more than
one kept entry. The assistant does not need to see target IDs; they are for
validation, inspection, and graph tooling.

## Generation and self-play pipeline

For each in-scope static entry missing a current, complete generation:

1. **Check prompt size.** Build the entry-scoped prompt and enforce a fixed,
   versioned application limit. If it is too large, record
   `last_error_code=input_too_large` and skip the entry. Do not silently
   truncate the answer: missing middle context can change which follow-ups are
   sensible.
2. **Generate candidates.** Send the registered questions and answer to a
   structured-output generator. Request 0..5 natural next questions. State
   explicitly that zero is valid. Reject empty, duplicate, and normalized
   aliases already registered on the source entry.
3. **Retrieve candidates.** Run each remaining question through
   `_hybrid_seed_ranked` with both the approved provenance scope and the shield
   visibility set derived from the source entry. This requires an explicit
   provenance filter in the batch path; shield filtering alone is insufficient.
   Do not resolve dynamic handlers during validation.
4. **Score relevance.** Reuse the `memory_filter` scoring schema and explicitly
   bound validator model, but apply the follow-up-specific absolute acceptance
   policy described above. Batch the questions from one source entry into one
   structured call where practical. Dynamic candidates expose registered
   question metadata only; handlers are never invoked.
5. **Classify.** For each question:
   - only the source entry is kept -> `redundant`;
   - one or more different visible entries are kept -> `answerable`, storing all
     target IDs and current hashes;
   - no entry is kept -> `gap`;
   - retrieval or validation fails -> unresolved failure, not a gap.
6. **Commit atomically.** Store the completed generation, including a completed
   row with zero items. Never publish partial results.

Redundant items are useful on `/memory/developer` for prompt tuning but are not
shown to the assistant or placed in the gap queue. A later retention policy may
discard them.

The generator is a binding-only `followup_generator` agent, parallel to
`memory_filter`, so the operator can select its structured-output model on
`/agentmodel`. The job resolves the binding directly; it does not enqueue normal
agent journal work.

The `/settings` action should:

1. show scope plus the complete resolved generator and validator provider chains
   before confirmation;
2. load and incrementally sync the merged Q&A registry;
3. generate only missing/stale entries;
4. report counts for complete, empty, answerable, gap, redundant,
   skipped-private, skipped-oversized, and failed entries without logging their
   content.

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
- **Source disappears:** hide its generation immediately and prune the orphaned
  slot during the next successful batch run.
- **Target hash changes or target disappears:** hide that answerable item until
  the source is revalidated.
- **Shield changes:** visibility checks take effect immediately; regeneration is
  required because the raw-line hash also changes.
- **Generation policy changes:** increment `policy_version`; older generations are
  stale even when entry text is unchanged.
- **Approved scope changes:** update its fingerprint; results from a broader or
  otherwise different scope become invisible immediately.
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
- A short candidate list with weak absolute ratings produces a gap; the live
  filter's keep-all behavior is never applied.
- Multiple validated targets are retained and all must be current and visible.
- An upstream-only run cannot retrieve or expose an overlay entry as a target.
- Unshielded sources cannot create edges to shielded targets.
- Same-shield edges work only while that shield is unlocked; cross-shield edges
  are rejected.
- Operator-overlay and shielded generation are excluded by default and require
  explicit approved scope plus pinned generator and validator groups.
- Oversized inputs are skipped without partial generation or raw-text logging.
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
   thresholds through `policy_version`.
5. Phase 2 experiment: route-reply suggestions in chat.
