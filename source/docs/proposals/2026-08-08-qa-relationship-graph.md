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

What genuinely needs deciding is which type pairs compose — a table with as
many rows as there are type pairs, authored once:

```text
member_of   ∘ member_of    → compose      (a grouping's member's grouping)
mentions_person ∘ member_of → compose
same_person ∘ *            → compose      (facets of one subject are transparent)
co_located  ∘ co_located   → do not       (everything shares a place eventually)
co_temporal ∘ co_temporal  → do not
same_topic  ∘ same_topic   → do not
```

The two `do not` families are the whole reason typing is worth its cost.
Place and time edges are hubs: composing them turns the graph into a single
connected blob where every node is two hops from every other, and neighbour
surfacing becomes noise. Person and grouping edges are sparse and stay
meaningful across a hop.

Traversal is bounded at depth 2 in v1 and the surfaced neighbour block marks
which entries are one hop and which are two, so the assistant can prefer the
near ones.

## Verification

The operator reviews candidates in a queue, not a graph. Each row shows the
two entries, the proposed type, the generator basis, and the LLM's one-line
justification. Actions:

- **Keep** — the edge is published.
- **Reject** — recorded, and the pair is not proposed again unless its basis
  changes. Rejections are training signal for prompt tuning, not deletions.
- **Retype** — keep the pair, change the type.
- **Suggest more, with a hint** — free-text steer ("look for shared
  workplaces") that re-runs typing over that entry's remaining candidates.

Two mechanisms keep the queue finishable:

**Auto-accept the precise class.** A subject-mention candidate whose matched
proper noun equals the target's subject exactly is published without review,
flagged `auto`. This is the highest-precision generator, and it is the one
that fixes the motivating case, so the fix does not wait on a review session.
Auto edges appear in the queue as reviewable-after-the-fact rather than
blocking.

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

Verification throughput is worth measuring too — kept/rejected ratio per
generator tells you which generator earns its place in the queue, and a
generator whose candidates are rejected most of the time should be dropped or
capped harder.

## Tests and acceptance criteria

- A candidate is proposed for the motivating shape: an entry whose answer names
  a proper noun that is another entry's subject yields a directed candidate
  with a `subject_mention` basis.
- Exact subject-mention candidates auto-accept; every other generator's
  candidates require review.
- Generation is deterministic: the same registry produces the same candidate
  set, and re-running proposes nothing new.
- A busy parent namespace does not flood the queue; path-proximity candidates
  are capped per parent.
- Typing never invents a pair: a returned pair not in the candidate set is
  discarded.
- A candidate the typing model calls `unrelated` never reaches the queue.
- Composition is applied by type: person and grouping edges compose to two
  hops, place, time, and topic edges do not, and no traversal exceeds depth 2.
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
  effort to re-derive what typed 1-link edges already imply. Replaced by the
  composition table above.
- **A fine-grained relation vocabulary** (`mother_of`, `employer_of`, …). The
  consumer reads the entry anyway, so precision here buys little, while the
  composition table grows with the square of the vocabulary and every typing
  call gets harder.
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
2. Auto-accept for exact subject-mention candidates, and the `related_keys`
   block behind a setting. This alone fixes the motivating case; ship it before
   any typing work and confirm against the live registry.
3. The typing model, the `unrelated` drop, and the review queue with
   keep/reject/retype and hint-driven re-suggestion.
4. Demand ordering, once variant A's unanswered-query events exist.
5. Typed composition to depth 2, and the two-hop marking in `related_keys`.
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
