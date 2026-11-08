# Noticing a repeated request

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:** [The conversation working set](2026-10-17-conversation-working-set.md)
(carries earlier evidence; this proposal carries earlier attempts),
[The request as its own turn](2026-10-19-request-as-its-own-turn.md)
(request placement and follow-up context),
[Turn latency and prompt redundancy](2026-08-31-turn-latency-and-prompt-redundancy.md)
(read before placing any new block).

## The problem, as observed

Ask the assistant to count the R's in "Strawberry". It answers 2. Ask again:
3. Again, now with "… use python for counting" on the end: 2. Again with
"… try again": 3. By the fifth message the person asking may be testing why
the answer keeps changing. The assistant still treats each message as a first
ask, follows the same route, and presents another answer without accounting
for the earlier ones.

The earlier exchanges may be in `conversation_history_xml`, which carries
the last 30 messages. But the instructions rightly say that old assistant
answers are not authoritative evidence, and warn against answering an earlier
request that resembles the current one. Neither rule makes the history of
*attempts* explicit: how often this was asked, what was actually tried, whether
a run failed, and what the assistant previously told the person asking.

An earlier answer is not evidence that its claim is true. It is evidence of
what was said. That distinction is the basis of this proposal.

Repetition alone does not establish dissatisfaction or an error. The operator
may be retrying a failed run, checking consistency, asking for a fresh reading
of changing data, or returning to a task after a break. The useful behavior is:

1. Recognize earlier attempts when they concern the current task.
2. Verify the current answer, choosing a stronger method when one is needed.
3. Acknowledge an explicit retry or a relevant inconsistency briefly.
4. Correct an earlier answer only when fresh evidence establishes the error.

Success in the Strawberry case is a verified count of **3** when counting R/r
without regard to case, with a brief explanation that earlier answers
disagreed. Changing the answer by chance, or merely adding an apology, does
not count as success.

## The design

A **repeat check** runs once near the start of a turn, before reply planning.
It finds earlier requests by the same person to the same agent in the same
room, resolves their runs, and freezes a bounded record for the turn. Eligible
matches produce a short `repeated_request` block. Detection results are
recorded even when no block is injected.

The first release favors precision: exact repetitions and resolved short
redos can reach the prompt; approximate matches are recorded for evaluation.
Neither a text match nor a classifier result proves that an earlier reply was
wrong. Five decisions shape the design.

### 1. Find candidates without confusing similar wording with the same task

**Scope and identity.** Take the room's most recent 1,000 human messages
strictly before the triggering message, restricted to the past 14 days and
after the latest context-invalidation marker. Filter to the same sender and
to messages with a run by this agent. Use message IDs and stable timestamp /
ID ordering, not display names or whichever message is newest when the worker
starts. A queued later message must not enter an earlier turn's repeat check.

Reply planning and repeat detection must resolve the same triggering UUID.
Today the prompt builder takes the last supplied message as current; supply
history bounded through the trigger, not through the newest room message.
If that identity cannot be established, skip injection and record the reason.
Manual runs without a triggering message UUID also skip detection.

Capture the context boundary before `handle()` removes marker rows. A marker
is a hard boundary for requests, counts, routes, and inherited chains, not
just reply excerpts. A profile switch must not turn another profile's task
into this person's retry. A marker posted for the current turn also clears
the eligible past, even though it follows the triggering message in storage.

An **attempt** is one human message with at least one exactly linked run. A
failed, stopped, or killed run still counts without a reply. Multiple
executions of one message count as one user attempt; retain their separate
run IDs and statuses. An ongoing run is labelled ongoing, not failed. The
repeat check itself never cancels or restarts it.

**Exact matches.** Compare complete stored text, normalizing only line endings.
Preserve case, punctuation, spacing, and operands: in a character-counting
task those can be the input. Never compare truncated prompt excerpts. Very
short messages (initially fewer than 24 non-whitespace Unicode code points)
go through the short-redo path below, even when identical. Otherwise unrelated
instances of "again" would become one chain. This cutoff is a tuning choice,
not subject detection. Even a long exact match is context to consider, not
proof that the person wants an acknowledgement.

