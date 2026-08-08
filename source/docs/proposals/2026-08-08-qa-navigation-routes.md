# Q&A navigation routes: reaching an entry retrieval cannot find

An entry can be present, unshielded, correctly embedded, and still unreachable
by the question a person actually asks.

This document is in two parts. The first is an **experiment** on the cheapest
candidate fix; passing it retires the second part entirely. The second is the
design to build only if that fix proves insufficient.

All examples are fictional. No entry text, path label, or person from an
operator overlay is reproduced here.

Two words are used precisely. An **edge** is the stored, reviewed relation
between two entries, held once and labelled for each direction. A **route** is
that edge traversed in one direction for one query. The operator approves
edges; the assistant is shown routes.

## The failure, measured

Two entries are household-shaped — each holds a couple plus their children:

```text
human.family.<elder>     parents: <elder>, <partner>;  children: ..., <adult>
human.family.<adult>     parents: <adult>, <spouse>;   children: ...
```

Asked "who is `<adult>`'s mother", retrieval returns `human.family.<adult>` —
the entry where `<adult>` *is* the parent. The answer is in
`human.family.<elder>`, where `<adult>` is listed as a child.

Measured against a live registry:

- The relational phrasing does not retrieve the target **at all**. It is not a
  low-ranked candidate; it is not a candidate.
- The bare kinship term in the operator's second language also returns nothing.
- The name with bundle words around it — "`<name>` family", "`<name>` childhood
  siblings" — retrieves and keeps the target every time.

This is a reachability problem, not a ranking problem. No re-scoring reaches an
entry that never becomes a candidate.

## Two candidate explanations

**Reach.** The target carries no phrasing near the question. More phrasings per
entry would close it — the follow-up proposal's variant B, alias enrichment.

**Granularity.** The entry bundles six people into one node while the questions
asked of it are single-person. The embedding of a six-person household is a
centroid close to no individual question, which is why bundle-shaped queries
succeed and person-shaped ones fail. The answer would be a derived
person-level projection.

**These are not mutually exclusive, and the experiment does not treat them as
rivals.** A node can have poor granularity *and* still be reachable once it
carries the right alias. The experiment tests one thing only: whether the alias
pipeline, end to end, fixes this failure. A positive result retires the other
work for this case. A negative result says alias enrichment is not proven
sufficient — and says nothing about which of routes or a person projection
should follow, because it tests neither.

## The experiment

It tests the **alias pipeline end to end**. Two properties must both hold, and
a partial pass is not a pass:

- **Feasibility.** Can a generator produce a phrasing near the failing question
  *unprompted*, from the entry's own content?
- **Sufficiency.** With that phrasing stored, does the original question
  retrieve the entry, survive the recall filter, and produce the right answer,
  without breaking queries that already worked?

Feasibility carries more weight than it looks. If a person must write the
alias, the mechanism collapses into "add a question to the registry by hand",
which the operator can already do and which needs no design at all.

### Phase 1 — feasibility

Read-only. No writes anywhere.

Pin the prompt and the model, then run **several deterministic trials** asking
for questions a person might ask that this answer resolves. One call producing
five outputs cannot establish infeasibility: it establishes that one
model-prompt-sample failed.

- **Passes** if a trial yields a phrasing recognisably equivalent to the
  failing question.
- **Inconclusive** if trials disagree, or if a different prompt or a stronger
  local model plausibly changes the result. Inconclusive is a legitimate
  outcome and must be recorded as one rather than rounded to failure.
- **Fails** only when repeated trials under a pinned setup produce nothing but
  bundle-shaped questions.

### Phase 2 — sufficiency

Only if Phase 1 passes.

Probing retrieval with the *generated* phrasing proves only that the generated
phrasing works. The alias must be stored and embedded, then the **original
failing question** run through the complete pipeline. A near match between the
two is not evidence either: `_normalize_query`
([memory/seed_memory.py](../../memory/seed_memory.py)) lowercases, strips
trailing `?!.` and collapses whitespace. It equates no paraphrases, so
exact-alias matching will not fire on a near miss.

