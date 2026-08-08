# Q&A navigation routes: reaching an entry retrieval cannot find

An entry can be present, unshielded, correctly embedded, and still unreachable
by the question a person actually asks. This proposes a route from the entry
retrieval *does* find to the entry that holds the answer — generated on demand
for entries that real queries touch, approved by the operator before it serves,
and ranked against the question being asked.

Two words are used precisely throughout. An **edge** is the stored, reviewed
relation between two entries, held once and labelled for each direction. A
**route** is that edge traversed in one direction for one query. Edges are what
the operator approves; routes are what the assistant is shown.

All examples are fictional. No entry text, path label, or person from an
operator overlay is reproduced here.

## Step 0: the experiment that may make this unnecessary

**Do this before writing any code in this document.** A cheaper mechanism may
close the observed failure, and if it does, most of what follows should never
be built.

The failing case, stated neutrally. Two entries are household-shaped:

```text
human.family.<elder>     parents: <elder>, <partner>;  children: ..., <adult>
human.family.<adult>     parents: <adult>, <spouse>;   children: ...
```

Asked "who is `<adult>`'s mother", retrieval returns `human.family.<adult>` —
where `<adult>` *is* the parent. The answer is in `human.family.<elder>`, where
`<adult>` is listed as a child, and the adult's entry names the elder nowhere.

The bridging fact — that the elder's household lists the adult as a child —
lives inside the elder entry's own answer. That is exactly the material the
follow-up proposal's **variant B (alias enrichment)** reads when generating
extra question phrasings for an entry. And retrieval already does exact-alias
matching, so a generated phrasing is not merely another signal: it is the
strongest one available.

### Protocol

1. Take the target entry's answer. Run one structured call against the local
   `memory_filter`-bound model asking for up to five natural questions a person
   might ask that this answer resolves. No storage, no pipeline.
2. Check whether any generated phrasing is a near-match for the real failing
   question.
3. Independently, probe `/memory/api/developer/query` with the failing question
   *and* with each generated phrasing, recording whether the target entry
   appears as a candidate and whether the recall filter keeps it.

### Decision rule

- **The generator emits a phrasing that retrieves the target** → alias
  enrichment closes this failure class. Specify variant B's storage and sync
  (the follow-up proposal marks it deliberately not implementation-ready) and
  stop here. Nothing below is needed for this case.
- **The generator emits nothing that reaches the target** → the bridge cannot
  be recovered from the target's own content, and a route between entries is
  justified. Continue.

Record the outcome in this document before proceeding; it is the premise
everything after it rests on.

## What a route has to do

Three requirements fall out of the failure. Each one is load-bearing, and a
design that leaves any of them to a later phase does not close the case:

1. **It must run in the direction of travel.** Retrieval lands on the *adult*
   entry. The route must lead from there to the elder. Content-derived edges
   naturally point the other way — the elder's answer names the adult — so
   direction is a property the design must handle, not defer.
2. **It must be ranked against the question.** A household has neighbours for
   spouses, children, health, projects, locations and events. Static type
   weights cannot know that "mother" selects one of them. An unranked route
   list crowds out the needed destination.
3. **Following it must not bypass relevance.** `memory_query {"uuid": ...}`
   resolves through `_query_memory_full`, which applies a shield check and
   returns the entry **in full, untruncated**, with `QueryContext(query="")` —
   there is no query at that seam and therefore no relevance decision. A route
   that can be followed is a route that can inject a large private answer into
   context without any filter having judged it.

## Design

### Generation is demand-driven, not corpus-wide

Do not enumerate the registry. For `N` entries there are `N(N-1)` ordered
pairs, and a review queue built from all of them is abandoned rather than
drained.

Candidates are generated for **one entry at a time, when a real recall touches
it**: after a successful `memory_query`, each kept seed entry with no current
candidate set is enqueued for generation. The unanswered-query events from the
follow-up proposal's variant A raise priority. Entries nobody queries are never
processed, and the review queue is bounded by what the operator actually asks
about.

This inverts the usual order and is the main reason this version is buildable:
the first review session covers a handful of entries, not a thousand pairs.

### Candidate generation, specified

Two generators run for a source entry. Neither calls a model.

**Subject mention.** Find proper-noun-like tokens in the source entry's answer,
and resolve each against the registry's subject index.

The subject index is built once per KB load: for every entry, its subject keys
are the last path segment plus the normalised form of each registered question,
both passed through `memory.seed_memory._normalize_query` (the same normaliser
the exact-alias table already uses). Path segments are slugs — a concatenated,
lowercased name — so a candidate token is normalised the same way before
lookup: casefold, strip internal whitespace and punctuation, keep non-ASCII
letters. `Ada Lovelace` and `adalovelace` collapse to one key, as do
`Kjeld Åström` and `kjeldåström`.

