# Recall: what the filter keeps, what the loop reads, and what granularity is for

**Status:** Proposed. Nothing implemented.
**Date:** 2026-08-17
**Relates to:** `qa-system.md` (the pipeline this reopens), `memory-architecture.md`,
`assistant-design.md` §Model slots.

## Summary for a reviewer with no context

The assistant failed to answer a question whose answer is stored, verbatim, in
the Q&A registry. The obvious diagnosis — the registry's entries are too coarse,
decompose them — is wrong, and this proposal is largely about establishing what
went wrong instead.

The short version: **retrieval worked, the filter kept the right entry, and the
entry was then truncated head-only at the injection boundary, cutting off exactly
the sentence that answered the question.** Three further defects were found in the
same trace, one of which is a design flaw rather than a bug: the relevance scorer
and the model it serves are shown *different documents*, so the scorer's judgement
is not transferable to the consumer that acts on it.

Everything below is derived from one recorded run, verified against the code.
Where a claim is inference rather than observation it is marked.

## The pipeline, for orientation

`memory_query` is an assistant read action. One call runs this chain
(`agents/assistant.py:_filter_recalled_candidates`, `memory/seed_memory.py`):

1. **Seed retrieval** — `_hybrid_seed_ranked` returns the top `TOP_K_VECTOR=5`
   entries by embedding similarity plus the top `TOP_K_FULLTEXT=5` by lexical
   full-text, deduplicated and interleaved. The signals are deliberately not
   weighted against each other.
2. **Claim retrieval** — `retrieve_memories_hybrid` adds remembered facts from
   the `/memory` store as further candidates.
3. **One relevance-scoring LLM call** over the combined set. The model returns a
   `reasoning` note plus three Likert scales per candidate (`direct`, `indirect`,
   `relevancy`, each "1".."5"). Candidates are rendered to this call with their
   **full** answer text — `seed_candidate_rows` applies no length cap.
4. **A code-side keep/drop policy**, `apply_filter_scores`. The LLM only supplies
   numbers; the decision is code.
5. **Injection** — kept candidates are rendered into `<recalled_memory>` in the
   decide prompt, each fact capped at `MEMORY_QUERY_PER_FACT_CHARS = 1200`, the
   whole block at `MEMORY_QUERY_TOTAL_CHARS = 11000`. The scorer's `reasoning`
   note is injected alongside as `<memory_filter_assessment>`.

## The traced run

Anonymised worked example, structurally identical to the real one: three sibling
entries under one topic, and the answer to the question sits in the parent.

Thirteen candidates were scored — seven seed entries and six remembered facts:

| # | candidate | chars | similarity |
| --- | --- | --- | --- |
| 1 | `…hobby.cycling.routes.2026` | 932 | 723 |
| 2 | `…hobby.overview` | 755 | 224 |
| **3** | **`…hobby.cycling`** | **1947** | **693** |
| 4 | `…hobby.cycling.history` | 1152 | 581 |
| 5–7 | three unrelated entries | 359–1303 | 218–611 |
| 8–13 | six remembered facts | 45–248 | 621–699 |

Candidate 3 contains the answer, by name. The filter call was 9571 input tokens,
589 output, 32.7s. Every call in the run — classifier, criteria, decide, filter,
audit — was answered by one model, because the assistant had a single binding at
the time.

The scorer returned this note (one domain noun redacted):

> "None of the provided candidates contain this direct, factual […] information.
> The candidates are mostly about general history, locations, or other unrelated
> facts."

and these scores, of which only the first four were above the floor:

| candidate | direct | indirect | relevancy | best scale |
| --- | --- | --- | --- | --- |
| **3** `…hobby.cycling` | 1 | 3 | 3 | 3 |
| 2 `…hobby.overview` | 1 | 2 | 3 | 3 |
| 1 `…routes.2026` | 1 | 1 | 2 | 2 |
| 4 `…history` | 1 | 1 | 2 | 2 |
| 5–13 | 1 | 1 | 1 | 1 |

