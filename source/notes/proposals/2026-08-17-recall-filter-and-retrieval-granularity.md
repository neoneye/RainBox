# Recall: three failures that agreed with each other

**Status:** Proposed. Nothing implemented.
**Date:** 2026-08-17
**Revision:** 2 — rewritten after external review. Revision 1 named truncation
as the cause, absolved the data model, and proposed a fix that made the
relevance scorer *less* informed. All three positions were wrong; what changed
and why is recorded in "What revision 1 got wrong" below, because the mistakes
are instructive about how a single trace misleads.
**Relates to:** `qa-system.md`, `memory-architecture.md`,
`assistant-design.md` §Model slots and §Acceptance criteria.

## Summary for a reviewer with no context

The assistant refused to answer a question whose answer is stored, verbatim, in
the Q&A registry. It did not fail to find it. **Three independent defects lined
up and pointed the same way:**

1. **The acceptance-criteria call pre-committed the run to a refusal, before any
   retrieval ran**, on a false premise about its own capabilities — violating its
   own instructions in the exact manner those instructions warn about by name.
2. **The recall filter's scorer read the answering text and judged it
   irrelevant**, then had its wrong conclusion injected beside the evidence.
3. **The injection then cut the answering sentence off**, head-only, at a
   1200-character cap.

Any one alone might have been survivable. Together they produced a confident,
well-formatted refusal that the reply audit approved as "sound".

The lesson that generalises: **this system's checks are not independent.** The
criteria, the scorer's assessment and the audit all consumed the same
conversation history and the same model, and all three agreed with each other
rather than with the stored data. A run's defences being correlated is worth
more attention than any single one of them being wrong.

## The pipeline, for orientation

`memory_query` is an assistant read action. One call runs this chain
(`agents/assistant.py:_filter_recalled_candidates`, `memory/seed_memory.py`):

1. **Seed retrieval** — `_hybrid_seed_ranked` returns the top `TOP_K_VECTOR=5`
   entries by embedding similarity over **questions only**, plus the top
   `TOP_K_FULLTEXT=5` by lexical full-text over questions and answers,
   deduplicated and interleaved. The two signals are not weighted against each
   other.
2. **Claim retrieval** — `retrieve_memories_hybrid` adds remembered facts from
   the `/memory` store as further candidates.
3. **One relevance-scoring LLM call** over the combined set, returning a
   `reasoning` note plus three Likert scales per candidate (`direct`,
   `indirect`, `relevancy`). Candidates are rendered here with their **full**
   answer text — `seed_candidate_rows` applies no length cap.
4. **A code-side keep/drop policy**, `apply_filter_scores`. The LLM supplies
   numbers; the decision is code.
5. **Injection** — kept candidates go into `<recalled_memory>` in the decide
   prompt, each fact capped at `MEMORY_QUERY_PER_FACT_CHARS = 1200` and the
   block at `MEMORY_QUERY_TOTAL_CHARS = 11000`. The scorer's `reasoning` is
   injected alongside as `<memory_filter_assessment>`.

Two calls bracket all of this: `acceptance_criteria` runs before step 0 and its
output is injected into every later prompt at source-priority rank 4;
`reply_audit` checks the finished message before it is sent.

## The traced run

Anonymised worked example, structurally identical to the real one: three sibling
entries under one topic, the answer in the parent, at the parent's end.

### Step 2 — the criteria call, before any retrieval

The `assumptions` field came back as (domain nouns redacted):

> "The user is asking for specific […] information ([…] name) that requires
> querying stored memory, **which I have previously determined I cannot access
> due to privacy and security limitations**. The scope of the answer must
> therefore be a reiteration of this limitation while maintaining a helpful tone
> based on past context."

Three things are wrong with this and the third is the interesting one:

- **It is false.** Stored memory is exactly what `memory_query` reads.
- **It violates `ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS`** (`agents/assistant.py`),
  which states: *"You constrain the SHAPE of the reply, never where its content
  is found: never name a source for the answer's facts, and never settle what
  the answer will cover."* It settled both.