Token extraction is capitalisation-based with a stop-list, and it is
deliberately recall-oriented: a token that resolves to no subject is dropped
silently, so over-extraction is cheap and under-extraction is the only real
failure. Multi-word names are tried longest-first. A token resolving to more
than one entry produces a candidate for each.

**Path proximity.** Entries sharing a parent namespace, and direct
ancestor/descendant pairs. Capped per parent so a busy namespace cannot flood
the queue.

Vector-neighbourhood generation is **not** in this version. It is the noisiest
source, it produces the candidates hardest to judge, and demand-driven
generation removes the coverage argument that justified it.

### The edge is one row, traversable both ways

An edge stores the pair once with a label for each direction:

```text
source_qa_id   human.family.<elder>
target_qa_id   human.family.<adult>
forward_label  lists_as_member
reverse_label  listed_by
```

Traversal from a matched entry follows edges in either direction and reports
the label for the direction travelled. The motivating case works because the
adult entry is reached by `reverse_label`, and that is a property of the read
path rather than an open question.

Labels are a small closed vocabulary: `lists_as_member` / `listed_by`,
`mentions` / `mentioned_by`, `same_subject` (symmetric), `narrower` / `broader`
for path descent. A typing model assigns the label pair and a one-line
justification; it never proposes pairs, only judges what the generators found.
`unrelated` drops the candidate before the operator sees it.

`same_subject` is an edge, never a merge. Two entries about one subject stay
two entries, and nothing here rewrites the registry to unify them.

### Only reviewed edges serve

An edge serves after the operator keeps it. There is no pending-serves mode.

The reason is requirement 3: following an edge returns a full untruncated entry
with no relevance decision, so a wrong route is not a wasted lookup — it is an
arbitrary private answer injected into context. That cost does not justify
serving unreviewed routes. Demand-driven generation is what makes a hard gate
affordable; without it, the gate would starve the feature.

Review actions: **keep**, **reject** (with a reason), **relabel**, **defer**,
**keep all for this entry**, and **suggest more with a hint** — a free-text
steer that re-runs typing over the entry's remaining candidates. Show a running
tally.

A human decision is monotonic *for the content it was made against*. A re-run
may propose, suggest a relabel, and mark stale; it may not move a kept edge
back to pending or revive a rejected pair. But the decision is scoped to the
endpoint hashes it was made under: if either entry's content hash changes, the
edge goes stale and a rejected pair becomes proposable again, because the text
the operator judged no longer exists. Rejection is permanent against
re-derivation, not against revision.

Rejections are consulted by the **generator**, keyed on
`(pair_key, source_sha, target_sha)`, so a regeneration over unchanged content
proposes a rejected pair nowhere. Rejected rows stay readable on the developer
page: they are what an audit needs, and the training signal for tuning the
typing prompt.

### Surfacing is ranked against the question

After the recall filter keeps its entries, collect the kept entries' reviewed
edges, then score each candidate route **against the current query** before
showing any of them.

Scoring reuses the existing machinery rather than inventing a second scorer:
each route becomes a row for `agents.query_filter_router` — the same
`FilterDecision` schema and `apply_filter_scores` policy already used for
recall — with the route rendered as its target path plus the direction label.
Routes the scorer drops are not surfaced. This is what makes "mother" select
the household route over the health and project routes, and it is the
requirement static weights could not meet.

Surviving routes render as:

```text
<related_keys note="routes from the entries above; key names only">
<uuid>  human.family.<elder>   listed_by
</related_keys>
```

Capped at three per kept source and six total.

**A path label is content.** `human.<person>.health.<condition>` discloses a
fact by existing. Surfacing routes therefore respects shields exactly as entry
content does, and route scoring runs *before* exposure so an off-topic path is
never shown. Sanitising the label prevents prompt-structure forgery; it does
not make the label neutral, and this document does not claim it does.

### Following a route

Following uses the existing `{"uuid": ...}` form. Because only reviewed edges
are surfaced, the untruncated read is of an operator-approved destination.

If a later version serves unreviewed routes, that read path must change first:
`_query_memory_full` would need the originating query plumbed through
`QueryContext` and a relevance decision applied before returning. That work is
a precondition of soft-gating, not an optimisation.

The decide prompt gains one line: when the answer concerns someone reached
through another entry, check the related keys before reporting that nothing is
stored. This composes with the bounded empty-read retry already in the loop.

