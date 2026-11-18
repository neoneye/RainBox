# Surfacing topic uncertainty while chatting

**Status:** Proposal. Nothing here is built. What it would replace is built:
the list of topic pairs with Merge and Not the same on a room's topics
(`notes/housekeeping-design.md`, Chatroom topics).
**Date:** 2026-11-18
**Related:**
- [The room digest](2026-11-09-room-digest.md): topics per room, assigned at
  the start of a turn, with the operator's reassignment outranking the
  model's. This proposal takes its assignment point and its precedence.
- [Detector labels](2026-11-09-detector-labels.md): the difference between a
  statement about the conversation and a statement about a detector. A
  corrected topic is the first kind.
- `docs/superpowers/specs/2026-11-18-room-topic-registry-design.md`: what
  exists, and the measurements quoted below.

## The problem

A room's requests are gathered into topics by models that are sometimes
unsure and sometimes wrong. Today the doubt is collected on a page: a list
of topic pairs that look like one, each with Merge and Not the same. The
operator's verdict on that, 2026-11-18:

> I'm not a fan of the "Merge" or "Not the same". It's something that I
> have to decide on. It would be better if the assistant could surface the
> confusion to me, while I'm chatting, so I don't get a negative surprise
> much later. When there are fewer than 5 items, then I don't care about
> them falling into different buckets.

Three requirements are in it:

1. **No queue.** A list of pending decisions is work to go and do.
2. **At the time.** The operator should learn of it in the conversation it
   concerns, and not days later on another page.
3. **Only when it matters.** Under five items, a wrong bucket is not worth
   anyone's attention.

## What the measurements say

All on one room of 160 messages and 56 parts.

- **Doubt against a large topic is rare.** Replaying the assignment, a part
  scored in the uncertain range (0.35 to 0.65) 10 times on the kind-of-task
  axis and 8 times on the named-thing axis. Against a topic that already
  had five parts: once, and never. With the operator's threshold, there is
  about one thing to surface per 160 messages.
- **Doubt is not where the errors are.** The worst mistake on record was
  confident: two names that sound alike scored 0.68 as the same thing, above
  two pairs that really were one activity (0.40 and 0.39). Surfacing only
  what the model is unsure of leaves its confident mistakes unseen.
- **A second model does not settle it.** Shown both topics' requests, the
  generative model called 12 of 14 nearest pairs the same.
- **Words written by the model do not carry intent.** Told which tool
  answered five requests, the naming call mentioned it in 2 of 8
  explanations. A count from the records did it every time.

And one fact about timing that every option below has to face:

- **Today a topic is known late.** A part is assigned after its chunk is
  summarized, and a chunk is summarized once 10 messages and 2 of context
  exist. That is six exchanges after the request. Nothing can be surfaced
  "while chatting" about a request the system has not yet filed.

## The options

### A. A list on the topics page (built)

Pairs that both models call one, each with Merge and Not the same.

- **Pros:**
  - Built. Nothing in the chat changes.
  - The operator's answer is unambiguous: a button.
- **Cons:**
  - It is the queue the operator does not want.
  - It arrives late: after the room was summarized, on a page nobody is
    looking at.
  - It asks about pairs of topics, which is the model's unit and not the
    operator's. They think in requests.

### B. The assistant asks, and waits for an answer

After its reply the assistant asks: is this the same as your earlier X?

- **Pros:**
  - It is in the conversation, at the time.
  - The answer is the operator's own words.
- **Cons:**
  - A question is still a decision, moved from a page into the chat. It
    interrupts the thing the operator was doing.
  - An unanswered question has no meaning. The room digest's rule is that
    silence is not success; it is not a yes either.
  - The answer has to be read by a model. "no, that's the other one" is a
    hard sentence for a 4B model, and a misread answer is a new error.
  - In a room with a bridge the question goes out to Telegram or Discord.

### C. A notice after the reply: filed, say if it is wrong

Housekeeping decides, files the request, and posts one line: filed with
your X (11 so far), say if it is something else.

- **Pros:**
  - No decision is owed. Saying nothing leaves the default standing.
  - It is in the conversation, and it works over a bridge, where there is
    no page to look at.
  - With the five-part threshold it would post about once per 160 messages.
