# Memory Architecture

## Purpose

The memory system gives agents durable, inspectable context beyond a single
chat prompt. It is designed around provenance: the system should not only store
what it believes, but also where that belief came from, whether a user confirmed
it, whether it has been superseded, and whether it is safe to inject into an
agent prompt.

This is separate from the older Q&A registry in `data/question_answer.jsonl`.
The Q&A registry is curated command/answer knowledge for `QueryAgent`; the new
memory layer is a general-purpose store for remembered claims and their
evidence.

## Current Pieces

### Durable Store

Memory is stored in Postgres through first-class tables in the `db/` package:

- `memory_claim`
- `memory_evidence`
- `memory_embedding`

`MemoryClaim` is the canonical belief. It holds the text of the memory, its
kind, scope, lifecycle status, confidence, sensitivity, and optional expiry.

`MemoryEvidence` is the audit trail. It records how the claim became known:

- `observed_from_source`
- `inferred_by_model`
- `confirmed_by_user`
- `imported_from_transcript`

The important design rule is that provenance is not a mutable field on the
claim. A claim can have multiple evidence rows. For example, a model-inferred
candidate can later receive a `confirmed_by_user` evidence row without losing
the original inference record.

`MemoryEmbedding` is an auxiliary pgvector table for active claims. It stores
one embedding per `(memory_uuid, model_name, text_hash)` so hybrid retrieval can
use semantic similarity without changing the claim/evidence source of truth.
Claims without a current embedding still remain retrievable through lexical and
entity signals.

### Claim Lifecycle

Claims have a `status`:

- `candidate`
- `active`
- `superseded`
- `rejected`
- `expired`

Only `active` memories are eligible for retrieval. Rejected and superseded
claims remain in the database so the system can explain what happened later.

`expires_at` is also respected during retrieval. A claim can still be marked
`active` in the table, but if its expiry timestamp is in the past, it is treated
as stale and excluded from prompt context.

### Scope and Sensitivity

Claims have a `scope`:

- `global`
- `agent`
- `room`
- `project`

Retrieval ranks room-scoped memories ahead of agent-scoped and global memories
when the current room or agent matches.

Claims also have a `sensitivity`:

- `public`
- `private`
- `secret`

Secret memories are excluded from normal retrieval. Private memories can be
retrieved, but the chat system prompt tells the model to use them only when
directly relevant.

### User Operations

Explicit memory commands are handled by `memory/ops.py` and routed through
`QueryAgent`.

Supported operations include:

- `remember that ...`
- `forget ...`
- `confirm that ...`
- `correct that OLD -> NEW`
- `what do you remember?`
- `what do you remember about ...`
- `why do you remember ...`
- `which memories did you use?`

These commands are parsed before the Q&A path is initialized, so simple memory
operations do not depend on LM Studio, embeddings, pgvector, or the curated Q&A
registry being available.

User-created memories become active with `confirmed_by_user` evidence. Forgetting
marks a claim `rejected`; correcting creates a new active claim and marks the old
one `superseded`.

### Retrieval

Runtime retrieval lives in `memory/retrieval.py`. There are currently two
retrieval paths:

- `retrieve_memories`: the legacy chat-memory path. It is deterministic and
  lexical: token overlap against claim text, subject, and object.
- `retrieve_memories_hybrid`: the assistant action path. It hard-filters first,
  then blends vector similarity from `memory_embedding`, Postgres full-text rank,
  and subject/object entity boosts.

Both paths are intentionally conservative:

1. Tokenize the current user message.
2. Consider only active, non-expired claims.
3. Exclude secret claims unless explicitly allowed.
4. Apply scope constraints before ranking.
5. Rank only the remaining allowed claims.
6. Return a small capped list.

Hybrid retrieval is additive, not a global replacement. The chat agents still
use the legacy lexical path so existing behavior stays simple and explainable;
the assistant's `query_memory` action uses the hybrid path and degrades to
lexical/full-text/entity signals when an embedding is missing or the embedder is
unavailable.

