# Q&A relationship graph: curated edges and neighbour surfacing

Treat the Q&A registry as a graph. Nodes are entries; edges are operator-verified
relationships between them. `memory_query` surfaces a matched entry's neighbours
as key names, so the assistant can navigate to an entry that retrieval alone
could not reach.

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
| Validated by    | an LLM validator, automatically           | the operator, explicitly                   |
| Direction       | derived from generated questions           | derived from content, typed by an LLM      |
| Composition     | out of scope ("chains emerge")            | the point (see below)                      |

That proposal's machinery is reused rather than reinvented: runtime content
hashes for freshness, the scope/shield visibility rules, the sanitised
structured-call helpers, and the `RetrievalEvent` telemetry vocabulary. Its
variant A (mining unanswered queries) is a **prerequisite** here, because it
supplies the demand signal that orders the verification queue.

Its variant B (alias enrichment) attacks the same failure from the retrieval
side, by giving the target entry more phrasings. B and this proposal are not
alternatives: B raises the chance the right entry is retrieved directly, this
one gives a route when it is not. B has no answer when the bridging fact lives
only in the other entry, which is precisely the observed case.

## Prior art

Checked against the agent-memory-atlas. Graph-backed memory is well trodden;
what is thin is a graph a person curates.

| System | What it contributes here |
|---|---|
| HippoRAG | Inverse-frequency seed weighting as the hub defence; synonymy as edges rather than entity merges; diffusion instead of traversal |
| M-flow | Path cost over typed edges — "one strong path is enough" — and a cheap deterministic check in front of every model call |
| NOOA Memory | The cautionary decay arithmetic that makes a second hop unreachable at defaults |
| Graphiti | The risk being avoided: entity-resolution mistakes reshaping a large part of the graph |
| Graphify | Corroboration as a gate rather than a counter; agent-reported `dead_end` outcomes |
| npcpy | The closest prior art for the review loop: approve / reject / edit / skip / defer, and retrieval that reads approved rows only |
| Argo | The failure that voids curation: a graph rebuilt wholesale on sync, with no approver in the schema |
| Memsem | The adjacent failure: approval states in a table no read path joins |
| Swafra | The consumption shape — rank, expand through edges, return best per source — and dangling edges on delete |

The atlas patterns this design instantiates, named so the vocabulary matches:
**Gate the Expensive Path** (free generators before the typing model),
**Zero-LLM Capture** (candidate generation needs no model to be durable),
**Trust-State Machine** (candidate → kept / rejected / stale),
**Rejected-Value Tombstone** (a rejected pair is not re-proposed),
**Evidence Before Belief** (every edge keeps the bases it was derived from).

Two observations worth stating plainly. RainBox's own atlas entry mentions
neither graphs nor relationships, so this is new capability rather than a
refinement of something already present. And no catalogued system combines a
derived-candidate graph with per-edge human approval — npcpy approves memories
but has no edges, Argo has edges but no approver. That gap is either an
opportunity or a warning, and the delivery sequence below is arranged so the
zero-model half proves itself before the curation half is built.

## Goals

- Let the assistant reach an entry that the user's phrasing cannot retrieve.
- Keep every published edge operator-approved.
- Keep the operator's verification effort sub-quadratic in the node count, and
  ordered by observed demand.
- Make multi-hop navigation a consequence of typed single-hop edges, not a
  second authoring project.
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
tens of thousands. That number rules out two tempting designs immediately: an
LLM cannot score all pairs, and a human certainly cannot review them.

Everything below follows from that single constraint. Candidates must be
proposed by something free, the LLM must be spent on judgement rather than
search, and the operator must see a queue ordered so that stopping early still
leaves the useful edges done.

## Candidate generation

Three generators run over the registry. None calls a model. Each proposes
directed candidate pairs with a `basis` recording why.

