# Set-valued questions: the registry holds the answer and cannot return it

**Status:** Implementable, as **two independent changes** that ship in order.
PR 1 (non-lossy alias table) is a correctness fix confined to one module. PR 2
(derived rosters) is scoped to a bounded MVP: rosters are synthesised as
ordinary `static` entries, and over-long ones truncate deterministically at
member boundaries. Pagination, the diagnostics UI, stemming and non-prefix sets
are named and **not designed**; they must not be built from this document.
**Date:** 2026-08-21
**Revision:** 6.1
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

**Neither branch calls `_exact_match`.** Exact alias matching lives entirely
outside the assistant, in four callers: `agents/query.py`,
`agents/query_router.py`, `agents/query_filter_router.py`, and
`webapp/memory_developer_views.py`. Any
design that relies on an authored alias being matched deterministically fixes
the chat route and leaves the measured route untouched. This constrains both
changes below and is the single most important fact in this document.

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
derived id, and no sync machinery at all. It is the design rosters must beat,
and it is not obviously worse.

- *Against the alternative:* it needs a **new call site**. The measured route
  never consults an alias table (see route note above), so this design requires
  a new hook inside `_action_query_memory` plus a decision about where it sits
  relative to the recall filter. It also matches authored phrasings only —
  a paraphrase reaches nothing, because there is no embedded node to find.
- *Against rosters:* a derived id, a sync digest, and a rendering cap are
  machinery this design does not need.

Rosters win once they are synthesised as **ordinary `static` entries** rather
than a new entry kind. At that point the remaining machinery is a digest, an
id, and a bounded string — while paraphrase retrieval, shield handling,
provenance tiering and every consumer that renders an entry come free, because
a roster *is* an entry. The competitor still needs a new call site and still
reaches only authored phrasings.

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
with it; and a relation index is a prerequisite either way. That index is
cheap to recompute from paths whenever something needs it — PR 2 does not
persist one, because nothing in it reads one. Datalog is an increment decided
later on evidence.
## PR 1 — make the alias table non-lossy

Independent of rosters, a live silent-data-loss bug (C3), and confined to
`memory/seed_memory.py`. It ships first because it is a correctness fix and
nothing else depends on it.

`_exact_match` returns `Match | None`, and all **four** callers act on a match
immediately by resolving and returning. "Pass every matching id forward as
candidates" is therefore not a local change — and the callers do not even share
one notion of what to fall back to:

| caller | behaviour when exact matching declines |
| --- | --- |
| `agents/query.py` | `_semantic_match` — gated on `MIN_SCORE` and `MIN_MARGIN` |
| `agents/query_router.py` | ungated `_semantic_ranked[0]`, resolved and handed to an LLM as context |
| `agents/query_filter_router.py` | the LLM relevance filter over hybrid candidates |
| `webapp/memory_developer_views.py` | a debug view; reports no exact match |

Three distinct fallbacks, one of them ungated. A candidate hand-off would have
to mean something different in each, so it is a caller redesign and is out of
scope.

The change that *is* local:

- `_alias_table` becomes `dict[str, list[str]]`, **deduplicated by `qa_id`**,
  order-preserving, so no id is discarded at load and none is repeated.
- `_exact_match` filters the candidate ids by shield **first**, then returns a
  `Match` only if exactly one visible id remains. Two or more visible ids means
  the alias is **ambiguous, so exact matching declines** and the caller
  proceeds down its existing fallback unchanged.

The deduplication is load-bearing, not tidiness. One entry may carry two
questions that collapse under `_normalize_query`, and the **shipped base
registry contains two such entries** (casing, and a trailing `?`). Without
dedup their alias maps to `[id, id]`, `_exact_match` counts two ids, declares
the alias ambiguous, and a lookup that works today stops working — a regression
introduced by a fix for silent loss, in entries that were never ambiguous.

**This can regress a currently-correct answer.** Today an ambiguous alias
returns one arbitrary entry at `score=1.0`; sometimes that entry is the right
one. Replacing it with a fallback is more principled and is not strictly better
in every case. The claim is that silent arbitrary selection among six entries
is not a behaviour worth preserving — not that nothing changes.

### Reporting is not part of PR 1

