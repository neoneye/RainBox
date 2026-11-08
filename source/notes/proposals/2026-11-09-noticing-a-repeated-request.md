# Noticing a repeated request

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:** `2026-10-17-conversation-working-set.md` (carries earlier
*evidence* into a turn; this proposal carries earlier *attempts*, and the two
sit side by side), `2026-10-19-request-as-its-own-turn.md` (the
`exchange_turns` layout, whose previous exchange a short follow-up leans on),
`2026-08-31-turn-latency-and-prompt-redundancy.md` (read before placing any
new block).

## The problem, as observed

Ask the assistant to count the R's in "Strawberry". It answers 2. Ask again:
3. Again, now with "… use python for counting" on the end: 2. Again with
"… try again": 3. By the fifth message the person asking is no longer asking
how many R's there are. They are asking *why the answer keeps changing*, and
nothing in the turn knows that.

Each of those five turns starts the same way:

- `conversation_history_xml` holds the last 30 messages. The earlier attempts
  may be in it or may already have scrolled out. When they are in it, the turn
  instructions tell the model to treat them as context only — "Old assistant
  answers in conversation_history_xml are never authoritative evidence" — and
  warn that a history message "close to" the current request "is the trap:
  answer the quoted request, not the one you have already answered".
- Nothing counts. A request sent for the fifth time looks exactly like one
  sent for the first time. Nothing says the earlier replies disagreed, or that
  every earlier attempt took the same route (answer directly, never run the
  code).

Both rules are right about facts: an old answer is not evidence. They are
silent about the *fact of repetition*, which is evidence of a different kind.
It is evidence about the conversation, and it is the operator's own verdict on
the earlier replies. A person who asks again has a reason, and the likely
reasons all call for a different reply than a first ask does:

- an earlier reply was wrong, and they want it fixed;
- earlier replies disagreed, and they want to know which one holds;
- they are testing whether the answer is stable, so a change of answer is
  itself the finding;
- the earlier attempt failed or stalled, and they are retrying.

In every case the right reply is the same shape:

1. Notice the repeat.
2. Say so in one sentence.
3. Answer fresh, by a route the earlier attempts did not take.
4. Name it plainly when earlier replies disagree with each other or with this
   one.

The failure today is the opposite: the loop takes the same path again, reaches
an answer by chance, and presents it as if no earlier answer existed.

## The design

A **repeat check** runs at the start of every turn, before the first model
call. It finds earlier messages in the room that ask what the current message
asks, and gathers how each was handled. When it finds any, the turn's prompts
get a short `repeated_request` block, and the run records what was found.

Six decisions shape it.

### 1. Detection is code, not judgment, except for one case

Three kinds of match, each decided by deterministic text comparison over the
room's earlier human messages:

| kind | what it catches | test |
|---|---|---|
| `same` | the request re-sent, or re-typed with small differences | token similarity ≥ 0.85 |
| `extended` | an earlier request with words added | every token of the earlier message appears, in order, in the current one, and at most 8 tokens are new |
| `rephrased` | the same question in different words | IDF-weighted token overlap ≥ 0.6 |

All three share two more conditions.

**The number tokens must be identical.** "sqrt of 1764" and "sqrt of 1746" are
the same task on a different input. They are not a repeat, and calling them
one would make every calculator session look like a complaint.

**Both messages must carry a subject.** A message with fewer than 3 tokens,
or whose IDF-weighted token mass is under the room's median, matches nothing
under these three tests, whether it is the earlier message or the current
one. Without this floor every "thanks" or "why?" would match every earlier
"thanks" or "why?". Under `extended`, an earlier "yes" would sit inside half of
all later messages. Short messages go only through the bare-redo path below,
which asks whether they redo something rather than whether they look alike.

**Whose messages count.** Only earlier messages from the same sender as the
current one, and only those this agent answered (they have a run, so they have
a route and a reply to show). A re-ask is that person's verdict on the replies
they got. Someone else in the room asking the same thing is not a retry. In a
shared room, telling them "this was asked before" would point at another
person's conversation.

Tokenization is language-generic, with no word lists:

- NFKC normalization, then casefolding;
- tokens are `\w+` runs under Unicode rules, so letters in any script count,
  and digits are tokens of their own;
- function words are handled by IDF computed over the room's own human
  messages, not by a stopword table. A word that appears in most messages
  weighs almost nothing. "strawberry" in a room that rarely says it weighs a
  lot.

