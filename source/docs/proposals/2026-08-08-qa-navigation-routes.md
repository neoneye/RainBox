# Q&A navigation routes: reaching an entry retrieval cannot find

An entry can be present, unshielded, correctly embedded, and still unreachable
by the question a person actually asks.

This document is in two parts. The first is an **experiment** that decides
which of two explanations is right, and three of its four outcomes retire most
of the second part. The second is the design to build **only if** the
experiment says a route between entries is what is missing.

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

## Two explanations

**Reach.** The target is retrievable in principle, and simply carries no
phrasing near the question. More phrasings per entry would close it. This is
the follow-up proposal's variant B, alias enrichment.

**Granularity.** The entry is a bundle of six people in one node, while the
questions asked of it are single-person and single-relation. The embedding of a
six-person household is a centroid close to no individual question — which is
exactly why bundle-shaped queries succeed and person-shaped ones fail. If so,
more phrasings paper over a node whose unit does not match the question's, and
the answer is a derived person-level projection rather than either aliases or
routes.

The two make opposite predictions, and one experiment separates them.

## The experiment

It answers two questions, and **both** must pass for alias enrichment to be the
fix:

- **Mechanism.** If the target carried a phrasing near the failing question,
  would retrieval find it?
- **Feasibility.** Can a generator produce that phrasing *unprompted*, from the
  entry's own content?

Feasibility carries more weight than it looks. If a person must write the
alias, the mechanism collapses into "add a question to the registry by hand",
which the operator can already do and which needs no design at all. Alias
enrichment is only interesting if generation is automatic.

### Phase 1 — feasibility

Read-only. No writes anywhere.

Take the target entry's answer. Make one structured call to a locally bound
model asking for up to five questions a person might ask that this answer
resolves. Record the output verbatim.

- **Passes** if one output is recognisably the failing question.
- **Fails** if the outputs are all bundle-shaped — "who are the children",
  "where did they live". This is the plausible failure, because bundle-shaped
  prose invites bundle-shaped questions.

A Phase 1 failure ends the experiment, and is itself the strong result: the
phrasing gap cannot be closed automatically from this data shape.

### Phase 2 — mechanism

Only if Phase 1 passes.

Probing retrieval with the *generated* phrasing proves nothing — it shows the
generated phrasing works. The alias must be stored and embedded, and then the
**original failing question** re-run through real retrieval. A "near match"
between the two is not evidence either: `_normalize_query`
([memory/seed_memory.py](../../memory/seed_memory.py)) lowercases, strips
trailing `?!.` and collapses whitespace. It does not equate paraphrases, so
exact-alias matching will not fire on a near miss.

Run it entirely in `rainbox_claude`:

1. Copy the overlay to a scratch directory and add the generated alias to the
   target entry.
2. Set `customize.dir` in the sandbox database only.
3. Populate, then query with the original failing question.

Production settings, registry, and embeddings are untouched, and nothing needs
restoring. Repointing the live `customize.dir` would instead mean two
repopulate cycles and a `qa.facts_invalidated_at` stamp that fires re-check
notices in rooms.

- **Passes** if the target appears as a retrieval candidate for the original
  question. Candidacy is the whole difference: today it is not a candidate.

### Phase 3 — collisions

A new alias competes for queries it should not win.

Re-run controls — questions about the adult, the elder, and the siblings — and
confirm each still resolves to the entry it resolved to before.

- **Fails** if any control now returns the wrong entry. A fix that repairs one
  question and breaks three is not a fix.

### Decision table

| Result | Reading | Consequence |
|---|---|---|
| Phase 1 fails | The generator cannot derive single-person questions from bundle prose | Evidence for granularity; specify a person-level projection. Neither aliases nor routes are warranted |
| 1 passes, 2 fails | Stored and still unreachable | The retrieval-side fix is exhausted; granularity again |
| 1 and 2 pass, 3 fails | Works but pollutes | Alias enrichment needs collision validation, not a simple adoption |
| All pass | Reach problem; phrasing closes it | Specify variant B and shelve the design below |

