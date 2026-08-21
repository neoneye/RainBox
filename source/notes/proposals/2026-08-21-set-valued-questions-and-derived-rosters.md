# Set-valued questions: the registry holds the answer and cannot return it

**Status:** One design (R1, derived rosters) is specified to the level of
identity, sync, shields and tests. One independent bug fix (R2, lossy alias
table) is ready. Stemming and cross-prefix sets are **named and not designed**;
they must not be built from this document.
**Date:** 2026-08-21
**Relates to:** `qa-system.md`, `2026-08-08-qa-navigation-routes.md`,
`2026-08-17-recall-filter-and-retrieval-granularity.md`

All examples below are fictional. No entry text, path label, answer, or person
from an operator overlay is reproduced here. Measurements were taken read-only
against the live registry; only shapes and counts are carried over.

## The failure, measured

A question of the form *"who are all my X"* has an answer set of **N entries**.
The registry stores each member as its own entry, siblings under one path
prefix:

```text
human.<subject>.friend.<person-a>
human.<subject>.friend.<person-b>
...                                  (six such entries in the live registry)
```

Asked the plural question, the candidate list handed to the recall filter
contained **two of the six**. The remaining four were not low-ranked; they were
absent. The model answered from what it was given, which is the correct
behaviour on an incorrect candidate set.

The nine candidates, by surfacing signal:

| signal | entry | why it is there |
| --- | --- | --- |
| semantic | `identity.role` | `"Who are you?"` — the *who* tax |
| semantic+fulltext | `<subject>.<unrelated-topic>` | holds the only **plural** occurrence of the predicate token in the whole registry |
| semantic | `identity.name` | `"What is your name?"` |
| fulltext ×3 | three unrelated `<subject>.*` entries | coverage 0.5 on the possessive alone |
| semantic ×2 | two of the six members | — |

Four of nine slots went to entries that cannot contribute, and two of those four
are about the assistant rather than the operator.

## This is a third axis, not a ranking complaint

Two failure modes are already documented. This is neither.

| axis | question arity | what breaks |
| --- | --- | --- |
| **Reachability** (`qa-navigation-routes`) | 1 | the one right entry never becomes a candidate |
| **Granularity / recall** (`recall-filter-and-retrieval-granularity`) | 1 | the right entry is a candidate and is dropped or truncated |
| **Arity** (this document) | N, unknown | every layer is built to return one entry, so N−k members are structurally unreachable |

`TOP_K_VECTOR = 5` and `TOP_K_FULLTEXT = 5` are not a tuning mistake. No value
of `k` is correct, because `N` is a property of the data and grows when the
operator adds a member. Raising the budget to 6 buys one entry and one more
friend breaks it again — while every arity-1 query pays the widened candidate
set. **Ranking cannot solve an arity problem.**

## Four causes, separated

Two are specific to arity, two are independent bugs that this measurement
happened to expose.

**C1 — `path` is inert.** `human.<subject>.friend.*` *is* the answer set,
already written down, already unique per entry, already validated against
duplicates at load. Nothing queries it. Grep puts `path` in rendering,
telemetry and the duplicate check — never in retrieval. The taxonomy the
operator maintains by hand is not an index.

**C2 — fixed top-k.** Covered above.

**C3 — the alias table is lossy.** `_alias_table` is built as a dict
comprehension keyed on the normalised question
([memory/seed_memory.py](../../memory/seed_memory.py) `_load_kb`), so a
question text appearing on two entries silently keeps the last one. In the live
registry one alias is shared by **six** entries and another by **three**;
`_exact_match` therefore returns one arbitrary member and reports `score=1.0`
for it. The loader hard-fails on a duplicate `id` and on a duplicate `path`
within a file, and says nothing about a duplicate question. This is silent data
loss and is independent of arity.

**C4 — no stemming.** `_tokenize` is exact-token. Measured on the same
registry, same query, singular versus plural:

| query form | full-text hits among the six members |
| --- | --- |
| singular | all six, coverage **1.000** |
| plural | **none** |

The plural is the form a set question naturally takes, and it is the form that
misses. The single plural hit is an unrelated entry that happens to carry the
plural token in an alias.

## Three architectures considered and not adopted

Asked directly, so recorded rather than left implicit.

**A graph database.** The path already *is* a triple —
`<subject>.<predicate>.<object>` — so the modelling win is real but already
paid for. `MATCH (me)-[:FRIEND]->(x)` and a prefix scan over 190 in-memory
entries return the same set; one needs a server, a migration and text2cypher,
the other needs neither. Text2cypher is the expensive half and is exactly the
half a graph database does not supply.

There is a genuine argument for the graph *model* that is not about this bug:
the path is a tree, and a tree gives each fact one home. The live registry
already shows the strain — a person appearing both as an object under one
subject and as a subject in their own right, with no link between the two; a
pair modelled as a compound key so that a dozen entries hang off a cartesian
node; a relation between two people filed under a third and reachable from
neither endpoint. That is a real and growing problem. It is not this problem,
and buying a graph database to fix this one would be paying migration cost to
learn whether migration is warranted.