Surfacing *which* aliases collide is a separate feature and must not hold up
the correctness fix. It needs plumbing that does not exist: `_load_jsonl`
tracks `lineno` only in local dicts and never stores it on an entry, so
`file:line` reporting means retaining line provenance per entry. And
`/settings/api/repopulate_memory` returns four sync counts which the UI renders
verbatim, so a diagnostics list would need an endpoint schema and a UI.

When it is built: it fires only from an explicit Repopulate request, never from
`_load_kb` or the automatic `_ensure_populated` reconcile — agents run in
freshly spawned processes, so a report emitted at load re-logs the same known
collisions on every agent turn. It names qa_ids and `file:line`, **never the
alias text**, which is operator prose and does not belong in long-lived logs.

## PR 2 — derived rosters as bounded static entries

Rather than teach retrieval to return sets, **synthesise the set as an ordinary
entry, converting an arity-N question back into arity 1.**

At load time, for **every declared relation**, synthesise a roster entry —
whatever its membership count, including one member and none.

**The roster is a `static` entry.** Not a new `kind`: shield-uniform membership
(below) means a roster is wholly visible or wholly hidden, so there is nothing
to evaluate at resolve time and its answer can be rendered once, at load. That
single decision removes `_resolve_match` changes, the fan-out across every
consumer that branches on `static`/`dynamic`, new telemetry columns, developer
view branches, `SeedMemory` handling, and the provenance-tiering problem — all
of which existed only to teach nine call sites about a third kind. A roster is
an entry; everything that already renders an entry renders it.

**It wins on the measured route by ordinary retrieval, not by exact match.**
Its authored questions give it full-text coverage 1.000 on the plural phrasing
and a strong vector score, so it takes a `_hybrid_seed_ranked` slot; and
because one entry stands for N, one slot is all it needs. Exact alias matching
is a bonus on the chat routes and must not be the mechanism any test relies on.

**No new action.** An `enumerate`-style verb would require the model to
recognise that a question is set-valued and select a different action for it —
a decision it has no reliable basis for, added to a ~30-verb list that costs
prompt budget every turn. A roster needs no such recognition, because by the
time retrieval sees it the question is arity 1 again.

**No new table, migration, or source of truth** — but not "nothing is
persisted": a roster's question nodes and digest go into the existing pgvector
table like any other entry's, which is what [Sync](#sync) reconciles. What
stays in memory is the *relation index*; the registry is already fully resident
at 190 entries.

### Relation vocabulary

A declaration names a **canonical prefix**, not a predicate. A predicate plus a
segment index cannot distinguish `human.<alice>.friend.*` from
`human.<bob>.friend.*`, and both rosters would inherit the same first-person
questions — one of them wrong, and the alias ambiguous between them. One
declaration is one roster.

The vocabulary lives in `<customize.dir>/relations.json`, beside the overlay:

```json
{
  "relations": [
    {
      "prefix": "human.<subject>.friend",
      "title": "friends",
      "complete": false,
      "questions": ["who are my friends", "my friends", "list my friends"]
    }
  ]
}
```

Members are the entries whose `path` begins with `prefix` plus **exactly one
further segment**. Deeper descendants are not members: a member's own subtree
(`…friend.<person-a>.travel`) belongs to that person, not to the roster.

`questions` are the roster's aliases and are **authored**. Shipping a
predicate→phrasing table in the repository would put an anglocentric table in a
shipped default; phrasings are language- and instance-specific, so they belong
with the operator's own data. The split that matters:

> **The phrasing is authored once. The membership is derived forever.**

A hand-written roster entry — the pattern the registry uses today for one
relation — requires re-editing prose every time a member is added. Declaring a
relation requires editing nothing after the first time.

A missing `relations.json` means no rosters and is not an error. The file joins
`_source_snapshot()`, so an edit re-triggers the reconcile `_ensure_populated`
already performs on a source change.

### Configuration validation

`relations.json` is operator-authored input driving persistent vector writes,
so it is validated **before any write**, and every failure is a hard error
naming the file and the offending declaration — the convention `_load_jsonl`
already uses. Repopulate fails and the registry is left as it was; a
half-declared relation never reaches Postgres.

