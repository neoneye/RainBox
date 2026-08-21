# Set-valued questions: the registry holds the answer and cannot return it

**Status:** The failure analysis and C1–C4 are settled. R1 (derived rosters) is
specified but **not yet implementation-ready**: the large-N rendering cliff is
bounded and stated rather than solved. R2 (lossy alias table) is ready at the
reduced scope defined below. Stemming and non-prefix sets are named and **not
designed**; they must not be built from this document.
**Date:** 2026-08-21
**Revision:** 2
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

### The route this failure travelled

The measurement came from the assistant, so the route is
`_action_query_memory` → `_filter_recalled_candidates` → `_hybrid_seed_ranked`
([agents/assistant.py](../../agents/assistant.py)), with
`retrieve_seed_answers` → `_semantic_ranked` as the fallback when the filter
call fails.

**Neither branch calls `_exact_match`.** Exact alias matching exists only on the
chat routes ([agents/query.py](../../agents/query.py),
[agents/query_filter_router.py](../../agents/query_filter_router.py)). Any
design that relies on an authored alias being matched deterministically fixes
the chat route and leaves the measured route untouched. This constrains R1 and
R2 below and is the single most important fact in this document.

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
already written down, already grouped. Nothing queries it. Grep puts `path` in
rendering, telemetry and the duplicate check — never in retrieval. The taxonomy
the operator maintains by hand is not an index.

Path uniqueness is **weaker than it looks** and any consumer must account for
it: `_load_jsonl` rejects a duplicate `path` only *within* one file. Two
entries with different ids and the same path, one per file, both survive the
merge — asserted by
`test_load_jsonl_allows_overlay_to_override_base_path`
([memory/test_seed_memory_errors.py](../../memory/test_seed_memory_errors.py)).
Only `id` is unique after merge.

**C2 — fixed top-k.** Covered above.

**C3 — the alias table is lossy.** `_alias_table` is built as a dict
comprehension keyed on the normalised question
([memory/seed_memory.py](../../memory/seed_memory.py) `_load_kb`), so a
question text appearing on two entries silently keeps the last one. In the live
registry one alias is shared by **six** entries and another by **three**;
`_exact_match` therefore returns one arbitrary member and reports `score=1.0`
for it. The loader hard-fails on a duplicate `id` and on a duplicate `path`
within a file, and says nothing about a duplicate question. This is silent data
loss, independent of arity, and — per the route note above — it affects the
chat routes only.

**C4 — no stemming.** `_tokenize` is exact-token. Measured on the same
registry, same query, singular versus plural:

| query form | full-text hits among the six members |
| --- | --- |
| singular | all six, coverage **1.000** |
| plural | **none** |

The plural is the form a set question naturally takes, and it is the form that
misses. The single plural hit is an unrelated entry that happens to carry the
plural token in an alias.

## Four architectures considered

**Alias → prefix → enumerate (the closest competitor).** Map an authored alias
to a canonical path prefix, and on a hit enumerate the matching in-memory
entries directly. This activates C1 with no synthetic entry, no vector node, no
derived id, and no sync machinery at all. It is the design R1 must beat, and it
is not obviously worse.

Two things decide it, and only one favours R1:

- *Against the alternative:* it needs a **new call site**. The measured route
  never consults an alias table (see route note above), so this design requires
  a new hook inside `_action_query_memory` plus a decision about where it sits
  relative to the recall filter. It also matches authored phrasings only —
  a paraphrase reaches nothing, because there is no embedded node to find.
- *Against R1:* everything in "What R1 costs" — a derived id, a sync digest,
  shield metadata, and a rendering cap — is machinery this design does not
  need.

R1 is chosen because it rides rails that already exist and inherits paraphrase
retrieval for free, not because the alternative is unsound. If R1's machinery
proves heavier in implementation than it reads here, this is the fallback and
it should be taken.

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
neither endpoint. That is a real and growing problem. It is not this problem.

**GraphQL.** No. It is an API query language for a client that already knows
the schema and what it wants; it performs no retrieval, ranking or fuzzy
matching. The caller here is in-process Python. Its one strength — letting a
caller select fields to avoid over-fetching — is the inverse of the failure
measured above, which is under-fetching. It also runs against
`is_function_calling_model=False` and the benchmarked preference for plain
text.