- **Cons:**
  - It is a row in the room. Rows are what chunks are cut from and what a
    bridge forwards, so each notice must be kept out of both.
  - A correction is still a sentence a model has to read, as in B, though
    only in the rare case where the operator objects.
  - It only speaks when the model is unsure, so it never shows a confident
    mistake.
  - It posts after the reply, from another process. A notice that lands
    while the operator is typing the next message reads as an interruption.

### D. A line inside the assistant's own reply

The assistant ends its reply with the filing: this is the sixth first-mention
lookup.

- **Pros:**
  - One voice, one message. Nothing arrives late or from elsewhere.
  - It is the assistant showing that it knows the shape of the conversation,
    which is what the room digest is for.
- **Cons:**
  - The topic must be known before the reply is written, so assignment
    moves into the turn and its time is added to every reply.
  - It needs the topics in the assistant's prompt, which no prompt has yet.
    That is the larger step, and it should not be taken to carry a
    housekeeping remark.
  - Every reply grows by a line that is about bookkeeping and not about the
    answer. The reply audit would have to learn to let it pass.

### E. Decide silently, by rule

No surfacing. Rules remove the cases: topics the model names alike are
merged, and topics under five parts are never compared (both built).

- **Pros:**
  - Nothing to read, nothing to answer.
  - Rules are testable, and these two fixed the case that prompted this.
- **Cons:**
  - A wrong filing is found only by opening the page: the negative surprise,
    later.
  - Rules cover what has been seen. The next kind of mistake has no rule.

### F. A topic chip on the request, in the chat page

Each request shows, beside its own message, the topic it was filed under:
quiet when the model was sure, marked when it was not. A click changes it.

- **Pros:**
  - At the time and in the place: on the message, in the conversation.
  - It is not a message. Nothing enters the room's rows, the chunks or a
    bridge, and the assistant's replies stay about the answer.
  - It shows every filing, so a confident mistake is as visible as a
    doubtful one. Only this option and A show those at all.
  - No decision is owed. A chip that is right is ignored.
  - A correction is a click on a list of the room's topics. No model reads
    it, and it is the operator's word in the operator's unit: this request.
- **Cons:**
  - It is not the assistant speaking. Over a bridge there is no chip.
  - It needs the topic early, like D: assignment per request, soon after
    the reply, and not per part after the chunk.
  - One more thing on a message row that already carries votes, a copy
    button and a menu.

## Recommendation

**F as the way it is surfaced, E as what keeps it quiet, and C only where
there is no page.** Then A is removed.

The reasoning, in the order the requirements were given:

1. **No queue.** F and E leave nothing pending. C leaves nothing pending
   either, but it adds a row; it is kept for the one place where a chip
   cannot be seen.
2. **At the time.** Only B, C, D and F are in the conversation. Of those, F
   is the only one that costs no words and no model reading.
3. **Only when it matters.** The mark on a chip, and the notice in C, both
   use the operator's threshold: the request scored in the uncertain range
   against a topic of five parts or more. By the measurement that is about
   once per 160 messages, which is rare enough that a mark means something.

B is not recommended at all: it is the decision the operator did not want,
with a model in the way of the answer. D is the right end state and the
wrong first step: once the room's topics are in the assistant's prompt for
their own sake, a reply can say what it noticed, and nothing here has to
change for that.

## The design

### 1. A request is filed when its reply has posted

A new housekeeping job, tier 1, per request: `request_topic`, subject the
request's message uuid. It runs when the reply has posted, so it adds
nothing to the reply's time, and a new message kills it like any other job.

- **What is compared is the request's own words**, and not a summary's
  phrase. This is the room digest's rule that the digest's words are the
  operator's or nobody's, and it removes the cause of this week's splits:
  a room summarized again worded the same requests anew, and they stopped
  matching. A request's words do not change.
- **Against the same things as today:** the nearest five topics by
  embedding, two distinct requests of each, both axes in one request to the
  decision model, joined at 0.5.
- **A short follow-up inherits.** A message the repeat check calls asking
  again, or one under 24 characters, takes its predecessor's topics and is
  not scored: "elaborate" has no topic of its own.
