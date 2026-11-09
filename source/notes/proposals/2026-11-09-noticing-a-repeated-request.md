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
wrong. Six decisions shape the design.

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
go through the short-message path below, even when identical. Otherwise
unrelated instances of "again" would become one chain. This cutoff is a tuning choice,
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

**Task-identity check.** That check is a decision model (see *The decision
model* below), asked one yes/no question per candidate pair: *does the current
message ask for the same task, with the same inputs, as the earlier one?* It
runs in two stages, the docs' "screen with a Noul first" pattern:

1. **Shortlist (code).** Take up to 8 candidates: the highest weighted-overlap
   scores with no threshold, plus the immediate predecessor when eligible.
   Pairs the numeric/quoted-input guard rejects never reach the model; that
   guard is free and exact. Requiring only a shared token, not a 0.6
   overlap, lets a rephrasing or another language in. "hvor mange r er der i
   strawberry" shares two tokens with "count the R's in Strawberry".
2. **Screen (d1:3b, one request).** All shortlisted pairs go in one fan-out
   request, one Noul each. In the probe, 14 candidates took 0.4 s. The screen
   is tuned for recall: it passes anything ≥ 0.5.
3. **Confirm (d3:4b, one request per survivor).** Usually zero to two
   survivors, at about 0.3 s each. A candidate is `confirmed` only when the
   confirming Noul is ≥ 0.8.

Both models must say yes. In the probe the two made different mistakes:
- d1 passed "count the R's in raspberry" (0.68); d3 rejected it (0.05).
- d3 rejected the method refinement "… use python for counting" (0.17), which
  d1 got right (0.97). That costs a miss, which is the safe direction.

`confirmed` is a fourth approximate kind. In the first release it is recorded
with both probabilities, still **recording only**. The recording week then
measures the identity check itself against the operator's verdicts. Rollout
step 6 promotes it on that data, not on the probe below.

The check never runs for exact matches, and never when the setting is `off`.