Rejected: unreadable or malformed JSON; `relations` absent or not a list; a
declaration that is not an object; `prefix`, `title` or `questions` missing,
empty, or of the wrong type; a non-string or empty question; a `prefix` with a
trailing dot or an empty segment; a non-boolean `complete`; two questions in
one declaration that collapse under `_normalize_query`; two declarations
sharing a `prefix`; two declarations whose distinct prefixes collide under
`uuid5`; a declaration two of whose members share a `path`; and a declaration
colliding with an authored entry at the same path.

### Membership, and the two collision cases

Membership is keyed by **`id`**, the only field unique after merge (C1). But
under a declared prefix the **`path` is the member's logical identity** — it is
the relation's object — so two members sharing a path **raise**, naming both
ids. Listing both and diagnosing it produces a wrong roster: the same person
twice, a count of 7 where the truth is 6, presented as confidently as a correct
one. Cross-file path reuse being legal in the generic merge does not make it
valid relation data.

If an **authored entry already occupies the roster's own path**, that raises
too, naming both sides. An operator who added a declaration, pressed Repopulate
and read a successful count would otherwise never learn it did nothing, and
silent opt-out is indistinguishable from successful derivation. The cost is
that hand-authoring a roster is no longer a way to opt out of generation; the
operator removes the declaration instead, which is the correct direction.

### Cardinality

**A declared relation always yields a roster**, at any membership count.
Generating only at two or more members — on the reasoning that a set of one is
not a set — manufactures the very failure this document exists to fix.

Consider a relation shrinking from two members to one. The roster vanishes, and
with it the authored plural aliases, which the surviving member need not carry,
in a registry where singular and plural do not match each other (C4). The
question that worked yesterday returns to top-k guessing, the completeness
qualifier disappears, and the id and its vector nodes churn every time
membership crosses the boundary. A cardinality rule that deletes aliases is a
retrieval gap dressed as tidiness.

Zero members is likewise a roster, and a useful one. `recorded friends (0)`
answers the question honestly; the alternative is retrieval falling through to
whatever else scores, which is the measured failure again. Under
`"complete": true` it is a *stronger* answer, not a degenerate one. A mistyped
prefix also announces itself as `(0)` the first time the operator asks their
own question — a better signal than a log line.

### Entry shape

```python
{
  "id":          <uuid5(_ROSTER_NS, prefix)>,
  "path":        prefix,
  "kind":        "static",
  "questions":   [...],            # from relations.json, verbatim
  "answer":      <rendered roster text, already bounded>,
  "shield":      <str | absent>,   # the members' common shield
  "_source":     "user-overlay",   # where the declaration lives
  "_derived":    "roster",
  "_row_sha256": <digest, below>,
}
```

`_source` is `"user-overlay"` because that is where the declaration lives, and
because `SeedMemory.source` drives the assistant's overlay-first tiering; a
roster tagged anything else sorts below unrelated overlay facts. `_derived`
records provenance for the one consumer that needs it (below) and for anyone
distinguishing derived from authored.

The id is `uuid5` over the **canonical prefix string** — the whole identity,
not a subject/predicate pair, which would collide across roots. **Identity is
deterministic because its inputs are:** the prefix is a string the operator
wrote, no model involved. This is not the pattern rejected in
`recall-filter-and-retrieval-granularity`, where a stable hash was proposed
over an LLM-produced slug. A synthesised id colliding with an authored id
raises like any other duplicate id.

Rosters are built **after** the base/overlay merge, so an overlay override of a
member is reflected without special handling.

### The one consumer rule, and one behaviour change

Being a `static` entry means every consumer already handles a roster. Two
consequences are worth stating rather than discovering.

**Full-text indexing must skip a roster's answer.** `_fulltext_index` tokenises
each entry's `answer` alongside its questions and counts those tokens into the
IDF table. A roster's answer contains every member's label, so indexing it
normally would surface the roster on single-person queries and inflate the
document frequency of exactly the name tokens that should be rare. The rule:
**an entry with `_derived == "roster"` indexes its `questions` only, never its
`answer`.** The answer stays available for rendering and for filter context.
This is the only retrieval-side change PR 2 makes.

**The roster becomes eligible for the always-on chat block.**
`retrieve_seed_memories` is static-only and feeds the "Curated facts" injected
into every chat turn. A roster is now a candidate there, competing on rank like
anything else. That is desirable — a bounded relation summary is good curated
context — but it is a behaviour change and belongs in the test list.

Because the answer is bounded at load, the roster also cannot inflate the
recall-filter prompt: `seed_candidate_rows` renders a static entry's answer
uncapped, which would have been a problem for an unbounded member list.

