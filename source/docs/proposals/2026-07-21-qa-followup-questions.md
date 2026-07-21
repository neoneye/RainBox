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

### One derived-content engine, many products

The generation machinery below (slots keyed by runtime content hash,
generate → retrieve → validate → classify, privacy scopes, policy
versioning) is deliberately consumer-agnostic: follow-up questions are its
first product, alias enrichment (variant B in the assessment) its second.
Nothing in the slot schema or the pipeline steps is follow-up-specific except
the classification vocabulary. Build it once; do not fork it per product.

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

Absolute thresholds inherit the scorer model's calibration, and scorers get
swapped (that is the point of the `memory_filter` binding). The defenses, in
order: the scale definitions carry anchored endpoint descriptions (already
the shared-schema convention); the phase-4 fixture set recalibrates
thresholds per `policy_version` bump when the scorer changes; and if
Likert-threshold validation proves unstable across scorer swaps, the fallback
is a binary authoritative-answer schema (`answers_the_question: yes|no`, same
`policy_version` mechanics). A local cross-encoder reranker would be the
numerically stablest validator; it stays a future swap because nothing in the
current stack serves one.

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
- Broader scopes are enabled by a **persistent generation policy**, configured
  once on `/settings` rather than re-approved ceremony-by-ceremony per run:
  the policy names the allowed provenance classes (and, separately, shield
  sets) plus the pinned generator and validator model groups for that scope.
  Runs display the active policy and reuse it; editing the policy changes the
  `scope_fingerprint`, which invalidates results produced under the old one.
  One-time setup, low-friction reruns — the friction lives where the decision
  is made, not where it is repeated. A trusted local model is the recommended
  default for both roles.
- Operator-overlay generation therefore requires that policy to exist — an
  explicit decision, made once.
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
version, and `scope_fingerprint` still matches the active approved scope.

Target freshness is deliberately looser than source freshness, because a
strict rule would make quality work self-defeating: fixing a typo in one
popular target entry would instantly orphan every inbound edge and trigger a
revalidation cascade. The stored `entry_sha` in `target_refs` is a
*confidence* marker, not a serving gate:

- A hint is servable while its target merely **exists and is visible** —
  correctness at follow time is enforced live anyway, because following a
  hint runs a fresh `memory_query` through retrieval and the recall filter.
  The stored target ref never short-circuits that.
