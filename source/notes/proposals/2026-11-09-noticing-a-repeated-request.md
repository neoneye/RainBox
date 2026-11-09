# Noticing a repeated request

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:** [The conversation working set](2026-10-17-conversation-working-set.md)
(carries earlier evidence; this proposal carries earlier attempts),
[The request as its own turn](2026-10-19-request-as-its-own-turn.md)
(request placement and follow-up context),
[Turn latency and prompt redundancy](2026-08-31-turn-latency-and-prompt-redundancy.md)
(read before placing any new block),
[Later verdicts on earlier messages](2026-11-09-later-verdicts-on-earlier-messages.md)
(a correction or confirmation of an earlier attempt, carried as its
`later_verdict`).

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

The release boundary is explicit: **only exact/redo context affects replies**.
Task-identity confirmation and reply checklists are experiments: their scores
appear in the inspector, never in prompts or acceptance decisions. Identity
experiments run after the turn; draft checklist experiments have a small,
fixed budget. Neither experiment is a prerequisite for shipping repeat context.
The mode table under *Rollout* governs every call.

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

**Short-message resolution comes before approximate identity checking.** A
short message may be a complete question, an action, a follow-up, or an
acknowledgement. Length does not determine its intent. Use one five-option
Choice, with the complete current message, the immediate predecessor, and the
predecessor's resolved request path. Do not supply earlier assistant replies.

The trigger is: no eligible exact match; current text at most 120 Unicode
code points; the latest earlier human message in the room belongs to this
sender and has a run by this agent, within 6 hours and the same context
boundary. Do not skip a newer unanswered message. Known redo or command
messages must take this path even when their text matches an older message.

| option | meaning | effect in the first release |
|---|---|---|
| `redo` | another attempt at the predecessor's task with the same inputs, including a short complete restatement or a method refinement | join that task's chain after validating its anchor |
| `expand` | more of, or more detail on, the previous reply | no repeat relation |
| `command` | an action expressed as its own request | consider only an adjacent exact repetition of that command |
| `acknowledge` | agreement or acknowledgement that asks for no work | no repeat relation |
| `new_request` | a different task or the same operation on different inputs | no relation on this path |

These descriptions become versioned instructions, without example phrases.
The top option is accepted only at probability ≥ 0.8; otherwise resolution is
`unknown`. Record all five scores. Short complete requests such as `2+2?`
and short Chinese questions must be in the evaluation set: they are not
commands merely because they fit this path.

For `command`, adjacent equal text establishes repeated wording, not an
identical write target, a failed earlier execution, or permission to replay a
side effect. Treat it as an `exact` relation with basis `adjacent_command`
only when the immediate predecessor matches and no intervening human request
exists. The acting loop must inspect current state and ordinary authorization.
The same command after other work does not inherit the old command's chain.
`expand` and `acknowledge` must never acquire chains from text equality.

Use the fast decision slot, once, with a 2-second end-to-end deadline and a
2,000-character budget for complete input data. Missing context, oversized
input, invalid output, or timeout produces `unknown` with a reason. Do not
truncate a task to classify it. Only eligible short messages incur this call;
longer redos and ambiguous group references remain first-release limitations.

**Approximate identity is a separate, post-turn experiment.** It asks whether
two complete requests name the same task and inputs. It does not classify
bare follow-ups as standalone tasks. Resolve short-message intent first;
`expand`, `acknowledge`, and unresolved implicit references are excluded.

1. **Retrieve, in code.** Reserve one of at most 8 slots for the eligible
   predecessor, then fill the rest by weighted overlap, newest first on ties.
   Other candidates need at least one shared lexical token, but no 0.6
   threshold. Deduplicate by message ID. Changed numeric/quoted-input guards
   still apply. This admits some cross-language rephrasings with shared terms;
   it cannot retrieve arbitrary translations with no shared vocabulary.
2. **Screen, on the fast slot.** One request contains a named Noul per pair.
   Keep each earlier message in a data field, identified by that question's
   fixed instruction; do not promote message text into instructions. A screen
   probability ≥ 0.5 makes a pair eligible for confirmation, not a match.
3. **Confirm, on the confirmation slot.** Check at most two survivors,
   highest screen score first, then retrieval score and recency. One Noul per
   request; ≥ 0.8 records `confirmation.status="confirmed"`. Lower valid
   scores are `not_confirmed`; untested or failed pairs are `unknown`, not
   negative examples.