**Prolog / Datalog.** The best long-term fit and the wrong first step.
`findall(X, friend(S, X), L)` is precisely the missing operation, rules would
*derive* rosters the operator currently hand-maintains as prose, and the syntax
is small enough that a local model emits it more reliably than SQL or Cypher.
Three things stop it being first: the closed-world assumption reads "never
written down" as "false", which is the one inference a personal-memory store
must never make; the members' value is several hundred words of prose each, so
facts can only sit beside the text as an index and must then be kept in sync
with it; and a relation index is a prerequisite either way. R1 produces that
index. Datalog over it is an increment decided later on evidence.

## R1 — derived rosters

Rather than teach retrieval to return sets, **synthesise the set as an ordinary
entry, converting an arity-N question back into arity 1.**

At load time, for every declared relation with two or more members, synthesise
a **roster entry**. It is an entry like any other: it embeds, it is full-text
indexed, it competes for candidacy, it obeys shields. Retrieval is not
modified. The assistant is not modified.

**The roster wins on the measured route by ordinary retrieval, not by exact
match.** Its authored questions give it full-text coverage 1.000 on the plural
phrasing and a strong vector score, so it takes a `_hybrid_seed_ranked` slot;
and because it is one entry standing for N, one slot is all it needs. Exact
alias matching is a bonus on the chat routes and must not be the mechanism any
test relies on.

Two things this deliberately does not do.

- **No new storage.** The relation index is derived in memory, not persisted.
  The registry is already fully resident and is 190 entries; `_fulltext_index`
  is the existing pattern for a cached derived index keyed on registry
  identity. A `(subject, predicate, object, qa_id)` table in Postgres would add
  a migration, a sync path and a second source of truth for data derived from a
  string already in RAM.
- **No new action.** An `enumerate`-style verb would require the model to
  recognise that a question is set-valued and select a different action for it
  — a decision it has no reliable basis for, added to a ~30-verb list that
  costs prompt budget on every turn. A roster needs no such recognition,
  because by the time retrieval sees it the question is arity 1 again.

### Relation vocabulary

A declaration names a **canonical prefix**, not a predicate. `predicate` plus a
segment index cannot distinguish `human.<alice>.friend.*` from
`human.<bob>.friend.*`, and both rosters would then inherit the same
first-person questions — one of them wrong, and the alias ambiguous between
them. One declaration is one roster.

The vocabulary lives in a new file `<customize.dir>/relations.json`, beside the
overlay:

```json
{
  "relations": [
    {
      "prefix": "human.<subject>.friend",
      "title": "friends",
      "questions": ["who are my friends", "my friends", "list my friends"]
    }
  ]
}
```

Members are the entries whose `path` begins with `prefix` plus one further
segment. Deeper descendants are **not** members: a member's own subtree
(`…friend.<person-a>.travel`) belongs to that person, not to the roster.

`questions` are the roster's aliases and are **authored**. This is deliberate
and is the design's one authoring cost. Shipping a predicate→phrasing table in
the repository would put an anglocentric table in a shipped default; phrasings
are language-specific and instance-specific, so they belong with the operator's
own data. The split that matters:

> **The phrasing is authored once. The membership is derived forever.**

A hand-written roster entry — the pattern the registry uses today for one
relation — requires re-editing prose every time a member is added. Declaring a
relation requires editing nothing after the first time.

A missing `relations.json` means no rosters and is not an error. The file joins
`_source_snapshot()`, so an edit re-triggers the reconcile that `_ensure_populated`
already performs on a source change.

### Membership, and the two collision cases

Membership is keyed by **`id`**, the only field unique after merge (C1). Two
members sharing a `path` across base and overlay are both listed and are
reported as a diagnostic; this is legal per the merge rules and is almost
certainly an operator mistake, so it is surfaced rather than silently resolved.

If an **authored entry already occupies the roster's own path**, generation for
that prefix is **suppressed** and reported. The authored entry wins. This is
what makes the existing hand-written roster safe: it keeps working, no
migration is required, and hand-authoring one is the supported way to opt out
of generation for a prefix.

### Entry shape

A roster is a third `kind`, resolved in `_resolve_match` beside `static` and
`dynamic`:

```python
{
  "id":        <uuid5(_ROSTER_NS, prefix)>,
  "path":      prefix,
  "kind":      "roster",
  "questions": [...],          # from relations.json, verbatim
  "_members":  [qa_id, ...],   # source order
  "_source":   "derived",
}
```

The id is `uuid5` over the **canonical prefix string** — the whole identity,
not a subject/predicate pair, which would collide across roots. Two
declarations sharing a prefix are a config error and raise. **Identity is
deterministic because its inputs are:** the prefix is a string the operator
wrote and no model is involved. This is not the pattern rejected in
`recall-filter-and-retrieval-granularity`, where a stable hash was proposed
over an LLM-produced slug. A synthesised id colliding with an authored id
raises like any other duplicate id.

