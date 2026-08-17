# Recall: decisions, rollout, and the incident that prompted them

**Status:** Proposed. Nothing implemented.
**Date:** 2026-08-17
**Revision:** 3 — restructured as an implementation plan after a second review.
Revisions 1 and 2 were post-mortems; the evidence now lives in an appendix and
the body makes decisions. Git holds the earlier text.
**Relates to:** `qa-system.md`, `memory-architecture.md`,
`assistant-design.md` §Model slots and §Acceptance criteria.

## What happened, in five lines

A turn refused to answer a question whose answer is stored verbatim in the Q&A
registry. Retrieval found the entry, the keep policy kept it, and three
independent defects then agreed with each other: the acceptance-criteria call had
already written that the answer must be a refusal; the recall scorer read the
answering text and called it irrelevant; and the injection cut the answering
sentence off head-only at 1200 characters. The reply audit approved the result in
17 tokens. Full evidence in the appendix.

## Decisions

Each decision states what is rejected and what it costs. Nothing here is
conditional on a later decision except where said.

### D1 — Model-generated `assumptions` stops reaching the answering prompts

`_format_criteria_markdown` emits `Processing`, `Formatting` and `Assumptions`,
and that Markdown is injected into both the decide prompt and the reply-audit
prompt. On the traced run `assumptions` carried a false claim about the system's
own capabilities and an instruction that the answer be a reiteration of it.

**Decision:** drop `assumptions` from the injected Markdown. Keep the field on
the structured object, the trace row and the `/assistant` inspector.

The criteria call's job is the *shape* of the reply. `processing` and
`formatting` are shape. `assumptions` is the only field that carries free-form
prose about content, and it is the field that failed. Removing it from the prompt
removes the mechanism rather than asking a model to police itself.

**Rejected — a code-side validator on the field.** Revision 2's checklist assumed
one while its own design section left the choice open. Detecting prose that
"settles the answer" needs either brittle keyword rules or another model call;
both are new failure surfaces guarding a field that has no business in the prompt
anyway.

**Rejected — reordering the criteria call after the first read.** It would make
the call observe rather than assume, and it costs a step, reorders the loop, and
leaves the field free to assert content once it has some.

**Cost, stated plainly:** `assumptions` is also where the criteria call records an
ambiguity the settings cannot resolve — the signal that the assistant should ask
a clarifying question rather than guess. Dropping it from the prompt weakens that
path. This is a real regression and it is why the harness (D6) measures
clarifying-question rate alongside unsupported-refusal rate. If the clarifying
path degrades measurably, the answer is a separate narrow field carrying only
unresolved ambiguities, not the return of free prose.

### D2 — `<memory_filter_assessment>` stops reaching the answering prompt

The scorer's `reasoning` is lossy commentary about a set of candidates, injected
beside the candidates themselves. On the traced run it was the second voice
asserting the answer was absent.

**Decision:** stop injecting it. Keep it in the trace and in the `memory_query`
observation data, where the operator reads it.

**Rejected — inject only when the scorer kept nothing.** Plausible, and it keeps
a genuinely useful "why nothing matched" note, but it adds a conditional prompt
section for a case the harness has not yet shown to matter. Revisit after D6.

### D3 — Both head-only truncation paths become `truncate_middle`, cap unchanged

`_fact_line` (`assistant.py:1146`) cuts seed entries with `text[:1200]`; a second
path cuts remembered facts the same way at `assistant.py:1679`.

**Decision:** switch both to `truncate_middle`. **Do not change the cap in the
same step.**

`agents/base.py:truncate_middle` already calls itself "the one shortening shape
every agent uses on text a model will read"; this restores a stated invariant and
brings the in-band marker with it. The same bug was fixed once already for the
run summarizer (`867cd4a`).

**Rejected for now — resizing the cap, and "a share of remaining budget."**
Revision 2 proposed both. The share-of-budget scheme is order-dependent: facts
rendered first would starve later ones unless the budget is computed across the
whole kept set with a reserved minimum per candidate, which is a design nobody
has written. And resizing before the harness exists is exactly the
measure-before-architecture violation this document keeps warning about. Cap
sizing is benchmarked in D6, not guessed here.

### D4 — Retrieval units are derived from parent entries, not authored

The traced parent is 1947 characters covering onset, diagnosis, affected areas,
treatment history, progress, time cost and current schedule, with three questions
attached. 28 of 148 operator entries (18.9%) exceed today's cap; p90 is 1684, max
5178.

**Decision:** keep the parent entry as the authoritative record and derive child
retrieval units from it. Children are **derived state, regenerated on
repopulate** — the same lifecycle the registry already has (`rebuild_kb` replaces
`_entries_by_id` wholesale).

That answers the lifecycle questions the review raised, and it answers them by
construction rather than by policy:

| question | answer |
| --- | --- |
| IDs when a parent is edited and boundaries move | child id = deterministic hash of (parent id, subtopic slug), not of offsets — an edit that does not change the subtopic does not reissue the id |
| persisted or regenerated | regenerated on repopulate; children are never authored |
| stale removal | none needed: the index is rebuilt, not patched |
| who authors child aliases | nobody — children carry a **contextual prefix** (subject + topic + subtopic) instead, which is what makes them self-contained |
| alias inheritance and sibling crowding | aliases are **not** inherited; inheritance is precisely what would make four children of one parent compete as four copies of the same question |

