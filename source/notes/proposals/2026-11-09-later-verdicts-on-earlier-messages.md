# Later verdicts on earlier messages

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:**
- [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md):
  repeat chains, reply checks, and the local decision-model slot this
  proposal reuses.
- [The conversation working set](2026-10-17-conversation-working-set.md):
  carries earlier evidence; this proposal carries earlier *judgements*.

## The problem

Conversations correct themselves. A few messages after an answer, the
operator says "no, that's wrong, there are 3". Or they realise their own
earlier message was wrong: "actually I was wrong earlier, it was 1999, not
1998". Or they check something they were unsure of: "I looked it up, my guess
was right, it was June".

Each of those is a verdict on an earlier message. Today none of them reaches
a later turn as a verdict:

- **The correction is just another message in history.** Thirty messages on,
  it has scrolled out. The wrong answer, or the wrong year, may still be in
  view, and nothing next to it says it was overturned.
- **A hedge is lost once confirmed.** "I think it was June, but I'm not sure"
  reads, a day later, exactly like a stated fact. When it is later
  confirmed, nothing records that the guess became a fact. When it is
  corrected, nothing marks it as superseded.
- **The verdict is the most valuable signal in the room, and it is
  discarded.** The operator is the ground truth. A "that's wrong" about an
  assistant reply is a free, labelled eval case, and today it is only text.

The thumbs up/down on agent replies (`feedback_event`) is the one verdict
channel that exists. It covers only assistant replies, only when the operator
clicks, and it carries nothing the next turn can see.

## The design

When a later message judges an earlier one, that judgement becomes a
**verdict record**. Every later turn that shows the earlier message shows its
current verdict with it.

Five decisions shape it.

### 1. A verdict is a new record, made where it happened

The earlier message and its run are never edited. A record stands as
written. A verdict is something that happened later, so it is its own row,
created at the time and place it happened:

```
message_verdict
  uuid, room_uuid, created_at
  target_message_uuid      the message judged
  target_author            'assistant' | 'human'
  target_run_uuid          the run that produced it, for assistant replies
  verdict                  'wrong' | 'right' | 'doubted' | 'unsure_at_time' | 'cleared'
                           | 'computed_differently' | 'computed_same'
  source                   'operator_mark' | 'operator_message' | 'computation' | 'self'
  evidence_message_uuid    the message that carried the verdict, when there is one
  evidence_run_uuid        the run that detected or computed it
  probability              the detector's probability; null for operator marks
  detector                 model, checklist version
```

The table is append-only: no updates and no deletes. A changed mind is a new
row. "I was wrong about being wrong" is a `wrong` or `right` written later,
and the operator withdrawing a mark is `cleared`. The current verdict on a
message is chosen by these rules:

1. **Operator marks outrank everything.** A mark set by hand wins over any
   detected verdict, whatever its age.
2. **Then the operator's own words**, detected from a message.
3. **Within one source, the newest row wins.**

Computation is not in this order: it records evidence, not a judgement. The
newest `computed_*` row is shown *beside* the current judgement, and never
replaces it.

Every row is kept, so the inspector can show how a message's standing
changed.

**The detecting run also records what it found**, in its own metadata,
because that is where it happened:
`metadata_["verdicts"] = [<verdict uuids>]`.

**In /assistant:**
- **The detecting run's timeline** gets a "verdict" row: which message it
  judged, what it found, and the probability.
- **The earlier run's page** gets a separate **Later verdicts** list below
  its timeline. Each entry is dated and links to the run or message where it
  happened. It is never on one of the earlier run's step rows. That run did
  what it did at the time, and a root-cause read of its timeline must not see
  a later event as its own failure.

**The existing thumbs up/down** (`feedback_event`) is read alongside, as
source `operator_vote`. It is not copied into the new table.
- **Why it isn't treated as `wrong`/`right`.** A downvote can mean wrong, but
  also too long, or the wrong tone, so it is shown as what it is: "you
  downvoted this".
