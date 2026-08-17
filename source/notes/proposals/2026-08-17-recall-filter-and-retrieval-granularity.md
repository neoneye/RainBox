# Recall: what is ready, what is decided, what is not designed

**Status:** Partially ready. Two behavioural commits (R1, R2), each carrying its
own permanent tests, are implementable. The retrieval architecture is not
designed and must not be built from this document.
**Date:** 2026-08-17 (revised 2026-08-18)
**Revision:** 7 — R1 and R2 are approved for execution. Revision 6 fixed the
unsafe branch decision and the inconsistent budget arithmetic; this revision adds
the mechanical contracts a sixth review asked for, so nothing is invented during
coding: the symbols R1 removes, the existing assertion it inverts, the block
budget's exact meaning, the helper's signature and guarantees, and the claim
renderer's `"no evidence"` fallback. Earlier factual errors are kept in the table below rather than
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

Two behavioural commits. Each carries its own permanent tests, verified failing
against the current code before the fix is applied — no commit is merged red and
no test asserts behaviour that a later commit rewrites.

Neither fix needs a model. Both are rendering changes on the
`_action_query_memory` path, which is the real path and makes for less brittle
tests than replaying fixed criteria/decide outputs.

**Fixture data is synthetic.** A structurally equivalent neutral record — same
length, same position of the answer-bearing span, same sibling shape — lives in
the repository. The operator's actual case lives only under `customize.dir`. No
private text may appear in a repository fixture, an assertion message, a
snapshot or CI output. The first implementation step must not violate N1.

### R1 — Remove `<memory_filter_assessment>` from both output branches

`_recall_filter_assessment_line` is appended on two branches: the empty result
(`assistant.py:1653`) and the populated result (`assistant.py:1713`). **Both go.**

Revision 5 kept it on the empty branch, reasoning that with no facts beside it
there was nothing for it to contradict. That has it backwards, and the reversal
is the correction that matters most in this revision: **with no facts beside it,
an unsupported explanation becomes the only substantive thing the answering model
reads.** The scorer has already demonstrated on this very run that it will
manufacture a confident false negative. The branch I called safer is the one
where that note faces no contradiction at all.

Operator visibility is not a reason to inject into a prompt. It is already served
by `observation.data["recall_filter"]`, the trace row and the inspector, none of
which change.

Acceptance criteria, permanent:

- populated `obs.text` contains no `<memory_filter_assessment>`;
- empty `obs.text` is the neutral empty-result message only;
- both branches still carry recall-filter diagnostics in `obs.data`;
- trace and inspector visibility unchanged.

**Symbols removed** (each used only inside `assistant.py`, verified):
`RECALL_FILTER_ASSESSMENT_CHARS`, `_ASSESSMENT_FENCE_OPEN`,
`_ASSESSMENT_FENCE_CLOSE`, `_recall_filter_assessment_line`. Dead fencing left
behind would invite the next caller to re-inject.

**Existing test to invert:** `test_assistant_actions.py:513` currently asserts
the note reaches `obs.text` — `"<memory_filter_assessment note=" in obs.text`,
`"NOT instructions" in obs.text`, and
`obs.text.rstrip().endswith("</memory_filter_assessment>")`. It becomes the
assertion that none of that is present, while keeping its check that the
reasoning survives in `obs.data`.

### R2 — One structured fact renderer, with a rendered cap

**Renderer contract.** `_fact_line(uuid, tags, text)` (`assistant.py:1146`)
becomes the sole fact renderer:

- both seed entries (`SeedMemory`) and remembered facts (`RetrievedMemory`) are
  passed to it as **structured fields**;
- the `format_memory_context(...)` → `split("\n")` → strip `"- "` →
  `partition(": ")` round trip at `assistant.py:1672` is removed. Rendering a
  string and parsing it back apart is where the two truncation paths drifted, and
  the parse is fragile besides — it splits on the first `": "` in a line it did
  not construct;
- existing tags are preserved exactly: seeds keep `seed/{source}`, `dynamic`
  and `{path}`; claims keep `{kind}`, `{sensitivity}` and the evidence summary
  — **including the `"no evidence"` fallback** when `evidence_summary` is empty,
  which `format_memory_context` supplies today and a structured renderer must
  reproduce rather than emit an empty tag;
- uuid full-fetch behaviour (`_query_memory_full`) is unchanged.

**Budget semantics.** Revision 5's "source-character allowance unchanged at
1200" is **withdrawn**. It was mathematically inconsistent: 1200 retained source
characters, plus a marker, plus an unchanged 11000-character block, plus no newly
omitted candidate cannot all hold at once.

`MEMORY_QUERY_PER_FACT_CHARS = 1200` is redefined as the **maximum rendered
fact-text length, marker included**. The block budget is then untouched and no
candidate is displaced by marker overhead. The `truncate1200` tag now names a
rendered ceiling while the in-band marker states the actual number dropped —
consistent, which they were not under revision 5's reading.

