# Q&A relationship graph: curated edges and neighbour surfacing

Treat the Q&A registry as a graph. Nodes are entries; edges are relationships
between them, derived automatically and adjudicated by the operator.
`memory_query` surfaces a matched entry's neighbours as key names, so the
assistant can navigate to an entry that retrieval alone could not reach.

All examples in this proposal are fictional. No entry text, path label, or
person from an operator overlay is reproduced here.

## The failure this exists to fix

An entry can be present, unshielded, correctly embedded, and still unreachable
by the question a person actually asks.

The observed case, stated neutrally. Two entries are *household*-shaped — each
holds a couple plus their children:

```text
human.family.<elder>     parents: <elder>, <partner>;  children: ..., <adult>
human.family.<adult>     parents: <adult>, <spouse>;   children: ...
```

Asked "who is `<adult>`'s mother", retrieval returns `human.family.<adult>` —
the entry where `<adult>` *is* a parent. The answer lives in
`human.family.<elder>`, where `<adult>` appears as a child. The relational
phrasing embeds toward the wrong one of the two, and `human.family.<adult>`
does not name `<elder>` anywhere, so there is nothing to follow.

Measured against a live registry: the relational phrasing does not retrieve the
target entry at all, while the name with family words around it does, every
time. This is a reachability problem, not a ranking problem — no amount of
re-scoring fixes an entry that is not a candidate.

Two properties of the failure drive the design:

- The information needed to bridge the two entries exists in exactly one of
  them, and only inside its answer text.
- The bridge is a *relationship*, not a similarity. No embedding of
  "`<adult>`'s mother" is close to an entry written from `<elder>`'s point of
  view.

## Relationship to the follow-up proposal

`2026-07-21-qa-followup-questions.md` also builds a graph over the same nodes.
It is a different graph and the two are complementary:

|                 | Follow-up edges (that proposal)          | Relationship edges (this one)              |
|-----------------|------------------------------------------|--------------------------------------------|
| An edge means   | "after A, question Q is answerable by B"  | "A stands in relation R to B"              |
| Shape           | question-shaped                           | entity-shaped                              |
| Validated by    | an LLM validator, automatically           | derived, then adjudicated by the operator  |
| Direction       | derived from generated questions           | derived from content, typed by an LLM      |
| Multi-hop       | out of scope ("chains emerge")            | a first-class read path                    |

That proposal's machinery is reused rather than reinvented: runtime content
hashes for freshness, the scope/shield visibility rules, the sanitised
structured-call helpers, and the `RetrievalEvent` telemetry vocabulary. Its
variant A (mining unanswered queries) is a **prerequisite** here, because it
supplies the demand signal that orders the review queue.

Its variant B (alias enrichment) attacks the same failure from the retrieval
side, by giving the target entry more phrasings. B and this proposal are not
alternatives: B raises the chance the right entry is retrieved directly, this
one gives a route when it is not. B has no answer when the bridging fact lives
only in the other entry, which is precisely the observed case.

## Goals

- Let the assistant reach an entry that the user's phrasing cannot retrieve.
- Keep every edge operator-reviewable, and every operator decision durable and
  final against later automation. Reviewed and unreviewed edges are always
  distinguishable; rejection is the only state that removes.
- Keep the operator's review effort sub-quadratic in the node count, and
  ordered by observed demand.
- Serve useful routes before the review queue is drained.
- Fail closed on visibility: an edge is usable only while both endpoints are.

## Non-goals

- Editing either JSONL file. Edges are derived data in Postgres; the registry
  stays human-owned. The app drafts, never writes.
- Modelling truth. An edge asserts "these two entries are related in way R",
  not that a proposition holds.
- Replacing retrieval. Neighbours are surfaced *after* a match; an entry with
  no match still contributes nothing.
- Restructuring household entries into per-person nodes. That is the operator's
  data shape to choose, and the design must work with entries as they are.

## The N² constraint

With `N` entries there are `N(N-1)` ordered pairs — for a few hundred entries,
tens of thousands. That number rules out two designs immediately: an LLM cannot
score all pairs, and a human certainly cannot review them.

The rest of the design follows from that single constraint. Candidates are
proposed by something free, the LLM is spent on judgement rather than search,
and the queue is ordered so that stopping early still leaves the useful edges
done.

## Candidate generation

