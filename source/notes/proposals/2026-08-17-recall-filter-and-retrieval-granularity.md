# Recall: what is ready, what is decided, what is not designed

**Status:** Partially ready. Two behavioural changes plus deterministic
regression fixtures are implementable; the retrieval architecture is not
designed and must not be built from this document.
**Date:** 2026-08-17 (revised 2026-08-18)
**Revision:** 5 — revision 4 was approved in shape by a fourth review, subject
to corrections now applied: the fixture sequencing would have merged a red CI
commit, the fixtures had to be stated as synthetic, `truncate_middle` does not
preserve a 1200-character rendered cap, and R1's second call site changed its
decision. Revision 3's factual errors are kept in the table below rather than
quietly folded in; git holds the earlier text.
**Relates to:** `qa-system.md`, `memory-architecture.md`,
`assistant-design.md` §Model slots and §Acceptance criteria.

## What happened, in five lines

A turn refused to answer a question whose answer is stored verbatim in the Q&A
registry. Retrieval found the entry and the keep policy kept it. Three **distinct
failure modes** then agreed with each other: the acceptance-criteria call had
already written that the answer must be a refusal; the recall scorer read the
answering text and called it irrelevant; and the injection cut the answering
sentence off head-only at 1200 characters. The reply audit approved the result in
17 tokens. Evidence in the appendix.

They were not independent — every relevant model call in the run (classifier,
criteria, decide, scorer, audit) resolved to the same model over overlapping
context. Revisions 1–3 called them independent while arguing the opposite; the
word is wrong and is corrected throughout.

## Corrections carried from earlier revisions

Stated plainly because two of them are load-bearing and one is unsafe.

| revision 3 claimed | actually |
| --- | --- |
| `assumptions` is "the only field that carries free-form prose about content" | **False.** `processing`, `formatting` and `assumptions` are all `str` with `min_length=1`, all required, all model-written prose (`AcceptanceCriteria`, `assistant.py:350`). Nothing stops the next capability claim landing in `processing`. |
| Children can be regenerated because "the index is rebuilt, not patched"; "stale removal: none needed" | **False, and unsafe.** The Repopulate button calls `sync_kb`, which reconciles row by row "instead of wiping it" and leaves failed rows stale by design. `rebuild_kb` does truncate but is non-transactional — "the table may then be empty or partially populated (PGVectorStore inserts row-by-row, no wrapping transaction)". Rebinding `_entries_by_id` in memory removes no vector nodes. |
| Child ids are stable as a hash of (parent id, subtopic slug) | **False.** The slug would be LLM-produced. A deterministic hash of nondeterministic input is nondeterministic, and it has no answer for duplicate slugs, splits or merges. |
| The counterfactual matrix includes "a dedicated child unit only" at step 6 | **Impossible as sequenced.** Hand-authored children were rejected, LLM derivation was blocked until the harness existed, and the prototype was step 8. |
| D1's rollback rule: wrong if clarification falls "without a matching fall" in refusals | **Not a criterion.** Different denominators, different harms. A 40% loss of correct clarification is not paid for by a 1% refusal reduction. |
| Step 7 is "a small, independently revertible commit" | **No.** It changes configuration state in `agent_model_binding`. Git reverts nothing. |

I also misattributed a recommendation in my reply to the second review, saying its
item 7 proposed deleting the Likert filter. Item 7 of that review was the
budget-allocation criticism; the delete recommendation came from the first
review's build list. The rebuttal was aimed at the wrong item.

## A finding that strengthens the block on D1

Revision 3 proposed removing `assumptions` from the injected criteria. The review
is right that this is channel whack-a-mole. It is worse than that:

**Most of the criteria block is a model paraphrase of a deterministic artifact
that the same prompt already carries — injected at higher priority than the
artifact itself.**

`_criteria_formatting_guide` feeds the criteria call the guide rendered by
`user_profile.format_formatting_guide` — deterministic, no model involved. The
criteria call's `formatting` field restates it. The decide prompt then carries
**both**: `formatting_guide` at tier 1, and `acceptance_criteria_markdown` at
tier 3 — and in `ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION` the paraphrase is
**rank 4** while the artifact it paraphrases is **rank 5**.

So the answering prompt is told to prefer a model's restatement of a
code-generated block over the block. `formatting` and `processing` are largely
redundant, lossy, and outrank their own source; `assumptions` is the field with
no deterministic source at all, and is where the incident's false premise landed.

That is the argument for the structural fix, and it is not a fix that can be
sequenced casually.