`extended` is the shape this kind of retrying actually takes: the request
grows a suffix ("… use python for counting", "… try again"). It needs no
knowledge of what the suffix means. It only needs the earlier message to be
inside the new one.

The one case code cannot decide is the **bare redo**: a short message with no
subject of its own, which only makes sense as "do the previous request again",
in whatever language the operator writes. A table of retry phrases would be an
English table in shipped code, so it is not an option. Instead:

- **Trigger.** The current message has at most 4 tokens, matches nothing under
  the three tests, and the same sender has a previous exchange in the room (a
  message of theirs answered by this agent).
- **The call.** One small plain-text classifier call. It is shown the previous
  request and the current message, and asked whether the current message asks
  to redo the previous request. It answers `yes` or `no`, nothing else. Plain
  text, because on these models it is the fastest of the output modes.
- **What it must tell apart.** "Elaborate" and "more details" are short
  follow-ups, not redos. The question must separate *do it again* from *say
  more*. That distinction is the whole reason the call exists.
- **When yes.** The current message joins the previous request's chain as kind
  `redo`.
- **When it fails** (unreachable, or anything other than yes/no), the check
  records `redo: unknown` and the turn goes on without a block.

It runs on the model group the turn already uses, so it costs no model load.
Recording-only (see *Rollout*) measures how often it fires. The trigger is
narrow by design, so most turns never make the call.

### 2. Chains, so a redo of a redo still counts

Matches are recorded per run (see *Recording*). An earlier message that was
itself recorded as a repeat of something brings its own chain along. So
"again", sent after "… try again", which followed the original request, is the
fourth attempt at one request, not a redo of the word "again".

The chain for the current message is:

- every earlier message it matches directly;
- plus the recorded chain of each of those (one hop is enough, because each
  record already holds its whole chain);
- ordered oldest first, with duplicates removed.

Records only exist from the first run that has this feature. There is no
backfill: a chain in an older room simply starts at the first attempt the
lexical tests can see.

**Scope:** the room's most recent 1,000 human messages, not just the 30-message
history window. A retry an hour later, after 40 messages of something else,
is exactly the case the window loses. Comparing one short message against a
thousand short messages costs milliseconds.

**Recency:** attempts older than 14 days are not listed, but they still count
toward the total. A chain whose newest earlier attempt is within 6 hours is
marked `recent="true"`. That is the case where acknowledging the repeat is
clearly right. A question asked again a week later may be a new need, and the
reply rule (decision 4) treats it more lightly.

### 3. What an earlier attempt shows

For each listed attempt, up to the 5 most recent, the rest counted as
`earlier="N"`:

- **when**, relative to now ("12 min earlier", "2 days earlier"), and the
  match kind;
- **the request**, verbatim, middle-truncated to 200 chars;
- **the route**: the action names the run took, in order, and nothing else —
  `reply`, or `python_run → reply`. This is what lets the loop change approach:
  if no attempt ever ran the code, running it is the obvious next try. No
  arguments, for the same reason the working set's search log carries none (an
  argument can carry text from a source this turn may not be allowed to see);
- **the grade**: the run summarizer's `outcome` word, when that run has one,
  labelled as the summarizer's. It is a self-assessment, and a weak one: a
  confidently wrong "2" is graded `resolved`. It is shown because `failed` is
  a useful signal; it is never presented as truth;
- **the reply excerpt**: the start and end of the reply that attempt
  delivered, middle-truncated to 160 chars. This is the one place an old answer
  enters the prompt on purpose. Seeing that four replies said "2, 3, 2, 3" is
  how the loop learns that something is wrong. The block labels the excerpts as
  what was said, not as evidence (decision 4).

**When a reply excerpt is withheld** (the attempt is still listed, with
`excerpt="withheld"` and the reason):

- **Remote models.** The earlier run used `diary_query`, its reply is outside
  the 30-message history window, and `assistant_models_all_local()` is false
  for this turn. An earlier reply can restate diary text, and the diary's rule
  is that its text only reaches local models. The check is per turn and runs
  whether or not `diary_query` is enabled in the room today. An earlier run may
  have used it while it was enabled. Replies *inside* the window already reach
  every model in full through `conversation_history_xml`. Withholding their
  excerpt here would protect nothing, so the rule does not pretend to. That
  existing exposure belongs to the history window's own policy, as the working
  set proposal notes, and this block does not widen it.
- **Context marker.** The earlier attempt sits before the room's latest
  context-invalidation marker. The marker exists to say that answers from
  before it may rest on facts that no longer hold, or on another profile's.

### 4. In the prompt, and what the model is told to do with it