Rosters are built **after** the base/overlay merge, so an overlay override of a
member is reflected without special handling.

### Sync

A roster has no source line, so it has no natural `_row_sha256`. It takes a
digest over its **complete synthesised representation** — every input that can
change what is stored or rendered:

```text
sha256(canonical_json({
  "prefix":   prefix,
  "title":    title,
  "questions": questions,            # ordered
  "members":  [[qa_id, row_sha256], ...],   # ordered
}))
```

Hashing members alone is insufficient: editing an alias or a title, reordering
members, or changing the declaration would leave stale vectors in Postgres and
stale aliases in the registry. Because `sync_kb` clears `_entries_by_id` and
`_alias_table` only when a row actually changed, a digest that misses a config
edit also misses the invalidation — the two failures compound.

Note the blast radius is bounded in the normal case: agents run in freshly
spawned processes, so an agent turn rebuilds the registry regardless. The
long-lived webapp process is where a stale registry actually persists.

### Rendering, and the large-N cliff

`memory_query` caps each fact at `MEMORY_QUERY_PER_FACT_CHARS = 1200`
([agents/assistant.py](../../agents/assistant.py)). A roster is O(N), so a
large enough roster loses its middle members — the original failure moved one
layer down. This is a real limit of the design and is stated, not solved.

Mitigation, which raises the cliff rather than removing it: render **labels
only**, comma-joined, with no qa_ids.

```text
<title> (<n>): <label>, <label>, …
```

At roughly 20 characters per name that fits about 60 members, against about 20
if each line carried a 36-character uuid. The model reaches any member's full
text by calling `memory_query` with that member's name — an ordinary query that
the members' own aliases already answer well — so dropping the uuids costs
nothing that the registry does not already provide.

`<label>` comes from a new optional `label` field on an entry, falling back to
the raw final path segment. Deriving a display name from a slug is not
attempted: casing and diacritics are unrecoverable from it, and guessing them
would print people's names wrong.

Beyond the cliff the roster must either paginate or truncate with an explicit
continuation marker. Pagination needs a verb the model must choose, which is
the thing R1 was built to avoid. **This is the open question that keeps R1 out
of implementation**, and it should be settled against a real distribution of N
rather than in the abstract.

### Shields

Member locking is evaluated at **resolve** time, since `qa.unlocked_shields` is
a runtime setting: locked members are omitted from the rendered list and from
`<n>`. `_entry_locked` gains a roster branch returning `True` when **every**
member is locked, so a fully-hidden roster is dropped in memory rather than
surfacing as an empty list.

That in-memory backstop is not sufficient on its own. `_shield_filters` admits
a node with no `shield` metadata via `IS_EMPTY`, so a roster carrying no shield
would occupy a vector slot before being dropped — candidate starvation, in the
document arguing about candidate starvation. So the roster's **node metadata**
carries the members' single common shield when all members share exactly one,
and no shield otherwise.

The residual case — every member locked, under two or more different shields —
still consumes one slot and is dropped in memory. It is bounded at one slot and
is accepted.

A roster's own `questions` carry no member content, so embedding them leaks
nothing about locked members.

### What R1 costs

One new `kind`, one derived index built beside `_fulltext_index`, one optional
entry field, one operator-owned config file, one node-metadata rule, one
digest. No dependency, no migration, no model call, no change to retrieval.

## R2 — make the alias table non-lossy

Independent of R1, and a live silent-data-loss bug (C3). Scoped down from what
a first reading suggests, because the callers do not support the obvious fix.

`_exact_match` returns `Match | None`, and both callers act on a match
immediately — `query.py` as `_exact_match(query) or _semantic_match(...)`,
`query_filter_router.py` by resolving and returning. "Pass every matching id
forward as candidates" is therefore not a local change: the filter router could
hand them to its LLM filter, the plain query agent has no arbitration stage at
all, and the assistant does not call exact matching in the first place. That is
a caller redesign and is out of scope here.

The change that *is* local:

- `_alias_table` becomes `dict[str, list[str]]`, so no id is discarded at load.
- `_exact_match` filters the candidate ids by shield **first**, then returns a
  `Match` only if exactly one visible id remains. Two or more visible ids means
  the alias is **ambiguous, so exact matching declines** and the caller
  proceeds down its existing semantic path unchanged.