Recorded redos never become direct anchors merely because their wording
matches. A repeated "Can you try that again please?" must resolve against the
current predecessor, even though it exceeds the short-message cutoff. Route
it through the classifier and require a compatible anchor. Unrecognized
context-dependent wording remains a limitation: the decide call must check
that exact matches concern the current task before using their counts or
history, and must not resolve a pronoun from text equality alone.

**Approximate candidates.** Distinguish retrieval from confirmation:

| kind | candidate test | initial use |
|---|---|---|
| `exact` | full-text equality as above, outside the short-message path | prompt and recording |
| `same` | token-sequence similarity ≥ 0.85 | recording only |
| `extended` | earlier token sequence is a prefix of the current one, with 1–8 added tokens | recording only |
| `rephrased` | IDF-weighted token overlap ≥ 0.6 | recording only |

For reproducibility, token-sequence similarity is `2 × LCS(a, b) /
(len(a) + len(b))`, where LCS is the longest common subsequence. Weighted
overlap is weighted Jaccard over distinct tokens: intersection weight divided
by union weight. Use `idf(t) = 1 + log((N + 1) / (df(t) + 1))` over the eligible
sender's messages in the captured scope, excluding the current request. Empty
inputs score zero. Record the matcher version and thresholds.

Bound approximate work separately from the message-count limit: skip pairs
where either text exceeds 4,000 characters or 256 lexical tokens, without
silently truncating them. Give fuzzy scoring a 50 ms per-turn work budget,
visit candidates newest first, and record skipped counts and reasons. It is
diagnostic work and may stop without affecting exact/redo detection. Compare
exact requests using full-text digests plus equality checks; cap candidate
text loading at 1 MiB per turn, record any scope shortening, and do not claim
a complete count when a loading or comparison limit was reached. These are
initial bounds to measure during recording, not latency guarantees.

Lexical comparison uses NFKC plus casefolding and Unicode letter/mark/number
runs, with letter/number boundaries split explicitly. Keep raw text alongside
it. This is not universal word segmentation: an unspaced sentence can remain
one token. Exact matching still works, and short messages can use the
classifier. Do not claim multilingual paraphrase coverage from `\w+`.

Before scoring, reject approximate pairs whose ordered numeric literals or
explicit quoted/code inputs differ. Preserve signs, decimal separators,
exponents, and case in those comparisons; do not silently equate ambiguous
number formats. Require at least three lexical tokens on each side for fuzzy
scoring. There is no room-median IDF floor: repeated subjects must not become
ineligible merely because they have become common.

These guards are insufficient to establish identity. "Count R's in strawberry"
and "Count R's in raspberry" share most words. Adding "but only uppercase
letters" changes the answer. Approximate candidates therefore neither join
chains nor trigger acknowledgements in the first release. Promoting them
requires an evaluated task-identity check, not just a lower threshold.

**Short redos.** A short follow-up can ask to do the previous task again
without naming it. Use one bounded classifier call, with no retry-phrase list:

- **Trigger:** no eligible exact match; current text at most 120 Unicode code
  points; the latest earlier human message in the room is from this sender
  and has a run by this agent, within 6 hours and the same context boundary.
  Do not skip a newer unanswered message to find a convenient older anchor.
- **Input:** current message, immediate predecessor, and its resolved
  request context when the predecessor is itself a redo, including method
  refinements (decision 3). Classify a redo of "again" against the underlying
  task. Do not supply old assistant answers.
- **Question:** does this message repeat or request another attempt at the
  same task with the same inputs? More detail, a changed operand, a new task,
  and an acknowledgement are not redos. A method change such as "check it
  with Python" can be a redo. Unclear references are `unknown`.
- **Output:** `yes`, `no`, or `unknown`, after trimming whitespace and
  casefolding. Any other output or failure becomes `unknown`. A `yes` links
  the current message to the resolved predecessor's chain as kind `redo`.
- **Limits:** one attempt on the first model resolved for the turn's decide
  slot, a 5-second end-to-end deadline, no fallback cascade. Record model,
  result, and elapsed time as a code-driven step. If the complete inputs
  exceed a 2,000-character data budget, skip as `unknown`; do not classify a
  cut task as if it were whole.

Failure must not prevent the ordinary turn from proceeding. Sharing a model
binding does not guarantee zero model-load cost, and short ordinary messages
will trigger the call too. Measure its hit rate and latency. Longer redos and
ambiguous group-room references remain first-release limitations.

