# Memory Commands

## Purpose

Memory commands let the user directly create, inspect, correct, and remove
first-class memories.

Commands are parsed by `memory/ops.py` and handled through `QueryAgent` before
the Q&A/vector path is initialized. That means explicit memory operations do not
depend on LM Studio embeddings or the Q&A registry being available.

These commands are separate from prompt-time memory retrieval. Normal chat still
uses the legacy lexical `retrieve_memories` path; the assistant's `query_memory`
action uses the newer hybrid `retrieve_memories_hybrid` path backed by
`memory_embedding` when embeddings are available.

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

Supersedes the old active claim and creates a new active claim with
`confirmed_by_user` evidence.

Example:

```text
correct that I prefer long answers -> I prefer concise answers
```

### Forget

```text
forget <topic>
```

Marks matching memory claims as `rejected`. Evidence remains for audit.

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
- Normal chat memory retrieval is still lexical; hybrid retrieval is currently
  additive and mainly used by the assistant.
- Conflict detection is basic and should be improved.
- Automatic memory extraction from chat/journal is not implemented yet.

## Operator Notes

Inspect and curate memory on the **`/memory` page**: claims grouped by status,
with an evidence timeline, supersession lineage, embedding freshness, and
provenance-safe lifecycle actions (activate / reject / reactivate / correct /
sensitivity / expiry). See `docs/memory-architecture.md` §8.

The raw tables are also browsable in Flask-Admin (`MemoryClaim`,
`MemoryEvidence`, `MemoryEmbedding`).

For architecture details, see `docs/memory-architecture.md`.