## Ready to implement now

Scoped so each is a small, independently revertible commit with an obvious
correctness argument.

### R1 — Stop injecting `<memory_filter_assessment>` beside recalled facts

`_recall_filter_assessment_line` is appended on **two** branches: the empty
result (`assistant.py:1653`) and the populated result (`assistant.py:1713`).
Revision 4 proposed removing both. Verifying the second branch changed the
decision.

**Decision: remove it from the populated branch only.**

On the populated branch the note sits beside the facts and can contradict them —
which is the incident. On the empty branch there are no facts for it to
contradict, and the code carries a documented rationale worth keeping: *"The
empty result is exactly when the operator wants to see what the recall filter
considered and dropped — and give the model the filter's own why-nothing-matched
note."*

Revision 4 rejected "inject only when the scorer kept nothing" as adding a
conditional section. That was wrong twice over: the conditional already exists in
the code, and the branch it was rejecting is the one with the better argument.

Both branches keep the note in the trace and in the observation data, where the
operator reads it. Tests must cover both, or the note stays model-visible on the
less common path.

**Open:** if evidence later shows the note misleads even with no facts beside it,
the empty branch goes too. Nothing yet shows that.

### R2 — One truncation renderer, using `truncate_middle`

Two head-only cuts exist: `_fact_line` (`assistant.py:1146`) for seed entries and
a second at `assistant.py:1679` for remembered facts. Revision 3 proposed
patching both.

**Patching both preserves the drift that produced the bug.** Route both through
one shared renderer, and have that renderer call `truncate_middle` —
`agents/base.py` already calls it "the one shortening shape every agent uses on
text a model will read". The same bug was fixed once for the run summarizer
(`867cd4a`); it recurred here because there were two paths.

**Source-character allowance unchanged at 1200.** Note this is not a rendered
cap: `truncate_middle(text, 1200)` retains 1200 source characters and then adds
its in-band marker — measured at 1249 characters returned, the overhead varying
with the digit count of the dropped-character total. Today's `text[:1200]`
renders at most 1200. So the change is a small rendered-length increase per
truncated fact against the 11000-character block budget.

R2's tests must therefore pin: maximum rendered line length; total-block
behaviour at the boundary; that no candidate is newly omitted through marker
overhead; and how the `truncate1200` tag and the in-band marker read together
(the tag names a source allowance, the marker states an actual drop — they are
not the same number and should not look like they are).

Sizing of the allowance itself is benchmarked once measurement exists, not
guessed. Revision 3's "share of remaining budget" scheme is withdrawn: it is
order-dependent and would let early facts starve later ones.

### R3 — Deterministic regression fixtures reproducing the failure

Not a live end-to-end test. Fixed scorer, criteria and decide outputs replayed
against the real rendering path.

**Fixture data is synthetic.** A structurally equivalent neutral record —
same length, same position of the answer-bearing span, same sibling shape —
lives in the repository. The operator's actual case lives only under
`customize.dir`. No private answer text may appear in a repository fixture, a
snapshot, a failure message or CI output. The first implementation step must not
violate N1.

**Sequencing.** Each regression test lands **in the same commit as the fix it
guards**, never before it:

| commit | test it carries |
| --- | --- |
| R3a (first) | characterization: the answer-bearing span is **absent** from the decide prompt, and the assessment block **is** present — recording the behaviour as it is today |
| R1 | the assessment block is absent on the populated branch, present on the empty branch |
| R2 | one renderer; both fact kinds cut from the middle; the span survives |

R3a is the recorded pre-fix fixture and stays green throughout by asserting the
*old* behaviour until its guarded commit inverts it. No commit is merged red, and
no test is marked expected-failure.

These are CI tests. They cannot say why a model behaved as it did — that needs
the separated evaluation tiers below, which are not designed.

## Decided in principle, blocked on design and measurement

### B1 — No model-written prose in the authoritative criteria block

**Direction:** inject only code-generated, typed values — locale, units, date
format, separators, spelling — which the formatting guide already produces
deterministically. If the loop needs an ambiguity signal, expose something narrow
and typed (`needs_clarification: bool` plus non-authoritative diagnostic text),
or let the decide call identify ambiguity itself, which is the call that can
actually read the evidence.

Revision 3's D1 (drop `assumptions`, keep the other two) is **withdrawn**: it
removes the field that failed and leaves two equally unconstrained ones.