Run it in `rainbox_claude`: copy the overlay to a scratch directory, add the
generated alias, set `customize.dir` in the sandbox database only, populate,
and query. Production settings, registry, and embeddings stay untouched.
Repointing the live setting would instead mean two repopulate cycles and a
`qa.facts_invalidated_at` stamp that fires re-check notices in rooms.

Passing requires **all three**, in order:

1. The target appears as a retrieval candidate for the original question.
   Today it is not a candidate, so this is the first real difference.
2. The recall filter **keeps** it. Candidacy is necessary and not sufficient,
   and the sandbox carries no model-group bindings by default — so either
   reproduce the operator's `memory_filter` binding in the sandbox, or run an
   equivalent controlled relevance decision and say which was done.
3. A full assistant turn on the original question returns an answer naming the
   correct person.

A run that stops at candidacy is recorded as **partial**, not as a pass.

### Phase 3 — collisions

A new alias competes for queries it should not win.

Draw the control set from **recent real queries** in the retrieval telemetry,
not from a handful invented for the test, and include the queries that
currently resolve to the target and to its neighbours. For each, assert the
**candidate set and the kept set**, not merely which entry came top — a
collision that reorders candidates without changing the winner is the same
defect one query later.

- **Fails** if any control's kept set changes. A fix that repairs one question
  and breaks three is not a fix.

### Decision table

| Result | Consequence |
|---|---|
| Phases 1, 2 and 3 all pass, including the final answer | Alias enrichment is sufficient **for this failure class**. Specify variant B; shelve the design below |
| Anything else — fail, partial, or inconclusive at any phase | Alias enrichment is not proven sufficient. Routes and a person-level projection remain open, and this experiment does not rank them: it tested neither |

The second row is the one worth stating plainly. A deterministic incoming-mention
route can succeed exactly where a generative alias fails, so an alias failure is
not evidence for a person projection. Choosing between those two needs its own
comparison, on its own evidence.

### Limits

This tests one entry against two questions — the relational phrasing and the
bare kinship term, which behaved differently in the probes. A clean result
means "this failure class, this entry", not "alias enrichment is sufficient" in
general.

**Record the outcome in this document before building anything below.**

---

# The design, if routes are what is missing

## What a route has to do

Three requirements. Each is load-bearing, and a design that defers any of them
does not close the case.

1. **It must be discoverable from the entry the query lands on.** Retrieval
   reaches the *adult* entry. The bridging text is in the *elder's* answer. A
   generator that reads only the touched entry's answer can never find it.
2. **It must be ranked against the question.** A household has neighbours for
   spouses, children, health, projects, locations and events. Nothing static
   knows that "mother" selects one of them.
3. **Following it must not bypass relevance.** `memory_query {"uuid": ...}`
   resolves through `_query_memory_full`, which applies a shield check and
   returns the entry **in full, untruncated**, with `QueryContext(query="")`.
   There is no query at that seam and therefore no relevance decision.

## Generation: an inverted mention index

Build the index once per KB load, by scanning every answer a single time:

```text
subject_key  ->  entry that is about it          (subjects)
subject_key  ->  entries whose answer mentions it (mentions)
```

This is a linear corpus scan, not pair enumeration. A demand event on entry `Y`
then reads both directions: entries `Y` mentions, and **entries that mention
`Y`**. The second is what surfaces the elder from the adult, and it is the
direction requirement 1 needs.

Demand-driven still applies: candidates are materialised for an entry when a
real recall touches it, so the review queue is bounded by what the operator
actually asks about. The index is corpus-wide; the *candidates* are not.

### Subject keys, specified against the code

`_normalize_query` is for alias matching and does none of the work needed here.
A separate `_normalize_subject` is required: casefold, remove internal
whitespace and punctuation, preserve non-ASCII letters. `Ada Lovelace` and
`adalovelace` collapse to one key; `Kjeld Åström` and `kjeldåström` likewise.