### Multi-hop is out of scope

Depth is 1. Two-hop traversal needs a weighting scheme whose composition
behaviour is established rather than asserted, and there is no evidence yet
that one hop is insufficient. Revisit when the telemetry below shows routes
being followed and single hops falling short.

## Storage

```text
qa_edge_candidate
- pair_key          HMAC-SHA256 over the ordered pair, domain "qa-edge-pair"
- source_qa_id, target_qa_id
- bases             JSONB list of {generator, detail}
- source_sha, target_sha    runtime row hashes at proposal time
- forward_label, reverse_label   null before typing
- justification     one line from the typing model
- typed_by_uuid, policy_version
- queued_at

qa_edge
- pair_key
- source_qa_id, target_qa_id
- forward_label, reverse_label
- status            kept | rejected | stale
- reviewed_by, reviewed_at, review_note
- source_sha, target_sha    the hashes the decision was made against
- created_at
```

Generation writes only to `qa_edge_candidate`; publishing is the sole writer of
`qa_edge`. A re-run may add candidates and mark edges stale. It may not delete
a kept edge, and a graph rebuilt wholesale on sync — no per-element lifecycle,
no approver recorded — cannot demonstrate that any human reviewed it. The
property is worth asserting across a full regeneration *and* an index rebuild.

An edge serves only while both endpoint hashes match current runtime hashes and
both endpoints are visible. An endpoint edit marks it stale and re-queues the
pair; deletion or a shield lock hides it immediately.

Both tables live in `db/models.py`, created by `init_db`, with `status` and the
label vocabulary enforced by CHECK constraints. RainBox already has
active/candidate flows, rejected-value tombstones and a review UI for claims;
edge review reuses those states and that UI idiom rather than growing a
parallel vocabulary.

## Privacy

The typing model receives entry content, which here means the operator's
private overlay. The typing model must be explicitly bound and local; prompts
and logs never record raw entry text; an unshielded source is typed only
against unshielded targets and cross-shield edges are refused.

**Overlay generation stays blocked until an authenticated operator control
plane exists.** The follow-up proposal states the reason and it applies
unchanged here: local providers reduce disclosure but do not establish *who
authorised the run*, and "started on localhost" is not an authorisation. Since
this feature has no value at upstream-only scope, the honest consequence is
that it does not ship before that control plane does. Step 0 and the read-path
work are unaffected.

## Telemetry

`RetrievalEvent` with `target_type="qa_edge"`:

- `considered` — a route was surfaced.
- `used` — the next `memory_query` in the same run read that route's uuid.
- `resolved` — the assistant's reply cited the followed entry.

`accepted` is deliberately not reused: it is defined against a recall-filter
decision, and the uuid read makes none. `resolved` is a distinct, honestly
named signal.

Let the assistant report a followed route as a **dead end** when the
destination did not help; a volunteered dead end is worth more than a silence
that looks identical to "never surfaced".

Telemetry may order the generation and review queues. It may not change an
edge's status. Exposure must never promote, or a recurring query certifies its
own routes.

## Tests and acceptance criteria

The first test is the motivating query, end to end, and it is the one that
matters:

- Given the two household entries and a kept edge between them, asking the
  relational question retrieves the adult entry, surfaces the elder entry as a
  route, follows it, and produces an answer naming the elder. A test asserting
  only that a forward candidate exists does not count.

Then:

- Traversal reaches the elder from the adult via `reverse_label`; direction is
  covered by a test, not by an open question.
- A route is scored against the current query before exposure, and an
  off-topic route for a matched entry is not surfaced.
- A shielded target never appears as a route while its shield is locked.
- Only `kept` edges are surfaced; a pending candidate reaches no prompt.
- Subject extraction resolves a spaced, capitalised, non-ASCII name to a
  concatenated lowercase path slug, and a token resolving to nothing is dropped
  without error.
- A token resolving to two entries produces two candidates.
- Generation runs only for entries a recall touched; a never-queried entry has
  no candidates.
- A rejected pair is invisible to the generator while both endpoint hashes are
  unchanged, and becomes proposable again when either changes.
- After a full regeneration and an index rebuild, every kept edge is still
  kept with its reviewer and reason.
- Editing an endpoint marks its edges stale; deleting one hides them.
- `related_keys` carries key names and uuids only, respects the caps, and a
  path containing fence-like text cannot forge structure.
- `considered` / `used` / `resolved` are recorded, and none changes an edge's
  status.
- Developer-page probes write no live telemetry.

## Considered and declined