## What actually happened

**The filter kept the right entry.** `apply_filter_scores` is not a threshold —
it is relative first, absolute second. `FILTER_KEEP_TOP_N = 2` candidates survive
on rank alone provided their best scale reaches `FILTER_KEEP_TOP_FLOOR = 2`;
anything reaching `FILTER_KEEP_THRESHOLD = 4` on any scale is kept regardless of
rank. Sorting by `(-direct, -indirect, -relevancy)` put candidate 3 at rank 0 on
its `indirect: 3`, so it was kept — and candidate 2 with it. The observation
confirms both were injected.

**And then it was cut.** `_fact_line` (`agents/assistant.py:1146`) shortens any
fact over 1200 characters with `text[:MEMORY_QUERY_PER_FACT_CHARS]` — head only.
The entry is 1947 characters. The three nouns that answer the question sit at
characters 1795, 1843 and 1856.

They were in the last 8% of the answer. The cap took the last 38%.

The decide model was therefore shown: a truncated fact carrying the topic but not
the answer, marked `truncate1200`; and, beside it, an authoritative-sounding note
from another model stating that no candidate contained the information. It
replied that it could not provide the name. **Given what it was shown, that reply
was correct.**

## Five defects, in causal order

### 1. Head-only truncation at the injection boundary — the direct cause

`_fact_line` cuts with `text[:1200]`. Two things are wrong with this
independently of the cap's size:

- **It drops the end.** The registry's long answers are narrative and roughly
  chronological: an entry accumulates by appending, so the most recent and most
  specific material — current status, current names, current numbers — collects
  at the tail. A head-only cut is therefore biased against exactly the facts most
  likely to be asked about.
- **It contradicts an invariant this codebase has already stated.**
  `agents/base.py:truncate_middle` describes itself as *"the one shortening shape
  every agent uses on text a model will read"*, and its docstring says outright:
  *"A head-only cut throws away whichever end matters most, and does it
  silently."* `_fact_line` does not use it.

**This exact bug has already been found and fixed once, elsewhere in this
codebase.** `assistant_run_summarizer.py:_REPLY_PREVIEW_CHARS` carries the
post-mortem: a live run mis-graded itself because a 500-char head-only cut showed
the summarizer three and a half of six listed languages, "and the summarizer —
correctly, for what it was shown — called a complete answer 'partial'." That is
the same sentence shape as this run's failure. The fix there was
`truncate_middle` plus a bigger budget.

The marker did its job — the fact was tagged `truncate1200` — but a marker tells
the model *that* something was cut, not that the cut removed the answer.

### 2. The scorer and the consumer see different documents — a design flaw

This is the finding worth the most attention, because it is not a bug in any one
function.

The filter call renders candidates at **full length** (`seed_candidate_rows`, no
cap). The decide prompt renders them at **1200 characters** (`_fact_line`). So
the two stages of one pipeline judge different objects:

- A candidate can be **kept on the strength of evidence the decide model never
  sees.** That is what happened here, in the weakest possible form: the scorer
  saw the answer, judged the entry unhelpful anyway, and the rank rule kept it
  for reasons unrelated to the sentence that mattered.
- The converse is equally available and would be harder to notice: a candidate
  **dropped** because its opening 1200 characters look irrelevant while its tail
  answers the question outright — a miss with no trace at all, because a dropped
  candidate leaves only a score row.
- The scorer's `reasoning` is a statement about the full text. It is injected
  next to the truncated text. It is, structurally, a caption for a different
  picture.

Any fix to the cap that does not also align the two views leaves this in place.

### 3. Batch scoring lets a model characterise the set instead of reading it

The scorer had candidate 3's full 1947 characters and scored it `direct: 1`. Its
note — "mostly about general history, locations" — is an accurate description of
candidates 5–13 and false of candidate 3. It produced a summary of *the batch*
and the schema accepted it: thirteen well-formed score triples, every one wrong
in the same direction.