Three generators run over the registry. None calls a model. Each proposes
directed candidate pairs with a `basis` recording why.

**Subject mention.** Entry A's answer contains a proper noun that is the
*subject* of entry B. Subjects come from the last path segment and from B's
registered questions. This is the most precise of the three, and the one that
catches the motivating case: the elder household's answer names the adult, who
is the subject of another entry, yielding `<elder> → <adult>`.

**Path proximity.** Entries sharing a parent namespace, and entries that are an
ancestor or descendant of one another. `human.family.<a>` and
`human.family.<b>` are siblings; `human.<p>.health` is a child of `human.<p>`.
Cheap, and it encodes grouping the operator already expressed by naming things.
Capped per parent, because busy namespaces would otherwise flood the queue.

**Vector neighbourhood.** Top-K entry-to-entry similarity using the embeddings
already in the pgvector table. It exists to catch relationships the other two
structurally cannot see. It is also the noisiest, so its candidates carry the
lowest base weight and sort last for review.

A pair proposed by several generators carries all their bases and ranks higher.
Generation is deterministic and idempotent: same registry, same candidates.

## Typing

A candidate becomes an edge only with a **type**. The vocabulary is small and
closed:

```text
mentions_person     A's content names the person B is about
member_of           A is a grouping (household, club, project) that lists B
same_person         A and B are about the same subject, different facets
co_located          A and B concern the same place
co_temporal         A and B concern the same period or event
same_topic          A and B are facets of one subject that is not a person
```

Coarse types are deliberate. The consumer is a language model that will read
the target entry anyway, so the type does not have to capture the full
semantics — it only has to be precise enough to set a base weight and to let
the assistant judge whether a route is worth taking. A fine-grained vocabulary
(`mother_of`, `employer_of`, …) makes every typing call harder and every new
type another base weight to calibrate, while adding nothing the assistant
cannot read for itself.

`member_of` is directional and carries a role note (`lists B as a member`)
rather than splitting into `parent_of`/`child_of`. Household entries in a
personal registry routinely list six people in one answer; per-person role
edges would demand exactly the data restructuring this design avoids.

`same_person` is an **edge, not a merge**. Two entries about one subject stay
two entries, and nothing here rewrites the registry to unify them. A wrong edge
adds a weak path; a wrong merge destroys two identities irreversibly, and
entity resolution is the stated top risk of the most mature graph system in the
atlas.

An LLM assigns the type and a one-line justification for each candidate. It
never proposes pairs — it only judges what the free generators found. When it
cannot type a candidate confidently it returns `unrelated`, which drops the
candidate before the operator ever sees it.

## Multi-hop traversal

Once `A —R1→ B` and `B —R2→ C` exist, `A ⇝ C` is a path: computed by traversal
at query time, costing nothing to store and nothing extra to review. Two-hop
pairs grow quadratically while edges do not, so multi-hop is a read-path
concern rather than an authoring one.

What needs deciding is which hops are worth taking, and the answer is a weight
rather than a rule. Place and time relate everything to everything eventually,
but the problem is *frequency*, not *type* — a place named in two entries is
informative, and the same relation to a place named in forty is not. So an edge
carries a weight inverse to how many entries share the thing that created it:

```text
weight(edge) = base(type) / log(1 + entries_sharing_the_basis)
```

Type sets the base — `member_of` and `mentions_person` start high,
`co_temporal` and `same_topic` low — and frequency does the discriminating. A
hub collapses toward zero without a rule naming it, and a new type needs a base
weight rather than a row in a compatibility table.

A path scores as the product of its edge weights, so each hop widens the field
and adds cost, and only coherent low-cost paths stay competitive. Two-hop
neighbours compete with one-hop neighbours on score rather than being admitted
by category. Traversal is bounded at depth 2, and each neighbour's hop count is
surfaced.

The decay schedule needs calibrating against a real registry rather than chosen
for tidiness. A schedule that puts a plausible two-hop path below the cut makes
multi-hop true only on paper, so the acceptance tests pin a two-hop case that
must appear in the block.

## Review

### The queue surfaces; it does not gate

A pending edge is **servable**, marked unreviewed. Keeping it promotes it to
reviewed and raises its weight. Rejecting it is the only action that removes
it.

The alternative — publish nothing until the operator has kept it — means the
feature does nothing until roughly a thousand decisions have been made, and a
queue that must be finished before anything works is a queue that gets
abandoned. The queue exists to surface what needs review, not to withhold.