**Short messages.** A short message is usually a command, not a request
with a subject: "elaborate", "more", "retry", "try again", "commit", "push".
Comparing it with the previous request ("is *retry* the same as *count the R's
in Strawberry*?") asks the wrong question. Most short commands are not
repeats at all:

- **"elaborate" and "more"** act on the previous *reply*;
- **"commit" and "push"** are actions of their own;
- **"thanks"** asks for nothing;
- **"retry" and "again"** are the ones that ask for the previous task again.

So the short-message path first classifies what the message **does**. Only
one of the answers leads to a chain. There is no list of command words:

- **Trigger:** no eligible exact match; current text at most 120 Unicode code
  points; the latest earlier human message in the room is from this sender
  and has a run by this agent, within 6 hours and the same context boundary.
  Do not skip a newer unanswered message to find a convenient older anchor.
- **Input:** current message, immediate predecessor, and its resolved
  request context when the predecessor is itself a redo, including method
  refinements (decision 3). The predecessor matters. "recount" alone was
  ambiguous in the probe (0.40), and after a counting request it was clearly a
  redo (0.95). Do not supply old assistant answers.
- **Question:** one Choice with five options, each a description (no example
  phrases):

  | option | what it means | effect |
  |---|---|---|
  | `redo` | another attempt at the previous request, same task and inputs; a method change ("check it with Python") counts | joins the predecessor's chain as kind `redo` |
  | `expand` | more of, or more detail on, the previous reply | none: not a repeat |
  | `command` | a standalone action of its own | a repeat only of the same command (below) |
  | `acknowledge` | thanks, agreement; asks for nothing | none |
  | `new_request` | something new, or the previous task on a different input | none on this path |

- **Output:** the top option counts only when its probability is ≥ 0.8.
  Otherwise, or on any failure, the result is `unknown`, which adds nothing.
  Record all five probabilities, not just the winner, so the band can be tuned
  on the operator's verdicts.
- **Repeated commands.** A `command` is its own task, so it can be repeated as
  itself. "push" sent again right after a "push" is a retry of the push. That
  holds only when the sender's immediately previous request is the same
  command (exact text, within 6 hours, same context boundary). Two "commit"s
  with other work between them are two separate commits, not a chain. This
  replaces the blanket rule that short messages never match exactly: the
  rule's worry, unrelated "again"s merging into one chain, cannot arise,
  because an "again" is classified `redo` and anchors on its predecessor
  instead.
- **Limits:** one request on `d1:3b`, a 2-second end-to-end deadline, no
  fallback cascade. Record model, probabilities, result, and elapsed time as a
  code-driven step. If the complete inputs exceed a 2,000-character data
  budget, skip as `unknown`; do not classify a cut task as if it were whole.

Failure must not prevent the ordinary turn from proceeding. Every short
message triggers the call, so measure its hit rate and latency. Longer redos
and ambiguous group-room references remain first-release limitations.

**The decision model.** Both checks run on a System One decision model, not
on the generative models the loop uses.

- **What it is.** A System One model reads a state plus typed questions, and
  returns calibrated probabilities instead of generated text. A Noul is a
  yes/no question answered with P(yes). A Choice picks one of up to 255 named
  options. The Jev family is the hosted line of these models. Ollama 0.40.15
  serves the same format locally at `POST /v1/systemone`, and models with the
  `decision` capability answer it. Two are installed:
  - `d1:3b`: lfm2, 2.7B parameters, Q8;
  - `d3:4b`: qwen3.5, 4.5B parameters, mxfp8.
- **Request shape.**
  - **Short message:** the state is
    `{"previous_request": …, "short_message": …}`, with one
    `{"type": "choice", "instructions": …, "criteria": {five options}}`.
  - **Identity:** the state is `{"earlier_message": …, "current_message": …}`,
    with one `{"type": "noul", "instructions": …}`. The instruction states the
    boundary: a different object, number, unit, direction or constraint is a
    different task, while different wording, language or requested method is
    not.
  - **Fan-out screen:** the earlier message goes inside each question's
    structured `instructions` instead, so the state carries only the current
    message.

  No instruction or option description contains an example phrase.
- **Why a decision model and not a plain-text yes/no call.**
  - **Speed.** It answers from one forward pass, with nothing decoded. In
    the probe a short-message Choice took 0.12 s on d1, and an identity Noul
    0.09 s on d1 and 0.3 s on d3. A generative call on the decide model takes
    seconds and loads the GPU the turn is about to need.
  - **Probabilities.** It returns a probability, so the bands are thresholds
    in code, tuned on verdicts, rather than a model's guess at `yes` / `no`
    / `unknown`.
- **Binding.** It gets a new model slot, `assistant.repeat_check`, bound to a
  model group whose members are System One models on a loopback host. Two
  rules, unlike other `assistant.*` slots:
  - **No fallback to `assistant.default`.** A generative group cannot answer
    a System One request. Unbound means both checks record `unknown` with
    reason `no_decision_model`, and exact matching still works.
  - **Remote members are refused**, so `assistant_models_all_local()` stays
    true wherever it was. The inputs are the operator's own messages.

**What the probe showed.** One run each, on hand-written cases:
- 14 identity pairs;
- 14 short messages asked as a yes/no redo Noul after "count the number of
  R's in Strawberry";
- 17 short messages (commands, redos, follow-ups, acknowledgements) asked as
  the five-option Choice. Each was asked three ways: alone, after the
  Strawberry request, and after "fix the typo in the README".

The scripts are kept out of the repo. Accuracy (top answer, or yes at 0.5):

| check | d1:3b | d3:4b | both must say yes |
|---|---|---|---|
| identity, per pair | 13/14, 0.09 s median | 13/14, 0.32 s median | 13/14, no false yes |
| redo as a yes/no Noul | 11/14, 0.08 s median | 13/14, 0.32 s median | 13/14, no false yes |
| short message as a Choice (51 classifications) | 47/51, 0.12 s median | 46/51, 0.43 s median | (d1 alone) |
| identity fan-out, 14 questions in one request | 0.39 s | 4.6 s | (screen on d1) |

**The Choice beats the yes/no question for short messages.** Asked "is
*elaborate* a redo?", d1 had no better answer than yes (0.72). Given an
`expand` option, it put "elaborate" there with 0.92–0.95 in every context.

With the ≥ 0.8 band, d1 gave **no wrong label** in the 51 classifications.
After a previous request, the ones that missed the band became `unknown`:
- "more" (0.67 expand, and 0.55 redo after the README fix);
- "why?";
- "igen";
- "and for raspberry?" after the README fix;
- three commands after the README fix (0.75–0.79).

"commit", "push", "deploy" and "run the tests" were `command` at 0.87–0.94
in the other two contexts. d3 was slower and less sure of commands (it called
"commit" after the Strawberry request an acknowledgement, at 0.61), so the
short-message check runs on d1.

What it shows:
- **Changed inputs are separated.** sqrt of 1764 against 1746, Tokyo against
  Toronto, feet→m against m→feet: all ≤ 0.2 on both models. This is what
  embeddings cannot tell apart. They are retrieval, and this is a decision.
- **Paraphrases and another language are joined.** "how many r are in
  strawberry?", the same question in Danish, and "weather forecast for paris
  tomorrow?" all scored ≥ 0.95 on both.
- **Redos in several forms are caught.** "again", "try again", "noch einmal",
  "check it with python" and "are you sure? recount" all scored ≥ 0.9 on both.
- **Some single words slip through.** The Danish single word "igen"
  ("again") was missed in every framing. As a Noul it scored 0.04 and 0.12.
  As a Choice it came out as command or acknowledge, at ≤ 0.68. Bare one-word
  redos in a smaller language are the expected weak spot; the failure is
  `unknown`, so no block.
- **d1 over-accepts on pairs.** Asked as a yes/no identity question, d1 passed
  "count the R's in raspberry" (0.68), and d3 put it under 0.3. That is why
  identity needs both models. On short messages the Choice removed d1's
  over-acceptance, so d1 is enough there.
- **d3's fan-out scales poorly in this Ollama build.** 14 questions took 4.6 s
  against 0.3 s for one, so d3 only confirms the screen's survivors.
- **Fan-out probabilities are not per-pair probabilities.** In d1's fan-out,
  every strawberry-shaped candidate scored about 0.91, raspberry included. The
  screen's output is a shortlist, never a verdict.

28 cases, one run, hand-written: enough to choose the shape, not the
thresholds. The thresholds come from the recording week.

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
- **Reply checks**, when that run recorded them (decision 6), as bands:
  `answered="no"` means the delivered reply missed the point of its request.
  The bands tell the two common retries apart:
  - **The earlier reply missed the point.** Answer the question this time.
  - **The earlier reply answered, and the person asked again anyway.**
    Suspect the answer itself, and verify it.
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
           route="reply" summarizer_outcome="resolved"
           answered="yes" constraints="yes">
    <request>count the number of R's in Strawberry</request>
    <reply_excerpt>There are 2 R's in "Strawberry".</reply_excerpt>
  </attempt>
  <attempt when="1 h earlier" match="exact" status="finished"
           route="reply" summarizer_outcome="resolved"
           answered="yes" constraints="yes">
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

### 6. Check whether a reply got the point, over several parameters

A repeat says *that* the person asked again, not *why*. The reply they got is
half of the why: did it miss the point, ignore a constraint, or answer
properly and still not convince? The same decision model answers that as a
checklist: one fan-out request, one Noul per parameter, read in code.

**The checklist.** The state is `{"request": …, "reply": …}`, plus the
turn's acceptance criteria when that call ran, plus the earlier reply
excerpts when `repeated_request` is present:

| check | question | when |
|---|---|---|
| `answers` | Does the reply answer what the request asks, about the subject the request names? | always |
| `constraints` | Does the reply follow every constraint the request states about format, length, method or scope? A request that states none passes. | always |
| `criterion_processing`, `criterion_formatting`, `criterion_assumptions` | Does the reply follow this: *the criterion's text*? | one per non-empty acceptance-criteria field, when that call ran |
| `acknowledges` | Does the reply say that this request was asked before? | `repeated_request` present and recent |

Code-checked facts sit beside them, with no model call:
- whether a `python_run` actually executed and returned ok;
- whether a read ran;
- the reply's length.

The model is never asked what the code already knows.

**Never asked: "is the answer correct?"** In the probe both models judged
"There are 2 R's in Strawberry" correct (0.84 and 0.82). A classifier
reading a reply cannot count letters any better than the model that wrote
it. Correctness comes from executed computation and fresh reads, which is
what the repeat rule pushes the loop towards. A question whose honest answer
needs work is not a checklist item.

**Two places it runs, one checklist:**

1. **After the turn, on the delivered reply.** It runs beside the run
   summarizer, enqueued at the same terminal states and off the request path.
   It writes `metadata_["reply_checks"]` once, on that run, as part of the
   run's own post-turn record, exactly as `summary` is written. It holds the
   probabilities, the bands, the model, and the checklist version. A later
   retry never revises it; the retry is recorded on the later run (decision
   5). This is what fills `answered=` and `constraints=` on listed attempts.
2. **In the turn, on the proposed reply, before the reply audit.** When the
   loop chooses `reply`, the checklist runs on the draft. That is d1, about
   0.16 s for three to five questions, and more with criteria. The result goes
   on that reply step's row in /assistant, where it happened. In the first
   release it is **recording only**: the generative reply audit still decides.
   The checklist runs next to the audit, whose verdicts are stored, so the
   recording week measures how far they agree:
   - Does `answers: no` predict an audit rejection?
   - Does the audit pass drafts the checklist flags?
   - Does `acknowledges: no` on a recent repeat match what the operator
     wanted?

   If the measurements show a clear `answers: no` is reliable, a later change
   can return such a draft to the loop at once, naming the failed check, and
   save the audit's seconds on that path. The generative audit stays for what
   a checklist cannot judge: whether claims are supported, and whether facts
   hold. That change is judged on this data, not made here.

**Bands.** The same rule as the other checks: ≥ 0.8 is `yes`, ≤ 0.2 is
`no`, and in between is `unclear`. Only `yes` and `no` reach a prompt.
`unclear` is recorded, never rendered as either.

**What the probe showed.** 12 hand-written (request, reply) pairs, one
fan-out each, three of them with earlier replies. All pairs are invented:
- a reply on the wrong subject;
- the wrong conversion direction;
- boards listed when tasks were asked;
- a paragraph when three bullets were asked;
- an ignored "use python";
- correct, wrong, and acknowledging Strawberry replies.

| check | d1:3b (0.16 s per reply) | d3:4b (0.9–2.2 s per reply) |
|---|---|---|
| `answers` | 12/12; in band 10 right, 0 wrong, 2 unclear | 12/12; in band 6 right, 0 wrong, 6 unclear |
| `constraints` | 5/6; missed the ignored bullet format (0.69) | 5/6; same miss (0.56) |
| `acknowledges` | 2/3 | 3/3 |
| `correct` (not adopted) | 4/5; "2 R's" judged correct | 3/5; same |
| "different answer, earlier one not called wrong" (not adopted) | 2/3 | 2/3 |

What it shows:
- **`answers` is the strongest signal.** The three replies that missed the
  point scored 0.04–0.15 on d1.
- **`constraints` catches a method the reply ignored, but not a format.**
  The ignored "use python" scored 0.36 and 0.35. For methods, the code check
  on the executed route is the reliable half; for formats the check is
  recorded, not trusted.
- **`acknowledges` is weaker on d1.** d1 read "There are 3 R's" as an
  acknowledgement (0.78); d3 got all three. It is recorded on d1 and
  revisited if the week shows misses.
- **Comparing against earlier answers was weak.** The "different answer, but
  the earlier one is not called wrong" check scored 2/3 on both models, so it
  is not part of the checklist.
- **The checklist runs on d1.** It costs a sixth of d3's time and gave no
  wrong in-band answer.

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
| "commit" or "push", once and then again right after; and again later with other work between | adjacent repeat is a retry of the command; the later one is a new command |
| "elaborate" or "more" after any request | classified `expand`; never joins a chain |
| another sender/agent, intervening human request, or profile marker | no implicit redo across the boundary |
| failed/stopped run without a reply; two runs for one message | failure stays visible; one user attempt; no fabricated reply |
| same live-state question next day | may get an exact block with `recent=false`; changed data alone does not imply error or require an apology |
| stable question with a previously verified answer | verify as needed; no compulsory method change or repeated preamble |
| reply outside admitted history, indirect diary reuse, or newly restricted memory | no newly exposed reply text; withheld stays distinct from absent |
| long/cut request, malicious XML, missing anchor, or classifier timeout | preserve task and bounds; escape content; unknown detection does not break the turn |
| queued messages A and B before A's worker starts | repeat detection and every prompt still answer A |
| multilingual short redo and unspaced substantive request | exercise classifier anchoring and document lexical misses |
| identity pairs: changed object, number, unit, direction or constraint; paraphrase; other language; method refinement | screen and confirm recorded per pair; changed-input pairs never `confirmed` |
| decision slot unbound, model unloaded, or deadline exceeded | `unknown` with a reason; exact matching and the turn unaffected |
| chain beyond 100 attempts, 1,000 messages, 14 days, or text/work limits | bounded work/storage and explicitly incomplete/scoped counts |

**Cost and integration.** Measure query count, scan time, classifier trigger
rate and p50/p95 latency, block size, prompt tokens, and end-to-end latency.
For the decision model, also measure:
- its cold-load time, since it is another resident model next to the decide
  model;
- whether running it shifts the decide call's prefill time;
- how calibrated its probabilities are against the operator's verdicts.
Batch message/run/step lookups; do not issue one query per candidate. Verify
the existing shared prefix, identical per-consumer renderings across steps,
and all three prompt layouts. Measure no-match turns too. Do not promise
millisecond latency before including database reads and classifier time.

## Rollout

1. Add exact request/run linkage, scoped candidate loading, lexical matcher,
   bounded chain builder, and renderer tests. No prompt injection.
2. Add a System One client for Ollama's `/v1/systemone` and the
   `assistant.repeat_check` slot (loopback only, no fallback). Then add the
   short-message Choice with deadline, instrumentation, and `unknown` fallback, and
   the identity screen-and-confirm, recording probabilities only. Test failure
   paths, an unbound slot, and ambiguous anchors.
3. Add the reply checklist (decision 6): post-turn on delivered replies, and
   in-turn on drafts beside the reply audit, recording only.
4. Enable recording and inspector feedback. Review false positives, misses,
   access boundaries, and latency before adding reply instructions.
5. Wire all five consumers and run fixtures and complete-turn replay. Enable
   exact/redo injection when fixtures pass, reviewed matches have acceptable
   precision, answer correctness does not regress, and latency is acceptable.
   Record precision and latency targets before evaluating the held-out set.
6. Consider approximate injection only if measured misses justify it. The
   candidate is the `confirmed` kind: its recorded probabilities against the
   operator's verdicts decide whether, and at which confirm threshold, it may
   join chains. Embeddings can broaden the shortlist but do not distinguish a
   retry from the same operation on different input; the identity Noul does.
   Any stored vectors follow the same scope and eligibility boundaries.

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
  of repetition is not a reliable "solved" verdict. The same decision model
  fits that digest's tagging. A Choice over the room's existing topic tags,
  paired with a Noul for "none of these fit", assigns each exchange a tag
  without a generative call. That belongs in the digest's own proposal.
- **Make old answers authoritative.** The block supports what was attempted
  and said. The current answer still needs its own evidence.
- **Resolve every paraphrase or follow-up.** Ambiguous or out-of-scope cases
  proceed through the ordinary turn; the inspector makes limitations visible.
- **Refuse, lecture, or compel an apology.** A repeat is still a request to
  help. Success is a more reliable answer, not more talk about being asked
  before.