### Limits

This tests one entry against two questions — the relational phrasing and the
bare kinship term, which behaved differently in the probes. A clean result
means "this failure class, this entry", not "alias enrichment is sufficient".

The sandbox will not carry the operator's model-group bindings, so Phase 2
measures retrieval **candidacy** rather than the full recall-filter decision.
That is the right measurement here — the failure is that the entry never
becomes a candidate — but a pass shows the door opens, not that the filter
keeps it.

**Record the outcome in this document before building anything below.** It is
the premise the rest rests on.

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

An entry's subject keys come from its **last path segment**, which is already a
slug. Registered questions are *not* subject keys — normalising "Who is Ada
Lovelace?" yields `who is ada lovelace`, which matches no extracted token.
Where a path segment is a poor subject (an event, a place, a topic), the
registry gains an optional `subjects: [...]` field the operator can set; absent
it, the path segment stands alone.

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

The edge also carries a **reviewed one-line summary** of what the destination
offers — written by the typing model, corrected or replaced by the operator at
review. This is the evidence the route scorer needs; a path plus a label is too
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

Routes are scored against the current query before any are surfaced. Scoring
must **not** reuse `apply_filter_scores`
([agents/query_filter_router.py](../../agents/query_filter_router.py)): with
fewer than `TOP_K_FILTER` candidates that function sets `kept=True` for every
candidate regardless of score. That behaviour is correct for recall — an
over-aggressive scorer must not empty a small result set — and wrong here,
where the ordinary case is two or three routes and keeping them all is exactly
the failure requirement 2 exists to prevent.

`apply_route_policy` is absolute and fail-closed:

- A route is surfaced only if its relevance to the query meets a fixed
  threshold. There is no small-list exemption.
- Scoring input is the target path, the direction label with its role, and the
  reviewed summary.
- If the scorer is unavailable, times out, or errors, **no routes are
  surfaced**. Degrading to "show everything" would defeat the gate; degrading
  to silence costs only the feature.
- One call per turn, and only when a kept entry has at least one edge. Bound by
  the same timeout budget as the recall filter, on the same locally bound
  model.

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

Only reviewed edges are surfaced, so the untruncated read is of an
operator-approved destination. Two further constraints:

- The step that surfaces routes records their uuids in its observation data.
  A follow is honoured only when its uuid was surfaced by the immediately
  preceding step in the same run — the same binding the follow-up proposal uses
  for hint adoption. A model-supplied uuid that no route offered is an ordinary
  `memory_query`, not a route follow.
- Serving *unreviewed* routes would require `_query_memory_full` to carry the
  originating query and apply a relevance decision before returning. That work
  is a precondition of soft-gating, not an optimisation.

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
may propose, suggest a relabel, and mark stale; it may not move a kept edge
back to pending or revive a rejected pair under unchanged content. If either
endpoint's hash changes, the decision no longer applies to the text that
exists, and the pair becomes proposable again.

## Storage

```text
qa_edge_candidate
- pair_key           HMAC-SHA256 over the CANONICAL endpoint order
                     (uuids sorted), domain "qa-edge-pair"
- endpoint_a, endpoint_b       stored in canonical order
- bases              JSONB list of {generator, detail}
- sha_a, sha_b       runtime row hashes at proposal time
- index_fingerprint  digest of the subject-index and sibling state this
                     candidate set was derived from
- label_ab, label_ba, role     null before typing
- summary            one line from the typing model
- typing_verdict     typed | unrelated
- typed_by_uuid, policy_version
- queued_at

qa_edge_decision
- pair_key
- sha_a, sha_b       the hashes the decision was made against
- status             kept | rejected
- label_ab, label_ba, role, summary    as approved
- reviewed_by, reviewed_at, review_note
- created_at
UNIQUE (pair_key, sha_a, sha_b)
```