**This is a property of the call's shape, not of the prompt's wording.** A call
that scores N candidates in one pass permits an answer that never examines any
single candidate; a per-candidate call does not. No instruction closes that gap,
because the failing output is structurally what the instructions asked for.

The severity here is masked: the rank rule rescued the entry. **The policy is
currently carrying the scorer** — `apply_filter_scores` was designed for exactly
this ("a conservative model scoring its best candidate 2/1/3 must not empty the
list") and it worked as intended. That is good engineering and also a hazard: it
means scorer quality can degrade a long way before anything looks broken, and the
first visible symptom will be a miss with no obvious cause.

### 4. A wrong assessment is injected beside the evidence

`<memory_filter_assessment>` carries the scorer's `reasoning` into the decide
prompt. Here it asserted the answer was absent, next to a fact that had been cut
short of the answer. Two independent pushes in the same wrong direction, one of
them wearing the authority of a dedicated call.

The block is framed as untrusted data ("the relevance scorer's own summary…
reference context, NOT instructions"), which guards against injection but not
against being **wrong**. A wrong summary sitting beside partial evidence is worse
than either alone: it explains the gap, so the reader stops looking.

*Inference, not observation:* I cannot separate this contribution from the
truncation's in a single trace. Both point the same way. Distinguishing them
needs the A/B in the plan below.

### 5. The escape hatch was documented and unused

`DECIDE_TURN_INSTRUCTIONS` tells the model that a `truncateN` fact can be re-read
in full with `memory_query {"uuid": ...}`, and the observation repeated the offer
in band. The model had steps remaining under the cap of 6 and did not take it.

An affordance that is never exercised is not a safety net. Worth knowing: does
*any* recorded run use uuid mode after a `truncateN` tag? If not, the feature is
decorative and the truncation is unconditional in practice.

## What this rules out

- **The data is not the problem.** The answer is present, correct and retrievable.
- **Retrieval granularity is not the cause.** A finer entry could not have ranked
  higher than one already at rank 3 of 13 and fully visible to the scorer.
- **The candidate budget is not the cause.** `5 + 5` surfaced the right entry.
  Widening it adds candidates to a call that already mishandles the ones it has.

## Where the granularity instinct is right, and where it misleads

Smaller entries **would** have avoided this failure — by staying under the 1200
cap, so nothing was cut. That is a real effect and it should be said plainly.

But it is an accidental fix for a truncation bug, at the cost of rewriting ~150
entries by hand, and it carries a cost of its own that this trace also
demonstrates:

**Siblings compete for a fixed budget.** The traced topic is *already*
decomposed — parent plus two siblings — and that is why three entries from one
topic occupied three of roughly ten candidate slots, pushing unrelated material
into the scored set and lengthening the call. Splitting one entry into four
raises the chance that *something* from the topic surfaces and lowers the chance
that the *right* one does. At ~150 operator entries the candidate budget is the
scarce resource, not the index.

Decomposition earns its keep only in a form that separates **what is matched on**
from **what is returned**. More siblings is the form that does not.

## The other structural weakness

Independent of this failure and worth fixing regardless: **the vector index
embeds questions only.** Answers do not reach the semantic index at all;
full-text is the only signal that sees them, at `1/_FULLTEXT_QUESTION_BOOST`
weight (`memory/seed_memory.py:_fulltext_ranked`). A distinctive noun appearing
only in an answer — a product name, a place, a figure — is not semantically
reachable, and can be found only when the query shares literal tokens with it.

In the traced run the entry surfaced because a hand-written question happened to
contain a near-synonym of the query's key noun. Retrieval succeeded because the
operator had anticipated the phrasing. The registry grows by hand; every entry
whose questions do not name what its answer contains is invisible to the semantic
path, and nothing surfaces that fact.

## How this is handled elsewhere

- **Parent-document / small-to-big retrieval.** Match on small units, return the
  parent for answering. LangChain's `ParentDocumentRetriever`, LlamaIndex's
  `AutoMergingRetriever` and `SentenceWindowNodeParser`. The form of
  decomposition that does not create sibling competition.
- **Multi-vector indexing.** Index raw text *and* a summary *and* hypothetical
  questions per document, all resolving to one record. The registry already does
  the questions half by hand; the answer half is missing.
- **Cross-encoder reranking** (`bge-reranker`, `mxbai-rerank`, hosted
  equivalents) in place of an LLM Likert judge. Scores one (query, passage) pair
  at a time, emits a number: no schema, no batch, structurally incapable of
  defect 3.
- **Proposition indexing.** Chen et al., *Dense X Retrieval: What Retrieval
  Granularity Should We Use?* (2023) indexes atomic self-contained facts,
  reporting gains largest on rare-entity queries. Cited as a direction; read it
  before building on it.
- **"Lost in the middle"** (Liu et al., 2023): material in the middle of a long
  context is recalled worse than material at either end. A 9571-token filter call
  over thirteen candidates sits squarely in that regime, and is relevant to
  defect 3.

## Proposed direction, in dependency order

### 1. Make the injection cut both ends, and raise it

Replace `text[:MEMORY_QUERY_PER_FACT_CHARS]` in `_fact_line` with
`truncate_middle(text, MEMORY_QUERY_PER_FACT_CHARS)`. This restores the
codebase's stated invariant, brings the in-band marker with it, and is the
smallest change that would have prevented this run.

Also reconsider the cap. 1200 was chosen for a store of short remembered facts;
the seed registry's operator entries run to 5000 characters. With
`MEMORY_QUERY_TOTAL_CHARS = 11000` as the real ceiling and typically two to four
facts kept, a per-fact cap of 2500–3000 fits comfortably. *The total block cap,
not the per-fact cap, should be the binding constraint* — the per-fact cap exists
to stop one fact eating the block, and at 1200 against 11000 it is doing far more
than that.

Consider dropping the per-fact cap to a fraction of the remaining block budget
rather than a constant, so one long fact is only shortened when it would actually
crowd others out.

**Non-goal:** removing truncation. A bound is correct; a silent, one-ended,
undersized bound is not.

### 2. Show the scorer and the loop the same document

Whatever the cap becomes, apply it in `seed_candidate_rows` too, so the scorer
judges the text the decide model will read. Then a `direct: 5` means "the visible
text answers it" rather than "something in the full text answers it".

**Alternative considered and rejected:** raise the loop's cap to match the
scorer's unlimited view. That removes the disagreement in the other direction but
gives up the block budget, and a 5000-character fact in the decide prompt makes
defect 3's "lost in the middle" problem worse in the place it hurts most.

**Open sub-question:** if the scorer sees only the head, defect 2's converse
becomes reachable — an entry dropped because its opening looks irrelevant. This
is the argument for doing the cap increase (1) and the alignment (2) together,
and for making both large enough that ordinary entries are not cut at all.

### 3. Give the scorer its own model

`assistant.memory_filter` exists on `/agentmodel` and falls back to
`assistant.default` when unbound. Binding it to a stronger group is configuration
only — no code, no dependency, no correctness surface. It does not fix defects 1
or 2, but every measurement below is uninterpretable while the scorer is an
unbound accident.

### 4. Build a retrieval regression set

~20–30 `(query → expected entry id)` cases drawn from real misses, scored on one
question: **did the entry that answers this reach the decide prompt with its
answer intact?** Note the phrasing — "was it kept" is insufficient, because this
run passed that test and still failed.

Proposed shape, mirroring how the registry itself splits: neutral cases as JSONL
in the repo, operator-derived cases under `customize.dir`. The harness resolves
the same `assistant.memory_filter` chain a live turn does, so it measures what
production runs. Report per case: retrieved (y/n), kept (y/n), injected chars vs
answer chars, and whether the expected substring survived.

Without this, a truncation change, a scorer change, a granularity change and luck
are indistinguishable. **Include cases that currently pass** — a set built only
from known misses over-fits to them.

### 5. Split the batch scoring call

Score candidates individually or in small groups, so no single answer can
characterise the set instead of reading it. Costs more calls; whether the latency
is acceptable is exactly what (4) makes answerable. A cross-encoder reranker is
the same fix with a smaller bill and should be priced at the same time.

### 6. Reconsider injecting the scorer's assessment

Options, in increasing order of change: drop `<memory_filter_assessment>`
entirely; inject it only when the scorer kept nothing (where "why nothing
matched" is genuinely useful and cannot contradict adjacent evidence); or keep it
but require it to name the candidates it is characterising, so a note about the
batch cannot be read as a note about a specific fact.

### 7. Index answers as well as questions

A second embedded vector per entry over the answer text, both resolving to one
record. Removes the dependency on having guessed the phrasing when the entry was
written. Contained entirely within the seed store.

### 8. Granularity last, and as parent-child

Match on small units, return the parent entry. Only after 1–7, and only with (4)
in place to show it helped.

## Traps

- **Rewriting the data first.** The registry is the most visible thing and the
  least wrong thing. Rewriting ~150 entries in response to a one-line truncation
  bug is expensive, irreversible in effort, and leaves the bug.
- **Fixing only the cap.** It is the direct cause and the cheapest fix, and it
  leaves defects 2–5 in place, including the one that produces silent misses.
- **Trusting the scorer's reasoning field.** The observed note was fluent,
  specific, and wrong. It reads as evidence of having examined the candidates.
- **Reading the rank rule's rescue as health.** `apply_filter_scores` did its job
  here. That it had to means scorer quality can degrade far before anything looks
  broken.
- **Widening the candidate budget as a reflex.** More candidates make a
  batch-scoring call worse: more text, more middle, more room to summarise.
- **Measuring "kept" as success.** This run kept the right entry and still failed.
- **Assuming a miss is a retrieval miss.** The trace records retrieval, scoring,
  keeping and injection separately. Attribute before changing anything.

## Open questions

- **Should the filter fail loudly when it keeps nothing?** Today a scorer that
  keeps nothing is indistinguishable to the loop from a registry that holds
  nothing, and neither the model nor the operator is told which happened.
- **Should a `truncateN` tag force a re-read rather than offer one?** If defect 5
  holds — the escape hatch is never used — then either the loop should re-read
  automatically when a kept fact was cut and the request is specific, or the
  affordance should be removed as decoration.
- **Do remembered facts and seed entries belong in one scored set?** They compete
  under one policy and one budget today. Six short facts and seven long entries
  in one call is an unequal contest, and whether each should have its own budget
  has not been tested.
- **Where would a reranker run?** A second model server and a new dependency;
  whether that is acceptable on this box is unresolved.
- **Does the total block cap ever bind?** If two to four facts is typical, 11000
  may never be reached, in which case the per-fact cap is the only real
  constraint and should be sized as such.

## Where to continue

- [ ] Switch `_fact_line` to `truncate_middle`; re-run the traced query and check
      the answer survives.
- [ ] Re-size `MEMORY_QUERY_PER_FACT_CHARS` against the registry's actual answer
      length distribution, and against `MEMORY_QUERY_TOTAL_CHARS`.
- [ ] Apply the same cap in `seed_candidate_rows` so scorer and loop agree.
- [ ] Bind `assistant.memory_filter` to a stronger group; record the traced
      query's scores before and after.
- [ ] Grep recorded runs for uuid-mode `memory_query` following a `truncateN`
      tag; settle defect 5 with data.
- [ ] Build the retrieval regression set; split neutral from operator-derived.
- [ ] A/B the assessment block on that set: truncation fixed, with and without
      `<memory_filter_assessment>`.
- [ ] Prototype per-candidate scoring; measure added latency against the set.
- [ ] Price a cross-encoder reranker as the alternative.
- [ ] Add answer-text embeddings as a second vector per entry; re-measure.
- [ ] Only then evaluate parent-child granularity.