Registered questions are *not* subject keys — normalising "Who is Ada
Lovelace?" yields `who is ada lovelace`, which matches no extracted token.

The last path segment is a usable subject for `human.family.<adult>` and a
misleading one for `human.<person>.health`, whose final segment is `health`. An
optional override would let those entries silently become "about health"
whenever the operator did not anticipate the problem. So:

- The registry gains a `subjects: [...]` field, **required for an entry to be
  graph-eligible**. An entry without it is skipped by generation and listed on
  the developer page as ineligible, with its inferred segment shown.
- The loader validates: a list of non-empty strings, normalised at load, with
  a collision between two entries' subject keys reported rather than silently
  resolved to one owner. Subject state is part of the KB cache and is
  invalidated by `sync_kb` like the rest.

This trades operator effort for the absence of silent, invisible failure. The
alternative — inferring subjects per namespace — needs a rule per namespace
shape and fails the same way the first time a new shape appears.

Mention extraction is capitalisation-based with a stop-list, multi-word
longest-first, and deliberately recall-oriented: a token resolving to no
subject key is dropped silently, so over-extraction is cheap and
under-extraction is the only real failure. A token resolving to several
subjects produces a candidate for each.

Path proximity remains as a second generator — shared parent namespace, direct
ancestor and descendant — capped per parent by a stated limit with a
deterministic order. Vector-neighbourhood generation is not included: it is the
noisiest source and demand-driven generation removes its coverage rationale.

## Edges carry the role, not just the membership

A membership label is not enough. "The adult appears in the elder's household"
does not say the adult appears *as a child*, and if several households mention
the adult, nothing distinguishes the one that answers "mother".

Labels therefore name the role, per direction:

```text
lists_as_child      / listed_as_child_by
lists_as_parent     / listed_as_parent_by
lists_as_member     / listed_as_member_by      (grouping with no role expressed)
mentions            / mentioned_by
same_subject                                    (symmetric)
narrower            / broader                   (path descent)
```

The edge carries a **reviewed one-line summary per direction**, `summary_ab`
and `summary_ba`. One summary cannot serve both: the edge is bidirectional and
each direction has a different destination, so a single field would describe
the wrong entry for one of them. Each is written by the typing model and
corrected or replaced by the operator at review.

This is the evidence the route scorer ranks against. A path plus a label is too
little to judge a route against a question, and the target's answer itself
cannot be shown to the scorer without leaking unfiltered content.

A typing model assigns the label pair, the role, and the summary. It never
proposes pairs. Candidates it calls `unrelated` are **retained and visible on
the developer page** rather than dropped, so its false negatives are
inspectable — a silently discarded candidate is a generator failure nobody can
see.

`same_subject` is an edge, never a merge. Two entries about one subject stay two
entries, and nothing here rewrites the registry.

## Route scoring has its own policy

Routes are scored against the current query before any are surfaced. This is
the gate that decides whether private target metadata is exposed and whether an
unfiltered full-entry read becomes reachable, so it is specified to the same
depth as the follow-up proposal's validator rather than named as a future
function.

Scoring must **not** reuse `apply_filter_scores`
([agents/query_filter_router.py](../../agents/query_filter_router.py)): with
fewer than `TOP_K_FILTER` candidates it sets `kept=True` for every candidate
regardless of score. That is correct for recall — an over-aggressive scorer
must not empty a small result set — and fatal here, where two or three routes
is the ordinary case and keeping them all is the failure the gate exists to
prevent.

**Input.** One row per route: target path, direction label with its role, and
that direction's reviewed summary. Never the target's answer. Bounded by a
fixed character limit; over it, the lowest-weight routes are dropped before the
call rather than truncated inside it.

**Schema.** Strict, `extra="forbid"`:

```text
RouteDecision
- items[]
  - route_id          echoed; unknown or duplicate ids are discarded
  - answers_query     1..5, anchored: 1 = a different subject entirely,
                      5 = this destination is what the question asks for
```

A single anchored dimension, deliberately. The recall filter's three scales
exist to separate direct answers from useful context, and *context* is exactly
what must not be surfaced here: a route that is merely topically adjacent is
the health-and-projects noise requirement 2 rules out. Indirect relevance is
never sufficient.

**Policy.** `apply_route_policy` is absolute and admits no small-list
exemption: a route is surfaced only at `answers_query >= ROUTE_KEEP_THRESHOLD`,
initially 4, carried by `ROUTE_POLICY_VERSION`. Any threshold change increments
that version.

**Model.** A dedicated `route_scorer` binding, not the `memory_filter` group —
that group may legitimately contain remote members, and this call sees paths
and summaries for entries the recall filter did not retrieve. Policy validation
**rejects any non-local member** at configuration time rather than at call
time.

**Failure is closed.** Scorer unavailable, timed out, over its input limit, or
returning an unparseable result surfaces **no routes**. Degrading to "show
everything" defeats the gate; degrading to silence costs only the feature.

**Calibration.** A fixture set of fictional routes with expected verdicts, run
before adopting a different scorer model, on the same discipline as the recall
filter's. Anchored endpoints are what make the absolute threshold portable
between models; the fixtures are what prove it did port.

**Cost.** One call per turn, and only when a kept entry has at least one edge.
Same timeout budget as the recall filter. A turn that surfaces no routes makes
no call.

Surviving routes render as:

```text
<related_keys note="routes from the entries above; key names only">
<uuid>  human.family.<elder>   listed_as_child_by
</related_keys>
```

Capped at three per kept source and six total.

**A path label is content.** `human.<person>.health.<condition>` discloses a
fact by existing. Shields are applied before scoring, so a locked target is
never scored and never surfaced. Sanitising a label prevents prompt-structure
forgery; it does not make the label neutral, and this document does not claim
it does.

## Following a route

Surfacing a route mints an opaque single-use **`route_token`** and shows it
beside the key. Following is `memory_query {"route_token": "..."}`, and the
token binds:

```text
run uuid + surfacing step uuid
directional edge decision (pair, direction, endpoint hashes)
the query it was scored against
target uuid
```

The server resolves the token, re-checks that the decision is still current and
both endpoints still visible, then returns the target. A token is spent once.

The weaker rule — "honour a uuid the preceding step surfaced" — was considered
and does not gate anything. `memory_query {"uuid": ...}` still returns the full
entry unfiltered, so a uuid held from an earlier step or an earlier turn reaches
the same content with no route scoring involved. That rule distinguishes
telemetry, not authorisation.

Plain uuid mode stays as the escape hatch for reading a fact already recalled
in full. It is not the route-follow protocol, and routes are not reachable
through it.

Serving *unreviewed* routes would additionally require `_query_memory_full` to
carry the originating query and apply a relevance decision before returning.
That work is a precondition of soft-gating, not an optimisation.

The decide prompt gains one line: when the answer concerns someone reached
through another entry, check the related keys before reporting that nothing is
stored. This composes with the bounded empty-read retry already in the loop.

## Review

Only reviewed edges serve. There is no pending-serves mode: a wrong route
injects a full private answer into context, and that cost does not justify
serving unreviewed. Demand-driven generation is what makes the hard gate
affordable.

Actions: **keep**, **reject** with a reason, **relabel** (including the role
and the summary), **defer**, **keep all currently shown** — which states the
count and requires confirmation, so it remains a decision rather than a
bypass — and **suggest more with a hint**.

The hint re-runs *typing* over that entry's candidates, including those
previously marked `unrelated`, with the hint as additional context. It cannot
invent pairs, because the typing model never proposes pairs; it can only
recover candidates the generators already found and typing discarded. A hint
that needs a pair no generator produced is a generator gap, and belongs in the
gap report rather than in the review loop.