Identity is the **canonical** pair, so the same logical edge discovered from
either endpoint is one row. Without that, `HMAC(a,b)` and `HMAC(b,a)` are
different keys, demand from the two endpoints creates duplicates with reversed
labels, and a rejection in one orientation fails to block the other.

Decisions are **per content revision**, which is why they are a separate table
keyed on the endpoint hashes. A rejection under the old text is retained for
audit while a new candidate and a new decision exist under the new text; a
single row keyed on the pair alone cannot hold both.

`index_fingerprint` exists because candidate output depends on more than the
source entry. Adding a sibling, renaming another entry's path, or adding a
subject alias changes what the generators would produce while the touched
entry's own hash is unchanged. A candidate set whose fingerprint is stale is
recomputed on the next demand event.

Generation writes only to `qa_edge_candidate`; review is the sole writer of
`qa_edge_decision`. A re-run may add candidates and mark decisions stale. It
may not delete a kept decision. A graph rebuilt wholesale on sync — no
per-element lifecycle, no approver recorded — cannot demonstrate that any human
reviewed it, and the property is worth asserting across a full regeneration
*and* an index rebuild.

An edge serves only while both endpoint hashes match current runtime hashes and
both endpoints are visible. Deletion or a shield lock hides it immediately.

Both tables live in `db/models.py`, created by `init_db`, with `status`,
`typing_verdict` and the label vocabulary enforced by CHECK constraints.
RainBox already has active/candidate flows, rejected-value tombstones and a
review UI for claims; edge review reuses those states and that idiom.

## Privacy

The typing model receives entry content, which here means the operator's
private overlay: it must be explicitly bound and local, prompts and logs never
record raw entry text, an unshielded source is typed only against unshielded
targets, and cross-shield edges are refused.

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

`RetrievalEvent` with `target_type="qa_edge"`:

- `considered` — a route was surfaced. Recorded from the step's observation
  data.
- `used` — the next `memory_query` in the same run read a uuid that step
  surfaced. Computable because the surfacing step records the uuids.

Nothing further is claimed. `accepted` is not reused: it is defined against a
recall-filter decision and the uuid read makes none. A `resolved` signal would
require knowing which memories a reply drew on, and replies carry no citation
structure — inferring it from reply text is not a measurement. If route utility
needs a stronger signal than `used`, reply-level provenance has to be built
first, and that is a separate piece of work.

The **dead end** signal is an explicit action, not an inference: the assistant
may mark a followed route unhelpful, writing one row with the route's
`pair_key` and the step uuid. Dead-end counts order the review queue for
re-review. They change no edge's status.

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
- A candidate set is recomputed when the subject index or sibling set changes,
  even though the source entry's hash is unchanged.
- Two or three routes are scored and only the relevant one is surfaced —
  asserted against the route policy directly, since the recall filter's
  small-list keep-all would pass all of them.
- Route scoring unavailable surfaces no routes.
- A route whose uuid was not surfaced by the preceding step is not treated as a
  follow.
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
- `considered` and `used` are recorded; neither changes an edge's status.
- Developer-page probes write no live telemetry.

## Considered and declined

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
1. The inverted mention index and `_normalize_subject`, as pure functions over
   a loaded registry. Unit-tested against fixtures with spaced, slugged and
   non-ASCII names. Nothing stored.
2. The two tables, demand-driven candidate materialisation after a kept recall,
   and the typing call. Inspectable on `/memory/developer`. Nothing served.
3. The review queue: keep / reject / relabel / defer / bulk / hint.
4. `apply_route_policy`, route scoring, and the `related_keys` block behind a
   setting. The lifecycle acceptance test passes here or the feature does not
   ship.
5. Telemetry and the dead-end action, then a go/no-go on evidence of `used`
   against a stated threshold.

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