### Prompt Injection

`ChatAgent.user_prompt` retrieves relevant memories with the legacy lexical path
and prepends a compact block before the normal IRC-style chat transcript:

```text
Relevant remembered facts:
- [preference, private, confirmed_by_user] Username prefers concise technical answers.

Chat history, oldest first:
...

Current message:
...
```

Before building the transcript, `ChatAgent` filters the room history to
`kind == "message"`. Diagnostic rows such as `debug-memory`, `debug-query`,
`progress`, and `thinking` are not shown to the model and cannot become the
current message.

The assistant uses memory differently: its bounded action loop can call
`query_memory`, which uses `retrieve_memories_hybrid` and returns the formatted
memory context as an observation inside the persisted assistant trace.

### Memory-Use Audit

When `ChatAgent` injects memories, it also posts a diagnostic chat row:

- `kind="debug-memory"`
- `content_type="json"`

That row records the query, journal id, memory UUIDs, retrieval reasons,
confidence, and provenance labels. The user can later ask which memories were
used, and `memory/ops.py` reads the latest `debug-memory` row to explain the
previous answer.

### Relevance Telemetry

Memory retrieval now also emits structured retrieval telemetry through
`RetrievalEvent`.

For normal chat memory retrieval, `ChatAgent` writes:

- `target_type="memory_claim"`
- `stage="retrieved"` for each retrieved memory
- `stage="used"` for each memory injected into the prompt
- `source="chat_memory_retrieval"`

The current chat path injects every retrieved memory, so `retrieved` and `used`
currently have the same target set. That is an honest first phase: it records
which memories entered the answer context, but it is not full final-answer
attribution.

For assistant memory retrieval, `retrieve_memories_hybrid` writes:

- `target_type="memory_claim"`
- `stage="retrieved"` for each hybrid-ranked memory
- `source="memory.hybrid"`

The assistant trace then records the action and observation that consumed those
memories.

The query-filter router also emits retrieval telemetry for Q&A entries:

- `retrieved`
- `accepted`
- `rejected`
- `used`

Its `used` events are explicitly marked with metadata:

```json
{"used_signal": "accepted_candidate_approximation"}
```

That matters because the router knows what the filter accepted, but it does not
yet prove which accepted candidates influenced the final reply.

### Feedback And Downvotes

User-facing agent messages can be upvoted or downvoted. Feedback is stored in
`FeedbackEvent`.

Each feedback event snapshots nearby context:

- rated message text and content type
- latest prior human message
- same-turn `debug-memory` payload when present
- same-turn `debug-query` payload when present

Downvotes are linked back into retrieval telemetry. If a downvoted answer had
same-turn memory or query diagnostics, the system writes `RetrievalEvent` rows
with:

- `stage="downvoted"`
- `source="chat_feedback"`
- `target_type="memory_claim"` for memory UUIDs
- `target_type="qa_entry"` for Q&A targets

Diagnostic lookup is scoped to the rated turn: same room, same agent, before the
rated reply, and after the latest prior human message. This prevents a no-memory
answer from accidentally downvoting a memory that was used in an earlier turn.

### Eval Loop

The memory system is now connected to an eval loop.

Feedback can be promoted into `EvalCase` rows. Downvotes default to regression
cases; upvotes default to train cases. Eval cases can then be run through
`evals/runner.py`, producing:

- `EvalRun`
- `EvalResult`
- summary metrics
- pass/fail status per case

`evals/compare.py` compares candidate runs against baselines and applies a
regression gate. `evals/optimizer.py` can run bounded candidate configurations
and select a safe candidate when the gate rules and optimizer-specific rules are
satisfied.

The loop is intentionally deterministic first. Chat-reply cases currently score
known output snapshots; memory-retrieval cases call live deterministic retrieval.
LLM-as-judge is not part of the current architecture.

The important architectural shift is this:

```text
real usage -> feedback -> eval case -> eval run -> comparison/gate -> safer change
```