A human decision is monotonic **for the content it was made against**. A re-run
may propose candidates and suggest a relabel; it may not move a kept edge back
to pending or revive a rejected pair under unchanged content, and it writes to
the decision table never. If either endpoint's hash changes, the decision stops
being servable by computation — no transition is written — and the pair becomes
proposable again, because the text the operator judged no longer exists.

## Storage

```text
qa_edge_generation                one row per (touched entry, inputs)
- source_qa_id
- source_sha            runtime row hash of the touched entry
- index_fingerprint     digest of the subject-index and sibling state used
- generator_version
- state                 running | complete | failed
- candidate_count       0 is a real, durable result
- locked_at             per-entry advisory lock, pg_advisory_xact_lock on
                        source_qa_id
- started_at, finished_at, error_code
UNIQUE (source_qa_id, source_sha, index_fingerprint, generator_version)

qa_edge_candidate
- generation_id         FK -> qa_edge_generation, ON DELETE CASCADE
- pair_key              digest over the CANONICAL endpoint order
- endpoint_a, endpoint_b        stored in canonical order
- bases                 JSONB list of {generator, detail}
- sha_a, sha_b          runtime row hashes at proposal time
- label_ab, label_ba, role      null before typing
- summary_ab, summary_ba
- typing_verdict        typed | unrelated
- typed_by_uuid, policy_version
- queued_at

qa_edge_decision
- pair_key
- sha_a, sha_b          the hashes the decision was made against
- status                kept | rejected
- label_ab, label_ba, role, summary_ab, summary_ba    as approved
- reviewed_by, reviewed_at, review_note
- created_at
UNIQUE (pair_key, sha_a, sha_b)
```

**The generation row is what makes "current" answerable.** Without it, an entry
whose honest result is *no candidates* has nothing to distinguish "generated,
found none" from "never generated", so it regenerates on every query; a crash
midway leaves partial candidates that look complete; and two assistant runs can
generate the same source concurrently. The row carries the completeness the
fingerprint describes, which is why the fingerprint belongs on the attempt
rather than on its results. A per-entry advisory lock — the same
`pg_advisory_xact_lock` pattern already used in `db/memory.py` — makes the
attempt single-flight.

Identity is the **canonical** pair, endpoints sorted, so the same logical edge
discovered from either endpoint is one row and a rejection blocks both
orientations. `pair_key` is a plain domain-separated digest over those two
columns, **not** an HMAC: the endpoint columns are already stored, so keying
adds no protection against a database reader while making every pair identity
and tombstone dependent on the Flask `SECRET_KEY` surviving rotation.

Decisions are **per content revision**, which is why they are their own table
keyed on endpoint hashes. A rejection under the old text is retained for audit
while a new candidate and decision exist under the new text.

**Staleness is derived, not stored.** A decision is servable only while
`sha_a`/`sha_b` match current runtime hashes and both endpoints are visible;
otherwise it is stale by computation. There is no stale *transition* and
generation never writes to the decision table — a re-run may add candidates,
and it may not delete a kept decision or revive a rejected pair under unchanged
content. A graph rebuilt wholesale on sync, with no per-element lifecycle and
no approver recorded, cannot demonstrate that any human reviewed it; the
property is worth asserting across a full regeneration *and* an index rebuild.

Deletion or a shield lock hides an edge immediately.

All three tables live in `db/models.py`, created by `init_db`, with `state`,
`status`, `typing_verdict` and the label vocabulary enforced by CHECK
constraints. RainBox already has active/candidate flows, rejected-value
tombstones and a review UI for claims; edge review reuses those states and that
idiom.

**Dynamic entries are excluded.** Handler-backed entries have no static answer
to scan, and neither generation nor typing may invoke a handler to obtain
material. They are ineligible as sources and, in this version, as targets.

## Privacy

The typing model receives entry content, which here means the operator's
private overlay: it must be explicitly bound and local, prompts and logs never
record raw entry text, an unshielded source is typed only against unshielded
targets, and cross-shield edges are refused.