- **The vote buttons stay as they are.** Inspector buttons for
  `wrong` / `right` / `cleared` are the new operator marks, on human messages
  as well as replies.

### 2. Four ways a verdict is made

| source | trigger | what it can say |
|---|---|---|
| `operator_mark` | the operator marks a message on /assistant or in the room | `wrong`, `right`, `cleared` |
| `operator_message` | a message of theirs says an earlier message was wrong, right, or doubtful | `wrong`, `right`, `doubted` |
| `computation` | a later run in the same repeat chain answered by executed computation, and its answer differs from, or matches, the earlier reply | `computed_differently`, `computed_same` |
| `self` | a human message states something its writer says they are unsure of | `unsure_at_time` |

**`operator_message`: detection, after the turn.** It runs in the post-turn
housekeeping, beside the run summarizer, so it costs the request path nothing.
The current turn already sees the operator's words directly; the record is
for every turn after it.

- **The question.** One Choice per candidate target, on the decision model.
  The state is `{"earlier_message": {"author", "text"}, "current_message"}`,
  and the options are:
  - `says_wrong`: says the earlier message was wrong, or corrects it;
  - `says_right`: says it was right, or confirms it;
  - `doubts`: questions it without saying it was wrong;
  - `none`: says nothing about whether it was right or wrong.

  The descriptions contain no example phrases.
- **Candidates**, at most 4:
  - the assistant reply the current message follows;
  - the sender's own earlier messages within the last 6 hours that are
    marked `unsure_at_time`, or that share a token with the current message;
  - the earlier attempts of a repeat chain, when there is one.
- **Bands.** A verdict is written only when the top option is ≥ 0.8. Below
  that, nothing is written; the probabilities go on the run's record, so the
  band can be tuned.
- **Multiple targets.** When more than one candidate clears the band, all are
  written. "both of your earlier answers were wrong" judges two messages.

**`computation`: only where the answer cannot change.**
- **When it applies.** A later attempt in a repeat chain whose route
  executed `python_run` successfully, and whose reply checks say `answers:
  yes`, is compared with each earlier attempt's reply. The question is the
  same-answer Noul: "Do the two replies give the same answer to the
  request?".
- **What it writes.** ≥ 0.8 writes `computed_same` on the earlier reply; ≤ 0.2
  writes `computed_differently`; anything between writes nothing.
- **Why not `wrong` or `right`.** A successful `python_run` proves that the
  program ran, not that it computed what was asked. The program or its inputs
  can be the mistake. So a computation records what it found, a later
  computed answer that differs or agrees, and never a judgement. Only the
  operator, by mark or in their own words, makes an earlier message `wrong`
  or `right`.
- **Why only computation.** Reads of stored data are excluded on purpose.
  Stored facts and live state change between turns, so a different answer
  from a later read is not evidence that the earlier answer was wrong when it
  was given. A letter count does not change.

**`self`: hedges, flagged when they are written.**
- **The questions.** Every human message gets two Nouls in the post-turn
  housekeeping:
  - "Does the message state something?";
  - "Does the writer say they are unsure about something they state?"
- **What it writes.** Both ≥ 0.8 write `unsure_at_time`. The first question
  exists because the probe read the bare question "are you sure?" as a hedge
  (0.82). A question states nothing, so it cannot be a guess.
- **What it enables.** A later `says_right` on that message then reads as
  *your guess was confirmed*, and a `says_wrong` as *your guess was wrong*.

### 3. What a later turn sees

**In `conversation_history_xml`**, a message with a current verdict carries
it, and a hedged statement says so:

```xml
<message author="assistant" …>There are 2 R's in "Strawberry".
  <later_verdict verdict="wrong" source="operator_message"
                 when="20 min later"/>
</message>
<message author="user" …>I think the meeting was in June, but I'm not sure.
  <stated_as>unsure</stated_as>
  <later_verdict verdict="right" source="operator_message"
                 when="1 day later"/>
</message>
```

