# The room digest: topics, a compact timeline, and a fading summary

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:**
- [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md):
  repeat chains, the request profile, and the decision-model slots.
- [Later verdicts on earlier messages](2026-11-09-later-verdicts-on-earlier-messages.md):
  per-message judgements, the main input to topic status.
- [The conversation working set](2026-10-17-conversation-working-set.md):
  carries earlier *evidence*. The digest carries what was *talked about*, and
  sits beside it.

## The problem

A ReAct turn sees the room's last 30 messages. A room used every day passes
that within a day or two. A test room with 140 messages over ten days is
ordinary. Everything before the window is gone from the loop's view:

- what the conversation has been about;
- which threads were settled;
- which threads keep coming back.

What the loop is missing is not facts. Facts belong to memory, the diary and
the working set, and the history rules rightly keep old answers from posing as
evidence. It is missing the *shape* of the conversation:
- **The current topic, and how long it has been going.** Is this the third
  message about one thing, or a new subject?
- **The recent topics, and whether each was settled.**
- **A sense of the whole room.** Coarse for the distant past, finer for
  recent days.

The operator described three lenses on this:

1. **A tag table:** one row per topic, with its status.
2. **A compact timeline:** one short line per exchange, naming its topics.
3. **A Fibonacci-like summary:** recent exchanges in detail, older ones in
   progressively broader chunks, with some overlap at the chunk edges.

## Principles

**It describes topics and outcomes, never answers.** "Letter counting:
asked 5 times, the operator said answer 2 was wrong, a later computation
gave 3" is in scope. "Strawberry has 3 R's" is not. An answer in a summary is
an old answer posing as a fact. It reaches the next turn already shortened,
and a small model believes it. The digest says *what was discussed and how it
went*. To use the content, the loop reads it again.

**It is built from records, not reconstructed.** Topic assignments, verdicts,
repeat chains and chunk summaries are rows written once, when they happen.
Nothing is backfilled, and an old row is never rewritten. Status is
*derived* at read time from those rows, so it can change without anything
being edited.

**Silence is not success.** A topic nobody came back to is `quiet`, not
`solved`. Only the operator settles a topic, by mark or in their own words.

## The design

Eight decisions shape it.

### 1. The unit is the exchange

An **exchange** is one human message plus this agent's reply or replies to
it. That is the same identity the repeat proposal records:
`metadata_["request_message"]` on the run. Exchanges are numbered in room
order, from the latest context boundary forward, and each row stores its
message UUIDs, so a number is never the only key.

The digest covers two regions:
- **The window.** The newest 30 to 39 messages, verbatim in
  `conversation_history_xml`, starting at a chunk edge (decision 5). The
  digest gives them topic tags only.
- **The off-screen region.** Everything older. The digest gives it tags, the
  timeline, and the fading summary.

### 2. Topics: a small table per room, assigned by a decision model

```
room_topic        uuid, room_uuid, slug, title, created_at, created_by_message
exchange_topic    message_uuid, topic_uuid, probability, source, model, created_at
topic_alias       topic_uuid, alias_of_uuid, created_at       (merges)
topic_mark        topic_uuid, mark ('open'|'settled'|'cleared'), created_at  (operator)
```

All four are append-only.
- A **rename** is a new title row: `room_topic` keeps its title history and
  the newest row wins.
- A **merge** is an alias row.
- The operator's word on a topic is a `topic_mark` row.

**Slugs are short and readable** (`letter-count`, `unit-conversion`), not
`TAG82`. A 4B model is poor at indirection through an id table, and a slug
costs a few tokens more.

**Assignment.** The request profile runs at the start of the turn (repeat
proposal, decision 1). It gains one more named question in the same call:
a `topic` Choice over the room's 40 most recently used topics, plus `none`.
Each option's description is the topic's title.

| result | effect |
|---|---|
| a topic at ≥ 0.8 | `exchange_topic` row, source `profile` |
| `none` at ≥ 0.8 | a new topic, named after the turn by one local generative call (slug + title, both under 60 characters), then assigned |
| below the band | no topic; the timeline shows `?` for that exchange |