### 2. Keep chains bounded, and make their counts honest

For an exact match, collect directly matching earlier requests and in-scope
recorded redos anchored to those requests. This finds "original → again →
original" as well as repetitions of the full wording. For a classified redo,
begin with its immediate predecessor and resolved anchor. Add compatible
recorded ancestors, deduplicate by human message UUID, and order oldest first.
"Original request → try again → again" is three attempts, including the current
message. Select the newest compatible recorded anchor, or the newest directly
matching full request when no recorded anchor exists. A new request starts
with itself as anchor.

Inheritance does not waive eligibility. Recheck each referenced message's
room, sender, agent linkage, context boundary, age, and position in the
1,000-message snapshot. Reject forward references, cycles, missing/deleted
messages, and incompatible anchors. Approximate similarity is not transitive:
A≈B and B≈C must never silently create an A=C chain.

Store at most **100 eligible prior message IDs** in `chain`: the substantive
anchor, when it is a prior message, plus the newest other IDs that fit. The
separate `anchor_message` field references that same ID, not an extra attempt.
Read bounded stored chains, with no unbounded recursive walk. An anchor must
itself remain eligible. If missing or outside scope, an implicit redo cannot
revive it.

`attempts` is the number of distinct retained prior messages **plus the
current message**. It counts known attempts in this scope, not lifetime
attempts. Set `chain_truncated=true` when the 100-ID cap, a loading limit,
or a previously truncated ancestor makes the count incomplete; show "at least
N known attempts" then. Do not add ancestors' totals: overlapping chains
would double-count. The 14-day limit applies to counting as well as display.

`recent=true` means the newest eligible prior attempt was within 6 hours of
the triggering message. Freeze this and relative times for the turn. Recency
helps judge whether an acknowledgement is useful; it does not establish an
error or an obligation to apologise.

There is no historical rewrite. Older runs may contribute when their message
link can be recovered exactly, but have no recorded redo ancestry. A missing
legacy link is a gap, not permission to infer one from proximity.

### 3. Show what happened without reviving inaccessible reply text

Each listed attempt contains:

- **When and match kind**, relative to the frozen turn time.
- **Request**, verbatim, middle-truncated to 200 characters and labelled when
  cut. This is for recognition, never the matching input.
- **Runs and status**, including no-reply and ongoing cases.
- **Executed route**, such as `reply` or `python_run (failed) → reply`. Use
  execution records and outcomes, not every row with an action name. Exclude
  classifier, criteria, program-writing, and audit calls. Rejected Python is
  not executed Python; unknown routes are not assumed to be `reply`. Collapse
  legacy duplicate step phases.
- **Summarizer outcome**, when available, labelled `summarizer_outcome`.
  This is a weak self-assessment, separate from run status. A confidently
  wrong answer can still have outcome `resolved`.
- **Reply excerpt**, where admissible, middle-truncated to 160 characters
  with the cut marked. Use the delivered message, not a proposed reply,
  summary, or reasoning. If several replies were delivered, identify the one
  shown and retain all exact message/run links in the record.

Routes contain action names and execution outcomes only: no arguments,
programs, queries, or observations. Such text can contain details from a
source the current turn is no longer allowed to read.

**Reply-text policy for the first release.** Only reuse excerpt text already
present in that consumer's current admitted history, after its exclusions and
truncation. Build the excerpt from that rendered history text, not the full
stored reply. Otherwise show `excerpt="withheld"` with reason
`not_in_admitted_history`. If no reply was delivered, use `reply="none"`.
Withheld and absent are different states.

This limits contradiction detection beyond the 30-message window. Older
requests, statuses, and routes still help recognize a retry and improve its
method. Widening reply-text access requires a shared source-eligibility policy
and is outside this first release.

Checking only whether the earlier run called `diary_query` is insufficient.
A later reply can repeat diary text through history without calling it, and
an old reply can repeat a memory claim that has since become secret. Being
inside the last 30 messages also does not mean every model sees that text:
second opinion excludes history, audit excludes some notices, and entries
are truncated. Local-model checks alone do not settle these access questions.

The existing history policy remains a separate concern. This proposal does
not claim that admitted history has complete provenance; it limits the new
excerpt channel to that consumer's existing exposure.