Shield visibility binds the whole pipeline, not only the final scoring step. A
locked entry must not leak through a candidate's `bases`, through an
`unrelated` row on the developer page, through a summary, or through a count —
the index is built over visible entries for the acting shield set, and every
inspection surface re-checks at read time.

The **route scorer** is a second, repeated disclosure and needs stating
separately. It runs on live turns, and it sees target paths and summaries for
entries the recall filter did not retrieve — content the model would not
otherwise have seen. It must therefore be the same locally bound model under
the same rules, shields applied before scoring rather than after, and its
prompts subject to the same no-raw-text logging rule.

**Overlay generation stays blocked until an authenticated operator control
plane exists.** Local providers reduce disclosure but do not establish *who
authorised the run*, and "started on localhost" is not an authorisation. Since
this feature has no value at upstream-only scope, the honest consequence is
that it does not ship before that control plane does. The experiment above and
the read-path work are unaffected.

## Telemetry

`RetrievalEvent` currently constrains `target_type IN ('qa_entry',
'memory_claim','skill')` and `stage` to a fixed list
([db/models.py](../../db/models.py)), so `qa_edge` and a dead-end stage raise
`IntegrityError` today. This needs a guarded constraint migration — the model
declaration for fresh databases and the drop/recreate block in `db/__init__.py`
for existing ones — and it belongs in the delivery step that first writes an
event, not in a later one.

Events target the **decision revision**, `(pair_key, sha_a, sha_b)`, not the
pair alone. Keying on the pair would pool events from before and after a
content change into one series, which is precisely when route quality is most
likely to have changed.

- `considered` — a route was surfaced. Written from the surfacing step's
  observation data.
- `used` — a `route_token` minted by that step was redeemed. Exact, because the
  token is single-use and carries the step uuid.

Nothing further is claimed. `accepted` is not reused: it is defined against a
recall-filter decision and the route read makes none. A `resolved` signal would
require knowing which memories a reply drew on, and replies carry no citation
structure — inferring it from reply text is not a measurement. If route utility
needs a stronger signal than `used`, reply-level provenance has to be built
first, as its own work.

**Dead end** is an explicit action, `mark_route_dead_end`, with args
`{"route_token": "..."}`. It is accepted only for a token redeemed earlier in
the same run, so the assistant cannot mark a route it never followed. It writes
one `RetrievalEvent` at `stage="dead_end"` against the decision revision, with
the acting agent as actor, deduplicated per (run, revision), and FIFO-bounded
like the other streams. Dead-end counts order the review queue for re-review;
they change no edge's status.

Telemetry may order the generation and review queues. It may never change an
edge's status, or a recurring query certifies its own routes.

## Tests and acceptance criteria

The first test is the whole lifecycle, and no partial version substitutes for
it:

- Given **only the two registry entries** — no edge pre-created — querying for
  the adult's mother touches the adult entry, demand generation discovers the
  elder through an *incoming* mention, the operator keeps the edge, the same
  query ranks that route and no other, following it returns the elder entry,
  and the answer identifies the elder.

Then:

- The inverted index yields the elder from the adult; a generator reading only
  the adult's answer yields nothing, and the test asserts both.
- A source whose honest result is zero candidates records a complete generation
  with `candidate_count = 0`, and a second demand event for unchanged inputs
  does not regenerate it.
- A generation interrupted mid-write is not servable and does not read as
  complete; the next demand event redoes it.
- Two concurrent demand events for one source produce one generation.
- An entry without a `subjects` field is ineligible and reported, never treated
  as being about its last path segment.
- Two entries whose subject keys collide are reported rather than resolved
  silently to one owner.
- A dynamic entry is skipped, and no handler is invoked during generation or
  typing.
- A candidate set is recomputed when the subject index or sibling set changes,
  even though the source entry's hash is unchanged.