Memory is no longer just a retrieval feature. It is becoming measurable system
behavior.

## How This Relates To Existing Memory-Like Systems

The project now has several memory-like layers:

- Chat transcript: short-term conversational context.
- Journal: durable episodic record of agent work.
- Q&A registry: curated static/dynamic knowledge for `QueryAgent`.
- Memory claims/evidence: general long-term memory with provenance.
- Memory embeddings: semantic index for active memory claims.
- Workspace shell state: narrow procedural state for shell sessions.

The new memory layer should not replace all of these. It should become the
place for durable, reusable claims that benefit from provenance, lifecycle, and
retrieval control.

## Strengths

The current architecture has a good foundation:

- Provenance is modeled explicitly.
- User confirmation does not erase earlier evidence.
- Forget/correct operations are auditable.
- Retrieval is deterministic and testable.
- Hybrid retrieval exists for the assistant while chat keeps the simpler legacy
  path.
- Sensitive memory has a first-pass safety model.
- Prompt injection is compact.
- Diagnostic memory use is inspectable.
- Memory commands avoid unnecessary LLM/vector dependencies.
- Chat memory retrieval emits relevance telemetry.
- Downvotes can be linked back to retrieved memories and Q&A entries.
- Feedback can become regression/train eval cases.
- Eval runs, baselines, comparison, and optimizer scaffolding exist.
- Diagnostic rows are kept out of prompts.

This is now more than a memory MVP. It is an early measurable memory system:
retrieval, feedback, telemetry, and evals all exist in the same loop.

## Current Limitations

The system is still conservative and incomplete:

- Chat memory retrieval is still lexical, so normal chat can miss semantically
  related memories with different wording.
- Hybrid retrieval is currently additive and mainly used by the assistant; it is
  not yet the default for every memory consumer.
- There is no automatic extraction of candidate memories from chat or journal
  rows.
- There is no conflict detector for competing active claims.
- `sensitivity` is manually assigned and coarse.
- There is no dedicated memory management UI beyond Flask-Admin.
- Memory is injected into `ChatAgent` and available to the assistant through
  `query_memory`, but not broadly integrated into every agent type.
- Evidence stores excerpts, but not rich source navigation or source snapshots.
- The system does not yet summarize episodes from `journal` into reusable
  memories.
- Relevance telemetry records context injection, not true final-answer
  attribution.
- The eval loop is deterministic and useful, but not yet a full behavioral
  benchmark for live LLM answers.
- Eval comparison safety still needs strict case-set invariants: baseline and
  candidate runs should compare the same eval cases by default.
- The optimizer currently tests bounded retrieval settings; it is not an
  autonomous prompt/source optimizer.

These are acceptable constraints for this stage. They keep the system auditable
while the semantics settle.

## Directions To Go

### 1. Tighten Eval Comparison Invariants

The eval loop is now important enough that comparison semantics matter.

Baseline and candidate runs should compare equivalent case sets by default. A
candidate should not pass by:

- omitting hard baseline cases
- adding easy candidate-only cases that inflate its mean score

Missing baseline cases are already handled. Candidate-only cases should also be
treated as a gate/optimizer failure unless an explicit partial-comparison mode
is introduced later.

This is not glamorous, but it protects the trustworthiness of every later memory
optimization.

### 2. Add Candidate Extraction, But Keep Human Confirmation

The safest next step is automatic candidate extraction from chat and journal
events:

- detect likely preferences
- detect stable project facts
- detect decisions
- detect procedures that were useful

These should be stored as `candidate` claims with `inferred_by_model` or
`imported_from_transcript` evidence. They should not become active until the
user confirms them.

This keeps automation useful without letting the model pollute long-term memory.

### 3. Add Conflict Detection

Before creating or activating a claim, compare it to existing active claims with
similar subject/predicate/object or high textual overlap.

The first version can be simple:

- exact normalized text duplicate: do not create another claim
- same subject and predicate but different object: flag conflict
- correction path: supersede instead of parallel active claims