- **It failed in the precise way its own prompt predicts.** The next sentence of
  those instructions reads: *"The conversation history in front of you is the
  standing trap — it is the one source you can see, so it is the one you will be
  tempted to nominate, and nominating it tells the assistant to answer from the
  transcript instead of reading what is stored."* The phrase "I have previously
  determined" is the model doing precisely that: lifting a stance out of the
  transcript and promoting it to a constraint. The warning is verbatim in the
  prompt and did not hold.

This is not a soft influence. The criteria are injected into every later call as
`<acceptance_criteria_markdown>`, ranked **above** conversation history and above
the formatting guide in `SOURCE_PRIORITY_SECTION`. The run had committed to its
answer before it looked anything up.

### Step 3–4 — retrieval and scoring

Thirteen candidates: seven seed entries, six remembered facts.

| # | candidate | chars | similarity | direct | indirect | relevancy |
| --- | --- | --- | --- | --- | --- | --- |
| **3** | **`…hobby.cycling`** | **1947** | **693** | **1** | **3** | **3** |
| 2 | `…hobby.overview` | 755 | 224 | 1 | 2 | 3 |
| 1 | `…routes.2026` | 932 | 723 | 1 | 1 | 2 |
| 4 | `…history` | 1152 | 581 | 1 | 1 | 2 |
| 5–13 | 3 unrelated entries, 6 facts | 45–1303 | 218–699 | 1 | 1 | 1 |

Candidate 3 contains the answer, by name, and was rendered to the scorer in
full. The scorer's note:

> "None of the provided candidates contain this direct, factual […] information.
> The candidates are mostly about general history, locations, or other unrelated
> facts."

The filter call: 9571 input tokens, 589 output, **32.7 seconds**.

**The keep policy saved it anyway.** `apply_filter_scores` is relative first:
`FILTER_KEEP_TOP_N = 2` candidates survive on rank alone if their best scale
reaches `FILTER_KEEP_TOP_FLOOR = 2`. Candidate 3's `indirect: 3` put it at rank 0.
Kept.

### Step 5 — injection, and the cut

`_fact_line` (`agents/assistant.py:1146`) shortens anything over 1200 characters
with `text[:MEMORY_QUERY_PER_FACT_CHARS]` — head only. The entry is 1947
characters. The three nouns that answer the question sit at characters **1795,
1843 and 1856**: the last 8% of the answer, inside the 38% the cap removed.

So the decide model received a truncated fact carrying the topic but not the
answer; a note from the scorer asserting no candidate held it; and criteria
instructing it that the answer must be a reiteration of a limitation. It refused.

### Step 6 — the audit

`reply_audit` returned `{"reason": "The message is sound.", "verdict": "send"}` —
17 output tokens. The check that exists to catch a reply that does not answer the
request approved one whose entire content was a false statement about the
system's own capabilities.

## Every call in this run was the same model

Classifier, criteria, decide, filter, audit: one binding, one model, because the
assistant had a single model group at the time. The criteria's false premise, the
scorer's false note and the audit's approval are **not three independent
judgements**. They are one model's disposition, sampled five times, over
substantially overlapping context.

The per-call slots now on `/agentmodel` (`assistant.acceptance_criteria`,
`assistant.memory_filter`, `assistant.reply_audit`, …) make independence
*possible* for the first time. It is not automatic: binding them all to the same
group reproduces exactly this.

## What revision 1 got wrong

Recorded because the errors are a pattern worth recognising, not out of ceremony.

- **"The data is not the problem" / "a finer entry could not have ranked
  higher."** Wrong. A dedicated retrieval unit for the requested subtopic could
  have ranked first, scored `direct: 5`, and — being short — never been
  truncated. *Presence in the candidate set is not effective retrievability.*
  Revision 1 also contradicted itself: a later section conceded that smaller
  entries would have avoided the failure. An internally inconsistent document
  deserves to be read at its strongest claim.
- **"Align the scorer and the loop by truncating the scorer's input too."**
  Wrong, and worse than the disease: it buys consistency by making both stages
  blind, and it contradicts the same document's own survey of standard practice.