**GraphQL.** No. It is an API query language for a client that already knows
the schema and what it wants; it performs no retrieval, ranking or fuzzy
matching. The caller here is in-process Python. Its one strength — letting a
caller select fields to avoid over-fetching — is the inverse of the failure
measured above, which is under-fetching. It also runs against
`is_function_calling_model=False` and the benchmarked preference for plain
text.

**Prolog / Datalog.** The best long-term fit and the wrong first step.
`findall(X, friend(S, X), L)` is precisely the missing operation, rules would
*derive* rosters the operator currently hand-maintains as prose, symmetry could
be declared once rather than inserted twice, and the syntax is small enough
that a local model emits it more reliably than SQL or Cypher. Three things stop
it being first: the closed-world assumption reads "never written down" as
"false", which is the one inference a personal-memory store must never make;
the members' value is several hundred words of prose each, so facts can only
sit beside the text as an index and must then be kept in sync with it; and a
fact table is a prerequisite either way. **R1 below produces that fact table.**
Datalog over it is an increment that can be decided on evidence.

## Correction to the approach I proposed in conversation

I proposed a `(subject, predicate, object, qa_id)` table in Postgres plus a new
`kb_enumerate` assistant action. Both are wrong and R1 replaces them.

- **The table is unnecessary.** The registry is already fully in memory and is
  190 entries. `_fulltext_index` already demonstrates the pattern for a cached
  derived index keyed on registry identity. A Postgres table would add a
  migration, a sync path and a second source of truth for data derived from a
  string already in RAM.
- **A new action is worse than no action.** It would require the model to
  recognise that a question is set-valued and choose a different verb for it —
  a decision it has no reliable basis for, added to a ~30-verb action list that
  costs prompt budget on every turn.

The better move is to stop asking retrieval to return sets: **synthesise the
set as an ordinary entry, converting an arity-N question back into arity 1.**

## R1 — derived rosters

At load time, for every `(subject, predicate)` group with two or more members,
synthesise a **roster entry**. It is an entry like any other: it embeds, it is
full-text indexed, it competes for candidacy, it obeys shields. Retrieval is
not modified. The assistant is not modified.

### Relation vocabulary

Which path segments are relations is **operator-declared, not inferred**. The
paths are not uniformly subject-predicate-object: alongside the relational
shape there are topic paths several levels deep, and at least one predicate
appearing at a different depth with no subject before it. Inferring the shape
would be guessing.

The vocabulary lives in a new file `<customize.dir>/relations.json`, beside the
overlay:

```json
{
  "relations": [
    {
      "predicate": "friend",
      "subject_at": 1,
      "questions": ["who are my friends", "my friends", "list my friends"],
      "title": "friends"
    }
  ]
}
```

`questions` are the roster's aliases and are **authored**. This is deliberate
and is the design's one authoring cost. Shipping a predicate→phrasing table in
the repository would put an anglocentric table in a shipped default; phrasings
are language-specific and instance-specific, so they belong with the operator's
own data, next to the overlay that already holds them. The split that matters:

> **The phrasing is authored once. The membership is derived forever.**

A hand-written roster entry — the pattern the registry uses today for one
relation — requires re-editing prose every time a member is added. Declaring a
relation requires editing nothing after the first time.

A missing `relations.json` means no rosters and is not an error. The file joins
`_source_snapshot()` so an edit becomes visible on the next message without a
Repopulate press, exactly as the overlay does.

### Entry shape

A roster is a third `kind`, resolved in `_resolve_match` beside `static` and
`dynamic`:

```python
{
  "id":        <uuid5(_ROSTER_NS, f"{subject}:{predicate}")>,
  "path":      f"{root}.{subject}.{predicate}",
  "kind":      "roster",
  "questions": [...],          # from relations.json, verbatim
  "_members":  [qa_id, ...],   # source order
  "_source":   "derived",
}
```

**Identity is deterministic because its inputs are.** `subject` and `predicate`
are substrings of a path the operator wrote; no model is involved. This is not
the rejected pattern from `recall-filter-and-retrieval-granularity`, where a
stable hash was proposed over an LLM-produced slug. A synthesised id colliding
with an authored id is an operator error and raises like any other duplicate
id.

Rosters are built **after** the base/overlay merge, so an overlay override of a
member is reflected without special handling.

### Resolution

`_resolve_match` for `kind == "roster"` renders one line per member that is
visible under the current unlocked shields:

```text
<title> (<n>):
- <label>  [qa_id]
- ...
Call memory_query with a qa_id above for that entry in full.
```

The roster is an **index card, not a data dump** — it keeps the observation
bounded, which is the existing contract for `memory_query`, and it reuses the
already-documented affordance of re-querying by qa_id for full text.

`<label>` comes from a new optional `label` field on an entry, falling back to
the raw final path segment when absent. Deriving a display name from a slug is
not attempted: casing and diacritics are unrecoverable from it, and guessing
them would print people's names wrong.