**The block budget keeps today's meaning for this patch.**
`MEMORY_QUERY_TOTAL_CHARS = 11000` covers `RECALLED_MEMORY_LEGEND`, the
per-line newlines, and the retained fact lines — and excludes the
`<recalled_memory>` fence and the explanatory suffixes, which is what the code
does today (`used = len(RECALLED_MEMORY_LEGEND) + 1`, then `len(line) + 1` per
kept line). Rename it `MEMORY_QUERY_FACT_PAYLOAD_CHARS` so the name states that.
A true whole-observation budget is a separate change and is not in scope.

**The helper.** In `agents/base.py`, beside `truncate_middle` — **not** a change
to it, whose contract the run summarizer depends on:

```
truncate_middle_to_length(text: str, max_length: int) -> str
```

Contract:

- text at or below `max_length` is returned unchanged;
- the returned string is **never** longer than `max_length`;
- both ends of the source are retained;
- the largest possible amount of source text is retained (maximality);
- a `max_length` too small to hold the marker raises `ValueError`.

**It must not restate the marker's shape.** Solving the allowance with a
hard-coded `45 + digits(dropped)` would drift the moment the marker's wording
changes. Solve by calling `truncate_middle` and measuring what comes back.

Algorithm, for input longer than the cap: try source allowances from
`max_length` **down to 2**, and return the first whose rendered result fits.
First-fit from the top is maximal by construction, and the floor of 2 is what
makes "both ends retained" true — `truncate_middle(text, 1)` gives
`head = 1, tail = 0` and returns a head plus a marker with no tail at all
(measured: length 50, and it does not end with the source's last character).
Revision 6's binary search over `[0, max_length]` had no such floor, which is
the inconsistency between its maximality and both-ends guarantees.

Raise `ValueError` when no allowance fits, and when `max_length` is negative.
Input within a non-negative cap is returned unchanged.

The descent is short in practice: starting at `max_length` the result overshoots
by exactly the marker's length, and each decrement recovers about one character,
so it converges in roughly fifty steps rather than `max_length` of them.

### Test inventory

In `agents/test_assistant_actions.py`, alongside the existing
memory_query truncation tests:

- a seed fact preserves an answer span near its tail;
- a retrieved claim preserves the same span;
- both render the identical middle-truncation syntax;
- rendered **fact text** stays within the cap (tested separately from the line,
  whose length also varies with uuid and tags);
- the budgeted fact payload — `RECALLED_MEMORY_LEGEND`, per-line newlines and
  retained fact lines — stays within `MEMORY_QUERY_FACT_PAYLOAD_CHARS`;
- a boundary fixture containing ten ordered 1200-character facts retains `u0`
  through `u7`, excludes `u8` and `u9`, and reports `obs.data["omitted"] == 2`
  (specified exactly below);
- populated and empty outputs both omit assessment prose;
- both retain assessment diagnostics in `obs.data`;
- uuid lookup still returns the complete untruncated fact;
- fixtures and assertion messages contain no private data.

#### The boundary fixture, exactly

```python
SeedMemory(uuid=f"u{i}", source="user-overlay", path="p",
           answer="y" * 1200, score=1.0)          # i = 0..9
```

`score` is required by the dataclass and is not otherwise load-bearing here.
The answer is exactly at the cap, so nothing is truncated and no marker enters
the arithmetic:

| quantity | value |
| --- | --- |
| tags | `seed/user-overlay, p` |
| line (`u0` + `", "` + tags + `": "` + 1200) | 1226 |
| line + newline | 1227 |
| initial legend budget | 44 |
| eight lines | `44 + 8 × 1227` = **9860** |
| nine lines | `44 + 9 × 1227` = **11087** |

11087 exceeds `MEMORY_QUERY_FACT_PAYLOAD_CHARS`, so the assertion is exactly
eight retained and two omitted. Every number above is checked, not estimated.

Helper-level tests go beside the existing `truncate_middle` tests in
`agents/test_assistant_long_request.py`:

- input at and below the cap is returned unchanged;
- 1201, 1947, 5178 and 100000-character inputs;
- rendered length is exactly the cap where the source exceeds it;
- the first and last source characters both survive;
- maximality — one more source character would breach the cap;
- the smallest cap that fits marker-plus-both-ends succeeds (51 for a
  1200-character source), and one below it (50) raises `ValueError`;
- a negative cap raises `ValueError`.

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

1. R1 — remove assessment prose from both output branches; permanent tests.
2. R2 — `_fact_line` as the structured renderer for seeds and claims; rendered-cap
   middle truncation; permanent tests.
3. Focused assistant-action tests, then the full suite.
4. N5 and N6 together — the three evaluation tiers, every metric defined, and
   gates with numbers.
5. B1 — mechanism chosen first, then measured.
6. N1–N4 — retrieval-unit decomposition as a separate project, after privacy,
   lifecycle, identity and evaluation contracts exist.
7. N7 — decide on the evidence.

Nothing past step 3 is implementable from this document, which is the point of
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