**This can regress a currently-correct answer.** Today an ambiguous alias
returns one arbitrary entry at `score=1.0`; sometimes that entry is the right
one. Replacing it with a semantic fallback is more principled and is not
strictly better in every case. The claim to make is that silent arbitrary
selection among six entries is not a behaviour worth preserving — not that
nothing changes.

Duplicate aliases are additionally **reported**, not raised, so the operator can
consolidate them. Raising was rejected: the live overlay holds at least two such
collisions, so hard-failing would refuse to load the registry until roughly nine
lines were edited, and ambiguity is representable rather than corrupt.

Detection must **normalise per entry before comparing across entries**. The
shipped base registry contains two entries whose own question lists collapse
under `_normalize_query` (casing and a trailing `?`); those are harmless
within-entry variants and must not be reported as competing ids.

The reporting surface is **not designed here**. `/settings/api/repopulate_memory`
returns sync counts and the UI renders exactly those four counts
([webapp/settings_views.py](../../webapp/settings_views.py)); returning a
diagnostics list from the loader does not make it visible. Endpoint schema and
UI rendering are a separate, small piece of work. Until it exists the
diagnostic is logged only, and R2 ships on that basis.

## Named, not designed

Do not build these from this document.

**Stemming (C4).** The measurement is recorded above so it is not lost. It is
not designed here because a stemmer is language-coupled — `_tokenize` already
carries an English stopword list, and adding an English stemmer would deepen an
anglocentric default rather than fix it — and because it changes scoring for
*every* query, not just set-valued ones. R1 reduces the symptom by giving the
plural phrasing a high-coverage entry to land on; it does not make the tokenizer
correct. A language-agnostic fold (suffix truncation, character n-grams) is the
obvious candidate and is crude enough that it needs its own measurement first.

**Sets that are not a path prefix.** "Who did I meet at *X*" spans two
predicates and a topic subtree. R1 covers exactly the sets the path already
groups. Anything else needs the relation index plus rules — the Datalog
increment, decided later on evidence.

**The roster diagnostics UI.** See R2.

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

The primary test runs the **measured route**. A test that asserts only
`_exact_match` resolution would certify a mechanism the failing path never
calls, and would pass while the real route still drops the roster.

R1 — route:

1. Six synthetic members under one declared prefix; `_action_query_memory` on
   the plural authored question yields an observation naming **all six**. Pinned
   at six so a later top-k change cannot make it pass for the wrong reason.
2. The same, with the recall filter forced to fail, exercising the
   `retrieve_seed_answers` fallback.
3. The roster appears in `_hybrid_seed_ranked` candidates for a **paraphrase**
   not present in `questions` — the property that distinguishes R1 from
   alias→prefix→enumerate.
4. Exact-alias resolution on the chat route, marked as the chat route's
   behaviour and not as the fix.

R1 — construction:

5. A seventh member appears with no edit to `relations.json`.
6. A one-member group and an undeclared prefix synthesise nothing.
7. A member's own deeper descendant is not a member.
8. An authored entry at the roster path suppresses generation and reports.
9. Two members sharing a path across base and overlay: both listed, reported.
10. Roster id byte-identical across two independent loads; two declarations
    sharing a prefix raise.

R1 — sync:

11. Digest changes on: a member's answer, member addition, member removal,
    member reordering, an alias edit, a title edit. Digest unchanged on an
    edit to an unrelated entry.
12. Removing `relations.json` removes the roster's nodes; an absent file yields
    zero rosters and no error.

R1 — shields and size:

13. A shielded member is omitted from the list and the count while locked, and
    present when unlocked.
14. Every member locked ⇒ not a candidate; with all members under one shield,
    assert the node metadata carries it so no vector slot is consumed.
15. A roster large enough to cross `MEMORY_QUERY_PER_FACT_CHARS` renders a
    defined, asserted result — not silent middle-loss.

R2:

16. Two entries sharing one normalised question ⇒ both ids retained in
    `_alias_table`; `_exact_match` declines and the caller reaches its semantic
    path.
17. A unique alias still yields exactly one id at `score=1.0` — the
    no-regression case.
18. An entry whose own questions collapse under `_normalize_query` is **not**
    reported as a duplicate.

## What this does not fix

- Reachability of an entry that is never a candidate (the other proposal).
- Set questions whose members are not siblings under one path prefix.
- Rosters beyond the rendering cliff.
- The `who are …` pull toward `identity.*`. A roster competes for the same
  slots as everything else and merely needs one instead of N.
- The same person existing as a subject under one root and an object under
  another, with no link between them. That is the tree-versus-graph strain, and
  it is still open.