**Follow-ups inherit.** A short message classified `redo` or `expand`, or an
adjacent repeated command, takes its predecessor's topic, with source
`inherited`. It is not classified by its own words, since "elaborate" has no
topic of its own.

**One topic per exchange** in the first release. A second topic would need
its own evaluation, because the Choice is calibrated for one answer.

**Operator controls** on /assistant:
- rename a topic;
- merge two topics;
- mark a topic open or settled;
- reassign an exchange.

Renaming uses the click-to-rename modal (`notes/ui-modal-rename.md`). A
reassignment is a new `exchange_topic` row with source `operator`, which
outranks `profile`.

### 3. Topic status is derived, with four states

| status | when |
|---|---|
| `contested` | the newest verdict on any exchange in the topic is `wrong` or `doubted`, with no later `right` |
| `open` | the operator marked it open; or a repeat chain in it is `recent` and its newest attempt has no `right` verdict |
| `settled` | the operator marked it settled; or the newest attempt carries a `right` verdict from the operator |
| `quiet` | none of the above |

**How the rules combine:**
- **The operator's topic mark outranks every derived signal**, and a newer
  mark outranks an older one.
- **A computation's `computed_differently` makes a topic `contested`**, but
  only when no operator verdict is newer. `computed_same` never settles a
  topic on its own.
- **Reply-check scores do not set status** in the first release. They are
  diagnostics, as in the repeat proposal.

The status carries its basis, so the loop and the operator can see why:
`contested (you said the answer was wrong, 2 h ago)`.

### 4. The compact timeline

One line per exchange, newest last, as the operator sketched it:

```
51 letter-count
52 letter-count
53 letter-count
54 unit-conversion
55 assistant-self
56 assistant-self
```

Runs of one topic collapse to one line, with a count and the topic's current
status:

```
51-53 letter-count ×3 contested
54    unit-conversion
55-56 assistant-self ×2
```

**What it covers.** Every exchange since the latest context boundary, window
included, so the current topic's run is visible. Exchanges with no topic show
`?`, and those still collapse with their neighbours.

**Bound.** At most 40 lines after collapsing. Beyond that, the oldest lines
are dropped and the fading summary (decision 5) covers that range.

### 5. The fading summary: fixed chunks, counted from the first message

The operator's design:

- **Messages are counted from 0**, the room's first message, however many
  are appended later.
- **Level 1:** every 10 messages are compacted into one summary: messages
  0–9, then 10–19, and so on. Each summary also reads the 2 messages before
  its chunk and the 2 after it.
- **Level 2:** every 10 level 1 summaries are compacted into one, with the
  same margin of 2 messages on each side.

**Appending never invalidates a summary.** A chunk's range depends only on
the message count. A new message can complete the newest chunk, and touches
nothing older. Once written, a summary is final. It is never recomputed,
re-cut or merged, whatever is appended after it.

**The chunks:**

| level | covers | written when | reads |
|---|---|---|---|
| 1 | messages 10k to 10k+9 | message 10k+11 exists (its after-margin) | the 10 messages, plus 2 before and 2 after as context |
| 2 | level 1 chunks 10j to 10j+9, which is messages 100j to 100j+99 | its tenth level 1 summary is written | the 10 summaries, plus 2 raw messages before and 2 after |
| 3 | 10 level 2 chunks, 1,000 messages | the same rule | only in rooms that large |

**Every message counts:** human messages, replies, and progress messages
the loop posted during a turn. That makes the count a plain position in the
room, with no judgement about which messages matter.

**A chunk ends after the last message it covers, not at an index.** Each
node stores its first and last message UUIDs. The next chunk starts at the
message after the previous chunk's last one. A message deleted later
therefore renumbers nothing already written. Its chunk's summary text is
withheld at render time through the provenance rule (decision 8), and is
never rewritten.

**Margins are context, not content.** The 2 messages on each side are
marked as context and are not summarized, so no message is summarized
twice. With them, the summarizer can tell whether a topic began before the
chunk or runs on after it. It records that as structure:

```
{"summary": "...", "topics": ["letter-count", "assistant-self"],
 "continues_from_previous": ["letter-count"],
 "continues_into_next": []}
```