**Subject mention (highest precision).** Entry A's answer contains a proper
noun that is the *subject* of entry B. Subjects come from the last path
segment and from B's registered questions. This is the generator that catches
the motivating case: the elder household's answer names the adult, who is the
subject of another entry, yielding `<elder> → <adult>`.

**Path proximity.** Entries sharing a parent namespace, and entries that are
an ancestor or descendant of one another. `human.family.<a>` and
`human.family.<b>` are siblings; `human.<p>.health` is a child of `human.<p>`.
Cheap, and it encodes grouping the operator already expressed by naming
things. Capped per parent, because busy namespaces would otherwise flood the
queue.

**Vector neighbourhood.** Top-K entry-to-entry similarity using the embeddings
already in the pgvector table. Lowest precision of the three; it exists to
catch relationships the other two structurally cannot see, and its candidates
are always operator-reviewed, never auto-accepted.

A pair proposed by several generators carries all their bases and is ranked
higher. Generation is deterministic and idempotent: same registry, same
candidates.

## Typing

A candidate becomes an edge only with a **type**, because types are what make
composition safe. The v1 vocabulary is small and closed:

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
semantics — it only has to be precise enough to decide whether a hop is worth
taking and whether two hops compose. A fine-grained vocabulary
(`mother_of`, `employer_of`, …) multiplies both the typing burden and the
composition table while adding nothing the assistant cannot read for itself.

`member_of` is directional and carries a role note (`lists B as a member`)
rather than splitting into `parent_of`/`child_of`. Household entries in a
personal registry routinely list six people in one answer; forcing per-person
role edges would demand exactly the data restructuring this design promised
not to require.

`same_person` is deliberately an **edge, not a merge**. HippoRAG adds synonymy
edges between similar entity phrases instead of collapsing them, and the atlas
draws the contrast explicitly: Graphiti's stated biggest risk is that
entity-resolution mistakes reshape a large portion of the graph, while "a wrong
synonymy edge adds a weak diffusion path, [and] a wrong merge destroys two
identities irreversibly." Two entries about the same person stay two entries.
Nothing in this design ever rewrites the registry to unify them.

An LLM assigns the type and a one-line justification for each candidate. It
never proposes pairs — it only judges what the free generators found. When it
cannot type a candidate confidently it returns `unrelated`, which drops the
candidate before the operator ever sees it.

## Composition replaces a second verification round

The natural instinct is to establish 1-link relationships, verify them,
then establish 2-link relationships and verify those too. That second round
should not exist.

Once `A —R1→ B` and `B —R2→ C` are verified, `A ⇝ C` is a path. It is
computed by traversal at query time and costs nothing to store. Verifying it
separately would re-confirm facts already confirmed, and the number of 2-hop
pairs grows quadratically while the number of edges does not.

What genuinely needs deciding is which hops are worth taking. The obvious
answer is a boolean table — person and grouping edges compose, place and time
edges do not, because place and time are hubs that turn the graph into a blob
where everything is two hops from everything.

That answer is too blunt, and the atlas says why. HippoRAG's seed weights are
divided by the number of chunks an entity appears in, so ubiquitous entities do
not dominate; its report calls this "the kind of detail that separates a
working PPR system from one that always returns the hub nodes." The hub problem
is a *frequency* problem, not a *type* problem. A boolean ban on `co_located`
discards the rare shared place — which is genuinely informative — along with
the common one.

So edges carry a weight, inverse to how many entries share the thing that
created them:

```text
weight(edge) = base(type) / log(1 + entries_sharing_the_basis)
```

A place named in two entries keeps most of its weight; one named in forty
collapses toward zero without a rule having to name it. Type still sets the
base — `member_of` and `mentions_person` start high, `co_temporal` and
`same_topic` low — but frequency does the discriminating, and a new type does
not require a new row in a composition table.

A path's score is the product of its edge weights, following M-flow, where
"each hop expands the semantic field, but each edge also adds cost, so only
paths with coherent, low-cost connections remain competitive." Two-hop
neighbours compete with one-hop neighbours on score rather than being admitted
by category.