- **The chunk's parts stop being members.** A part keeps its summary and
  its result; a topic's members are requests. A topic's share is then
  counted in exchanges, which is what the operator counts in.

Cost: about six requests to the decision model per message, a third of a
second each, no text generated.

### 2. The chip

On the request's row in `/chat`, in the line that holds the time:

| state | shown |
|---|---|
| filed, sure | the topic's name, grey |
| filed, unsure, topic of five or more | the name with a mark, and on hover the score and the two requests it was compared with |
| a topic of its own | nothing, until a second request joins it |
| not filed yet | nothing |

Both axes can be shown; the kind of task first, the named thing only when
it recurs. The chip arrives over the stream the page already holds open, as
a change to the message's row, so an idle tab does no more than it does now
(`notes/chat-frontend-rules.md`).

A click opens a small menu: the room's recurring topics of that kind, "a
topic of its own", and "rename this topic", which opens the modal the
topics page uses. Choosing one writes the operator's filing, which outranks
the model's and is never rewritten by it. That is a statement about the
conversation, in the sense of the detector labels proposal, and it is also
the label: a request the operator moved is a recorded miss of the matcher,
with its score.

### 3. The notice, where there is no chip

For a room that has a bridge, and only for the marked case: one line after
the reply, from the housekeeping role, of kind `notice`:

> Filed with *first mentions in the diary* (11 so far). Reply "other topic"
> if it is something else.

- It is left out of the counted messages, so no chunk and no topic holds
  it, and it is not sent to the model as conversation.
- At most one per room per day. A second doubtful request that day is only
  marked.
- "other topic" is matched as text, exactly, as the first words of the next
  message, and then makes the request a topic of its own. No model reads
  the answer. Anything else the operator says is a message like any other.

This part is optional. Without it, a bridged room has topics and no
surfacing, as every room has today.

### 4. What goes away, and what stays

- **Goes:** the list of pairs with Merge and Not the same, and the job
  behind it (`room_topic_merges`), with its table. Its generative call is
  the one measured to say "same" 12 times in 14.
- **Stays:** the two rules (same name, under five parts), the topics page
  as the place to read the whole, the modal that renames a topic and merges
  it into another, and "answered with", counted from the records.
- **Changes:** the topics page gains what the chip cannot show: each
  topic's requests, newest first, each linking to its message in the room.

## What it costs to build

Sizes as in the roadmap: S is a day or two, M several.

| step | size | depends on |
|---|---|---|
| 1. The `request_topic` job, members as requests | M | the decision slot and the embedder, both in use |
| 2. The chip and its menu on `/chat` | M | step 1; the message row's meta and the open stream |
| 3. Removing the pair list and its job | S | step 2 in use |
| 4. The notice for bridged rooms | S | step 1; a rule that keeps notices out of counted messages |

Steps 1 and 2 are the proposal. Step 3 follows when the chip has been in
use long enough to trust. Step 4 can be left out.

## What is the operator's to decide

1. **Five.** The threshold is theirs and is used as given. It could be a
   setting beside the chunk size.
2. **Whether a sure filing shows a chip at all.** Shown, every confident
   mistake is visible and every row carries a word more. Hidden, only
   doubt is shown, and a confident mistake is found on the topics page.
   The recommendation is shown, grey.
3. **Whether the named thing gets a chip.** It recurred less and was wrong
   more than the kind of task. It could stay on the topics page only.
4. **Whether the notice is wanted** in bridged rooms, or silence there.
5. **What to do with the topics there are.** They were filed by summary
   phrases. A room would be filed again by its requests, beside the old
   filing, as a new prompt version summarizes a room again; the operator's
   own names and explanations carry over by name.

## What this does not solve

- **Why a topic recurs.** The chip says what a request was filed under,
  not why the operator keeps asking it. "Answered with" stays the only
  thing that says what a topic exercises.
- **Topics across rooms.** A chip names a topic of this room.
- **A confident mistake nobody looks at.** Showing it is not the same as
  it being seen. The matcher's misses are counted from the operator's
  corrections, so they are only counted where the operator corrected.