**Attributes, not text.** The verdict's own message is in history too, when
it is in the window. A verdict whose evidence has scrolled out still shows,
which is the point.

**Which verdicts a turn sees, and how they are bound:**
- **Bound to the exact message.** Each rendered verdict carries its record
  id, source, value and time, on the exact message UUID it judges. It never
  goes on every reply of a run, or on every attempt in a chain.
- **Chosen once per turn.** The turn takes the current verdicts once, at the
  start, from records that exist then. Only verdicts whose evidence message
  precedes the triggering message and sits inside the same context boundary
  count. The result is frozen for every step and every consumer of the turn.
- **Frozen, even if a new verdict lands mid-turn.** A verdict written during
  the turn waits for the next one.

**In `repeated_request`**, each attempt carries its current verdict as
`later_verdict=`. An attempt judged `wrong` is the clearest possible signal
that the loop must not repeat that answer.

**The rule added to the decide instructions**, beside the rule about earlier
answers:

> A `later_verdict` on an earlier message records what was decided about it
> after it was written. `wrong` means do not reuse it: for an assistant
> reply, its answer was rejected; for the user's own message, the user
> corrected it, and the correcting message supersedes it. `right` means the
> user agreed at that time. It still does not replace a fresh read for data
> that changes. `doubted` means the user questioned it, and nothing was
> settled. A statement marked `stated_as` unsure, with no later verdict, is a
> guess. Do not present it as an established fact. `computed_differently`
> means a later computation gave a different answer. That is a reason to
> verify, not proof that either answer is right.

**The audit** gets the same verdict attributes in its own history. A reply
that repeats an answer judged `wrong` is a defect it can name.

**Prompt cache.** A verdict changes the bytes of an earlier history message,
so the turn after a verdict reuses the cached prefix only up to that message.
The cost is one partial prefill per verdict, not per turn. Verdicts are rare
next to turns, and the recorded `prompt_eval_count` measures it.

### 4. Memory gets a review flag, not a retraction

A claim remembered from a message can outlive the message's correction.
`memory_evidence` already links a claim to its source, with `source_type
'chat_message'` and the message id in `source_id`. When a verdict of `wrong`
lands on such a message:

- **The claim is flagged for review on /memory**, linking to the verdict.
  The flag is its own row, not an edit to the claim.
- **It is not retracted automatically.** The verdict is detected by a model
  at ≥ 0.8, not certain. The operator decides, with the evidence one click
  away.
- **An operator mark of `wrong` flags the same way.** Retracting stays a
  deliberate act on /memory, as it is now.

A `right` on a message whose claim came from an `unsure_at_time` statement
adds a `confirmed_by_user` evidence row. That provenance already exists, and
the confirmation is exactly what it describes.

### 5. The decision model, bound explicitly

Every detector here runs on **`assistant.repeat_confirm`**, the repeat
proposal's confirmation slot. That proposal leaves the slot available to
verdict detectors, and asks this one to name its slot, not assume a group
member. Its fast slot, `assistant.repeat_check`, is not used here.

The repeat proposal's binding rules apply unchanged:
- **Loopback provider and verified local execution.** A local Ollama server
  relays a cloud model when its name carries `:cloud`, so a loopback host
  alone is not enough. Cloud-backed and unknown-locality models are
  rejected, and the check is repeated when a queued job executes.
- **No fallback to `assistant.default`.**
- **`unknown` with a reason when unbound.**
- **Model digest, server version and question version recorded on every
  check.**

Detection runs after the turn, so latency is not on the request path. That is
why the confirmation slot, with **d3:4b** in the probe, fits here: it scored
better on all three checks below.

**What the probe showed.** One run each, on hand-written cases. The cases
are the `verdict_choice`, `hedge` and `same_answer` experiments in
`benchmarks/fixtures/decision_probe_cases.json`. The recorded run, with raw
answers, digests and timings, is
`benchmarks/fixtures/decision_probe_runs/2026-11-09.json`. Re-run with
`python -m benchmarks.decision_probe`.