Later, semantic conflict detection can use embeddings or an LLM.

### 4. Expand Hybrid Retrieval Carefully

The secondary pgvector retrieval channel exists, but it should keep the same
guardrails as it spreads beyond the assistant:

1. Filter by status, expiry, sensitivity, scope.
2. Run vector search over eligible memory claims.
3. Merge lexical and vector results.
4. Keep provenance and retrieval reason visible.

This avoids a black-box memory dump. The model should receive only a small set
of memories, each with an explanation of why it was retrieved. Remaining work is
mostly adoption and freshness: decide which chat/agent paths should use hybrid
retrieval, ensure embeddings are current when memories become active or change,
and keep lexical fallback behavior explicit.

### 5. Improve Attribution

Current telemetry can say:

- this memory was retrieved
- this memory was injected into the prompt
- this answer was downvoted
- this memory was in the downvoted answer's diagnostic context

It cannot yet say:

- the model actually used this memory in the final wording
- the memory was the reason the answer was good or bad

Possible next steps:

- ask the model to cite memory UUIDs it used
- post-process answers for explicit memory references
- add small attribution eval cases
- compare answer quality with and without selected memories

The system should treat attribution as evidence, not certainty. Poor attribution
signals can be more damaging than no attribution signal.

### 6. Use Memory Across More Agents

`ChatAgent` is the first consumer. Other useful consumers:

- edit-document agents: remember user editing preferences and recurring
  validation failures
- router agents: remember room/project intent
- MCP/tool agents: remember successful tool patterns
- future planner/critic/verifier agents: retrieve relevant prior episodes

This should be opt-in per agent. Not every agent should receive every memory.

### 7. Promote Journal Rows Into Episodic Memory

The journal already records durable work. A summarizer could periodically create
`episode_summary` claims from completed tasks:

- what was attempted
- what succeeded
- what failed
- what test verified it
- what should be done differently next time

This would make repeated engineering work more effective without stuffing raw
journal JSON into prompts.

### 8. Build A Memory Review UI

Flask-Admin is enough for developers, but a memory-specific page would make the
system easier to operate:

- active memories
- candidates awaiting confirmation
- rejected/superseded history
- evidence timeline
- confirm/reject/correct buttons
- sensitivity controls
- "used in last answer" view

This is where trust improves: the user can see what the assistant believes and
edit it directly.

### 9. Tighten Sensitivity Policy

The current sensitivity model is a useful start, but it is still manual and
coarse. Future work should define rules such as:

- contact information defaults to `private` or `secret`
- credentials and tokens must never be stored as retrievable memory
- personal location should not be injected unless directly relevant
- private memories should not be used in unrelated casual replies

This is especially important because the project already contains personal
profile and contact facts in the older Q&A registry.

### 10. Add Source Navigation

Evidence currently records source type, source id, and excerpt. That is enough
for audit, but not ideal for investigation.

Useful next step:

- for chat messages, link to room/message
- for journal rows, link to admin journal row
- for files, record path and line number
- for APIs, record endpoint and timestamp

The goal is that "why do you remember this?" can lead to the original source,
not just a short excerpt.

## Recommended Sequence

The best next sequence is:

1. Finish eval comparison hardening so baseline and candidate runs use the same
   case set by default.
2. Add duplicate/conflict handling for explicit user memory commands.
3. Add a small memory review UI for candidates and active claims.
4. Add candidate extraction from chat, but keep it inactive until confirmed.
5. Improve attribution signals cautiously.
6. Add pgvector retrieval as a secondary channel.
7. Add journal-to-episode summaries.
8. Gradually opt more agents into memory retrieval.

This order protects trust. It improves the user's ability to inspect and correct
memory and verify changes before increasing automatic memory creation or
semantic recall.

## Design Principle

The memory system should optimize for reliable usefulness, not maximum recall.

A good memory is:

- relevant
- sourced
- inspectable
- correctable
- scoped
- safe to reveal

When those properties are uncertain, the memory should remain a candidate or be
left out of the prompt.