- **Serving unreviewed routes.** Following a route returns a full untruncated
  entry with no relevance decision, so a wrong route injects an arbitrary
  private answer rather than costing one lookup. Viable only after
  `_query_memory_full` carries a query and applies relevance.
- **Corpus-wide generation.** `N²` candidates produce a queue that is abandoned
  rather than drained. Demand-driven generation makes the hard gate affordable.
- **Multi-hop in v1.** Needs a weighting scheme whose composition behaviour is
  demonstrated. `base / log(1 + frequency)` was proposed and does not qualify:
  it exceeds the base below frequency ~1.7, so a second hop can raise a path
  score rather than lower it.
- **Static type-and-frequency ranking.** Frequency suppresses hubs; it does not
  know which neighbour answers *this* question. Query-conditioned scoring does.
- **Coarse types carrying no role.** `member_of` alone cannot distinguish the
  household where a person is a parent from the one where they are a child,
  which is the whole distinction the motivating case turns on. Direction labels
  carry it instead.
- **Vector-neighbourhood candidates.** Noisiest generator, hardest candidates
  to judge, and demand-driven generation removes its coverage rationale.
- **Merging entries about the same subject.** A wrong edge is a weak route; a
  wrong merge is irreversible identity loss.
- **Permanent rejection across content revisions.** A pair judged unrelated
  under one text may be related under a replacement. Rejection is keyed to the
  endpoint hashes it was decided against.
- **Letting the LLM propose pairs.** Spending a model on search over `N²` when
  deterministic generators cover the ground with better precision.
- **Storing edges in the JSONL.** The registry stays human-owned; derived data
  belongs in Postgres.
- **Surfacing target answer text.** It would bypass the recall filter and
  consume the observation budget the recalled facts need.

## Delivery sequence

0. **Run the Step 0 experiment.** Record the result here. If alias enrichment
   reaches the target, specify variant B instead and stop.
1. Subject index and the two generators, with the normalisation rules above.
   Pure functions over a loaded registry, unit-tested against fixtures with
   spaced, slugged and non-ASCII names. Nothing stored.
2. The two tables, demand-driven enqueue after a kept recall, and the typing
   call. Inspectable on `/memory/developer`. Nothing served.
3. The review queue: keep / reject / relabel / defer / bulk / hint.
4. Query-conditioned route scoring and the `related_keys` block behind a
   setting, serving kept edges only. The end-to-end acceptance test passes
   here or the feature does not ship.
5. Telemetry and the dead-end signal, then a go/no-go on evidence of
   `resolved`, not of `used`.

Steps 1–2 are inert and cheap. Step 4 is where the design either works against
the live registry or is abandoned.

## Prior art

Checked against the agent-memory-atlas. Per-edge curation is not new: Nova-AI
gates every relation write and keeps a three-value status per relation,
core-memory runs an approval workflow with an approver and a reason, and
engram-alpha carries durable confirmed/approved/demoted anchors. What is
specific here is applying it to *navigation between existing entries*,
generated on demand rather than over a corpus.

| System | Contribution |
|---|---|
| Nova-AI | Confirmation before a relation is written; rejections that re-extraction cannot lift; rejected rows kept visible for audit |
| core-memory | Approval that survives an index rebuild, with an approver and reason retained |
| npcpy | The review loop — approve / reject / edit / skip / defer with a tally — and retrieval that reads approved rows only |
| Argo | The failure that voids curation: a graph rebuilt wholesale on sync with no approver in the schema |
| Memsem | The adjacent failure: approval states in a table no read path joins |
| Graphiti | The risk avoided by never merging: entity resolution reshaping the graph |
| engram-alpha | "Exposure doesn't validate": retrieval stamps observability only |
| Omi, Memledger, Mnemosyne | Rejection the generator must consult, keyed on the value rather than on identity alone |
| Logseq | The warning against unmarked automatic writes |
| Swafra | The consumption shape: rank, expand through edges, return best per source |

Patterns instantiated: **Gate the Expensive Path**, **Zero-LLM Capture**,
**Trust-State Machine**, **Rejected-Value Tombstone**, **Evidence Before
Belief**.

## Open questions

- **Whether the typing model earns its call.** Direction labels may be
  derivable deterministically from the generator basis — a subject mention in a
  household answer is `lists_as_member` by construction. If so, the typing
  model can be dropped and the operator reviews deterministic proposals.
  Measure on real candidates before building the typing call in step 2.
- **How many routes a household entry actually accumulates.** The cap of three
  per source is a guess. If a well-connected entry generates fifteen plausible
  routes, query-conditioned scoring carries more weight than assumed and needs
  its own evaluation set.