**Tune so the second hop can actually survive.** NOOA Memory is the cautionary
case: with a per-hop decay of 0.6 and an edge weight of 0.6, a two-hop relay
scores 0.078 against an activation floor of 0.05 — it clears by almost nothing,
and the system ships with `hops` defaulting to 1, so the multi-hop path does
not run at all unless an operator opts in. A decay schedule that makes two-hop
arithmetically unreachable is multi-hop in name only. The acceptance tests
below pin a two-hop case that must appear in the block.

Traversal stays bounded at depth 2, and the block marks each neighbour's hop
count.

## Verification

The operator reviews candidates in a queue, not a graph. Each row shows the
two entries, the proposed type, the generator basis, and the LLM's one-line
justification. Actions:

- **Keep** — the edge is published.
- **Reject** — recorded, and the pair is not proposed again unless its basis
  changes. Rejections are training signal for prompt tuning, not deletions.
- **Retype** — keep the pair, change the type.
- **Skip / defer** — pass without deciding; deferred rows return later, skipped
  ones sink.
- **Keep all for this entry** — bulk-accept the remaining candidates of a
  source the operator has come to trust mid-review.
- **Suggest more, with a hint** — free-text steer ("look for shared
  workplaces") that re-runs typing over that entry's remaining candidates.

The five-way loop with defer and a bulk accept is taken from npcpy, the
atlas's most literal implementation of memory a person adjudicates before it
takes effect — extractions land as `pending_approval` and reach no prompt until
someone presses a key, and its `build_context` reads approved rows only.
Defer and bulk-accept are what make a long queue finishable rather than
abandoned; a review loop offering only keep and reject stalls on the first row
the operator is unsure about. Show a running tally, as npcpy does.

Three mechanisms keep the queue finishable:

**Auto-accept on corroboration, not on a single generator.** A pair proposed by
two independent generators — say an exact subject mention *and* path proximity
— publishes without review, flagged `auto`. Graphify's rule is the model here:
a node cited by one useful result is `tentative` and becomes `preferred` only
at two distinct corroborations, corroboration as a gate rather than a counter.
Single-generator candidates always go to the queue. This is stricter than
trusting exact subject-mention alone, and it still clears the motivating case,
which both generators find.

**Order by demand.** The queue is sorted by recent retrieval activity on the
source entry, using the unanswered-query and verdict telemetry from the
follow-up proposal's variant A. Entries nobody queries sink. Stopping halfway
through leaves the queried half done, which is the half that matters.

The verification surface lives on `/memory/developer`, beside the existing
retrieval inspection panels; it is operator tooling, not a user-facing page.

## Consumption

After the recall filter keeps its entries, `memory_query` appends a neighbour
block outside `recalled_memory`:

```text
<related_keys note="verified links from the entries above; key names only, not facts">
<uuid>  human.family.<elder>     member_of, 1 hop
<uuid>  human.<other>.health     same_person, 2 hops
</related_keys>
```

Key names and uuids only — never target answer text, which would smuggle
unfiltered content past the recall filter and blow the observation budget. The
assistant follows a neighbour with the `{"uuid": ...}` form of `memory_query`
it already has.

Caps mirror the follow-up proposal's hint block: three per kept source, six
total, deduplicated, one-hop before two-hop. The block is sanitised with the
same untrusted-data fencing as recalled memory, since path labels are operator
text.

The decide prompt gains one line: when the answer is about someone reached
through another entry, the related keys are where to look before reporting
that nothing is stored. This closes the loop with the bounded empty-read retry
already in the loop, which currently reformulates blindly.

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
- status            pending | kept | rejected | auto | superseded
- typed_by_uuid     model that assigned the type
- policy_version    
- reviewed_at       

qa_edge
- pair_key          
- source_qa_id, target_qa_id, type
- origin            operator | auto
- source_sha, target_sha
- created_at
```

Freshness follows the same rule as the follow-up tables: an edge is servable
only while both endpoint hashes match the current runtime hashes. An endpoint
edit hides its edges and re-queues the pair for typing; it does not silently
keep asserting a relationship derived from text that changed. Deletion or a
shield lock hides the edge immediately.

**Regeneration must never overwrite an operator decision, and the schema must
record who made it.** This is the failure that voids the whole feature, and the
atlas has a worked example: Argo is denied its human-review and trust-state
marks because its Neo4j graph is rebuilt wholesale by a `clearGraph` on every
sync, carries no per-element lifecycle, and *no schema records an approver*. A
curated graph that a routine resync flattens is not curated. Hence `qa_edge`
carries `origin` and a `reviewed_by`/`reviewed_at` stamp, generation only ever
inserts into `qa_edge_candidate`, and publishing is the only writer of
`qa_edge`. A re-run may add candidates and mark edges stale; it may not delete
a kept edge or resurrect a rejected pair.

The companion trap is building the review machinery and never reading it.
Memsem carries a pending/approved/rejected candidate status "in a table no read
path joins" — the states exist, nothing consumes them. The delivery sequence
below is ordered so consumption ships before typing for exactly this reason.

RainBox already has this vocabulary for claims: active/candidate flows, a
five-actor trust model, rejected-value tombstones, and a review UI. Edge review
should reuse those states and that UI idiom rather than growing a parallel
approval system with different words for the same thing.

`pair_key` is HMAC-SHA256 over the ordered pair with the Flask `SECRET_KEY`,
domain-separated `qa-edge-pair`, consistent with the digest convention already
established.

Both tables are declared in `db/models.py` and created by `init_db`, with
`status` and `type` enforced by CHECK constraints — so adding a type later is
a constraint migration, not just a new Python string.

## Privacy

The typing model receives entry content, which for this registry means the
operator's private overlay. That is the same boundary the follow-up proposal
draws, and the same conclusions hold: the typing model must be explicitly
bound and local by default, prompts and logs must never record raw entry text,
and candidates must respect shield visibility — an unshielded source is typed
only against unshielded targets, and cross-shield edges are rejected outright.

One difference is worth stating plainly. The follow-up proposal defaults to
upstream-only scope and gates overlay processing behind an authenticated
control plane. This design has **no value at upstream scope**: generic
published entries have few interesting relationships, and every case that
motivates the feature lives in the overlay. So this feature is overlay-scoped
from the start, and inherits that authentication requirement rather than
pretending an upstream-only version is useful. Until that control plane
exists, generation runs only when explicitly started by the operator on
localhost, and that limitation belongs in the settings copy.

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
a **dead end** when the target turned out not to help. Graphify has the agent
report `useful | dead_end | corrected` against the nodes it cited, and a
volunteered dead end is worth more than a silence that looks identical to
"never surfaced". Dead-end counts feed the review queue as re-review
candidates.

Verification throughput is worth measuring too — kept/rejected ratio per
generator tells you which generator earns its place in the queue, and a
generator whose candidates are rejected most of the time should be dropped or
capped harder.

## Tests and acceptance criteria

- A candidate is proposed for the motivating shape: an entry whose answer names
  a proper noun that is another entry's subject yields a directed candidate
  with a `subject_mention` basis.
- A pair proposed by two independent generators auto-accepts; a pair proposed
  by one always reaches the queue, however precise its basis.
- Regeneration is non-destructive: after a full re-run, every kept edge is
  still kept, every rejected pair is still rejected, and each carries the
  reviewer stamp it had before.
- A kept edge records an approver; nothing else can write `qa_edge`.
- A two-hop neighbour with plausible weights actually appears in the block —
  the decay schedule must not put it below the cut arithmetically.
- Edge weight falls as the shared basis becomes more common: an entity in two
  entries outranks one in forty, with no type rule involved.
- Generation is deterministic: the same registry produces the same candidate
  set, and re-running proposes nothing new.
- A busy parent namespace does not flood the queue; path-proximity candidates
  are capped per parent.
- Typing never invents a pair: a returned pair not in the candidate set is
  discarded.
- A candidate the typing model calls `unrelated` never reaches the queue.
- No traversal exceeds depth 2, and a hub basis shared by many entries cannot
  push a sparse one-hop neighbour out of the block.
- A rejected pair is not re-proposed while its bases and endpoint hashes are
  unchanged, and is re-proposed once they change.
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
  result records `accepted`.
- Developer-page probes write no live telemetry, matching existing behaviour.

## Considered and declined

- **Verifying 2-link relationships as their own round.** Quadratic operator
  effort to re-derive what typed 1-link edges already imply. Replaced by
  weighted traversal over verified single hops.
- **A boolean compose / do-not-compose table over type pairs.** The first
  answer to the hub problem, and too blunt: banning `co_located` composition
  discards the informative rare shared place along with the useless common one.
  Inverse-frequency weighting separates them without a rule naming either, and
  it does not grow when the vocabulary does.
- **A fine-grained relation vocabulary** (`mother_of`, `employer_of`, …). The
  consumer reads the entry anyway, so precision here buys little, while every
  typing call gets harder and each new type needs its own base weight.
- **Merging entries judged to be about the same subject.** `same_person` stays
  an edge. A wrong edge is a weak path; a wrong merge is irreversible identity
  loss, and entity resolution is the stated top risk of the most mature
  graph system in the atlas.
- **Letting the LLM propose pairs.** Spending a model on search over `N²` when
  three free generators cover the ground with better precision. The model is
  spent on judgement instead.
- **Storing edges in the JSONL.** The registry stays human-owned; derived data
  belongs in Postgres, matching the follow-up proposal's rule.
- **Surfacing neighbour answer text instead of key names.** It would bypass the
  recall filter, which is the component responsible for deciding what content
  reaches the model, and would consume the observation budget the recalled
  facts need.
- **A graph database.** A few hundred nodes and a bounded traversal depth fit
  in memory at query time.
- **Auto-accepting vector-neighbourhood candidates.** Its precision is the
  lowest of the three generators; auto-accepting it would fill the graph with
  plausible-but-wrong edges that the operator would then have to hunt down.

## Delivery sequence

1. Candidate generation and the two tables, with the three generators and no
   model involvement. Inspectable on `/memory/developer`. Nothing is served.
2. Auto-accept for two-generator-corroborated candidates, and the
   `related_keys` block behind a setting. This alone fixes the motivating case;
   ship it before any typing work and confirm against the live registry.
   Consumption lands here, deliberately ahead of curation, so the states can
   never become a table nothing reads.
3. The typing model, the `unrelated` drop, and the review queue with
   keep/reject/retype and hint-driven re-suggestion.
4. Demand ordering, once variant A's unanswered-query events exist.
5. Inverse-frequency edge weighting and depth-2 weighted traversal, with
   hop counts marked in `related_keys`. Pin the two-hop acceptance test first,
   so the decay schedule cannot quietly make the second hop unreachable.
6. The funnel telemetry, then a go/no-go on the neighbour block on evidence of
   `accepted`, not of `used`.

Steps 1 and 2 are worth building even if the rest is never approved: they are
free of model calls, and they close the observed failure.

## Open questions

- **Symmetry.** Edges are stored directed. Whether `related_keys` should
  traverse them in reverse — the elder's household is reachable from the
  adult's only backwards — is unresolved. Reverse traversal is what the
  motivating question actually needs, which argues for it, but it doubles the
  neighbour set and re-raises hub noise. The v1 answer is to traverse reverse
  edges only for `member_of` and `mentions_person`, and to measure.
- **When the graph disagrees with the text.** An edge asserts a relationship
  the answer text may later contradict. Endpoint-hash freshness catches edits
  but not a stale relationship in unedited text. No mechanism is proposed; the
  operator's review is the only check, and that is a known limit rather than a
  solved problem.