Blast radius is what makes this safe here, and it is what separates an edge
from a claim. A wrong claim is asserted to the user as fact. A wrong edge costs
one wasted lookup: it surfaces a key name, the assistant may follow it, and the
recall filter still decides what content reaches the model. Systems that
adjudicate *memories* are right to hard-gate; this one adjudicates *routes*.

Reviewed and unreviewed edges must be distinguishable everywhere they appear —
in the observation, in the UI, and in any export. Automatic writes that land
unmarked become indistinguishable from the operator's own, and the distinction
cannot be reconstructed later.

### The review loop

The operator reviews candidates in a queue, not a graph. Each row shows the two
entries, the proposed type, the generator basis, and the LLM's one-line
justification. Actions:

- **Keep** — promote to reviewed; the edge gains weight and stops being marked
  unreviewed.
- **Reject** — the only action that removes. Recorded with the rejecter and a
  reason.
- **Retype** — keep the pair, change the type.
- **Skip / defer** — pass without deciding; deferred rows return later, skipped
  ones sink.
- **Keep all for this entry** — bulk-accept the remaining candidates of a
  source the operator has come to trust mid-review.
- **Suggest more, with a hint** — free-text steer ("look for shared
  workplaces") that re-runs typing over that entry's remaining candidates.

Defer and bulk-accept are what make a long queue survivable: a loop offering
only keep and reject stalls on the first row the operator is unsure about. Show
a running tally.

### A human decision is monotonic

A person's decision outranks any later automatic match, and no automatic pass
may downgrade it. A re-run may propose new candidates, suggest a retype, and
mark edges stale; it may not move a reviewed edge back to pending, and it may
not revive a rejected pair. Automation proposes, a person decides, and the
asymmetry only runs one way.

### Rejection is enforced at generation

A rejected pair must be invisible to the *candidate generator*, not merely
absent from the published set. The common failure is a rejection that only the
publisher consults: the source material is retained, re-extraction proposes the
same pair, and it re-enters as a fresh candidate — or a dedup lookup filters on
a status that the rejection path never writes, so the rejected value is
recreated rather than blocked.

The rejection is therefore pinned by `pair_key`, a key derived from the pair
itself, and generation consults it before proposing. A full regeneration
proposes a rejected pair nowhere.

Rejected edges stay inspectable while unusable. A person auditing the graph
needs to see what the system refuses to use, and the rejections are the
training signal for tuning the typing prompt. This is distinct from staleness:
a *stale* edge was valid and its endpoint changed, a *rejected* edge should
never have existed. Both are retained; only the first can return on its own.

### Order by demand, without letting exposure certify

The queue sorts by recent retrieval activity on the source entry, using the
unanswered-query and verdict telemetry from the follow-up proposal's variant A,
so that stopping halfway leaves the queried half done.

Demand ordering reads *query* activity on the entry, never adoption of the edge
itself, and no telemetry event may change an edge's weight or status. A
surfaced edge gets followed, which would raise its own source's activity, which
would surface it more: exposure may order the queue, but it must never promote,
or a broad recurring query ends up certifying its own neighbours.

The review surface lives on `/memory/developer`, beside the existing retrieval
inspection panels; it is operator tooling, not a user-facing page.

## Consumption

After the recall filter keeps its entries, `memory_query` appends a neighbour
block outside `recalled_memory`:

```text
<related_keys note="links from the entries above; key names only, not facts">
<uuid>  human.family.<elder>     member_of, 1 hop, reviewed
<uuid>  human.<other>.health     same_person, 2 hops, unreviewed
</related_keys>
```

Each row is marked `reviewed` or `unreviewed`, so the assistant can weigh a
route the operator has never seen. Key names and uuids only — never target
answer text, which would smuggle unfiltered content past the recall filter and
blow the observation budget. The assistant follows a neighbour with the
`{"uuid": ...}` form of `memory_query` it already has.

Caps mirror the follow-up proposal's hint block: three per kept source, six
total, deduplicated, one-hop before two-hop. The block is sanitised with the
same untrusted-data fencing as recalled memory, since path labels are operator
text.

The decide prompt gains one line: when the answer is about someone reached
through another entry, the related keys are where to look before reporting that
nothing is stored. This closes the loop with the bounded empty-read retry
already in the assistant, which currently reformulates blindly.

## Storage

```text
qa_edge_candidate
- pair_key          keyed digest of (source_qa_id, target_qa_id)
- source_qa_id
- target_qa_id
- bases             JSONB list of {generator, detail}
- source_sha        runtime row hash of the source at proposal time
- target_sha        runtime row hash of the target at proposal time
- proposed_type     from the typing model; null before typing
- justification     one line from the typing model
- typed_by_uuid     model that assigned the type
- policy_version
- queued_at

qa_edge
- pair_key
- source_qa_id, target_qa_id, type
- status            pending | kept | rejected | contested
- reviewed_by       null while pending; set once and never cleared
- reviewed_at
- review_note       the rejecter's reason, retained for audit
- weight            derived; type base over basis frequency
- source_sha, target_sha
- created_at
```

`status` lives on the edge rather than only on the candidate, because a pending
edge is servable: the read path filters `status != 'rejected'` and marks
anything not `kept` as unreviewed. A rejected row is retained rather than
deleted — it is what stops the pair being proposed again, and what an operator
auditing the graph needs to see.

Freshness follows the same rule as the follow-up tables: an edge is servable
only while both endpoint hashes match the current runtime hashes. An endpoint
edit hides its edges and re-queues the pair for typing; it does not silently
keep asserting a relationship derived from text that changed. Deletion or a
shield lock hides the edge immediately.

**Regeneration must never overwrite an operator decision, and the schema must
record who made it.** A curated graph that a routine resync flattens is not
curated, and a graph rebuilt wholesale on every sync — with no per-element
lifecycle and no approver in the schema — cannot demonstrate that any human
ever reviewed it. Hence the `reviewed_by`/`reviewed_at` stamp, and hence
generation only ever inserts into `qa_edge_candidate` while publishing is the
sole writer of `qa_edge`. A re-run may add candidates and mark edges stale; it
may not delete a kept edge or resurrect a rejected pair. The property is worth
asserting end to end, across a full regeneration *and* an index rebuild.

The companion trap is building the review machinery and never reading it —
approval states in a table no read path joins. The delivery sequence below is
ordered so consumption ships first for exactly this reason.

RainBox already has this vocabulary for claims: active/candidate flows, a
five-actor trust model, rejected-value tombstones, and a review UI. Edge review
should reuse those states and that UI idiom rather than growing a parallel
approval system with different words for the same thing.

`pair_key` is HMAC-SHA256 over the ordered pair with the Flask `SECRET_KEY`,
domain-separated `qa-edge-pair`, consistent with the digest convention already
established.

Both tables are declared in `db/models.py` and created by `init_db`, with
`status` and `type` enforced by CHECK constraints — so adding a type later is a
constraint migration, not just a new Python string.

## Privacy

The typing model receives entry content, which for this registry means the
operator's private overlay. That is the same boundary the follow-up proposal
draws, and the same conclusions hold: the typing model must be explicitly bound
and local by default, prompts and logs must never record raw entry text, and
candidates must respect shield visibility — an unshielded source is typed only
against unshielded targets, and cross-shield edges are rejected outright.

One difference is worth stating plainly. The follow-up proposal defaults to
upstream-only scope and gates overlay processing behind an authenticated
control plane. This design has **no value at upstream scope**: generic
published entries have few interesting relationships, and every case that
motivates the feature lives in the overlay. So this feature is overlay-scoped
from the start and inherits that authentication requirement, rather than
pretending an upstream-only version is useful. Until that control plane exists,
generation runs only when explicitly started by the operator on localhost, and
that limitation belongs in the settings copy.

## Telemetry

Reuse `RetrievalEvent` with `target_type="qa_edge"`:

- `considered` when an edge is included in a `related_keys` block.
- `used` when the next `memory_query` in the same run reads one of the uuids
  that block offered.
- `accepted` when that read returns an entry the recall filter keeps.

The `considered → used → accepted` funnel is the go/no-go signal, exactly as
for follow-up hints. A neighbour block that is shown constantly and followed
rarely is prompt budget being burned, and should be cut. Distinguish the two
failure modes: never followed means the block is not useful; followed but not
kept means the edges are wrong.

Add one signal the funnel cannot infer: let the assistant mark a followed edge
a **dead end** when the target turned out not to help. A volunteered dead end
is worth more than a silence that looks identical to "never surfaced", and
dead-end counts feed the queue as re-review candidates.

None of these events may change an edge's weight or status. They are
observability.

## Prior art

Checked against the agent-memory-atlas. Graph-backed memory is well trodden,
and so is curating one — Nova-AI gates every relation write behind a
confirmation and keeps `unverified`/`confirmed`/`rejected` on each one,
core-memory runs `pending | approved | rejected` with an approver and a reason,
and engram-alpha carries durable `confirmed_at`/`approved_at`/`demoted_at`
anchors plus a human pin. Per-edge curation is not new. What this design
contributes is applying it to *navigation between existing entries* rather than
to the facts themselves.

| System | Contribution |
|---|---|
| Nova-AI | Confirmation before a relation is written; three-value status per relation; human decisions monotonic against automatic ones; rejections that re-extraction cannot lift; rejected rows kept visible for audit |
| core-memory | The soft gate — pending stays retrievable, rejection is the only state that removes — and approval that survives an index rebuild |
| npcpy | The review loop: approve / reject / edit / skip / defer with a tally, and the opposite gating choice, defensible because it adjudicates memories rather than routes |
| HippoRAG | Inverse-frequency weighting as the hub defence; synonymy as edges rather than entity merges |
| M-flow | Path cost over typed edges — each hop widens the field and adds cost |
| NOOA Memory | The decay arithmetic that can make a second hop unreachable at defaults |
| Graphiti | The risk being avoided: entity-resolution mistakes reshaping a large part of the graph |
| engram-alpha | "Exposure doesn't validate": retrieval stamps observability only |
| Graphify | Corroboration as a gate rather than a counter; agent-reported dead ends |
| Argo | The failure that voids curation: a graph rebuilt wholesale on sync, with no approver in the schema |
| Memsem | The adjacent failure: approval states in a table no read path joins |
| Omi, Memledger, Mnemosyne | Rejection the generator must consult, and pinning it by a key derived from the value |
| Tokenmizer | `contested` — when the evidence cannot say which of two supersedes the other, mark both and keep both visible |
| Logseq | The warning against unmarked automatic writes |
| Swafra | The consumption shape — rank, expand through edges, return best per source |

The atlas patterns this design instantiates, named so the vocabulary matches:
**Gate the Expensive Path** (free generators before the typing model),
**Zero-LLM Capture** (candidate generation needs no model to be durable),
**Trust-State Machine** (pending → kept / rejected / contested / stale),
**Rejected-Value Tombstone** (a rejected pair is not re-proposed),
**Evidence Before Belief** (every edge keeps the bases it was derived from).

RainBox's own atlas entry mentions neither graphs nor relationships, so this is
new capability for this codebase.

## Tests and acceptance criteria

- A candidate is proposed for the motivating shape: an entry whose answer names
  a proper noun that is another entry's subject yields a directed candidate
  with a `subject_mention` basis.
- Generation is deterministic: the same registry produces the same candidate
  set, and re-running proposes nothing new.
- A busy parent namespace does not flood the queue; path-proximity candidates
  are capped per parent.
- Typing never invents a pair: a returned pair not in the candidate set is
  discarded.
- A candidate the typing model calls `unrelated` never reaches the queue.
- A pending edge is servable and is marked unreviewed everywhere it appears;
  only a rejected edge is withheld.
- A rejected pair is invisible to the candidate generator, not merely absent
  from the published set: a full regeneration proposes it nowhere.
- A kept edge cannot be moved back to pending, and a rejected pair cannot be
  revived, by any automatic pass.
- Corroboration by two generators raises weight; it does not change status.
- Regeneration is non-destructive: after a full re-run *and* an index rebuild,
  every kept edge is still kept, every rejected pair is still rejected, and
  each carries the reviewer and reason it had before.
- A kept edge records an approver; nothing else can write `qa_edge`.
- A two-hop neighbour with plausible weights appears in the block — the decay
  schedule must not put it below the cut arithmetically.
- Edge weight falls as the shared basis becomes more common: an entity in two
  entries outranks one in forty, with no type rule involved.
- No traversal exceeds depth 2, and a hub basis shared by many entries cannot
  push a sparse one-hop neighbour out of the block.
- Editing an endpoint hides its edges until re-typed; deleting one or locking
  its shield hides them immediately.
- Cross-shield edges are never created, and a same-shield edge is invisible
  while that shield is locked.
- `related_keys` carries key names and uuids only — never target answer text —
  and respects the per-source and total caps.
- A path label containing fence-like or instruction-like text cannot forge
  structure in the observation.
- The queue orders by recent retrieval demand, and an empty telemetry window
  degrades to a stable deterministic order rather than an error.
- Edge exposure records `considered`, following one records `used`, and a kept
  result records `accepted` — and none of the three changes an edge's weight,
  status, or queue position.
- Developer-page probes write no live telemetry, matching existing behaviour.

## Considered and declined

- **A hard review gate before an edge may serve.** A queue that must be drained
  before anything works is a queue that is abandoned. A wrong route costs one
  lookup and the recall filter still governs what content reaches the model, so
  the blast radius does not justify the gate. The opposite choice is correct
  for memories, which are asserted as fact.
- **Reviewing two-link relationships as their own round.** Quadratic operator
  effort to re-derive what single-hop edges already imply. Replaced by weighted
  traversal.
- **A boolean compose / do-not-compose table over type pairs.** Banning
  `co_located` composition discards the informative rare shared place along
  with the useless common one. Inverse-frequency weighting separates them
  without a rule naming either, and does not grow when the vocabulary does.
- **A fine-grained relation vocabulary** (`mother_of`, `employer_of`, …). The
  consumer reads the entry anyway, so precision here buys little, while every
  typing call gets harder and each new type needs its own base weight.
- **Merging entries judged to be about the same subject.** `same_person` stays
  an edge. A wrong edge is a weak path; a wrong merge is irreversible identity
  loss.
- **Letting the LLM propose pairs.** Spending a model on search over `N²` when
  three free generators cover the ground with better precision. The model is
  spent on judgement instead.
- **Treating all three generators as equally trustworthy.** Vector
  neighbourhood is the noisiest, so it contributes the lowest base weight and
  sorts last for review. Its candidates still serve while pending, which is
  acceptable only because they serve marked and ranked below everything else.
- **Storing edges in the JSONL.** The registry stays human-owned; derived data
  belongs in Postgres.
- **Surfacing neighbour answer text instead of key names.** It would bypass the
  recall filter, which is the component responsible for deciding what content
  reaches the model, and would consume the observation budget the recalled
  facts need.
- **A graph database.** A few hundred nodes and a bounded traversal depth fit
  in memory at query time.

## Delivery sequence

1. Candidate generation and the two tables, with the three generators and no
   model involvement. Inspectable on `/memory/developer`. Nothing is served.
2. The `related_keys` block behind a setting, serving pending edges marked
   unreviewed. This alone fixes the motivating case; ship it before any typing
   or review work and confirm against the live registry. Consumption lands
   ahead of curation so the states cannot become a table nothing reads.
3. The typing model, the `unrelated` drop, and the review queue with
   keep / reject / retype / defer / bulk-accept and hint-driven re-suggestion.
   Rejection is wired into the generator here, not at publish time.
4. Demand ordering, once variant A's unanswered-query events exist.
5. Inverse-frequency edge weighting and depth-2 weighted traversal, with hop
   counts marked in `related_keys`. Pin the two-hop acceptance test first, so
   the decay schedule cannot quietly make the second hop unreachable.
6. The funnel telemetry, then a go/no-go on the neighbour block on evidence of
   `accepted`, not of `used`.

Steps 1 and 2 are worth building even if the rest is never approved: they are
free of model calls, and they close the observed failure.

## Open questions

- **Symmetry.** Edges are stored directed. Whether `related_keys` should
  traverse them in reverse — the elder's household is reachable from the
  adult's only backwards — is unresolved. Reverse traversal is what the
  motivating question actually needs, which argues for it, but it doubles the
  neighbour set and re-raises hub noise. The starting answer is to traverse
  reverse edges only for `member_of` and `mentions_person`, and to measure.
- **Detecting disagreement between an edge and the text.** An edge asserts a
  relationship the answer text may later contradict. Endpoint-hash freshness
  catches edits but not a stale relationship in unedited text. The `contested`
  status reserves room for the resolution — a re-typing pass that contradicts a
  kept edge marks it rather than silently flipping it, and the operator
  resolves — but nothing here re-reads an unedited entry, so what *detects* the
  disagreement is unanswered.
