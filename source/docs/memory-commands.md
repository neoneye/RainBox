# Memory Commands

## Purpose

Memory commands let the user directly create, inspect, correct, and remove
first-class memories.

Commands are parsed by `memory/ops.py` and handled through `QueryAgent` before
the Q&A/vector path is initialized. That means explicit memory operations do not
depend on LM Studio embeddings or the Q&A registry being available.

These commands are separate from prompt-time memory retrieval. Both normal chat
(via `build_chat_memory_block`) and the assistant's `query_memory` action use
the hybrid `retrieve_memories_hybrid` path backed by `memory_embedding` when
embeddings are available. The legacy lexical `retrieve_memories` function is
retained only for deterministic memory-retrieval eval cases in `evals/runner.py`.

## Commands

### Remember

```text
remember that <fact>
```

Creates an active memory claim with `confirmed_by_user` evidence.

Example:

```text
remember that I prefer concise technical explanations
```

### Recall All

```text
what do you remember?
```

Lists active memories visible to the current scope.

### Recall Topic

```text
what do you remember about <topic>?
```

Lists active memories matching the topic.

### Explain Memory

```text
why do you remember <topic>?
```

Shows evidence/provenance for matching memories.

### Correct

```text
correct that <old> -> <new>
```

Routes through the governed `correct_belief` path: in one atomic transaction it
supersedes the old claim, tombstones the old value (so it cannot silently
re-enter via model inference), and creates a new active claim with
`confirmed_by_user` evidence whose conflict/tombstone keys are derived from the
new text. If the new value conflicts with a *different* same-scope active claim,
the correction is refused (and rolled back) so two contradicting beliefs are
never left active; resolve that conflict first. A conflict with a broader-scope
claim is kept as a scoped exception.

Example:

```text
correct that I prefer long answers -> I prefer concise answers
```

### Forget

```text
forget <topic>
```

Marks matching memory claims as `rejected` and tombstones the rejected value so
the model cannot silently re-learn it. Evidence remains for audit. (The
assistant's internal undo of its own `remember` is the one rejection that does
*not* tombstone — undo means "didn't mean to add that", not "this is wrong".)

### Last Used Memories

```text
which memories did you use?
```

Reads the latest `debug-memory` diagnostic row and reports which memories were
injected into the previous answer context.

## Provenance

Supported evidence provenance values:

- `observed_from_source`
- `inferred_by_model`
- `confirmed_by_user`
- `imported_from_transcript`

User-created commands currently create `confirmed_by_user` evidence.

## Lifecycle

Memory claims can be:

- `candidate`
- `active`
- `superseded`
- `rejected`
- `expired`

Only active, non-expired memories are retrieved for prompts.

## Sensitivity

Memory sensitivity values:

- `public`
- `private`
- `secret`

Normal chat retrieval excludes `secret`. Private memories can be retrieved, but
should only be used when directly relevant.

## Limitations

- Command parsing and command lookups are currently lexical/exact-match oriented.
- Chat memory retrieval quality depends on hybrid scoring (vector + full-text +
  entity); semantic recall degrades when embeddings are unavailable.
- Write-time conflict detection is deterministic and keyed on common structured
  shapes ("X is Y", "X prefers Y", "X uses Y", …); free-text claims have no
  structured key and are conflict-exempt (semantic conflict detection is future
  work).
- Automatic memory extraction from chat/journal is not implemented yet.

## Operator Notes

Inspect and curate memory on the **`/memory` page**: claims grouped by status,
with an evidence timeline, supersession lineage, embedding freshness, and
provenance-safe lifecycle actions (activate / reject / reactivate / correct /
sensitivity / expiry). It also surfaces conflict candidates with resolution
actions (supersede / reject / not_conflict / scoped_exception) and tombstones
that have suppressed a model re-assertion. See `docs/memory-architecture.md` §8.

The raw tables are also browsable in Flask-Admin (`MemoryClaim`,
`MemoryEvidence`, `MemoryEmbedding`, `MemoryRejectedValue`).

For architecture details, see `docs/memory-architecture.md`.