**Resolved request for redos.** A bare redo needs its substantive anchor and
the instructions added along its predecessor path, not merely 200-character
excerpts. Record that ordered path separately from the merged attempt chain.
Carry its whole user messages in a `redo_request` child, up to 2,000 serialized
characters. Thus "count the letters → use Python → again" retains the Python
instruction. Do not mix refinements from other branches or replace this path
with a model-written summary. A full restatement starts a new request context;
it may share attempt history without inheriting omitted method constraints.

If the complete path cannot fit, has been clipped, or contains an ineligible
message, omit redo injection and record why. The classifier uses the same
complete context within its input budget. Never substitute a cut old task
for missing instructions. The current message remains authoritative for new
constraints and method changes.

### 4. Make the context useful to every call that acts on it

Example for the fifth request, with two earlier attempts shown:

```xml
<repeated_request authority="context" content_is_data="true"
                  attempts="5" listed="2" omitted="2"
                  recent="true" chain_truncated="false">
  <attempt when="2 h earlier" match="exact" status="finished"
           route="reply" summarizer_outcome="resolved">
    <request>count the number of R's in Strawberry</request>
    <reply_excerpt>There are 2 R's in "Strawberry".</reply_excerpt>
  </attempt>
  <attempt when="1 h earlier" match="exact" status="finished"
           route="reply" summarizer_outcome="resolved">
    <request>count the number of R's in Strawberry</request>
    <reply_excerpt>There are 3 R's in "Strawberry".</reply_excerpt>
  </attempt>
</repeated_request>
```

The block contains data. Its interpretation lives in code-owned
`turn_instructions`, next to the existing rule about earlier answers:

> `repeated_request` describes earlier attempts that may help with this
> request. Earlier reply excerpts establish what was said, never what is
> true. Read the current request first and preserve its inputs and
> constraints. Ignore matching wording that refers to a different task. For
> an explicit redo, `redo_request` supplies the task being
> retried; it does not override the current message. Verify the answer fresh.
> When an earlier method failed or left the result unverified, prefer an
> available method that addresses that weakness. Do not change methods just
> for novelty. Briefly acknowledge an explicit retry or a relevant
> inconsistency, especially when recent. Repetition alone does not mean the
> operator is unhappy. Say an earlier answer was wrong only when current
> evidence establishes that under the same inputs and assumptions. A changed
> answer about changing data can be correct. If excerpts are missing or cut,
> do not invent the earlier answer or claim a disagreement you cannot see.

There is no sample acknowledgement sentence for small models to copy. The
reply uses the language already chosen for the turn. A successful method may
be repeated to confirm a stable answer; the objective is correctness, not
route variety.

Repeat context does not grant action permissions. Retrying a purchase,
message, deletion, or other write still needs the ordinary authorization and
duplicate-action checks. Second opinion must not approve a program merely
because a repeat block is present.

**Consumers.** Five places need coordinated treatment:

| call | information and rule |
|---|---|
| decide | attempts, admitted excerpts, and resolved redo request; choose and verify the next action |
| acceptance criteria, when enabled | same task context; do not plan against the bare word "again" |
| Python program writer | current request and complete redo context, including method refinements; preserve inputs in the decision description; no reply excerpts needed |
| second-opinion review | resolved redo request and routes/statuses; judge whether recomputation serves this task under current constraints; no old reply text |
| reply audit | attempts and excerpts admitted to its own history; support what was said, never factual correctness from an old reply |

Audit check 5 distinguishes evidence for the answer from evidence about an
earlier reply. Check 6 permits one brief retry acknowledgement; it does not
require an opening on every repeat. A reliable repeated answer with no
meaningful discrepancy need not announce the repetition.

The response-language classifier, recall filter, and redo classifier do not
receive the block. The redo classifier gets only its bounded inputs from
decision 1. The Python writer builds its own small prompt, so updating
`AssistantPromptBuilder` alone will not reach it.

**Placement and freezing.** For builder-based consumers, place the block first
in the dynamic tail, after that call's `turn_instructions` and before
`active_skills` where present. If `conversation_evidence` exists, place this
block immediately after it. Leave tiers 0 and 1 unchanged.