```xml
<repeated_request authority="context" content_is_data="true"
                  attempts="5" recent="true">
  <attempt when="2 h earlier" match="same" route="reply" graded="resolved">
    <request>count the number of R's in Strawberry</request>
    <reply_excerpt>There are 2 R's in "Strawberry".</reply_excerpt>
  </attempt>
  <attempt when="1 h earlier" match="extended" route="reply" graded="resolved">
    <request>count the number of R's in Strawberry.. use python for counting</request>
    <reply_excerpt>Counting the letters, "Strawberry" has 3 R's.</reply_excerpt>
  </attempt>
  …
</repeated_request>
```

The block has no note inside it. What it means, and what to do about it, is
in `turn_instructions`, which the code owns, the same split the other blocks
use. The rule added to the decide instructions:

> `repeated_request`, when present, lists earlier messages in this room that
> asked what the current request asks, and how each was handled. A person who
> asks again has a reason: an earlier reply was wrong or incomplete, earlier
> replies disagreed, or they are checking whether the answer holds. Answer the
> current request fresh, under the same read rules as any request: an earlier
> reply excerpt is what was said, never evidence for what is true. Where the
> routes show that an approach was never tried, prefer it. When the block is
> marked recent, open the reply with one short sentence saying that this was
> asked before; when earlier replies disagree with each other or with this
> answer, say so plainly and say which one was wrong. Do not apologise at
> length. When the block is not marked recent, mention the earlier attempts
> only if their replies disagree with this one.

That rule deliberately contains no sample sentence. On these models an example
phrase in a prompt is copied into replies word for word. The acknowledgement
has to be the model's own sentence, in the reply language the turn already
chose.

It fits next to the existing "closeness is the trap" rule rather than against
it. That rule stops the model from answering an *earlier* message instead of
the current one. This rule tells it that the current message *is* the earlier
one again, which is a different situation.

**Which calls get the block.** The block, and the rule that explains it, go to:

- **the decide call**, where the reply is written;
- **the second-opinion review**, which today reviews `python_run` programs.
  On a bare redo, or when the loop runs code to change route, the program can
  look like more than the message asked for, and the reviewer would mark it
  `[not_asked]`. Its rule gains: "when `repeated_request` is present, a program
  that checks or recomputes what earlier attempts answered serves the request";
- **the reply audit**, in two of its checks:
  - Check 5 (support) would otherwise reject "my earlier answers said 2 and 3"
    as a claim no observation supports. It becomes "against the turn's
    observations, and for statements about earlier attempts, against
    `repeated_request`". The block supports *what was said before*, never
    *what is true*.
  - Check 6 (how it addresses the user) treats an opening that restates the
    question as a defect, and the acknowledgement could read as one. It gains
    an exception: "when `repeated_request` is marked recent, one opening
    sentence noting that the request was asked before is required, not a
    restatement";
- **the acceptance-criteria call**, when that call is enabled, because it
  plans the reply.

The classifier and the recall filter do not get it.

**Placement.** The block is constant for the whole turn. It goes first in the
dynamic tail (tier 3), before `active_skills`, so every decide call of the
turn shares the prefix through it, and the tier-1-only calls are unaffected.
The working set's `conversation_evidence` block is proposed for the same
position and is not built. If it is built, this block goes right after it.
Both are constant for the turn, and evidence ranks above context.

**Source priority.** Context only, at the same rank as
`conversation_history_xml`: "conversation_history_xml and repeated_request
(context only)". It informs how to answer, never what is true.

**Budget.** The block is at most 1,500 chars, measured on the serialized
block. Attempts are admitted newest first, and an attempt that does not fit
is counted, not cut. A turn with no repeat gets no block and pays nothing.

### 5. Recording: the repeat goes on the run where it happened

Each run records `assistant_run.metadata_["repeat"]`:

```json
{"injected": true,
 "request_message": "<uuid of the human message this run answers>",
 "chain": ["<message uuid>", "<message uuid>"],
 "matches": [{"message": "<uuid>", "kind": "extended", "score": 1.0}],
 "redo_check": "not_needed",
 "recent": true, "attempts": 5, "listed": 4, "withheld": 0,
 "block_chars": 1180,
 "block": "<the exact serialized repeated_request block>"}
```

- `request_message` is recorded because `assistant_run` has no column for the
  message it answers. Today that link exists only through the journal
  payload's `message_uuid` and the reply's `meta.assistant_run_uuid`. With it
  recorded, a chain lookup reads one JSONB key instead of joining through the
  journal.
