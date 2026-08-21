# Set-valued questions: the registry holds the answer and cannot return it

**Status:** The failure analysis and C1–C4 are settled. R1 (derived rosters) is
**blocked**, on six named items: the `kind` fan-out across existing consumers,
provenance tiering, member dereference, completeness semantics, the large-N
rendering cliff, and diagnostics delivery. Each has a proposed resolution
below; none is built. R2 (lossy alias table) is ready at the reduced scope defined below.
Stemming and non-prefix sets are named and **not designed**; they must not be
built from this document.
**Date:** 2026-08-21
**Revision:** 4
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
indexed, it competes for candidacy, it obeys shields.

Retrieval's *ranking* is not modified. Nothing else about "unmodified" survives
contact with the code — see [The `kind` fan-out](#the-kind-fan-out) and
[Provenance](#provenance). A third `kind` is not a free extension point.

**The roster wins on the measured route by ordinary retrieval, not by exact
match.** Its authored questions give it full-text coverage 1.000 on the plural
phrasing and a strong vector score, so it takes a `_hybrid_seed_ranked` slot;
and because it is one entry standing for N, one slot is all it needs. Exact
alias matching is a bonus on the chat routes and must not be the mechanism any
test relies on.

Two things this deliberately does not do.

- **No new table, migration, or source of truth.** Not "nothing is persisted":
  a roster's question nodes and its digest go into the existing pgvector table
  like any other entry's, which is what the whole [Sync](#sync) section exists
  to reconcile. What stays in memory is the *relation index* — the registry is
  already fully resident at 190 entries, and `_fulltext_index` is the existing
  pattern for a derived index keyed on registry identity. A
  `(subject, predicate, object, qa_id)` table would add a migration and a
  second source of truth for data derived from a string already in RAM.
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
      "complete": false,
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

### Configuration validation

`relations.json` is operator-authored input that drives persistent vector
writes, so it is validated **before any write**, and every failure is a hard
error naming the file and the offending declaration — the convention
`_load_jsonl` already uses for the JSONL. Repopulate fails and the registry is
left as it was; a half-declared relation never reaches Postgres.

Rejected: unreadable or malformed JSON; `relations` absent or not a list; a
declaration that is not an object; `prefix`, `title` or `questions` missing,
empty, or of the wrong type; a non-string or empty question; a `prefix` with a
trailing dot or an empty segment; a non-boolean `complete`; two questions in one
declaration that collapse under `_normalize_query`; two declarations sharing a
`prefix`; two declarations whose distinct prefixes collide under `uuid5`; a
declaration whose members are not **shield-uniform**; a declaration two of
whose members share a `path`; and a declaration colliding with an authored
entry at the same path (see below).

**Shield-uniform** means every member falls in the same shield class, where
*unshielded* is a class of its own. One unshielded member alongside one
`shield: A` member carries a single named shield and is still not uniform:
stamping the roster `A` would hide public data, and leaving it unstamped would
consume vector budget while locked. Both mixtures — two named shields, and
named-plus-unshielded — raise.

A declaration matching **fewer than two** members is not an error — membership
is data and legitimately shrinks — but it produces no roster, and an operator
who declared a relation and got nothing needs to know why.

This is the one contract R1 cannot fulfil by raising. Everything else that can
go wrong is now a hard error; this cannot be, because a member being removed
must not break the registry. So it needs a **surface** — and
`/settings/api/repopulate_memory` returns four sync counts, which the UI
renders verbatim. **Diagnostics delivery is therefore R1's sixth blocker**, not
a deferred nicety: endpoint schema and UI rendering must exist before R1 ships,
or the contract must be dropped and the case left silent.

### Membership, and the two collision cases

Membership is keyed by **`id`**, the only field unique after merge (C1). But
under a declared prefix the **`path` is the member's logical identity** — it is
the relation's object — so two members sharing a path **raise**, naming both
ids.

Listing both was the earlier answer and produces a wrong roster: the same
person twice and a count of 7 where the truth is 6, presented with the same
confidence as a correct one. Cross-file path reuse being legal in the generic
merge does not make it valid relation data, and a diagnostic cannot carry this
while diagnostics are logs-only.

If an **authored entry already occupies the roster's own path**, that is a
**configuration error and raises**, naming both the declaration and the
authored entry. Suppress-and-report was the earlier answer and is wrong while
diagnostics are logs-only: the operator would add a declaration, press
Repopulate, read a successful count, and never learn the declaration did
nothing. Silent opt-out is indistinguishable from successful derivation.

The cost is that hand-authoring a roster is no longer a way to opt out of
generation for a prefix; the operator removes the declaration instead. That is
the correct direction — the declaration is the thing that should be absent when
no derivation is wanted.

### Entry shape

A roster is a third `kind`, resolved in `_resolve_match` beside `static` and
`dynamic`:

```python
{
  "id":        <uuid5(_ROSTER_NS, prefix)>,
  "path":      prefix,
  "kind":      "roster",
  "title":     "friends",       # from relations.json; rendering needs it
  "questions": [...],           # from relations.json, verbatim
  "shield":    <str | absent>,  # the members' single common shield, if any
  "complete":  <bool>,          # see Completeness semantics
  "_members":  [qa_id, ...],    # source order
  "_source":   "user-overlay",  # see Provenance
  "_derived":  True,
}
```

`title` and `shield` are real keys, not derived at render time: `title` because
two sections need it, and `shield` because `_build_documents` reads node shield
metadata from `entry["shield"]` and must keep working unchanged.

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

### The `kind` fan-out

A third `kind` is not additive. Nine sites binary-branch on
`static` / `dynamic`, and a roster falls through all of them:

| site | behaviour on a roster | action |
| --- | --- | --- |
| `_resolve_match` | `"(unknown kind in match)"` | add the roster branch — planned |
| `seed_candidate_rows` (`query_filter_router.py`) | row carries **neither `answer` nor `handler`** | must render the member list |
| `_build_documents` | node metadata carries neither | acceptable; metadata is excluded from the vector |
| `retrieve_seed_memories` | `kind != "static"` ⇒ **dropped** | acceptable: the always-on chat block is static-only by design. State it. |
| `retrieve_seed_answers`, `assistant.py` answer extraction (2 sites) | `else` branch calls `_resolve_match` | already correct once the branch exists |
| `assistant.py` telemetry (`qa_static` / `qa_dynamic`) | counted as neither | add a counter — **and see below** |
| assistant trace, HTML (`assistant_views.py`) | hard-coded static/dynamic columns | add the column |
| assistant trace, Markdown export (`assistant_views.py`) | hard-coded 5-column table | add the column |
| `/memory/developer` rendering | shows neither | add the branch |

A new `qa_roster` key in `AssistantObservation.data` is not visible by adding
it: both trace renderers key off `'qa_static' in data` and emit fixed columns.
"One telemetry counter" is three sites with their own tests.

**`seed_candidate_rows` is the one that matters.** Those rows are what the
recall filter judges relevance from. A roster presented to the filter with no
content at all is a roster the filter drops — which would defeat R1's entire
mechanism on the measured route.

It must carry a **summary, not the member list**: `recorded <title> (N)`
alongside the matched question is everything the filter needs to judge
relevance, and `build_filter_prompt_rows` renders each field **uncapped**, so
a full member list would inflate the filter prompt long before the roster ever
reached the rendering cliff. Members are resolved only once the roster is
kept.

### Provenance

`SeedMemory.source` is documented as `"user-overlay" | "upstream"` and is
populated from the entry's `_source`. The assistant tiers on exactly that
distinction — `overlay = [s for s in seeds if s.source == "user-overlay"]`,
everything else second.

A roster tagged `_source = "derived"` therefore lands in the **upstream** tier
and sorts below unrelated overlay facts, despite being derived from a
declaration that lives in the operator's own customize directory. That is a
behaviour change smuggled in through a string.

So a roster carries `_source = "user-overlay"`, matching where its declaration
lives, and derivation is recorded separately in `_derived`. Any consumer that
wants to distinguish derived from authored reads `_derived`; no consumer that
tiers on provenance changes meaning.

### Sync

A roster has no source line, so it has no natural `_row_sha256`. It takes a
digest over its **complete synthesised representation** — every input that can
change what is stored or rendered:

```text
sha256(canonical_json({
  "prefix":   prefix,
  "title":    title,
  "questions": questions,            # ordered
  "complete": complete,
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

**Each line keeps its qa_id**, at a cost of about 36 characters:

```text
<title> (<n>):
- <label>  [qa_id]
```

Dropping the uuids to fit more members was the earlier answer and rested on a
claim the registry does not support — that a member is reachable by name
because "the members' own aliases already answer well". No such invariant
exists. `label` is optional, the fallback path slug need not appear in
`questions` at all, labels need not be unique, and *this entire document is a
measurement of member retrieval failing*. A label-only roster is a display
list, not an index card, and cannot promise that a named member can be read in
full.

The qa_id is the only deterministic dereference the registry offers, so it
stays. The ceiling drops accordingly: roughly **20 members** against the
1200-character `MEMORY_QUERY_PER_FACT_CHARS` cap, not the ~60 that labels-only
would have bought. The observed N is 6.

`<label>` comes from a new optional `label` field on an entry, falling back to
the raw final path segment. Deriving a display name from a slug is not
attempted: casing and diacritics are unrecoverable from it, and guessing them
would print people's names wrong.

**The bound belongs inside the roster renderer, not at a consumer.** The
1200-character cap is `memory_query`'s alone. The chat routes resolve an exact
alias straight through `_resolve_match` and post the result with no cap at all,
so a roster capped at `_fact_line` would be truncated destructively on one
route and unbounded on the other. `_resolve_match`'s roster branch therefore
applies the limit itself, and consumer-specific wrapping happens after.

Beyond the ceiling the roster must paginate or truncate with an explicit
continuation marker. Pagination needs a verb the model must choose, which is
the thing R1 was built to avoid. This is one of R1's blockers and should be
settled against a real distribution of N rather than in the abstract.

### Completeness semantics

Datalog is rejected above because a personal-memory store must never read
"never written down" as "false". A roster rendered as `friends (6)` in answer
to *"who are all my friends"* makes a weaker version of that same inference: it
presents a registry prefix as a complete real-world set.

The two are not equally dangerous — Datalog would *derive negations*, while a
roster only over-claims exhaustiveness in its own rendering — but the root is
identical, and it would be incoherent to reject one and adopt the other
silently.

So completeness is **declared, not assumed**. A declaration may set
`"complete": true`, which is the operator asserting the prefix holds everyone.
The default is `false`, and the rendering says so:

```text
recorded friends (6):        # default
friends (6):                 # "complete": true
```

The count is always the number of *entries*, never a claim about the world.

### Shields

Member locking is evaluated at **resolve** time, since `qa.unlocked_shields` is
a runtime setting: locked members are omitted from the rendered list and from
`<n>`. `_entry_locked` gains a roster branch returning `True` when **every**
member is locked, so a fully-hidden roster is dropped in memory rather than
surfacing as an empty list.

That in-memory backstop is not sufficient on its own. `_shield_filters` admits
a node with no `shield` metadata via `IS_EMPTY`, so a roster carrying no shield
occupies vector budget before being dropped — candidate starvation, in the
document arguing about candidate starvation. So the roster's node metadata
carries the members' common shield.

**The residual is not bounded at one slot, and the earlier text saying so was
wrong twice over.** `TOP_K_NODES = 50` counts *question nodes*, not entries —
the constant's own comment warns that "one strong entry's alternates can eat
most slots". A roster with three aliases is three nodes, and several locked
rosters multiply out against a budget of 50 before any in-memory filter runs.

The resolution is to make the mixed case impossible rather than to absorb it:
**a roster is generated only when its members are shield-uniform** — all
unshielded, or all carrying the same one shield, with *unshielded* counting as
a class of its own (see
[Configuration validation](#configuration-validation)). Any other mixture
raises. The semantics are then exact: the roster's shield *is* its members'
shield.

The alternative — list-valued shield metadata admitted when any member's shield
is unlocked — is strictly more expressive and requires changing
`_shield_filters` to handle a list-valued key. That is a retrieval change, and
this design claims not to make one. If the uniform-shield restriction proves
too tight in practice, that is the way out, and the claim must be retracted
with it.

A roster's own `questions` carry no member content, so embedding them leaks
nothing about locked members.

### What R1 costs

More than it first appeared, and the honest tally is the argument for keeping
alias→prefix→enumerate alive as the fallback:

- One new `kind`, plus **branches at six consumer sites** it fans out to,
  three of which are just "show the telemetry counter".
- One derived index beside `_fulltext_index`, one digest, one node-metadata
  rule, one optional entry field.
- One operator-owned config file, with a full validation pass in front of it.
- A provenance decision that touches how the assistant tiers results.
- A diagnostics endpoint and UI that do not exist yet.

No dependency, no migration, no model call, and no change to retrieval
*ranking*. The competitor needs none of the first four lines and pays instead
with a new call site and no paraphrase reach.

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

- `_alias_table` becomes `dict[str, list[str]]`, **deduplicated by `qa_id`**,
  order-preserving, so no id is discarded at load and none is repeated.
- `_exact_match` filters the candidate ids by shield **first**, then returns a
  `Match` only if exactly one visible id remains. Two or more visible ids means
  the alias is **ambiguous, so exact matching declines** and the caller
  proceeds down its existing semantic path unchanged.

The deduplication is load-bearing, not tidiness. One entry may carry two
questions that collapse under `_normalize_query`, and the **shipped base
registry contains two such entries** (casing, and a trailing `?`). Without
dedup their alias maps to `[id, id]`, `_exact_match` counts two ids, declares
the alias ambiguous, and a lookup that works today stops working — a
regression introduced by a fix for silent loss, in entries that were never
ambiguous at all.

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

Detection compares **distinct qa_ids** per normalised alias, which is the same
deduplication the table itself applies: the within-entry variants described
above are one id and are not a collision.

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

**The diagnostics surface.** Still undesigned, but no longer merely deferred:
it is R1's sixth blocker (see [Configuration validation](#configuration-validation))
and remains optional for R2, which ships logging-only.

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

R1 — consumers (the fan-out):

5. `seed_candidate_rows` renders a roster row carrying `recorded <title> (N)`
   — neither an empty row (which the filter drops, defeating R1) nor the full
   member list (which inflates an uncapped filter prompt). Assert both halves.
6. A roster's `SeedMemory.source` is `"user-overlay"`, and the assistant's
   overlay/upstream partition places it in the overlay tier.
7. `/memory/developer`, the `qa_*` telemetry counters, and **both** assistant
   trace renderers (HTML and Markdown export) account for a roster rather than
   silently omitting it.

R1 — construction:

8. A seventh member appears with no edit to `relations.json`.
9. A one-member group and an undeclared prefix synthesise nothing, and the
   under-two-member case is visible on the repopulate result.
10. A member's own deeper descendant is not a member.
11. An authored entry at the roster path **raises**, naming both sides.
12. Two members sharing a path across base and overlay **raise**, naming both
    ids — no roster is produced with an inflated count.
13. Roster id byte-identical across two independent loads; two declarations
    sharing a prefix raise; two distinct prefixes colliding under `uuid5` raise.
14. Each rejected `relations.json` case from
    [Configuration validation](#configuration-validation) raises **before** any
    vector write, asserted by the table being unchanged afterwards.

R1 — sync:

15. Digest changes on: a member's answer, member addition, member removal,
    member reordering, an alias edit, a title edit, a `complete` flip — each
    asserted separately, since each is a distinct field of the canonical form.
    Digest unchanged on an edit to an unrelated entry.
16. Removing `relations.json` removes the roster's nodes; an absent file yields
    zero rosters and no error.

R1 — shields, completeness and size:

17. A shielded member is omitted from the list and the count while locked, and
    present when unlocked.
18. All members under one shield: the node metadata carries it, asserted at the
    metadata level so no vector budget is consumed while locked.
19. Shield-uniformity raises for **both** non-uniform mixtures: two distinct
    named shields, and one unshielded member alongside one shielded member.
    The second is the case a "two or more distinct shields" check lets through.
20. Default rendering says `recorded <title>`; `"complete": true` drops the
    qualifier. The count equals the number of entries in both cases.
21. A roster large enough to cross `MEMORY_QUERY_PER_FACT_CHARS` renders a
    defined, asserted result — not silent middle-loss — and the bound is
    asserted at `_resolve_match`, so the **chat route** (which applies no cap of
    its own) is covered by the same test.

R2:

22. Two entries sharing one normalised question ⇒ both ids retained in
    `_alias_table`; `_exact_match` declines and the caller reaches its semantic
    path.
23. A unique alias still yields exactly one id at `score=1.0` — the
    no-regression case.
24. An entry whose **own** questions collapse under `_normalize_query` keeps a
    single-element alias list and `_exact_match` **still returns that entry**.
    Asserting only that no diagnostic fires would miss the regression this
    guards. Run it against the two real base-registry entries, by id.

## What this does not fix

- Reachability of an entry that is never a candidate (the other proposal).
- Set questions whose members are not siblings under one path prefix.
- Rosters beyond roughly 20 members.
- Prefixes whose members carry more than one distinct shield: no roster.
- Whether a prefix is a *complete* real-world set. R1 renders what is recorded
  and lets the operator assert completeness; it cannot verify the assertion.
- The `who are …` pull toward `identity.*`. A roster competes for the same
  slots as everything else and merely needs one instead of N.
- The same person existing as a subject under one root and an object under
  another, with no link between them. That is the tree-versus-graph strain, and
  it is still open.