**Why it is blocked rather than ready:** the direction is settled; the mechanism
is not. Three variants remain live — a typed `needs_clarification` signal, a
non-authoritative diagnostic string, or ambiguity detection moved into the decide
call — and measurement cannot evaluate a mechanism nobody has chosen. The
variants and their expected behaviour must be written down first, then measured.

It also changes what every answering prompt carries and removes the only path by
which an unresolved ambiguity becomes a clarifying question, so it needs the
evaluation tiers to show what that costs. Also open: whether a required field no
consumer reads should be generated at all — today `assumptions` costs tokens on
every turn.

## Not designed — do not build from this document

Each names what it needs before it can be designed, rather than proposing a
shape.

### N1 — Privacy governance for any derivation over the registry

**This is a release blocker and revision 3 did not mention it.** The operator
overlay holds identity, health, relationship, sexual, financial and family
records. Revision 3 wrote "the derivation pass is an LLM pass over the registry"
as a single clause under an architecture decision.

Before any such pass is designed, the following must be settled and written down.

**The derivation call:** local-only execution or an explicit allowlist of
providers; provider retention and logging posture; explicit operator consent per
scope; caching so unchanged private records are not re-sent on every repopulate;
and behaviour when no approved derivation model is reachable.

**The derived artifacts, which are themselves sensitive personal data:**
contextualised child text, embeddings, caches, debug traces and the output of
*failed* derivations all inherit the sensitivity of their source. Governance must
cover storage and backups; retention and deletion; what may appear in logs and
exception messages; how `shield` values propagate to derived units and what
happens when a parent's shield changes *after* derivation (an unshielded child of
a shielded parent is a leak, and so is a stale child of a newly shielded parent);
removal of every derived artifact when a parent is deleted; and whether an
embedding is treated as sensitive even though it carries no plaintext.

No part of the retrieval architecture proceeds before this exists.

### N2 — Child-index lifecycle

Needs, at minimum: parent-aware deletion of a parent's previous children;
staging with atomic swap, or per-parent insert-before-delete; preservation of the
prior child set when derivation fails; and stamps carrying parent content hash,
derivation prompt version, model version and schema version.

Revision 3's answer ("regenerated on repopulate, no stale removal needed") rested
on a false reading of `sync_kb` and must not be reused.

### N3 — Child identity

Treat chunk ids as internal, versioned, disposable retrieval coordinates; keep
the parent uuid as the stable public identity. Do not promise durable child
identity until something consumes it. Splits, merges and duplicate slugs need
defined behaviour.

### N4 — The parent/child retrieval contract

Revision 3 was internally inconsistent: children do not inherit aliases (D4) but
"question aliases and child answer text" are embedded (D8). Which object owns the
question vector is unanswered, and each answer has a cost — aliases on children
reintroduces sibling crowding; aliases only on the parent cannot select a child;
fan-out to all children moves candidate inflation into reranking.

This is a cross-cutting contract change, not a new index. It must specify what
`Match.qa_id`, `get_entry`, `_resolve_match`, exact-alias lookup, uuid lookup and
dynamic entries do when a result points at a child, and what `QueryAgent`,
`QueryRouter`, `QueryFilterRouter` and the assistant's `memory_query` each
receive.

### N5 — Evaluation design

Revision 3 listed metrics without a test design. Three tiers must be separated:
deterministic tests with fixed model outputs (R3); replay of recorded traces for
stage attribution; live repeated evaluation for model behaviour. A live LLM
failure is not a stable CI test.

Every metric needs a definition before it is quoted: nDCG needs graded relevance
labels; "actual answer" needs accepted aliases or an explicit judge;
unsupported-refusal rate needs a labelled answerable-query denominator;
clarification rate needs a set of genuinely ambiguous queries; error correlation
across criteria, scorer and audit needs comparable binary error definitions and
enough cases to mean anything. Twenty cases with undefined labels produce
decorative percentages.

Causality needs paired repeated trials at fixed model configuration reporting
pass rates — not one run per cell.

### N6 — Gates and rollback that are actually criteria

Independent gates with numbers, not a "matching fall": unsupported refusals
improve by at least X; correct clarification stays within non-inferiority margin
Y; incorrect clarification does not rise beyond Z. Cost gates must separate
offline derivation cost from per-query latency and state a budget for each — a
real quality gain may justify a bounded runtime increase, and "equal or lower
cost" forbids that by accident.

Configuration changes (model bindings) need a snapshot of prior values and a
documented configuration rollback, separate from git.

### N7 — Whether the generative Likert filter survives