Freeze each consumer's permitted rendering once for the turn, including times,
statuses, and grades. Renderings can differ because consumers admit different
history. Repeated calls of the same kind use identical bytes. This preserves
the existing shared head and enables reuse within that call kind; it does not
make different instructions share a prefix past their divergence point.
Cover `single_turn`, `request_turn`, and `exchange_turns`, preserving the
current request's existing placement.

**Source priority.** Rank `repeated_request` alongside
`conversation_history_xml`, as context only. Escape XML through the existing
builder so request/reply text cannot forge instruction tags. The Python
writer must also delimit the anchor as request context, not system policy.

**Budget.** The attempt portion, including wrapper and attributes, is at most
1,500 serialized characters. The optional redo anchor adds at most 2,000,
for a 3,500-character total cap per consumer. Admit at most five prior attempts,
newest first, then display oldest first. If one does not fit, count it as
omitted and continue trying smaller candidates. Count escaping and truncation
markers in the budget. Never partially serialize an attempt to make it fit.

`listed + omitted = attempts - 1`; `withheld` counts listed attempts whose
reply text was withheld, not another category of attempt. No block means no
injected tokens, but detection still costs queries and comparisons, and a
triggered classifier still has latency.

### 5. Record the observation on the run where the repeat happened

Record `payload.message_uuid` as `metadata_["request_message"]` when the run
is created, independently of the repeat setting. This is an exact identity
link, not a detection result. Today `assistant_run` has no triggering-message
column. Legacy links can come from journal payloads; delivered replies link
back through `meta.assistant_run_uuid`. Do not use the inspector's
nearest-message / time-window helpers for detection.

An abbreviated `metadata_["repeat"]` record:

```json
{
  "version": 1,
  "mode": "on",
  "injected": true,
  "anchor_message": "<substantive request uuid>",
  "chain": ["<prior human message uuid>", "<prior human message uuid>"],
  "chain_truncated": false,
  "matches": [{"message": "<uuid>", "kind": "exact", "score": 1.0}],
  "redo_check": "not_needed",
  "recent": true,
  "attempts": 3,
  "renderings": {
    "decide": {"listed": 2, "omitted": 0, "withheld": 0,
               "block_chars": 740, "block": "<exact serialized block>"}
  }
}
```

Also record the snapshot cutoff, context marker, matcher configuration,
candidate count, exact run/reply links, approximate candidates, omission
reasons, and timings. Bound approximate diagnostics to the 20 highest-scoring
pairs, with a count of the rest. Record no-match and error results too: an
error means detection was unavailable, not that there was no repeat.

`redo_check` is `not_needed`, `yes`, `no`, or `unknown`, with a separate reason
for skips/failures. Store exact permitted renderings for all applicable
consumers. In recording-only mode these are **would-send** renderings and
`injected` is false. Do not log withheld text in another field or diagnostic.

The recorded block explains a historical run. New live turns recheck
eligibility rather than reinjecting old serialized blocks. Replaying a
recorded prompt on another model also requires today's access rules; a trace
is not authorization to send historical private context elsewhere.

The /assistant inspector shows a "repeated request" row at the start of the
turn, with known count, match basis, limits, and links to earlier runs.
Recording-only rows say "would show — not given to the model"; approximate
candidates say "possible match — recording only". The operator can mark a
match correct, incorrect, or uncertain and flag a missed repeat from any run.
Store feedback separately from the original snapshot, with message IDs and
matcher version.

Earlier runs are not rewritten or retrospectively marked failed. A later
retry is a new observation about the conversation, not a change to what the
earlier run executed or delivered. Conversely, `resolved` is not proof that
an earlier answer was correct.

## Measuring it

**Detection quality.** Run recording-only for at least a week and until enough
labelled examples exist to assess each enabled path. Operator labels establish
intended repetition. Review detected candidates and sampled non-matches;
misses cannot be measured from positive rows alone. Report precision and
recall by match kind, language/script, request length, and recency. Tune on one
set of chains and assess on held-out chains, not adjacent turns from the same
chain. A week elapsed is not a release gate by itself.

**Reply quality.** Replay complete turns with fixed tool responses, using the
same model/configuration with and without the block. A stored final decide
call can test wording, but cannot show that the assistant chose and executed
a better verification method. Repeat stochastic cases and score:

- verified answer correctness and preservation of the current inputs;
- useful verification, including whether code actually executed successfully;
- accurate acknowledgement/correction, without invented earlier answers or
  assumptions about the operator's intent;