- Two or three routes are scored and only the relevant one is surfaced —
  asserted against `apply_route_policy` directly, since the recall filter's
  small-list keep-all would pass all of them.
- A route scoring below the threshold is not surfaced even when it is the only
  route for that source.
- Route scoring unavailable, timed out, or unparseable surfaces no routes.
- A `route_scorer` group containing a non-local member is rejected at
  configuration time.
- A `route_token` is single-use, bound to its run, step, decision revision and
  query; a replayed or foreign token is refused.
- A plain uuid read cannot reach a route that scoring did not surface.
- Each direction is scored against its own summary, and the summary written for
  the opposite destination is never used.
- `_normalize_subject` maps a spaced, capitalised, non-ASCII name to a
  concatenated lowercase slug, and is distinct from `_normalize_query`.
- A token resolving to two subjects produces two candidates; one resolving to
  none is dropped without error.
- The same logical edge discovered from either endpoint produces one row, and a
  rejection blocks it in both orientations.
- A rejection under one pair of endpoint hashes is retained when a new decision
  is made under new hashes.
- A candidate typed `unrelated` is retained and visible on the developer page.
- "Keep all currently shown" states its count and requires confirmation.
- Only kept edges are surfaced; a pending candidate reaches no prompt.
- A shielded target is never scored and never surfaced while locked.
- After a full regeneration and an index rebuild, every kept decision survives
  with its reviewer and reason.
- `related_keys` carries key names and uuids only, respects the caps, and a
  path containing fence-like text cannot forge structure.
- `considered` and `used` are recorded against the decision revision, and
  neither changes an edge's status.
- The `RetrievalEvent` constraint migration accepts `qa_edge` and `dead_end` on
  both a fresh database and an upgraded one, while still rejecting unknown
  values.
- `mark_route_dead_end` is refused for a token the run never redeemed, and is
  deduplicated per run and revision.
- Developer-page probes write no live telemetry.

## Considered and declined

- **A single summary per edge.** The edge is bidirectional and each direction
  has a different destination, so one field would hand the scorer evidence
  about the wrong entry half the time.
- **Honouring any uuid the preceding step surfaced.** Plain uuid mode returns
  the full entry unfiltered, so a uuid held from an earlier step or turn
  reaches the same content ungated. That rule is telemetry, not authorisation;
  a single-use `route_token` is.
- **An optional `subjects` field.** Entries like `human.<person>.health` would
  silently become about `health`. Requiring it for graph eligibility trades
  operator effort for the absence of an invisible failure.
- **HMAC for pair identity.** The endpoint columns are stored anyway, so keying
  adds no protection from a database reader while tying every pair identity and
  tombstone to `SECRET_KEY` surviving rotation.
- **Reusing `apply_filter_scores` for routes.** Its small-list keep-all is
  correct for recall and fatal here: the ordinary route case is two or three
  candidates, all of which it keeps regardless of score.
- **Serving unreviewed routes.** Following returns a full untruncated entry
  with no relevance decision, so a wrong route injects an arbitrary private
  answer. Viable only after the uuid read carries a query and applies
  relevance.
- **Corpus-wide candidate enumeration.** `N²` candidates make a queue that is
  abandoned rather than drained. The *index* is corpus-wide and linear; the
  candidates are demand-scoped.
- **Generating from the touched entry's answer alone.** Cannot find a bridge
  that lives in the other entry, which is the motivating case exactly.
- **Membership labels without a role.** `listed_by` cannot distinguish the
  household where someone is a parent from the one where they are a child.
- **Multi-hop.** Needs a weighting scheme whose composition behaviour is
  demonstrated. `base / log(1 + frequency)` exceeds its base below frequency
  ~1.7, so a second hop could raise a path score rather than lower it.
- **Vector-neighbourhood candidates.** Noisiest generator, hardest candidates
  to judge, and demand-driven generation removes its coverage rationale.
