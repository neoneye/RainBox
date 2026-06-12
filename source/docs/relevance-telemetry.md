# Relevance Telemetry

## Purpose

Relevance telemetry records what retrieval systems showed to agents and how
those candidates performed downstream.

It is not memory truth. It is not a replacement for evals. It is an event log
for questions like:

- Which memories are retrieved often?
- Which memories are injected into chat prompts?
- Which Q&A entries are rejected by the relevance filter?
- Which retrieved targets appear in downvoted answers?
- Where should we create eval cases before tuning retrieval?

The source of truth is `retrieval_event`. Counters and reports should be derived
from events.

## Current Producers

### Chat Memory Retrieval

`ChatAgent` retrieves first-class memory claims through
`memory.retrieval.retrieve_memories`.

For each memory returned, it writes:

- `target_type="memory_claim"`
- `target_id=<memory uuid>`
- `stage="retrieved"`
- `source="chat_memory_retrieval"`

For each memory injected into the prompt, it writes:

- `stage="used"`
- `filter_label="relevant"`

Today every retrieved memory is injected, so `retrieved` and `used` have the
same target set. Interpret `used` as "entered the answer context", not "proved
to have influenced the final text".

### Query Filter Router

`QueryFilterRouterAgent` records Q&A retrieval decisions for
`target_type="qa_entry"`.

It writes:

- `retrieved` for top-K semantic or exact-alias candidates.
- `accepted` for candidates kept by the LLM relevance filter or exact path.
- `rejected` for candidates shown to the filter but not kept.
- `used` for accepted candidates passed into the final route/reply path.

Its `used` rows include:

```json
{"used_signal": "accepted_candidate_approximation"}
```

That metadata is important. It says the candidate was accepted into the final
answer context, not that final wording causally relied on it.

### Feedback Downvotes

Downvotes are captured as `FeedbackEvent` rows. When same-turn diagnostic
context is available, the feedback path writes `RetrievalEvent` rows with:

- `stage="downvoted"`
- `source="chat_feedback"`
- `target_type="memory_claim"` for memory UUIDs in `debug-memory`
- `target_type="qa_entry"` for Q&A targets in `debug-query`

Diagnostic lookup is scoped to the rated turn:

- same room
- same agent
- before the rated reply
- after the latest prior human message

That prevents a downvote on a no-memory turn from penalizing a memory used in an
earlier turn.

## Data Model

```text
retrieval_event
- id
- uuid
- target_type        qa_entry | memory_claim
- target_id          qa_id or memory UUID
- stage              retrieved | accepted | rejected | used | downvoted
- query
- room_uuid
- agent_uuid
- journal_id
- source
- retrieval_rank
- retrieval_score
- filter_label       relevant | irrelevant | unknown | null
- metadata           JSONB
- created_at
```

Events are append-only. Do not mutate old rows to "correct" a count. Add a new
event or fix the producer.

## Rollups

Useful derived counters:

```text
retrieved_count = count(stage = retrieved)
accepted_count = count(stage = accepted)
rejected_count = count(stage = rejected)
used_count = count(stage = used)
downvoted_count = count(stage = downvoted)
```

Useful derived rates:

```text
acceptance_rate = accepted_count / retrieved_count
rejection_rate = rejected_count / retrieved_count
usage_rate = used_count / retrieved_count
downvote_rate = downvoted_count / used_count
```

Interpretation examples:

- high retrieved + high rejected: retrieval is noisy for this target.
- high used + high downvoted: target may be stale, broad, wrong, or harmful in
  context.
- low retrieved + eval expects it: retrieval may be missing important memory.
- high used + low downvoted: likely useful, but still not proof of causality.

## Relationship To Feedback And Evals

Telemetry tells you where to inspect. Evals tell you whether a change helped.

Recommended loop:

```text
retrieval events
-> suspicious pattern or user downvote
-> feedback_event
-> eval_case
-> eval_run
-> comparison/gate
-> retrieval/prompt change
```

Do not tune memory or Q&A behavior from raw counters alone. Create representative
eval cases and verify the change.

## Current Limits

- `used` means "entered answer context" for chat memory.
- Query-filter `used` is marked as an accepted-candidate approximation.
- Downvotes do not prove the target caused the bad answer.
- There is no full answer attribution yet.
- There is no rollup stats table yet; reports should query event rows.

## Avoid These Mistakes

- Do not treat filter rejection as permanent truth.
- Do not lower `MemoryClaim.confidence` from relevance counters. Confidence is
  about claim truth; telemetry is about retrieval/usefulness.
- Do not compare Q&A IDs and memory UUIDs without `target_type`.
- Do not optimize on high raw counts without considering exposure volume.
- Do not treat downvotes as automatic memory deletion signals.

## Next Directions

- Add admin/report views for noisy retrieved targets and risky downvoted targets.
- Add attribution signals cautiously, such as model-cited memory UUIDs.
- Add eval cases for high-impact telemetry patterns.
- Consider a derived rollup table only after query volume justifies it.

## Design Principle

Telemetry should make retrieval behavior inspectable. It should not silently
change what the system believes.