- `redo_check` is one of `not_needed`, `yes`, `no`, `unknown`.
- `chain` is what the next run follows (decision 2).
- `block` is the historical record. Rebuilding an old turn's block later could
  differ (excerpts withheld under different gates, newer summarizer grades),
  so replay and the inspector read what was recorded.
- `injected` is false in the recording-only phase.

The /assistant inspector shows a "repeated request" row at the start of the
turn, with the attempt count and links to the earlier runs. **The earlier runs
are not marked.** The re-ask is a verdict that arrives later, so it belongs to
the later run, where it happened. A run that delivered "2" ran without fault
at the time and stays clean on the timeline. Earlier rows are never rewritten.

## Measuring it

The operator is the ground truth, and repeats are already in their rooms.

1. **Detection quality, before anything reaches a prompt.**
   - Recording-only runs for a week, with the operator marking, on the
     inspector row, whether each detected repeat was one.
   - Misses come from the other direction: the operator flags a turn that was
     a repeat and shows no row.
   - The thresholds in decision 1 are starting points, tuned on those verdicts.
   - Expected weak spot: `rephrased` with few shared words. That is where an
     embedding signal would go (see *Rollout*, step 5), and only if the
     verdicts show misses.
2. **Reply behaviour, replayed.** Take stored decide calls from real repeat
   chains and replay them on the same model, with and without the block. Score:
   - Does the reply acknowledge the repeat?
   - Does it take a different route when one exists?
   - Does it name a disagreement?
   - Does it stay short?

   A prompt-only rule can measure zero on the small models, so it is measured
   before it is trusted.
3. **The Strawberry case**, scripted:
   - five attempts, with the earlier replies alternating 2 and 3;
   - the fifth reply must acknowledge the repeat, say the earlier answers
     disagreed, and reach 3 by a route that counts (running the code);
   - fixture text only, no live room.
4. **Negative cases**, which must produce no block:
   - the same calculation on different numbers;
   - "elaborate" after an answer (a follow-up, not a redo);
   - a live-state question asked again the next day, where earlier replies
     legitimately differ. This one should get a block that is *not* recent,
     and a reply that does not apologise.
5. **Cost.** How often the redo classifier fires, its latency, and
   `block_chars`. The prefix boundary of every call that shares the block
   must not move.

## Rollout

1. The matcher and chain builder, with tests on synthetic rooms:
   - each match kind;
   - the identical-numbers rule;
   - the subject floor: an earlier "thanks" matching a later "thanks", and an
     earlier "yes" inside a longer message, both produce nothing;
   - same sender only: another member's identical message is not a match;
   - IDF from the room's own messages;
   - the 1,000-message scope and the 14-day / 6-hour bounds;
   - a chain through a recorded redo;
   - excerpts withheld on a remote-model turn after a diary run, and before a
     context marker;
   - the budget counting, not cutting, an attempt that does not fit.
2. The redo classifier, on its narrow trigger, with its `unknown` fallback.
3. Recording only: `metadata_["repeat"]` with `injected: false`, and the
   inspector row labelled "would show — not given to the model". A week of
   operator verdicts tunes the thresholds.
4. The block in the four prompts, with the instruction and audit changes.

   One setting, `assistant.repeated_request`, gates the whole check, with three
   values:
   - `off`: nothing runs, and there is no classifier call;
   - `record`: the check and the classifier run, and the result is recorded
     but not injected;
   - `on`: the result is recorded and injected.

   It starts at `off`. It moves to `record` for step 3's week; chains only
   exist from the first recorded run, so recording has to come before
   injection. It moves to `on` once the replayed calls and the scripted cases
   pass.
5. Embedding similarity as a fourth match kind, only if step 3's verdicts show
   `rephrased` misses that lexical overlap cannot reach. Earlier requests'
   vectors would be written after each turn, off the request path, so a turn
   embeds only its own request.

## What this does not do

- **It does not summarise the room.** A digest of the room's topics, with a
  status per topic (open, solved), coarser the further back it goes, is the
  natural next step. Repeat chains are its best source of "open": a topic
  asked five times is not solved, whatever the summarizer graded. It is a
  separate proposal, built on what this one records.
- **It does not treat earlier replies as evidence.** It shows them so that a
  disagreement is visible, and the rules keep them on the "what was said" side.
- **It does not refuse or lecture.** A repeat is answered, every time. The
  acknowledgement is one sentence, and it is skipped when the earlier attempt
  is old and its reply agrees.