**Rejected — hand-authoring children.** ~150 entries, and it puts the operator in
the loop for every future edit.

**Open, and blocking D8 only:** the derivation pass is an LLM pass over the
registry and is its own quality surface. It needs the harness before it, not
after.

### D5 — Parent diversity is an evaluated policy, not an invariant

Revision 2 proposed capping each parent to one or two candidates. A question
needing three facts from one record would break under that as an invariant.

**Decision:** implement the per-parent cap as a configurable N applied **after
reranking**, defaulting to 2, and report its effect in the harness. It is a
diversity knob, not an architectural rule.

### D6 — The regression harness lands before any behavioural change

**Decision:** the first commit is a failing end-to-end regression reproducing the
observed failure, with stage-level assertions. Every fix after it lands
individually, and the harness is re-run between each.

Stages asserted, because a single end-to-end assertion cannot tell these apart:

| stage | assertion |
| --- | --- |
| retrieval | expected unit in candidates; chunk recall@k |
| ranking | reranker MRR / nDCG over the case set |
| injection | answer-bearing span present in the decide prompt; injected chars vs source chars |
| answer | **the actual answer**, not the entry id and not a surviving substring |
| refusal | unsupported-refusal rate |
| clarification | clarifying-question rate (D1's stated cost) |
| cost | latency and tokens per turn |

The harness also records, per call: resolved model uuid, model family, sampling
settings, and agreement between the criteria, scorer and audit calls — see D7.

Neutral cases in the repo; operator-derived cases under `customize.dir`. Include
cases that currently pass.

**Counterfactual matrix**, run once the harness exists and before the
architecture work: truncation only; D1 only; D2 only; a dedicated child unit
only; then combinations. This is what settles whether the criteria or the
truncation was decisive — the question revision 2 asserted an answer to and could
not support.

### D7 — "Different groups" is an experiment in decorrelation, not independence

Every call in the traced run — criteria, decide, scorer, audit — was answered by
one model, because the assistant had a single binding. The per-call slots make
separation possible.

**Decision:** bind `assistant.acceptance_criteria`, `assistant.memory_filter` and
`assistant.reply_audit` away from `assistant.default`, and **treat it as an
experiment whose effect the harness measures**, not as a guarantee. Distinct
group uuids can still resolve to the same model config or the same family; the
harness records the resolved model uuid and family per call and reports error
correlation between them. No claim of independent defences is made on the basis
of the binding alone.

### D8 — The architecture, after the counterfactuals

Embed question aliases **and child answer text**; run lexical retrieval over the
same children; fuse the two rankings with reciprocal rank fusion (which needs no
commensurable scores — the property the current code already relies on when it
refuses to weight its two signals); rerank (query, chunk) pairs; inject the
winning chunk, expanding to neighbours or the parent on demand.

This is the shape Anthropic's
[Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
describes (chunk, contextualise, embeddings + BM25, fuse, rerank), that
Elasticsearch's `semantic_text` implements by scoring a parent through its best
passage, and that LlamaIndex's sentence-window implements by expanding a matched
sentence to a local window rather than to the whole parent.

**Rejected — deleting the generative Likert filter outright.** It cost 32.7
seconds on the traced run, was wrong, and the code-side rank rule rather than the
filter is what saved the candidate. All true, and still not grounds to delete a
component on one trace: the same helper serves `query_filter_router`. It is
*replaced* by a reranker if and when D6 shows the reranker better on the same
cases.

## Rollout sequence

1. Failing end-to-end regression reproducing the observed failure (D6).
2. Stage-level assertions: retrieval, ranking, injection, answer, refusal,
   clarification, cost (D6).
3. `truncate_middle` in both paths, cap unchanged (D3).
4. Drop `assumptions` from the injected criteria Markdown (D1).
5. Stop injecting `<memory_filter_assessment>` (D2).
6. Run the counterfactual matrix; publish which change moved which metric (D6).
7. Bind the three slots to distinct groups; record model identity and error
   correlation (D7).
8. Prototype derived child units on one parent family, with contextual prefixes,
   without duplicating the authoritative record (D4).
9. Define and test regeneration, id stability, fusion and parent diversity (D4,
   D5).
10. Compare child-chunk retrieval against entry retrieval on the harness.
11. Select and integrate a reranker, or keep the filter, on that evidence (D8).
12. Benchmark cap sizes (D3's deferred half).

## Evaluation and rollback

- **Gate for each of 3–5:** the harness's existing cases do not regress, and the
  targeted metric moves. A change that fixes the traced case and lowers answer
  correctness elsewhere is reverted, not tuned.
- **Rollback:** 3–5 and 7 are each a small, independently revertible commit.
  8–11 land behind the harness and are not enabled for live turns until the
  chunk path beats the entry path on answer correctness at equal or lower cost.
- **Stop condition for D1's cost:** if clarifying-question rate falls without a
  matching fall in unsupported-refusal rate, D1 is wrong as implemented and the
  narrow-field variant is built instead.

## Open questions

- **Was the criteria call or the truncation decisive?** Not answerable from one
  trace; the counterfactual matrix in D6 answers it, and the answer determines
  whether D1 or D4/D8 was the higher priority all along.
- **Should a `truncateN` tag force a re-read rather than offer one?** The
  affordance exists in `DECIDE_TURN_INSTRUCTIONS`, steps remained, and it was not
  used. Cheap to settle: grep recorded runs for uuid-mode `memory_query`
  following a `truncateN` tag.
- **Do remembered facts and seed entries belong in one scored set?** Six short
  facts against seven long entries under one policy and one budget, never tested.
- **Where does a reranker run?** A second model server and a new dependency.

## Appendix — the evidence

### The criteria call, step 2, before any retrieval

`assumptions` came back as (domain nouns redacted):

> "The user is asking for specific […] information ([…] name) that requires
> querying stored memory, **which I have previously determined I cannot access
> due to privacy and security limitations**. The scope of the answer must
> therefore be a reiteration of this limitation…"

False — stored memory is what `memory_query` reads. It also violates
`ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS`, which says: *"You constrain the SHAPE of
the reply, never where its content is found: never name a source for the answer's
facts, and never settle what the answer will cover."*

And it failed in the manner its own prompt names. The next sentence reads: *"The
conversation history in front of you is the standing trap — it is the one source
you can see, so it is the one you will be tempted to nominate."* "I have
previously determined" is the model lifting a stance out of the transcript and
promoting it to a constraint.

**Bounded claim.** The criteria strongly predisposed the decide and audit calls
toward refusal. It cannot be called decisive: in the decide prompt, successful
current-turn observations are source-priority **rank 1** and
`acceptance_criteria_markdown` is **rank 4**, so fresh evidence is explicitly
supposed to override it. Whether it would have is what D6's counterfactual
answers.

### Retrieval and scoring, steps 3–4

Thirteen candidates — seven seed entries, six remembered facts. Anonymised;
structurally identical to the real case.

| # | candidate | chars | similarity | direct | indirect | relevancy |
| --- | --- | --- | --- | --- | --- | --- |
| **3** | **`…hobby.cycling`** | **1947** | **693** | **1** | **3** | **3** |
| 2 | `…hobby.overview` | 755 | 224 | 1 | 2 | 3 |
| 1 | `…routes.2026` | 932 | 723 | 1 | 1 | 2 |
| 4 | `…history` | 1152 | 581 | 1 | 1 | 2 |
| 5–13 | 3 entries, 6 facts | 45–1303 | 218–699 | 1 | 1 | 1 |

Candidate 3 holds the answer and was rendered to the scorer **in full**. Its
note: *"None of the provided candidates contain this direct, factual […]
information. The candidates are mostly about general history, locations, or other
unrelated facts."* — an accurate description of candidates 5–13 and false of
candidate 3. The call took **32.7s** for 9571 in / 589 out.

**The scorer did not receive the acceptance criteria.** `_recall_filter_prefix`
is request + conversation history + identity only. Criteria poisoning explains
the decide and audit behaviour; it does not explain this miss.

**The anchoring mechanism is in the schema, not the batch.** `FilterDecision`
declares `reasoning` before `items`, and its docstring says so deliberately —
*"the model writes its overall does-anything-match assessment first and the
scores are conditioned on it."* Summarise the set, conclude nothing matches, then
emit thirteen rows consistent with the conclusion already written. Per-candidate
scoring reduces cross-candidate contamination but is not *incapable* of
misreading a passage, and thirteen generative calls against a 32.7-second batch
call is a poor trade — which is why D8 reaches for a reranker rather than a loop.

**The keep policy saved it.** `apply_filter_scores` is relative first:
`FILTER_KEEP_TOP_N = 2` survive on rank alone above `FILTER_KEEP_TOP_FLOOR = 2`.
Candidate 3's `indirect: 3` put it at rank 0. That the policy had to rescue the
scorer means scorer quality can degrade a long way before anything looks broken.

### Injection, step 5

`_fact_line` cut at 1200 characters, head only. The entry is 1947 characters; the
three nouns answering the question sit at **1795, 1843 and 1856** — the last 8%
of the answer, inside the 38% the cap removed.

The decide model therefore received: a fact carrying the topic but not the
answer; a note asserting no candidate held it; and criteria instructing that the
answer be a reiteration of a limitation.

### The audit, step 6

`{"reason": "The message is sound.", "verdict": "send"}` — 17 output tokens.

### A correction carried from revision 2

Revision 2 claimed a candidate could be **dropped** because its opening looked
irrelevant while its tail answered the question. That cannot happen in the
current pipeline: `seed_candidate_rows` renders the full answer to the scorer.
The claim was inherited from revision 1's rejected "truncate the scorer's input
too" design and should have gone with it.

The real effect is weaker and still supports D4: a short relevant span is diluted
inside a long document, and scores accordingly. That is an argument for chunking,
not evidence of a truncation-driven drop.