On the traced run it cost 32.7 seconds, was wrong, and the code-side rank rule
rather than the filter is what saved the candidate. That is grounds to evaluate a
reranker against it — on defined cases, per N5 — not grounds to delete a
component that also serves `query_filter_router` on the evidence of one trace.

## Sequence

1. R3a — synthetic characterization fixtures recording today's behaviour.
2. R1, then R2 — each its own commit, carrying the test that inverts its
   characterization assertion. CI green at every commit.
3. N5 — design the three evaluation tiers and define every metric.
4. N1 — privacy governance, written and agreed.
5. B1 — with the cost measured under N5/N6.
6. N2–N4, N6 — design, then evaluate, then build.
7. N7 — decide on the evidence.

Nothing past step 2 is implementable from this document, which is the point of
saying so here.

## Open questions

- **Was the criteria call or the truncation decisive?** Not answerable from one
  trace, and not answerable by a single rerun per condition either. Needs N5.
- **Should a `truncateN` tag force a re-read rather than offer one?** The
  affordance exists, steps remained, it was not used. Cheap to settle: grep
  recorded runs for uuid-mode `memory_query` after a `truncateN` tag.
- **Should `assumptions` be generated at all** if no consumer reads it after B1?
- **Do remembered facts and seed entries belong in one scored set?** Six short
  facts against seven long entries under one policy and one budget, never tested.

## Appendix — the evidence

### The criteria call, step 2, before any retrieval

`assumptions` came back as (domain nouns redacted):

> "The user is asking for specific […] information ([…] name) that requires
> querying stored memory, **which I have previously determined I cannot access
> due to privacy and security limitations**. The scope of the answer must
> therefore be a reiteration of this limitation…"

False — stored memory is what `memory_query` reads. It violates
`ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS` (*"never name a source for the answer's
facts, and never settle what the answer will cover"*), and it failed in the
manner that prompt names: *"The conversation history in front of you is the
standing trap — it is the one source you can see, so it is the one you will be
tempted to nominate."* "I have previously determined" is the model doing exactly
that.

**Bounded claim.** The criteria strongly predisposed the decide and audit calls
toward refusal. Not decisive: successful current-turn observations are
source-priority rank 1 and `acceptance_criteria_markdown` is rank 4, so fresh
evidence is supposed to override it.

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
note — *"None of the provided candidates contain this direct, factual […]
information. The candidates are mostly about general history, locations, or other
unrelated facts."* — describes candidates 5–13 accurately and candidate 3 falsely.
9571 in / 589 out / **32.7s**.

**The scorer never receives the acceptance criteria.** `_recall_filter_prefix` is
request + conversation history + identity only, so criteria poisoning does not
explain this miss.

**The anchoring mechanism is in the schema.** `FilterDecision` declares
`reasoning` before `items`, deliberately — *"the model writes its overall
does-anything-match assessment first and the scores are conditioned on it."*
Summarise, conclude nothing matches, emit rows consistent with the conclusion.
Per-candidate scoring reduces cross-candidate contamination but is not incapable
of misreading a passage, and thirteen generative calls against one 32.7-second
call is a poor trade.

**The keep policy saved it.** `FILTER_KEEP_TOP_N = 2` survive on rank alone above
`FILTER_KEEP_TOP_FLOOR = 2`; candidate 3's `indirect: 3` put it at rank 0. That
the policy had to rescue the scorer means scorer quality can degrade a long way
before anything looks broken.

### Injection, step 5

Cut at 1200 characters, head only. The entry is 1947 characters; the three nouns
answering the question sit at **1795, 1843 and 1856** — the last 8% of the
answer, inside the 38% removed.

### The audit, step 6

`{"reason": "The message is sound.", "verdict": "send"}` — 17 output tokens.

### Registry shape

148 operator entries: p50 482 chars, p90 1684, max 5178; 28 (18.9%) over today's
1200 cap, 4 (2.7%) over 2500. The traced parent covers onset, diagnosis, affected
areas, treatment history, progress, time cost and current schedule in one answer
with three questions attached — which is the case for finer retrieval units, and
is not a case this document is yet able to design.

### A correction carried from revision 2

Revision 2 claimed a candidate could be **dropped** because its opening looked
irrelevant while its tail answered the question. That cannot happen today:
`seed_candidate_rows` renders the full answer to the scorer. The claim was
inherited from revision 1's rejected "truncate the scorer's input too" design.
The real effect is weaker — a short relevant span diluted inside a long document
— and it still argues for finer units.