- A target-hash mismatch downgrades the edge to `stale` for graph analysis
  and gap classification, and queues **item-level revalidation** at the next
  batch run (one retrieval + one validator call for that question — never a
  regeneration of the source's candidate list).
- Only target **deletion or shield-locking** hides the hint immediately.

Source-side freshness stays strict (a source edit invalidates its own
generation): the questions were generated *from* that text. This division
handles source edits, target edits, target deletion, overlay replacement,
scope revocation, and policy upgrades without a foreign key to a Q&A table
that does not exist.

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

### Hint adoption telemetry

Whether hints are worth their prompt budget is measurable, not arguable: when
a live `memory_query` arrives whose normalized query equals a hint shown in
the same run's previous step, record an adoption event
(`target_type="seed_followup"`, `source="memory_query.hints"`, target = the
hint's source `qa_id` + ordinal). Per-hint adoption over time is the pruning
signal (hints nobody follows stop earning their slot) and the phase-4
go/no-go metric for keeping the hint block at all. Same FIFO-bounded
retention as the recall-KPI streams.

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

Each gap and unanswered-query row also offers an **add-entry affordance**:
export a prefilled overlay-entry skeleton (the question filled in, the answer
left to write) as a JSON line the operator pastes into their overlay file.
Authoring happens at the moment of observed failure, and the overlay file
stays operator-owned — the app drafts, never writes it.

The answerable edges also form a directed graph over entries, which yields a
second authoring signal for free: entries with **zero inbound edges** are
unreachable by navigation (islands the assistant can only find by direct
query), and entries with zero outbound follow-ups mark confirmed dead ends.
The corpus is a few hundred nodes, so graph analyses (connectivity, and
inbound-edge centrality to rank "core concept" entries) run in memory at
report time over `target_refs` — plain Python; no graph database, no graph
extension.

### Chat suggested-question chips (payload-driven, no LLM)

For the chat surface, hints should bypass the model entirely: when a reply
used a seed entry, attach that entry's current answerable follow-ups to the
chat API response metadata and let the frontend render them as clickable
suggestion chips under the reply. Clicking a chip posts the question as the
operator's next message and records an adoption event.

This is strictly better than prompting the model to offer follow-ups in the
chat path: zero token cost, zero added latency, zero prompt-injection surface
(the generated text never enters a model context), deterministic rendering,
and chip clicks are a far cleaner adoption signal than inferring whether the
assistant "followed" a hint. The earlier phase-2 idea — letting the route LLM
phrase "want to know about ...?" — is superseded by chips.

The assistant `memory_query` hint block above is the one place a prompt-side
channel is justified at all: mid-ReAct-loop there is no UI to render chips
into — the model *is* the consumer deciding the next action. That is also why
it stays the gated experiment while chips ship on plain evidence of clicks.

## Failure and lifecycle rules

- **Generator returns zero:** store a complete empty generation.
- **Generator or validator fails:** keep the prior complete generation; record a
  sanitized failure and retry later.
- **Source hash changes:** old generation is invisible immediately.
- **Source disappears:** hide its generation immediately and prune the orphaned
  slot during the next successful batch run.
- **Target hash changes (target still visible):** hint stays servable (live
  retrieval re-validates at follow time); edge marked `stale` for
  graph/classification; item-level revalidation at the next batch run.
- **Target disappears or its shield locks:** hide that answerable item
  immediately.
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
- Source edits and overlay overrides suppress hints before regeneration;
  target *edits* downgrade edges to stale without suppressing (a followed
  hint still passes through live retrieval + filter); target deletion or
  shield-locking suppresses immediately.
- A typo-level edit to a heavily-referenced target entry does not hide any
  inbound hint and triggers only item-level revalidation, never source
  regeneration.
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
- A live recall keeping zero items records one `unanswered` event
  (distinguishing zero-candidates from all-rejected); dev-page probes record
  none; the per-query stream is FIFO-bounded.
- The CHECK constraints accept the new stage/target values and still reject
  unknown ones.

## Implementation notes

Decisions an implementer needs that the sections above leave open, settled
here so the build does not stall on them:

- **Unanswered-query events (prerequisite for the mined-gap report).** Today a
  recall that keeps nothing writes no verdict rows — the "asked and got
  nothing" case, the mined report's most important input, leaves no trace.
  Add `target_type="recall_query"` and `stage="unanswered"` to the
  RetrievalEvent vocabulary; on every live `memory_query` whose recall keeps
  zero items, record one event with `target_id` = a short hash of the
  normalized query, the raw query in `query`, and
  `{"candidates_total": N, "rejected_total": M}` in metadata (distinguishing
  "nothing retrieved" from "everything rejected"). FIFO-bounded per query
  hash by the same capacity setting as the verdict streams.
- **Schema changes are CHECK-constraint changes.** `stage` and `target_type`
  are enforced by database CHECK constraints (unknown values raise
  IntegrityError), so the new `recall_query`, `unanswered`, and
  `seed_followup` values require a constraint migration, not just new Python
  strings. The new tables follow the existing pattern: declared in
  `db/models.py`, created by `init_db`.
- **Validator identity.** Validation resolves its scorer exactly like live
  recall (`resolve_filter_model_group` — the `memory_filter` binding first).
  Overlay/shielded runs snapshot the resolved generator and validator groups
  at approval time and abort if resolution changes mid-run; that is what
  "pinned" means operationally.
- **Run execution model.** The generation job runs as a single-flight
  background run (the benchmark-runner pattern): `/settings` starts it,
  polls a progress endpoint, and shows the last run's summary counts. One
  run at a time; starting while one is active is refused.
- **Per-entry locking.** The concurrent-run rule reuses the existing
  `pg_advisory_xact_lock` pattern (`db/memory.py`) keyed on `qa_id`.
- **Adoption matching.** Hint-adoption comparison uses the alias normalizer
  (`_normalize_query`) on both sides — the same normalization the exact-alias
  table already relies on.
- **Alias enrichment is sketched, not specified.** Before delivery step 3,
  variant B needs its own short spec: how derived question nodes are stored
  in the pgvector table (marking, shields, entry-hash freshness) and how they
  interact with `sync_kb`'s epoch/repopulate logic. Do not build it from
  this document alone.

## Assessment: is this the right direction?

Three candidate investments compete for the same goal — making the Q&A file
answer more of what actually gets asked. This proposal is one of them; the
honest ranking puts it last in delivery order, even though its machinery is
worth building.

### A. Telemetry-mined gaps (observed demand) — cheapest, highest signal

The recall-verdict and router-filter telemetry already stores the *real*
queries that flowed through retrieval, per candidate, with the filter's
relevance verdicts. Queries where every candidate was rejected — or the
observation came back "no relevant remembered facts" — are demand the KB
demonstrably failed to meet. Mining them is a report over existing rows: no
LLM calls, no new privacy flow (the queries were already processed), and the
gaps are demand-*observed* rather than demand-*predicted*. A synthetic gap
from self-play says "someone might ask this"; a mined gap says "the operator
asked this and got nothing." When both exist, mined gaps outrank synthetic
ones in the report.

Limitation: it only sees questions someone already asked, and the verdict
FIFOs retain a bounded recent window — it finds potholes on roads already
driven. Synthetic follow-ups explore roads not yet taken. The two are
complements, not substitutes.

### B. Alias enrichment (generated question phrasings) — fixes observed misses

The retrieval failures that motivated this whole line of work were phrasing
mismatches: a natural question ("how is X related to hobby Y") embedding far
from a terse registered phrasing ("Hobby Y / events"). Generating additional
question *phrasings* per entry — validated by self-play (the new phrasing
must retrieve its own entry decisively, and must not collide with a different
entry's territory) and stored as derived pgvector nodes, never in the JSONL —
raises recall of content the KB *already has*. It reuses this proposal's
machinery nearly one-for-one: the same generation slots, runtime-hash
freshness, privacy scopes, policy versioning, and self-play validation, with
the classification inverted (a phrasing that retrieves a *different* entry is
the failure case). Its consumer is the retrieval stack itself: no prompt
budget, no new assistant behavior to validate, value delivered on the next
query.

### C. Follow-up edges and hints (this proposal) — unique but unproven consumer

Navigation edges and synthetic gap discovery are things neither A nor B
provides. But the hint block's marginal value over the assistant simply
re-querying on its own is unproven — which is why hints sit behind developer
inspection and adoption telemetry, with a go/no-go gate. The gap report half
of C is safer value; the navigation half is the experiment.

### Recommendation

Build the shared generation machinery once (it serves B and C); ship value in
the order the evidence supports: **A first** (days of work, zero model calls),
**B second** (same machinery, fixes the observed failure class), **C's gap
report third, C's hints last** behind their adoption metric.

One structural tension to keep in view: the privacy-safe default scope
(upstream static entries) covers the least valuable content — generic
entries anyone could regenerate. The operator value of every variant above
lives in the overlay, which is opt-in by design. The opt-in flow must
therefore be genuinely easy with a pinned local model, or all three variants
quietly under-deliver by only ever running on the content that matters least.

## Considered and declined

- **Bandit-ranked hint selection** (contextual bandits / Thompson sampling
  over hint click-through): needs traffic volume a single-operator system
  does not have — adoption counts here are single digits, where a bandit is
  noise-chasing. Plain adoption counts with FIFO retention are proportionate.
  Revisit only if the system ever serves many users.
- **Semantic-similarity freshness** (embedding distance between old and new
  target text deciding edge survival): replaced by the simpler rule above —
  serve while the target is visible, re-validate live at follow time,
  revalidate item-level lazily. Adds an embedding call and a second magic
  threshold for a problem the live re-query already solves.
- **Negative generation** (out-of-scope questions embedded as negative
  examples to sharpen hybrid search): research-grade change to the retrieval
  stack; precision is currently owned by the recall filter, which sees every
  candidate anyway. Could become interesting as filter-scorer *fixture*
  material (hard negatives for the phase-4 eval set), not as retrieval
  machinery.
- **Graph database / Postgres graph extension**: the corpus fits in memory;
  see the gap-report section.

## Delivery sequence

1. Telemetry-mined gap report (variant A): the unanswered-query event
   producer (small CHECK-constraint migration) plus a report over verdict and
   unanswered rows on `/memory/developer`; no model calls.
2. Tables, current/stale lookup helpers, `followup_generator` binding,
   generation job, the persistent generation policy on `/settings`, and
   tests — the shared machinery, built once for B and C.
3. Alias enrichment (variant B) on that machinery; measure recall improvement
   on the mined-gap queries from step 1.
4. Follow-up generation (variant C): `/memory/developer` inspection and the
   combined gap report (mined + synthetic, mined ranked first) with the
   add-entry affordance.
5. Chat suggested-question chips (payload-driven; adoption = clicks).
6. `memory_query` answerable-hint block with caps, sanitation, and adoption
   telemetry; evaluate on an anonymized fixture set; tune thresholds through
   `policy_version`; keep or drop the block on the adoption evidence.