- unnecessary tool calls, duplicate writes, audit failures, and reply length.

**Required fixtures.** Use synthetic rooms, not live private conversations:

| case | expected result |
|---|---|
| five exact Strawberry requests, prior replies alternate 2 and 3 | fifth turn executes a count, reaches 3 under case-insensitive counting, and briefly identifies the inconsistency |
| original → "try again" → "again", or original → "again" → original | same task history; three known attempts; writer and reviewer receive the actual task |
| original → "use Python" → "again" | retain the method instruction throughout the redo path |
| identical long redo wording after a different task | resolve the current predecessor; never borrow the older task from text equality |
| appended "use Python", same task | classifier can resolve a short redo when eligible; otherwise approximate candidate only in the initial release |
| changed number, sign, decimal, quoted input, unit, object, or negation | no fuzzy-chain merge; classifier rejects changed-task cases |
| "thanks", "yes", "elaborate", or "more details" | no redo block merely because those words occurred before |
| another sender/agent, intervening human request, or profile marker | no implicit redo across the boundary |
| failed/stopped run without a reply; two runs for one message | failure stays visible; one user attempt; no fabricated reply |
| same live-state question next day | may get an exact block with `recent=false`; changed data alone does not imply error or require an apology |
| stable question with a previously verified answer | verify as needed; no compulsory method change or repeated preamble |
| reply outside admitted history, indirect diary reuse, or newly restricted memory | no newly exposed reply text; withheld stays distinct from absent |
| long/cut request, malicious XML, missing anchor, or classifier timeout | preserve task and bounds; escape content; unknown detection does not break the turn |
| queued messages A and B before A's worker starts | repeat detection and every prompt still answer A |
| multilingual short redo and unspaced substantive request | exercise classifier anchoring and document lexical misses |
| chain beyond 100 attempts, 1,000 messages, 14 days, or text/work limits | bounded work/storage and explicitly incomplete/scoped counts |

**Cost and integration.** Measure query count, scan time, classifier trigger
rate and p50/p95 latency, block size, prompt tokens, and end-to-end latency.
Batch message/run/step lookups; do not issue one query per candidate. Verify
the existing shared prefix, identical per-consumer renderings across steps,
and all three prompt layouts. Measure no-match turns too. Do not promise
millisecond latency before including database reads and classifier time.

## Rollout

1. Add exact request/run linkage, scoped candidate loading, lexical matcher,
   bounded chain builder, and renderer tests. No prompt injection.
2. Add the short-redo classifier with deadline, instrumentation, and `unknown`
   fallback. Test failure paths and ambiguous anchors.
3. Enable recording and inspector feedback. Review false positives, misses,
   access boundaries, and latency before adding reply instructions.
4. Wire all five consumers and run fixtures and complete-turn replay. Enable
   exact/redo injection when fixtures pass, reviewed matches have acceptable
   precision, answer correctness does not regress, and latency is acceptable.
   Record precision and latency targets before evaluating the held-out set.
5. Consider approximate injection only if measured misses justify it. Evaluate
   task-identity confirmation first. Embeddings can broaden retrieval but do
   not distinguish a retry from the same operation on different input. Any
   stored vectors follow the same scope and eligibility boundaries.

One setting, `assistant.repeated_request`, is read once per turn:

- `off` (default): no repeat scan, classifier call, or injected block.
- `record`: detection and would-send renderings are recorded, without injection.
- `on`: exact/redo results are recorded and injected; approximate candidates
  remain recording-only until a separately evaluated change.

Turning it off is the rollback. Stored identity and historical records remain;
neither permits reuse of ineligible context. No earlier run is modified by
rollout or rollback.

## What this does not do

- **Summarize the room or declare a topic solved.** Chains can inform a future
  topic digest, but repetition is not a reliable "open" verdict and absence
  of repetition is not a reliable "solved" verdict.
- **Make old answers authoritative.** The block supports what was attempted
  and said. The current answer still needs its own evidence.
- **Resolve every paraphrase or follow-up.** Ambiguous or out-of-scope cases
  proceed through the ordinary turn; the inspector makes limitations visible.
- **Refuse, lecture, or compel an apology.** A repeat is still a request to
  help. Success is a more reliable answer, not more talk about being asked
  before.