### Sync

A roster has no source line, so its `_row_sha256` is a digest over its
**complete synthesised representation** — every input that can change what is
stored or rendered:

```text
sha256(canonical_json({
  "prefix":    prefix,
  "title":     title,
  "questions": questions,                    # ordered
  "complete":  complete,
  "render":    [ROSTER_RENDER_VERSION, ROSTER_ANSWER_MAX_CHARS],
  "members":   [[qa_id, row_sha256], ...],   # ordered
}))
```

Hashing members alone is insufficient: editing an alias or a title, reordering
members, or flipping `complete` would leave stale vectors in Postgres and stale
aliases in the registry. Because `sync_kb` clears `_entries_by_id` and
`_alias_table` only when a row actually changed, a digest that misses a config
edit also misses the invalidation — the two failures compound.

The blast radius is bounded in the normal case: agents run in freshly spawned
processes, so an agent turn rebuilds the registry regardless. The long-lived
webapp process is where a stale registry actually persists.

### Rendering, and deterministic truncation

```text
recorded friends (6):
- <label>  [qa_id]
- <label>  [qa_id]
```

**Each line keeps its qa_id**, at about 36 characters. Dropping the uuids to
fit roughly three times as many members is tempting, and rests on a claim the
registry does not support: that a member is reachable by name because its own
aliases already answer well. No such invariant exists — `label` is optional,
the fallback path slug need not appear in `questions`, labels need not be
unique, and *this document is a measurement of member retrieval failing*. The
qa_id is the only deterministic dereference the registry offers, so it stays.

`<label>` comes from a new optional `label` field on an entry, falling back to
the raw final path segment. Deriving a display name from a slug is not
attempted: casing and diacritics are unrecoverable from it, and guessing them
would print people's names wrong.

**Truncation is deterministic and happens at member boundaries.**

```python
ROSTER_ANSWER_MAX_CHARS = 1100   # below MEMORY_QUERY_PER_FACT_CHARS = 1200
ROSTER_RENDER_VERSION = 1
```

The algorithm, which never slices rendered text:

1. Build the header and every complete member line.
2. If the whole thing fits in `ROSTER_ANSWER_MAX_CHARS`, return it.
3. Otherwise emit the **largest prefix of member lines** for which
   `header + lines + omission marker` fits.
4. Zero displayed members is a legal outcome — header plus marker alone.
5. If even `header + marker` cannot fit, **reject the declaration**: its title
   is too long to render anything.
6. **Reject any member whose label or qa_id contains a newline**, which would
   otherwise forge a line boundary.

```text
- … 14 additional recorded members omitted
```

The **header count is total membership**; the marker reports omitted
membership; the two always sum. Bounding at synthesis rather than at a
consumer matters because the chat routes post `_resolve_match` output with no
cap of their own.

`ROSTER_RENDER_VERSION` and `ROSTER_ANSWER_MAX_CHARS` both enter the digest, so
changing the format or the budget dirties every existing roster row instead of
leaving old renderings embedded — the same failure the `complete` field would
have had if it were omitted.