When rendering, two neighbouring chunks that share a topic across their edge
are shown joined, so a thread crossing an edge reads as one thread. The
chunk's `topics` come from the exchange topic records (decision 2) where
they exist. The summarizer writes the summary text and the two flags.

**Why the edges are not moved to topic changes.** Cutting where the topic
changes would make a chunk's range depend on topic records. Those can
change later: an operator reassigns an exchange, or a topic is named after
the turn. A written summary would then cover the wrong range, and would have
to be invalidated. That is exactly what fixed counting avoids. The margin
and the flags handle a topic that crosses an edge.

**Why there is no bisect for topic transitions.** It was measured
(`topic_transitions` in the probe fixture), and it fails in two ways:
- **A topic that comes back looks like no change.** d3 rated messages 1 and
  8 of a 12-message sequence as the same topic, at 0.98, with four
  transitions in between.
- **Follow-ups read as new topics.** "again", "thanks" and "elaborate" were
  scored as topic changes.

Fixed counting needs no transitions at all.

**Rendering is aligned to chunk edges.** For n messages before the current
request:

1. **The verbatim window** starts at the largest multiple of 10 that leaves
   at least 30 messages after it. It holds 30 to 39 messages, not exactly
   30. A window cut at exactly 30 would split a chunk: half of it would be
   verbatim and half not, so it would be shown either twice or not at all.
2. **Level 2** (and level 3) summaries cover every complete group of 100
   messages before the window.
3. **Level 1** summaries cover the rest up to the window: 0 to 9 chunks.
4. **Gaps.** A level 2 chunk not yet written is shown as its level 1
   chunks.

When the tenth level 1 chunk of a group is written, the group is shown as
one level 2 summary instead. This follows the operator's rule
(compact when N accumulate), so the level 1 detail before the window ranges
from 0 to 90 messages.

**What changes from turn to turn.** The window's first message and the list
of summaries change only when the count crosses a multiple of 10: about once
per five turns. In between, a new turn only appends to the end of the
history.

**Prefix reuse becomes possible.** Today's 30-message window loses reuse
across turns, because its first message changes every turn. An aligned
window keeps the history's head byte-identical for about five turns.

The backend reuses those bytes only if nothing per-turn renders before them.
Today the request renders ahead of `conversation_history_xml` in the context
message, so the gain needs a layout change that puts the summaries and the
history ahead of the per-turn sections. That change is a separate,
measured step (the prefill time on turns 2 to 5 after a chunk edge), not
something this proposal claims.

**Cost.** One generative call per 10 messages at level 1, plus one per 100
at level 2: about 0.11 calls per message. A level 1 chunk completes about
once per five turns, so one call per finished turn keeps pace.

**What the steps cost in detail.** Detail falls off in steps, not smoothly
at φ:
- 30 to 39 messages verbatim;
- 0 to 90 messages at level 1;
- everything older at level 2.

That is coarser than a Fibonacci gradient, and a deliberate trade for
summaries that are written once and never invalidated.

**Rooms that predate the digest.** Chunks depend only on the message count,
not on topic records, so an existing room's history can be chunked by the
same job, run over a backlog. Chunks older than topic recording have empty
`topics` lists. Whether to run that backlog is a separate decision: a room
of 2,000 messages is about 220 generative calls.

**What a summary may say.** At most 300 characters per chunk:
- which topics came up;
- what was asked, in kind ("conversions between metric and imperial units");
- how it went, from records only: repeats, verdicts, failed runs.

**What it may not say:** the content of an answer, a value, a name read from
a source, or a quoted reply. The summarizer is instructed so, and its output
is screened with a decision-model Noul: "Does the summary state the content
of an answer, a value or a name from a reply?" A summary that fails the screen
is not stored. The chunk renders as its topic list only, and the failure is
recorded. That screen has not been measured. It is added to the probe fixture
before the chunk step ships.

**Who writes it.** A local generative model, on a slot verified local by the
repeat proposal's rules: loopback and verified local execution. It runs in
post-turn housekeeping (decision 8), never on the request path.