Keep retrieval kinds (`same`, `extended`, `rephrased`, or `predecessor`) and
confirmation status separately. Record both probabilities and both model
identities. Do not multiply scores or assume independent errors. In this
release **even confirmed pairs cannot join chains or reach prompts**.

Budget one screen call at 2 seconds and two confirmation calls at 1 second
each, with a 4-second total deadline including loading and queue time. Use
at most 16,000 characters of complete pair data across the screen; drop whole
lowest-ranked pairs that do not fit. Each confirmation has a 4,000-character
data cap. Skips are recorded. One job per captured turn, with no retry or
fallback cascade; do not fan out confirmation calls against the local GPU.
The job reads that turn's frozen candidate snapshot, never later room messages,
and writes diagnostics without modifying its chain or prompt snapshot.

**Decision-model contract.** System One returns typed scores for a shared
state and named questions. The Ollama request is `{model, state, questions}`;
questions use `type: "noul"` or `type: "choice"`. Require support for the five
options above. The endpoint returns scores rather than a prose answer; no
universal single-pass or zero-decoding guarantee is assumed. See the official
[endpoint reference](https://docs.ollama.com/api/systemone).

Read a Noul from `answers[name].noul` and a Choice from
`answers[name].probabilities[choice]`. Do not threshold `confidence`, which
measures how strongly a Choice distribution favors an option. Check expected
question/option keys, finite values in [0, 1], and a normalized Choice
probability sum within rounding tolerance. Missing, extra, or invalid results
produce `unknown`. Scores are model estimates; calibration on these tasks
must be measured. See the [decision guide](https://docs.ollama.com/capabilities/decision).

Two bindings make stage selection explicit:

| proposed slot | role | reported probe model |
|---|---|---|
| `assistant.repeat_check` | short-message Choice, identity screen, reply checklist | `d1:3b` |
| `assistant.repeat_confirm` | identity confirmation; available to separate verdict detectors | `d3:4b` |

Resolve the first eligible member of each binding once per turn. There is no
fallback to `assistant.default`, no positional interpretation of two members
in one group, and no model download on the request path. A missing fast slot
skips its checks; a missing confirmation slot skips confirmation only. Exact
matching continues in either case. The separate later-verdict proposal must
select its intended role explicitly rather than assume a group member.

Both bindings require a loopback provider **and verified local execution**.
A local Ollama endpoint can relay a cloud model; a loopback hostname alone is
insufficient. Reject cloud-backed models and unknown locality, rechecking at
execution time for queued jobs. Apply the existing source-eligibility rules
as well. See [Ollama's cloud routing](https://docs.ollama.com/capabilities/decision#use-a-cloud-model).
Include these slots in the assistant's model-locality accounting; do not
assume that the existing helper already knows about them.

Pin and record model digest, provider/server version, question-set version,
and thresholds. Probe aliases are examples, not hard-coded model selection.
The recorded measurements and their limits are collected under *Probe
evidence* below.

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

Reply-check scores (decision 6) remain inspector diagnostics in this release.
Do not render `answered=` or `constraints=` attributes, even when a score is
confident. A model's assessment is not the cause of the operator's retry.

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
> an explicit redo, `redo_request` supplies the task being retried; it does
> not override the current message. Verify the answer fresh.
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
  "short_resolution": {
    "status": "not_needed",
    "label": null,
    "probabilities": null,
    "relation": "exact",
    "reason": "full_text_match"
  },
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

`short_resolution.status` is `not_needed`, `classified`, or `unknown`.
When classified, store the five-option label, all probabilities, resulting
relation (`redo`, `exact`, or `none`), and its basis. Every executed check also
records its model digest, question version, elapsed time, and failure/skip
reason where applicable. A low-confidence result keeps its scores but has no
accepted label or relation.

Store exact permitted renderings for every applicable consumer. In `record`
these are **would-send** renderings and `injected` is false. Do not log withheld
text in another field. Post-turn `identity_checks` and `reply_checks` are
separate diagnostics: append them idempotently using a key-specific update,
without replacing concurrent metadata or revising the original repeat result.
Store inputs or immutable references sufficient to reproduce each check under
the existing trace-access policy; that record grants no new prompt access.

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

**Optional later-verdict integration.** The separate
[verdict proposal](2026-11-09-later-verdicts-on-earlier-messages.md) is not a
prerequisite. Only when it is implemented and `assistant.later_verdicts=on`
may a listed reply carry a verdict. Bind it to that exact reply UUID, not to
all replies or runs in an attempt. Include the verdict record ID, source,
value, and time, not an unqualified `later_verdict="wrong"`.

Select eligible verdicts once at the repeat snapshot: their records must
exist at capture time and their evidence messages must not follow the current
trigger or cross the context boundary. Apply that feature's access rules,
then freeze the result for all steps. A missing or disabled dependency produces
no verdict fields. Keep the same source labels in the audit.

A verdict reports a judgment, not proof of truth. In particular, successful
Python execution plus an `answers: yes` score and a changed answer does not
prove the earlier answer wrong: the program or its inputs could be wrong.
This proposal must not create a correctness verdict from those signals or
silently inherit that guarantee from the separate design.

### 6. Measure reply quality without making the checklist an authority

A decision-model checklist can estimate whether a reply addresses the task
and follows its constraints. It cannot establish why someone asked again,
and it cannot establish factual correctness. In the first release it is
**diagnostic in both `record` and `on`**: no scores enter later prompts, reject
a draft, skip the existing audit, or create a later verdict.

**Inputs.** Check the exact current request, complete resolved redo path when
needed, proposed/delivered reply, and the frozen acceptance criteria if that
call ran. Omit earlier reply excerpts: these questions do not need them.
Supply only a code-owned flag indicating an eligible repeat for the optional
acknowledgement measurement. Preserve task inputs and method refinements; a
check against the word "again" alone is invalid.

Missing or truncated required context means `unknown`, not a guessed score.
The new consumer gets no general history or source access. Its current reply
and criteria are processed only by a verified local model under the turn's
access rules; queued checks revalidate eligibility before reading them.

| check | question | interpretation |
|---|---|---|
| `answers` | Does the reply address the task and subject in the resolved request? | relevance estimate, not correctness |
| `constraints` | Does the reply follow the explicit format, length, method, and scope constraints? | estimate; distinguish no stated constraint from a verified pass |
| `criterion_processing`, `criterion_formatting`, `criterion_assumptions` | Does the reply follow this frozen criterion? | one per non-empty field; criteria are themselves model-derived |
| `acknowledges` | Does the reply mention the retry or earlier attempts? | descriptive only, when repeat context exists; omission is not a defect |

Code records executed actions and outcomes, successful reads, and reply
length separately. A successful `python_run` proves that execution succeeded,
not that it computed the intended result. The model is not asked to infer
execution from a reply saying "I used Python". Correctness evaluation needs
an independently established expected result or appropriate source evidence.

**Bands.** For each Noul, ≥ 0.8 is `yes`, ≤ 0.2 is `no`, otherwise `unclear`.
Missing input/output or failed calls are `unknown`; a check that does not apply
is `not_applicable`. None of these states is silently converted into a pass
or failure. Retain raw scores and threshold version. The acknowledgement
measurement never becomes a required opening merely because a repeat is recent.

**Two uses, one versioned checklist:**

1. **Draft diagnostics.** Run beside the existing reply audit, without feeding
   it the scores. At most two distinct drafts per turn, one fast-slot call
   each. Bind each record to the step/draft hash and resolved input hash.
   Repeated evaluation of the same input reuses the record. Further drafts
   get `skipped: draft_limit`; they still receive the ordinary audit.
2. **Delivered-reply diagnostics.** After the turn, check the exact delivered
   reply UUID. Reuse a draft result only when its entire input hash matches,
   including reply bytes, request path, criteria, flags, model, and checklist
   version. Otherwise enqueue one bounded check. With multiple delivered
   messages, record separate identities; initially evaluate at most the last
   two and mark earlier messages skipped. No delivered reply means
   `not_applicable`, not `answers: no`.

Each call has at most 6 questions, 8,000 characters of complete input data,
and a 1-second wall-clock deadline including queue/load time. Oversized input
is skipped whole, never cut to manufacture a judgment. Two draft checks add
at most 2 seconds on the request path. Post-turn work is best-effort, at most
two checks, idempotently keyed by run/reply/input hash/version; missing models
or failures never retry through another model. These are initial limits to
measure, not promises that a cold model will finish in time.

Persist draft records on their steps and delivered records under
`metadata_["reply_checks"]`, merging only that key. Snapshot mode and versions
with the job. Switching the feature `off` cancels pending checks before they
start; later completion cannot retroactively alter a response or its repeat
snapshot. Ordinary turn processing never waits for post-turn diagnostics.

Compare checklist judgments with operator labels, not just agreement with
the generative audit: both models can make the same mistake. Any future use
as a rejection gate or prompt hint needs its own held-out evaluation and
explicit rollout change. Repeat detection can ship while that work continues.

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

1. Add exact request/run linkage, scoped candidate loading, bounded chains,
   complete redo paths, and renderer tests. Verify queued-message identity.
2. Add the local decision client and explicit fast/confirmation bindings.
   Implement short-message resolution with failure and locality checks.
   Approximate identity is an optional post-turn diagnostic, not a dependency.
3. Enable `record` and inspector feedback. Add optional draft/delivered
   checklists with their independent input and latency caps. Review false
   positives, misses, costs, and access boundaries; keep all model-derived
   diagnostic scores out of prompts.
4. Wire the five repeat-context consumers and run complete-turn replay. Before
   examining held-out results, record minimum precision, minimum labelled
   sample/coverage, and maximum p95 added latency. Enable `on` only when those
   targets and the required fixtures pass and answer correctness does not
   regress. A week of recording alone is insufficient.
5. Separately consider promotion of identity-confirmed candidates or checklist
   scores. Preserve retrieval provenance and compare against human labels;
   confidence thresholds are not a substitute for task identity. Promotion
   must define new chain rules and foreground latency limits before shipping.

One setting, `assistant.repeated_request`, is captured once per turn:

| work | `off` | `record` | `on` in the first release |
|---|---|---|---|
| exact scan and eligible short-message resolution | no | yes | yes |
| repeat rendering | none | would-send snapshot | inject eligible exact/redo context, including adjacent-command candidates |
| approximate identity experiment, after the turn | no | bounded diagnostics if configured | same diagnostics; never injected |
| draft/delivered reply checklists | no | bounded diagnostics if configured | same diagnostics; never influence acceptance or prompts |
| later-verdict fields in repeat context | none | would-send only if separate feature is `on` | only if separate feature is `on` and evidence is eligible |

A missing decision binding skips only the work requiring it; it does not
block the ordinary reply or exact matching. `off` is the rollback and prevents
pending diagnostic jobs from starting. The independent later-verdict feature
retains its own switch; turning repeat checking off does not change its policy.
Stored request identity and historical records remain. Rollout and rollback
never rewrite an earlier run.

## What this does not do

- **Summarize the room or declare a topic solved.** Chains can inform a future
  topic digest, but repetition is not a reliable "open" verdict and absence
  of repetition is not a reliable "solved" verdict. Topic tagging belongs in
  that separate design.
- **Make old answers authoritative.** The block supports what was attempted
  and said. The current answer still needs its own evidence.
- **Resolve every paraphrase or follow-up.** Ambiguous or out-of-scope cases
  proceed through the ordinary turn; the inspector makes limitations visible.
- **Refuse, lecture, or compel an apology.** A repeat is still a request to
  help. Success is a more reliable answer, not more talk about being asked
  before.


## Probe evidence and its limits

The following are **exploratory results**, on hand-written cases. They are
reproducible from the repository:
- **Cases.** Expected labels and exact question text are in
  `benchmarks/fixtures/decision_probe_cases.json` (fixture version 1).
- **Runner.** `python -m benchmarks.decision_probe d1:3b d3:4b`.
- **Recorded run.** `benchmarks/fixtures/decision_probe_runs/2026-11-09.json`
  holds every raw answer, model digests, the server version and timings.
  That run reproduced every top-answer count below.

The environment was Ollama 0.40.15 with `d1:3b` (lfm2, 2.7B, Q8, digest
`8c2d9ddae441…`) and `d3:4b` (qwen3.5, 4.5B, mxfp8, digest `510b1771140e…`).
These aliases and timings describe that probe, not required deployment models
or latency guarantees. The first call of each model, already resident, took
2.6 s and 1.8 s; a cold load was not measured (`--cold` measures it).

Counted inside the bands (Noul ≥ 0.8 or ≤ 0.2, Choice top ≥ 0.8):

| experiment | d1:3b accepted (wrong) / total | d3:4b accepted (wrong) / total |
|---|---|---|
| identity, individual pairs | 13 (0) / 14 | 14 (1) / 14: the method refinement, rejected at 0.17 |
| redo as yes/no | 10 (1) / 14: "igen" rejected at 0.04 | 11 (1) / 14: "igen" at 0.12 |
| five-option Choice | 37 (0) / 51 | 29 (0) / 51 |
| reply checklist, `answers` | 10 (0) / 12 | 6 (0) / 12 |
| reply checklist, `correct` | 5 (1) / 5: the wrong count accepted | 2 (1) / 5 |

### Request resolution

| experiment | d1:3b | d3:4b | combined result reported |
|---|---|---|---|
| identity, 14 individual pairs | 13/14, 0.09 s median | 13/14, 0.32 s median | 13/14, no false yes at 0.5 on both |
| redo as yes/no, 14 cases | 11/14, 0.08 s median | 13/14, 0.32 s median | 13/14, no false yes at 0.5 on both |
| five-option Choice, 17 messages in three contexts (51 classifications) | 47/51, 0.12 s median | 46/51, 0.43 s median | d1 alone proposed |
| identity fan-out, 14 pairs in one request | 0.39 s | 4.6 s | screen on d1 proposed |

The five-option Choice was reported to give no wrong accepted label on d1
at ≥ 0.8. Recounted from the raw output, 37 of the 51 classifications cleared
the band:
- 13 of 17 with the message alone;
- 14 after the Strawberry request;
- 10 after the README fix.

That is 73% coverage and 0 of 37 accepted wrong. It is still one run on
selected cases, not a precision estimate. The
51 Choice classifications are separate from the two 14-case experiments;
"28 cases" is not the total evidence base.

Useful observations, without generalizing beyond those cases:

- d1 accepted the raspberry substitution at 0.68; d3 rejected it at 0.05.
  d3 also missed the "use Python" method refinement at 0.17 while d1 scored
  it 0.97. Combining them trades some false positives for misses.
- In d1's fan-out, strawberry-like candidates scored about 0.91, including
  raspberry. Individual-pair scores do not calibrate the batched screen.
- The Choice placed "elaborate" in `expand` at 0.92–0.95, where a yes/no redo
  question had scored it yes at 0.72. Offering a competing interpretation
  helped in this small sample.
- Danish "igen" remained below the acceptance band; commands after a README
  task also sometimes abstained. Short complete requests and more languages
  need explicit coverage.
- Tested input changes (numbers, cities, conversion direction) and some
  paraphrases were separated. This is evidence for testing the pipeline,
  not a guarantee that a decision model establishes identity or that an
  embedding cannot help retrieve candidates.

### Reply checklist

Twelve invented request/reply pairs were reported, including three with prior
replies. Different checks used different subsets:

| check | d1:3b | d3:4b |
|---|---|---|
| `answers` | 12/12 by top answer; 10 right, 0 wrong, 2 unclear in band | 12/12 by top answer; 6 right, 0 wrong, 6 unclear in band |
| `constraints` | 5/6 by top answer | 5/6 by top answer |
| `acknowledges` | 2/3 | 3/3 |
| correctness, rejected as a checklist item | 4/5; accepted the wrong count of 2 | 3/5; also accepted that count |
| changed answer without correcting the old one, not adopted | 2/3 | 2/3 |
| fan-out time per reply | about 0.16 s | about 0.9–2.2 s |

The ignored-method example scored 0.36/0.35, and ignored bullet formatting
0.69/0.56. All four are **unclear** under the proposed bands; they are not
successful high-confidence detections. An unchanged "There are 3 R's" reply
scored 0.78 for acknowledgement on d1, also an abstention. Both models judged
the wrong Strawberry count correct (0.84/0.82), which is a direct reason not
to use this checklist as a correctness oracle.

Before using any threshold for production behavior, retain synthetic cases,
expected labels, exact question text, raw outputs, model digests, server
version, candidate order/batch composition, and warm/cold timing in a versioned
eval fixture. Repeat runs on held-out chains and report accepted precision,
recall, abstention rate, and sample size separately. The observed absence of
accepted errors in a tiny selected sample is not a calibrated guarantee.