This does not solve arbitrary N and is not pretending to. It is safe, testable,
and correct for the observed N of 6. Pagination is deferred (see [Named, not
designed](#named-not-designed)).

### Completeness semantics

Datalog is rejected above because a personal-memory store must never read
"never written down" as "false". A roster rendered as `friends (6)` in answer
to *"who are all my friends"* makes a weaker version of that same inference: it
presents a registry prefix as a complete real-world set.

The two are not equally dangerous — Datalog would *derive negations*, while a
roster only over-claims exhaustiveness in its rendering — but the root is
identical, and rejecting one while silently adopting the other is incoherent.

So completeness is **declared, not assumed**. A declaration may set
`"complete": true`, the operator asserting the prefix holds everyone. The
default is `false`, and the rendering says so:

```text
recorded friends (6):        # default
friends (6):                 # "complete": true
```

The count is always the number of *entries*, never a claim about the world.

### Shields

A roster carries its members' shield, so `_entry_locked` treats it exactly like
any other entry — visible when the shield is unlocked, hidden otherwise, and
excluded in SQL by `_shield_filters` so it occupies no vector budget while
locked. This is what makes a `static` roster possible at all: there is no
per-member visibility to evaluate at resolve time.

That requires **shield-uniform** membership: every member in the same shield
class, where *unshielded* is a class of its own. One unshielded member
alongside one `shield: A` member carries a single named shield and is still not
uniform — stamping the roster `A` would hide public data, and leaving it
unstamped would expose member labels that are supposed to be shielded.

A non-uniform declaration **yields no roster for that prefix**, and the rest of
the registry loads normally. Raising would be disproportionate: shielding a
member is data evolution, a privacy decision the operator is entitled to make
at any time, and it must not take the knowledge base down until a declaration
is edited. Every *other* failure above is malformed configuration, which
raises.

Suppression is **silent for the MVP** — no per-load log. Agents run in freshly
spawned processes, so a warning emitted during construction re-fires on every
agent turn. A persistent diagnostic is the right home for this and does not
exist yet; until it does, the trade is recorded in [What this does not
fix](#what-this-does-not-fix).

### What PR 2 costs

One digest, one id scheme, one bounded renderer, one optional entry field, one
operator-owned config file with a validation pass, and **one narrow retrieval
rule** (skip a roster's answer in full-text indexing). No new `kind`, no new
action, no dependency, no migration, no model call, no consumer fan-out.

**No persisted relation index.** Membership is computed during synthesis, used
for the answer and the digest, and discarded. Nothing keeps a queryable
`(subject, predicate, object)` structure, because nothing in this MVP reads
one. A future consumer that needs an index can recompute it from paths — the
data is the same either way, and carrying unused structure for a hypothetical
caller is how the third `kind` got here.

## Named, not designed

Do not build these from this document.

**Pagination.** The MVP truncates; it does not page. Settling pagination means
deciding the continuation syntax, how a continuation is requested, and whether
a request costs a verb the model must choose — which is the thing rosters exist
to avoid. Decide it against a real distribution of N, not in the abstract.

**The diagnostics surface.** Endpoint schema and UI for load-time findings:
duplicate aliases (PR 1) and suppressed non-uniform rosters (PR 2). Neither
change blocks on it; both get better with it.

**Stemming (C4).** The measurement is recorded above so it is not lost. It is
not designed here because a stemmer is language-coupled — `_tokenize` already
carries an English stopword list, and adding an English stemmer would deepen an
anglocentric default rather than fix it — and because it changes scoring for
*every* query, not just set-valued ones. PR 2 reduces the symptom by giving the
plural phrasing a high-coverage entry to land on; it does not make the
tokenizer correct. A language-agnostic fold (suffix truncation, character
n-grams) is the obvious candidate and is crude enough to need its own
measurement first.

**Sets that are not a path prefix.** "Who did I meet at *X*" spans two
predicates and a topic subtree. PR 2 covers exactly the sets the path already
groups. Anything else needs the relation index plus rules — the Datalog
increment, decided later on evidence.

## Relationship to `2026-08-08-qa-navigation-routes.md`

Different failure, and no conflict. That document is about arity-1 reachability
and specifies an LLM-generated, operator-reviewed `qa_edge` graph with a full
candidate/decision lifecycle. It is unbuilt and gated behind its own
experiment; nothing here changes its status or consumes its design.

The one interaction worth noting: the path grouping PR 2 relies on is also a
deterministic source for that proposal's `same_subject` edge type, so that edge
type need not be model-generated or reviewed. PR 2 does not hand it an index —
it persists none — but it establishes that the grouping is trustworthy.
Neither blocks the other and the ordering is free.

## Tests

Permanent, in-repository, synthetic. **No overlay content, path label or person
appears in a fixture, an assertion message or a test name** — the operator's
actual case exists only under `customize.dir`. Each test is verified failing
against current code before its fix lands.

### PR 1

1. A unique alias yields exactly one id at `score=1.0` — the no-regression
   case.
2. Two entries sharing one normalised question: both ids retained in
   `_alias_table`; `_exact_match` declines; the caller reaches its fallback.
3. An entry whose **own** questions collapse under `_normalize_query` keeps a
   single-element alias list and `_exact_match` **still returns that entry**.
   Asserting only that no diagnostic fires would miss the regression this
   guards. Run it against the two real base-registry entries, by id.
4. One visible id plus one locked id ⇒ exact matching still resolves the
   visible one; shield filtering precedes the ambiguity decision.
5. Two visible ids ⇒ declines.

### PR 2 — the measured route

6. Six synthetic members under one declared prefix; `_action_query_memory` on
   the plural authored question yields an observation naming **all six**.
   Pinned at six so a later top-k change cannot make it pass for the wrong
   reason.
7. The same with the recall filter forced to fail, exercising the
   `retrieve_seed_answers` fallback.
8. The roster's questions become vector documents, and the **authored**
   phrasing surfaces it through `_hybrid_seed_ranked`. Deterministic: it
   asserts plumbing, which is what a unit test can hold.

   Paraphrase reach — an unseen phrasing surfacing the roster — is **not a unit
   test**. It depends on `embeddinggemma`, its version, and the surrounding
   corpus, so a fake ranker would prove nothing and a real one would make the
   suite environment-dependent. It is the property that distinguishes this
   design from alias→prefix→enumerate, so it is worth measuring: record it as a
   benchmark alongside the existing ones, not in the permanent unit suite.
9. Exact-alias resolution on a chat route, marked as that route's behaviour and
   not as the fix.

### PR 2 — construction

10. Rosters synthesise at **zero, one, six and seven** members, aliases intact
    at every count; an undeclared prefix synthesises nothing. The one-member
    case pins the rule that shrinking must not delete aliases.
11. A member's own deeper descendant is not a member.
12. Two members sharing a path **raise**, naming both ids.
13. An authored entry at the roster path **raises**, naming both sides.
14. Roster id byte-identical across two independent loads; two declarations
    sharing a prefix raise; two distinct prefixes colliding under `uuid5`
    raise.
15. Each rejected `relations.json` case from
    [Configuration validation](#configuration-validation) raises **before** any
    vector write, asserted by the table being unchanged afterwards.

### PR 2 — retrieval, shields, rendering

16. A query matching one member's label does **not** surface the roster, and
    the roster's indexed token set is asserted directly: its answer-token set
    is empty, and the member's name token is absent from it. Do **not** compare
    IDF values before and after — adding any document changes `n_docs`, so
    every token's IDF moves whether or not the roster mentions it. The claim
    is about which tokens the roster contributes, not about arithmetic.
17. A roster is eligible for the always-on chat block (`retrieve_seed_memories`
    returns it), since it is now `static`.
18. Common shield locked ⇒ the roster is absent from candidates *and* excluded
    at the metadata level, so it consumes no vector budget; unlocked ⇒ present.
19. Both non-uniform mixtures — two named shields, and one unshielded member
    beside one shielded member — yield no roster **while the rest of the
    registry loads**. The second is the case a "two or more distinct shields"
    check lets through; the still-loads half distinguishes suppression from
    raising.
20. Default rendering says `recorded <title>`; `"complete": true` drops the
    qualifier. The count equals the number of entries in both cases.
21. A roster exceeding the character limit truncates at a **member boundary**:
    no split label, no split qa_id, no half line. Header count equals total
    membership, marker equals omitted membership, and the two sum. Assert the
    bound at synthesis, so both the assistant and the uncapped chat routes are
    covered by one test.
22. Every listed member's qa_id resolves through `memory_query`'s uuid mode —
    the dereference the design promises.

### PR 2 — sync

23. Digest changes on each of: a member's answer, member addition, member
    removal, member reordering, an alias edit, a title edit, a `complete` flip
    — asserted separately, since each is a distinct field of the canonical
    form. Unchanged on an edit to an unrelated entry.
24. Removing `relations.json` removes the roster's vector nodes; an absent file
    yields zero rosters and no error.

## What this does not fix

- Reachability of an entry that is never a candidate (the other proposal).
- Set questions whose members are not siblings under one path prefix.
- Rosters beyond the character limit: the overflow is announced, not readable.
- Prefixes whose members are not shield-uniform: no roster, and **silently** so
  in the MVP. Shielding one member of a relation costs that relation its
  roster, with no signal until someone asks the question. The deliberate trade
  is against taking the whole knowledge base down over a privacy change the
  operator is entitled to make.
- The `who are …` pull toward `identity.*`. A roster competes for the same
  slots as everything else and merely needs one instead of N.
- The same person existing as a subject under one root and an object under
  another, with no link between them. That is the tree-versus-graph strain, and
  it is still open.