### 6. The current topic

The turn-start `topic` Choice (decision 2) gives the current request's topic
before any planning. The digest opens with it:

```
current topic: letter-count (contested; 3rd exchange in a row; first seen 2 h ago)
```

This is the "where were we" line. It is the cheapest part of the design, and
useful even with nothing else built. For a short follow-up, the topic is the
inherited one.

### 7. In the prompt

```xml
<room_digest authority="context" content_is_data="true">
  <current_topic slug="letter-count" status="contested" run="3"
                 first_seen="2 h ago"/>
  <topics>
    <topic slug="letter-count" status="contested" exchanges="7"
           last="now" basis="operator said wrong, 2 h ago"/>
    <topic slug="unit-conversion" status="quiet" exchanges="2" last="1 day ago"/>
  </topics>
  <timeline>…collapsed lines…</timeline>
  <earlier>
    <chunk level="2" messages="0-99" topics="assistant-self,memory-check">…</chunk>
    <chunk level="1" messages="100-109" topics="kanban-status">…</chunk>
    …
  </earlier>
</room_digest>
```

**Budget.** At most 3,000 serialized characters:

| part | limit |
|---|---|
| topics | 12 most recently used |
| timeline | 40 lines |
| summaries | whatever remains; the oldest chunks drop first, counted in an `omitted` attribute |

**Placement.** The block is constant for the turn. It goes in the dynamic
tail, right after `repeated_request`, so the turn's calls share their prefix
through it. The `earlier` part changes only at a chunk edge, so the layout
change in decision 5 may move it ahead of the per-turn sections, next to the
history it precedes.

**Source priority.** Context only, at the rank of `conversation_history_xml`.

**Consumers:**
- **The decide call**, for orientation.
- **The reply audit**, so that a reply to "what have we talked about?" which
  rests on the digest is not rejected as unsupported.

The digest supports statements about *what was discussed*, never about what
is true.

**The rule added to the decide instructions:**

> `room_digest` describes what this room has talked about and how each topic
> went. It never states what is true. To use the content of an earlier topic,
> read it again. A `contested` topic had an answer rejected. Do not repeat
> that answer without fresh evidence. A `quiet` topic is not resolved; it was
> simply not raised again. To say what the conversation has covered, use the
> digest.

### 8. Access, and housekeeping order

**Summary text follows the strictest rule in force.** A chunk summary is
model-written from replies, and replies can restate diary text or facts later
made secret. So:

- **Summary text goes only to verified-local models.** A consumer whose
  model is not verified local gets the digest without summary text: current
  topic, topics, statuses, timeline, and each chunk's topic list.
- **Provenance is kept per chunk.** Each `room_digest_node` stores the run ids
  it covers and whether any used `diary_query` or a memory read. A later
  policy change can then withhold exactly the affected chunks without
  rewriting any.
- **Before the latest context boundary, the timeline stops.** Topics continue
  across the boundary. A profile switch is not an audience boundary, but facts
  may have been invalidated. Chunks before the boundary are shown as topic
  lists only, marked `before_context_change`.

```
room_digest_node  uuid, room_uuid, level, first_message, last_message,
                  first_index, last_index, summary, topics, continues_from,
                  continues_into, provenance, model, digest, created_at
```

**Housekeeping order.** After each finished turn, the room's post-turn jobs
run in this order. Each is bounded, and each is skipped rather than retried
through another model when it fails:

1. **The run summarizer**, as today.
2. **The decision-model detectors**, all fast: verdicts, hedges, reply checks.
3. **Topic work.** A new topic is named here, after a turn-start `none`. This
   is one short generative call, only when needed.
4. **Chunk summaries.** At most one generative chunk call per finished turn.
   A level 1 chunk completes about once per five turns, so one call keeps
   pace. Any backlog renders as level 1 chunks until it catches up.

A job not yet started when the room receives a new message waits until that
turn ends. Housekeeping never competes with the request path for the GPU.

## Measuring it

- **Topic assignment**, against hand labels on synthetic rooms. Precision
  inside the band, abstentions, how often `none` creates a topic that should
  have been an existing one, and how often an existing topic absorbs a new
  subject.