- **"Batch scoring is a property of the call's shape, not the prompt's
  wording."** Overstated, and it missed the actual mechanism sitting in the
  schema (below).
- **Naming truncation as *the* cause.** It is one of three, and the one that
  fired last.

The common thread: revision 1 reasoned from a single trace, found a satisfying
mechanism, and stopped. The trace contained a louder failure two steps earlier.

## The defects, restated

### A. The criteria call can decide the answer before the work starts

Severity: highest. It runs first, its output outranks the transcript in every
later prompt, and a wrong `assumptions` field is indistinguishable from a right
one downstream. The instructions already forbid exactly this and were not enough.

Structural options, none yet chosen:

- **Validate the field in code.** The criteria call is structured output; a
  validator can reject an `assumptions` value that asserts what the answer will
  be or claims a capability limit, the same way `_structured_completion` already
  rejects schema violations and retries with the reason attached.
- **Run the criteria call after the first read**, so "what is available" is
  observed rather than assumed. Costs a step and reorders the loop.
- **Withhold conversation history from the criteria call**, since the transcript
  is the named trap and the call's job is shape, not content.
- **Give it its own model** (`assistant.acceptance_criteria`) so it is not the
  same disposition as the audit that later approves its consequences.

### B. Retrieval units are far coarser than the questions asked of them

The traced parent covers onset, diagnosis, affected areas, treatment history,
progress, time cost and the current schedule — in one 1947-character answer with
three questions attached. That is several retrieval units in one record, and the
requested fact was one sentence of it.

Registry answer-length distribution (148 operator entries):