### Shields

Member locking is evaluated at **resolve** time, since `qa.unlocked_shields` is
a runtime setting: locked members are omitted from the rendered list and from
`<n>`. `_entry_locked` gains a roster branch returning `True` when **every**
member is locked, so a roster whose members are all hidden never becomes a
candidate rather than surfacing as an empty list. Fail closed, consistent with
the existing malformed-shield rule.

A roster's own `questions` carry no member content, so embedding them leaks
nothing about locked members.

### Sync

A roster has no source line, so it has no natural `_row_sha256`. It takes

```text
sha256("roster:" + subject + ":" + predicate + ":" + "|".join(sorted(member row hashes)))
```

Any member edit, addition or removal changes the digest, so the existing
row-by-row `sync_kb` reconcile re-embeds the roster exactly when it should and
never otherwise. No new sync path.

### What R1 costs

No new dependency, no new table, no migration, no model call, no change to
retrieval or to the assistant's action set. One new `kind`, one derived index
built beside `_fulltext_index`, one optional entry field, one operator-owned
config file.

## R2 — make the alias table non-lossy

Independent of R1, and a live silent-data-loss bug (C3).

`_alias_table` becomes `dict[str, list[str]]`. `_exact_match` with a single id
behaves exactly as today. With more than one id the query is **ambiguous, not
answered**: all matching ids are passed forward as candidates rather than one
being picked arbitrarily at `score=1.0`. That is strictly more information than
today and cannot regress a currently-correct answer.

Duplicate aliases are additionally **reported** — not raised — on the Settings
repopulate result, with `file:line` per occurrence, so the operator can
consolidate them. Raising was considered and rejected: the live overlay
contains at least two such collisions, so hard-failing would refuse to load the
registry until the operator edited roughly nine lines, and the ambiguity is
representable rather than corrupt. The duplicate `id` and duplicate `path`
rules stay as they are — those are genuinely unrepresentable.

## Named, not designed

Do not build these from this document.

**Stemming (C4).** The measurement is recorded above so it is not lost. It is
not designed here because a stemmer is language-coupled — `_tokenize` already
carries an English stopword list, and adding an English stemmer would deepen an
anglocentric default rather than fix it — and because it changes scoring for
*every* query, not just set-valued ones. R1 removes the acute symptom by giving
the plural phrasing an exact alias to land on; it does not make the tokenizer
correct. A language-agnostic fold (suffix truncation, character n-grams) is the
obvious candidate and is crude enough that it needs its own measurement before
anyone writes it.

**Sets that are not a path prefix.** "Who did I meet at *X*" spans two
predicates and a topic subtree. R1 covers exactly the sets the path already
groups. Anything else needs the fact table plus rules — the Datalog increment,
decided later on evidence.

## Relationship to `2026-08-08-qa-navigation-routes.md`

Different failure, and no conflict. That document is about arity-1 reachability
and specifies an LLM-generated, operator-reviewed `qa_edge` graph with a full
candidate/decision lifecycle. It is unbuilt and gated behind its own
experiment; nothing in R1 changes its status or consumes its design.

The one interaction worth noting: R1's derived relation index is a
deterministic, zero-cost source for that proposal's `same_subject` edge type.
If both ship, that edge type need not be model-generated or reviewed at all.
Neither blocks the other and the ordering is free.

## Tests

Permanent, in-repository, synthetic. **No overlay content, path label or person
appears in a fixture, an assertion message or a test name** — the operator's
actual case exists only under `customize.dir`. Each test is verified failing
against current code before the fix lands.

R1:

1. Six synthetic sibling members under one declared predicate; the plural
   authored question resolves via `_exact_match` to the roster and the rendered
   answer names **all six**. Pinned at six so a later top-k change cannot make
   the test pass for the wrong reason.
2. A seventh member added to the fixture appears without any edit to
   `relations.json`.
3. A group with one member synthesises no roster.
4. An undeclared predicate synthesises no roster.
5. Shielded member omitted from the list and from the count while its shield is
   locked; present when unlocked.
6. Every member shielded ⇒ the roster is not a candidate.
7. Roster id is byte-identical across two independent loads.
8. Editing one member's answer changes the roster's `_row_sha256`; editing an
   unrelated entry does not.
9. Absent `relations.json` ⇒ zero rosters, no error.

R2:

10. Two entries sharing one normalised question ⇒ `_exact_match` yields both
    ids, neither silently dropped.
11. A unique alias still yields exactly one id at `score=1.0` — the
    no-regression case.

## What this does not fix

- Reachability of an entry that is never a candidate (the other proposal).
- Set questions whose members are not siblings under one path prefix.
- The `who are …` pull toward `identity.*`. R1 sidesteps it for authored
  phrasings by matching before retrieval runs; an unauthored phrasing still
  spends vector slots on entries about the assistant.
- The same person existing as a subject under one root and an object under
  another, with no link between them. That is the tree-versus-graph strain, and
  it is still open.