| check | d1:3b | d3:4b |
|---|---|---|
| verdict Choice, 14 cases (assistant replies and the person's own statements, one in Danish) | 13/14, none wrong at ≥ 0.8, 5 below band, 0.12 s | **14/14**, none wrong at ≥ 0.8, 3 below band, 0.42 s |
| hedge Noul, 8 messages (one in Danish) | 5/8, 0.06 s | **7/8**, 0.26 s |
| same-answer Noul, 8 reply pairs | 7/8, 0.08 s | **8/8**, 0.34 s |

The verdict Choice separated the cases that matter:
- **Corrections:** "no, that's wrong, there are 3", "nej, det er forkert",
  and "the release year I gave you is off by one" all came out `says_wrong`.
- **Confirmations:** "you were right" and "my guess was right, it was June"
  came out `says_right`.
- **Doubt, not a verdict:** "are you sure?" came out `doubts`.
- **Nothing to judge:** "thanks" and an unrelated question came out `none`.
- **Below band, no verdict written:** "hmm, that doesn't look right" landed
  at 0.77 on d3 and 0.54 on d1. That is the right outcome for a vague
  objection.

The same-answer check:
- **Different answers:** "2 R's" against "3 R's", and "91 is prime" against
  "No: 91 = 7 × 13", both scored ≤ 0.2.
- **Rounding:** "30.48 meters" against "About 30.5 m" scored 0.64 on d3, in
  the middle, so no verdict.

The hedge check's one miss on d3 was "are you sure?", which the "states
something" Noul in decision 2 exists to filter.

## Measuring it

- **Precision of detected verdicts.** For a week, verdicts are recorded and
  shown in /assistant, but not rendered into prompts. The operator confirms
  or rejects each one with the same buttons that create marks. A rejected
  detection is a `cleared` row with source `operator_mark`, so the
  correction is itself a verdict.
- **Recall.** The operator marks messages the detector missed. Those marks
  are the misses.
- **Effect on replies.** Scripted rooms, built from invented messages:
  - an answer corrected, then the same question asked 40 messages later
    (past the history window);
  - a hedge, then a confirmation, then a question that depends on it;
  - an own-statement correction, then a question about it.

  Each is replayed with and without the verdict attributes, scoring whether
  the reply reuses an answer judged `wrong`, and whether it presents an
  unconfirmed guess as fact.
- **Eval cases for free.** Every `wrong` on an assistant reply, by mark or
  by a confirmed detection, is offered to the eval set as a case. The request
  is known and the rejected answer is known; the operator supplies the
  expected answer when they accept it. Operator verdicts are already how
  evals are meant to grow, and this makes collecting them a side effect of
  talking.
- **Cost.**
  - Detector calls per turn, and their latency, all off the request path.
  - Partial-prefix prefills caused by verdicts.

## Rollout

1. The `message_verdict` table and its read model (current verdict per
   message, by the precedence in decision 1). Operator marks on /assistant
   and in the room. `operator_vote` read from `feedback_event`.
2. The detectors in post-turn housekeeping: verdict Choice, hedge Nouls,
   computation same-answer. Recording only.
3. The Later verdicts list on the earlier run's page, and the verdict row on
   the detecting run.
4. After a week of operator review: verdict attributes in history, in
   `repeated_request` and in the audit, plus the decide-instruction rule,
   behind `assistant.later_verdicts` (`off` / `record` / `on`).
5. Memory review flags and `confirmed_by_user` evidence rows.

## What this does not do

- **Rewrite an earlier message or run.** The verdict is a later event,
  stored as one.
- **Decide what is true.** A verdict records what the operator said, or what
  a deterministic computation showed. A detected verdict is a probability
  above a band, and the operator can overturn it.
- **Retract memory automatically.** It flags, and the operator decides.
- **Judge replies about data that changes.** A different answer from a later
  read is not a verdict on an earlier one; only computation is.