- **Dropping `unrelated` candidates silently.** Makes typing-model false
  negatives invisible, which is the failure mode hardest to detect from
  outcomes.
- **Merging entries about the same subject.** A wrong edge is a weak route; a
  wrong merge is irreversible identity loss.
- **Permanent rejection across content revisions.** A pair judged unrelated
  under one text may be related under a replacement.
- **Letting the LLM propose pairs.** Spending a model on search over `N²` when
  deterministic generators cover the ground with better precision.
- **Storing edges in the JSONL.** The registry stays human-owned; derived data
  belongs in Postgres.
- **Surfacing target answer text.** It would bypass the recall filter and
  consume the observation budget the recalled facts need.

## Delivery sequence

0. **Run the experiment.** Record the outcome above. Three of its four results
   stop the work here or redirect it.
1. The `subjects` field with loader validation, the inverted mention index and
   `_normalize_subject`, as pure functions over a loaded registry. Unit-tested
   against fixtures with spaced, slugged and non-ASCII names. Nothing stored.
2. The three tables, the generation row with its lock and durable empty
   completion, demand-driven materialisation after a kept recall, and the
   typing call. Inspectable on `/memory/developer`. Nothing served.
3. The review queue: keep / reject / relabel / defer / bulk / hint.
4. The `route_scorer` binding with its local-only validation, its calibration
   fixtures, `apply_route_policy`, the `route_token`, and the `related_keys`
   block behind a setting. The lifecycle acceptance test passes here or the
   feature does not ship.
5. The `RetrievalEvent` constraint migration, `considered`/`used`, and
   `mark_route_dead_end`, then a go/no-go on evidence of `used` against a
   stated threshold.

Steps 1 and 2 are inert and cheap. Step 4 is where the design either works
against the live registry or is abandoned.

## Prior art

Checked against the agent-memory-atlas. Per-edge curation is not new: Nova-AI
gates every relation write and keeps a three-value status per relation,
core-memory runs an approval workflow with an approver and a reason, and
engram-alpha carries durable confirmed/approved/demoted anchors. What is
specific here is applying it to navigation between existing entries, generated
on demand.

| System | Contribution |
|---|---|
| Nova-AI | Confirmation before a relation is written; rejections that re-extraction cannot lift; rejected rows kept visible for audit |
| core-memory | Approval that survives an index rebuild, with approver and reason retained |
| npcpy | The review loop — approve / reject / edit / skip / defer with a tally — and retrieval that reads approved rows only |
| Argo | The failure that voids curation: a graph rebuilt wholesale on sync with no approver in the schema |
| Memsem | The adjacent failure: approval states in a table no read path joins |
| Graphiti | The risk avoided by never merging: entity resolution reshaping the graph |
| engram-alpha | "Exposure doesn't validate": retrieval stamps observability only |
| Omi, Memledger, Mnemosyne | Rejection the generator must consult, keyed on the value rather than identity alone |
| Logseq | The warning against unmarked automatic writes |
| Swafra | The consumption shape: rank, expand through edges, return best per source |

Patterns instantiated: **Gate the Expensive Path**, **Zero-LLM Capture**,
**Trust-State Machine**, **Rejected-Value Tombstone**, **Evidence Before
Belief**.

## Open questions

- **Whether the typing model earns its call.** A mention found in a household
  answer may be `lists_as_member` by construction, and the role may be readable
  from the surrounding text deterministically. If so, typing collapses to a
  rule and the operator reviews deterministic proposals. Measure on real
  candidates before building step 2's typing call.
- **Whether a reviewed summary is enough evidence to rank a route.** The scorer
  never sees the target's answer, by design. If a one-line summary proves too
  thin to separate two households that both mention the same person, the
  remaining options are a longer reviewed summary, question-shaped routes, or
  conceding that this case needs the person-level projection instead.
- **How many routes a well-connected entry accumulates.** Three per source is a
  guess. If a household generates fifteen plausible routes, route scoring
  carries far more weight than assumed and needs its own evaluation set.