- **The answer screen.** It is added to `benchmarks/decision_probe` as an
  experiment: summaries that state an answer, a value or a name, and
  summaries that do not. It ships only with measured precision.
- **Orientation, in scripted rooms** of 80–200 invented exchanges with known
  topics. At turn N, replayed with and without the digest:
  - "What is the current topic?"
  - "What did we talk about before the kanban questions?"
  - "Is the letter-counting question settled?"
  - a repeat of a `contested` question.

  Scored on correctness, on whether a `quiet` topic is ever called solved,
  and on whether a rejected answer is repeated.
- **No answer leakage.** Across the scripted rooms, no rendered summary
  states the content of an answer. The screen's misses are counted here.
- **Cost.**
  - The turn-start Choice: its added latency, on top of the request profile
    it shares a call with.
  - Generative housekeeping calls per turn, and their seconds.
  - Backlog depth.
  - Digest characters.
- **The operator's view.** Every topic, status and chunk is visible on
  /assistant with its basis. Operator corrections (renames, merges, marks,
  reassignments) are counted as the signal of what the automatic parts get
  wrong.

## Rollout

1. The topic tables, the `topic` Choice in the request profile, follow-up
   inheritance, and naming new topics. Recording only: topics show on
   /assistant, and nothing reaches a prompt.
2. Derived status and the timeline, shown on /assistant, with the operator
   controls.
3. Chunk summaries in housekeeping, after the answer screen has been
   measured.
4. The `room_digest` block in the decide and audit prompts, behind
   `assistant.room_digest` (`off` / `record` / `on`):
   - `record` stores the would-send block on the run, as the repeat proposal
     does.
   - The current topic and the timeline can go `on` before the summaries do.

## What this does not do

- **Cross-room topics.** Topics are per room. Recognizing that two rooms
  discuss the same thing is the identity question again, across rooms, and
  belongs with the cross-session memory work this conversation started from.
- **Replace the history window or the working set.** The window keeps the
  verbatim recent messages, and the working set keeps exact evidence. The
  digest orients; it does not inform.
- **Promote anything to memory.** A topic is not a fact about the operator.
- **Say a topic is solved because it went quiet.**

## Probe evidence

The topic Choice was measured on 14 invented messages against six invented
topics plus `none`:
- the cases are the `topic_assignment` experiment in
  `benchmarks/fixtures/decision_probe_cases.json`, fixture version 4;
- the raw run is `benchmarks/fixtures/decision_probe_runs/2026-11-09-topic-assignment.json`.

| question | d1:3b | d3:4b |
|---|---|---|
| `topic` Choice (existing topics + `none`) | 14/14; 13 in band, 0 wrong; 0.17 s | 14/14; 14 in band, 0 wrong; 0.69 s |
| companion "starts a new topic" Noul | 10/14; 3 wrong in band | 4/14; nothing in band |

**The companion Noul failed for a structural reason.** The questions in one
request are answered in isolation against the shared state. That Noul's state
held only the message, so it never saw the topic list. The `none` option inside
the Choice carries the same meaning with the list in view, and got every case
right. So the design uses `none` and asks no companion question. Any
question that must see the options needs them in its own state or
instructions.

Topic transitions were measured on one 12-message invented sequence: five
topics, one of them returning after three others. All 18 pair questions went
in one request, with the sequence in the state:
- 11 adjacent pairs, which locate every transition;
- 7 non-adjacent pairs, which are what a bisect asks.

The raw run is `benchmarks/fixtures/decision_probe_runs/2026-11-09-topic-transitions.json`.

| | d1:3b | d3:4b |
|---|---|---|
| adjacent pairs, transitions (5) | 5/5 called different | 5/5 called different |
| adjacent pairs, continuations (6) | 1/6 called same | 2/6 called same: the follow-ups failed |
| topic returning (messages 1 and 8) | 0.10, called different | 0.98, called same, which is what misleads a bisect |
| request time | 0.8 s | 10.3 s |

So chunk edges are fixed by the message count, and neither pair comparison
nor bisection is used.