| | chars |
| --- | --- |
| p50 | 482 |
| p90 | 1684 |
| max | 5178 |
| over 1200 (today's cap) | 28 (18.9%) |
| over 2500 | 4 (2.7%) |

So 19% of entries are cut today. Raising the cap to 2500 would leave 4 — which is
why cap-raising looks like a fix and is actually a way of not noticing the
problem for a while.

### C. Head-only truncation, in two places

`_fact_line` (`agents/assistant.py:1146`) cuts seed entries with `text[:1200]`.
**A second, independent path cuts remembered facts** the same way at
`agents/assistant.py:1679`: `body[:MEMORY_QUERY_PER_FACT_CHARS]`. Revision 1's
checklist fixed only the first.

Both contradict a stated invariant: `agents/base.py:truncate_middle` calls itself
*"the one shortening shape every agent uses on text a model will read"* and warns
that *"a head-only cut throws away whichever end matters most, and does it
silently."*

**This bug has been fixed once already in this codebase.**
`assistant_run_summarizer.py:_REPLY_PREVIEW_CHARS` carries the post-mortem: a
500-char head-only cut showed the summarizer three and a half of six listed
languages, "and the summarizer — correctly, for what it was shown — called a
complete answer 'partial'." Same shape, different call, fixed in `867cd4a`.

### D. The scorer is anchored by its own schema

Revision 1 blamed the batch shape. The sharper mechanism is in the schema:
`FilterDecision` declares `reasoning` **before** `items`, and the class docstring
says this is deliberate — *"schema property order follows field order, so the
model writes its overall does-anything-match assessment first and the scores are
conditioned on it."*

That is an anchoring pipeline by construction:

1. summarise the whole set;
2. conclude nothing matches;
3. emit thirteen score rows consistent with the conclusion already written.

The observed note — "mostly about general history, locations" — is an accurate
description of candidates 5–13 and false of candidate 3. The design intended the
note to *calibrate* the scores; on this run it *determined* them.

Corrections to revision 1's framing: per-candidate scoring reduces cross-candidate
contamination but is not "structurally incapable" of misreading a passage; and
thirteen generative calls against a 32.7-second batch call is a poor latency
trade. Cross-encoders are routinely run in batches — the property that matters is
that each score is computed for one (query, passage) pair, not that batching is
absent.

### E. The scorer and the loop judge different documents

`seed_candidate_rows` renders full text; `_fact_line` renders 1200 characters. A
candidate can be kept on evidence the decide model never sees — this run — and,
invisibly, dropped because its opening looks irrelevant while its tail answers
the question, leaving only a score row.

Revision 1's fix (truncate both) was wrong. **The right resolution is that the
two representations differ by design:** match on small, self-contained units;
inject the winning unit, expanded to a neighbour window or its parent only when
needed. Then there is no disagreement to reconcile, because the scorer is judging
the thing that will be injected.

### F. A wrong assessment is injected beside the evidence

`<memory_filter_assessment>` carried the scorer's false note into the decide
prompt, next to a fact cut short of the answer, under criteria instructing a
refusal. It is framed as untrusted data, which guards against injection but not
against being wrong. It is lossy commentary, not evidence, and on this run it was
the third voice saying the same wrong thing.

### G. The escape hatch went unused

`DECIDE_TURN_INSTRUCTIONS` tells the model a `truncateN` fact can be re-read in
full via `memory_query {"uuid": ...}`, and the observation repeated the offer.
Steps remained. It was not taken. Whether *any* recorded run has ever used it is
an open question with a cheap answer.

## What others actually do

The production pattern is close to the instinct this proposal originally
dismissed:

- Split long records into passages or atomic propositions.
- **Make each chunk self-contained** by prefixing subject/topic context —
  Anthropic's own [Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
  describes exactly this: chunk, contextualise each chunk, index with embeddings
  **and** BM25, fuse, then rerank. Revision 1 surveyed several frameworks and
  missed the clearest published description.
- Retrieve broadly over chunks, fuse lexical and semantic rankings, rerank
  (query, chunk) pairs.
- Inject the winning chunk, expanding to neighbours or the parent when needed —
  Elasticsearch's `semantic_text` chunks documents and scores the parent by its
  best passage; LlamaIndex's sentence-window retrieves a sentence and expands to
  a local window, not to the whole parent.
- Chen et al., *[Dense X Retrieval](https://arxiv.org/abs/2312.06648)* (2023)
  finds atomic self-contained propositions outperform passage-level retrieval on
  downstream QA under a fixed context budget — the opposite of what revision 1
  cited it beside.

`truncate_middle` is an emergency patch. It is not retrieval.

## Proposed direction

### Now — stop the bleeding

1. **Fix both head-only truncation paths** (`_fact_line` and the claims path at
   `assistant.py:1679`) with `truncate_middle`.
2. **Re-size the per-fact cap against the real distribution**, not by feel.
   Revision 1's "2500–3000 fits comfortably" was wrong arithmetic: four
   3000-character facts are 12000 against an 11000 block. Size the per-fact cap
   as a share of remaining block budget, so one long fact is shortened only when
   it would actually crowd others out.
3. **Add a dedicated retrieval unit for the requested subtopic** — the concrete
   instance, fixed by hand, as the first worked example of the target shape.

### Next — measure, before any architecture

4. **Build the regression harness.** Revision 1's single metric ("did the
   answer-bearing substring reach the decide prompt") is necessary and
   insufficient: this run also shows criteria poisoning the prompt and an audit
   approving a false answer, neither of which that metric sees. Measure
   separately:

   | metric | catches |
   | --- | --- |
   | chunk recall@k | retrieval |
   | reranker MRR / nDCG | ranking |
   | answer-bearing evidence injected (chars vs answer chars) | truncation |
   | final answer correctness | the whole chain |
   | **unsupported-refusal rate** | defect A |
   | latency and token cost per turn | every "fix" that costs seconds |

   Neutral cases in the repo; operator-derived cases under `customize.dir`.
   Include cases that currently pass. Assert the **actual answer**, not the entry
   id and not a surviving substring.

5. **Bind the assistant's slots to different groups** — at minimum
   `assistant.acceptance_criteria`, `assistant.memory_filter` and
   `assistant.reply_audit` away from `assistant.default`. Configuration only, and
   it is the cheapest attack on the correlated-defences problem.

### Then — the architecture

6. **Derive child retrieval units from each parent entry**, keeping the parent as
   the authoritative record:

   ```
   parent Q&A entry
     ├── child: onset / history
     ├── child: affected areas
     ├── child: progress
     └── child: current schedule
   ```

   Each child carries a stable child id and parent id, a contextual prefix
   (subject + topic + subtopic) so it is self-contained, its own text, its own
   question aliases, and offsets into the parent's answer.

7. **Embed question aliases *and* child answer text** — not one vector per
   1947-to-5178-character answer. Run lexical retrieval over the same children.
   Fuse, and **cap each parent to one or two candidates** so siblings cannot
   crowd the budget (the failure mode revision 1 identified correctly and is the
   one thing it got right about granularity).
8. **Rerank (query, chunk) pairs**, and inject the winning chunk, expanding to
   neighbours or the parent only on demand.
9. **Drop `<memory_filter_assessment>` from the answering prompt.**
10. **Replace the generative Likert filter for seed Q&A.** On this run it cost
    32.7 seconds, was wrong, and the code-side rank rule — not the filter — saved
    the candidate. Replace with a reranker rather than delete outright: the same
    helper serves `query_filter_router`, and the decision should rest on the
    harness in (4), not on one trace.

### Also fix, independently

11. **Constrain the criteria call** — the options under defect A. This is the
    highest-severity defect and the one least addressed by anything else on this
    list; a perfect retrieval stack still loses to a run that has already decided
    to refuse.

## Traps

- **Reading the truncation fix as the fix.** It is one of three causes and the
  last to fire.
- **Reading the rank rule's rescue as health.** `apply_filter_scores` did its job.
  That it had to means scorer quality can degrade far before anything looks
  broken.
- **Measuring "kept" or "substring present" as success.** This run kept the right
  entry; a variant that also injected the answer could still refuse, because the
  criteria said to.
- **Assuming the checks are independent.** Criteria, scorer note and audit agreed
  with each other and disagreed with the data. Same model, overlapping context.
- **Deriving child chunks with an LLM and trusting the output.** A derivation
  pass over ~150 entries is itself a quality surface and needs the harness in (4)
  before it, not after.
- **Widening the candidate budget as a reflex.** More candidates make an anchored
  batch call worse.

## Open questions

- **Would a dedicated child chunk have rescued *this* run?** The review argues
  both that the run was already anchored on a refusal (defect A) and that a
  dedicated chunk scoring `direct: 5` would have fixed it. Those are in tension.
  Strong contrary evidence might have dislodged the criteria; it might have been
  explained away. Only the harness settles it, and the answer determines whether
  (11) or (6)–(8) is the real priority.
- **Should a `truncateN` tag force a re-read rather than offer one?** If defect G
  holds — the hatch is never used — either the loop re-reads automatically when a
  kept fact was cut and the request is specific, or the affordance is decoration.
- **Do remembered facts and seed entries belong in one scored set?** Six short
  facts against seven long entries under one policy and one budget is an unequal
  contest that has never been tested.
- **Where does a reranker run?** A second model server and a new dependency.
- **What is the derivation cost of child chunks?** ~150 entries, each needing a
  contextual prefix and defensible split points. Hand-authored is slow;
  LLM-derived needs its own gate.

## Where to continue

- [ ] Fix `_fact_line` and the claims path at `assistant.py:1679` with
      `truncate_middle`.
- [ ] Re-size the per-fact cap as a share of the block budget.
- [ ] Add a dedicated retrieval unit for the traced subtopic; re-run the query.
- [ ] Grep recorded runs for uuid-mode `memory_query` after a `truncateN` tag.
- [ ] Build the harness with the six metrics above; assert actual answers.
- [ ] Bind criteria, filter and audit slots to different groups; re-run.
- [ ] Add a code-side validator rejecting an `assumptions` field that nominates a
      source or settles the answer; measure unsupported-refusal rate before and
      after.
- [ ] Prototype child chunks with contextual prefixes on one parent family;
      measure recall@k against the harness.
- [ ] Embed child answer text; fuse lexical + semantic; cap candidates per parent.
- [ ] Price a cross-encoder reranker; compare against the Likert filter on the
      harness, including its 32.7-second baseline.
- [ ] Drop `<memory_filter_assessment>` from the answering prompt; A/B on the
      harness.
