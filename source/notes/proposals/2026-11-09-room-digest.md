# The room digest: topics, a compact timeline, and a fading summary

**Status:** Proposal. One part is built as an experiment, to see how a local
model does: a generated `asked` phrase and topic names per chunk of a room's
messages, shown on the Housekeeping page and reaching no prompt
(`notes/housekeeping-design.md`). There the chunk size and the context on each
side are settings, a chunk is summarized once that context exists, and
nothing waits for the window.
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
repeat chains and chunk edges are rows written once, when they happen. No
record is backfilled, and an old row is never rewritten. Status and each
chunk's contents are *derived* at read time from those rows, so they can
change without anything being edited.

**Silence is not success.** A topic nobody came back to is `quiet`, not
`solved`. Only the operator settles a topic, by mark or in their own words.

**Its words are the operator's, or nobody's.** The digest is made of three
kinds of content, in the order they earn their place:
1. **Records:** counts, topics, outcomes and dates. No words at all.
2. **Request excerpts:** the first characters of a request the operator
   typed, cut and marked as an excerpt. A request is not an answer, so
   quoting one breaks no rule above, and it costs no model call.
3. **Generated text:** a model's phrase about what was asked. It is the last
   rung, and it ships only if it measurably beats the excerpts.

No text from a reply ever enters the digest, by any of the three routes.

**It is an index, not a replacement.** Compaction loses detail by design.
What makes that safe is that every line carries the range of messages it
stands for, and the loop can read that range again (decision 9). The digest
tells the loop where to look. The messages stay the only source of what was
said.

## The design

Nine decisions shape it.

### 1. The unit is the exchange

An **exchange** is one human message plus this agent's reply or replies to
it. That is the same identity the repeat proposal records:
`metadata_["request_message"]` on the run. Exchanges are numbered in room
order, from the latest context boundary forward, and each row stores its
message UUIDs, so a number is never the only key.

The digest covers two regions, and no exchange is shown in both:
- **The window.** The newest 30 to 39 messages, verbatim in
  `conversation_history_xml`, starting at a chunk edge (decision 5). The
  digest gives them the timeline (decision 4): one topic tag per exchange.
- **The off-screen region.** Everything older. The digest gives it the
  fading summary (decision 5), whose lines continue the timeline at a
  coarser grain.

**A thread** is a run of exchanges that belong together. A chunk is
described by the threads that pass through it.
- **With topic records,** a thread is a run of exchanges of one topic.
- **Without them,** a thread is an opening request plus the follow-ups after
  it, decided with no model. A request is a follow-up when it is shorter
  than 24 non-whitespace code points (the exact matcher's floor in the
  repeat proposal), or when it is an exact or extended repeat of a request
  already in the thread. Any other request opens a new thread.

Threads are found over the whole room in message order, and a message's
thread depends only on the messages before it. So appending never changes an
earlier thread, and a thread can cross a chunk edge. Each chunk shows the
part inside it, marked `continues` when the thread began earlier.

The rule without topics splits too often and never joins two subjects. A
thread split in two costs one extra line. Two subjects joined into one would
hide the second.

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
| `open` | the operator marked it open; or a repeat chain in it is `recent` and its newest attempt has no `right` verdict; or its newest exchange ended in a run whose status is a failure |
| `settled` | the operator marked it settled; or the newest attempt carries a `right` verdict from the operator |
| `quiet` | none of the above |

**How the rules combine:**
- **The operator's topic mark outranks every derived signal**, and a newer
  mark outranks an older one.
- **A computation's `computed_differently` makes a topic `contested`**, but
  only when no operator verdict is newer. `computed_same` never settles a
  topic on its own.
- **A failed run makes a topic `open`** until a later exchange in the topic
  ends in a run that did not fail. This reads the run's status (a step
  limit, an error), which is a fact about the run. The run summarizer's
  `partial` and `resolved` are a model's judgement of its own work. They are
  counted in a chunk's outcome and never set a status.
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

**What it covers.** The exchanges of the verbatim window, so the current
topic's run is visible. Exchanges with no topic show `?`, and those still
collapse with their neighbours. Everything older is covered by the fading
summary (decision 5), whose thread lines are the same kind of line at a
coarser grain. No exchange is listed in both.

**Bound.** A window holds at most 39 messages, so the timeline is at most 20
lines, and usually under 10 after collapsing.

**Breaks are shown.** When 8 hours or more pass between two exchanges, a line
between them says so:

```
51-53 letter-count ×3 contested
— 3 days —
54    unit-conversion
```

A break costs nothing to derive, and it is the strongest hint a room has that
the operator is coming back to something, not continuing it. When the current
request itself follows a break, the `current_topic` line says so (decision 6).

### 5. The fading summary: fixed chunks, counted from the first message

The operator's design:

- **Messages are counted from 0**, the room's first message, however many
  are appended later.
- **Level 1:** every 10 messages form one chunk: messages 0–9, then 10–19,
  and so on.
- **Level 2:** every 10 level 1 chunks form one, which is 100 messages.
- **Level 3 and up:** the same rule again. Level *k* is 10^*k* messages.

**A chunk is built from records, not written by a model.** Its topics, its
exchange count, its verdicts, its repeat chains, its failed runs and its
dates already exist as rows (decisions 2 and 3, the repeat proposal and the
verdicts proposal). A chunk renders them for its range at read time, the same
way topic status is derived. So:
- a chunk costs no model call;
- an operator's reassignment or merge shows in every chunk it touches, and
  nothing is rewritten;
- a chunk of any higher level is the same rendering over more messages. It
  is never generated from the text of a lower level, so no text in the
  digest is a summary of a summary.

**A level 1 chunk is described thread by thread** (decision 1), in the order
the threads began. Each thread line carries:
- its topic, or `?`;
- its message ranges, which is what `room_history` takes (decision 9);
- its exchange count and its outcome;
- a **request excerpt**.

**The request excerpt.** The thread's opening request, wherever in the room
it lies, with whitespace collapsed, cut to its first 80 code points, back to the last whitespace when
there is one in the final 20, and marked with `…` when cut. It is read from
the message row at render time, so it is never stored, an edit shows at once,
and a deleted message has no excerpt. Three reasons it is the opening request
and nothing else:
- **It says what kind of thing was asked**, in the operator's own words, with
  no model between.
- **A follow-up has no content of its own.** "again" says nothing, and a
  correction ("no, it is 3") states an answer.
- **A reply is never quoted**, by the first principle.

An excerpt is 80 code points of a request, so it is not the request. A rule
stated in a request is not carried by it (What this does not do).

**Levels 2 and up have no excerpts.** They list topics with counts and
outcomes, which is the fade: threads with their opening words near the
window, topics behind them, and a room's main topics at the far end.

A model can add one thing neither records nor excerpts hold: a phrase saying
what was asked across a chunk, in kind. That phrase is optional (below).

**What is stored.** One node per level 1 chunk. Housekeeping writes it once,
with no model call, when the window has moved past the chunk: when at least
30 counted messages follow its last one. Until then the chunk is inside the
window, shown verbatim, and needs no node.

**Why the node waits for the window.** Two things change the newest messages
after they are posted:
- **A retry rewinds.** `delete_room_messages_from` in `db/chat.py` deletes
  every message from the retried turn's anchor onward.
- **A reply streams in place.** Its row is updated until `streaming` is
  false.

A node written at the chunk's tenth message could have its edge deleted by
the next retry, or hash a reply that was still streaming. By the time 30
counted messages follow a chunk, both are settled, so its edges and hash hold.

```
room_digest_node  uuid, room_uuid, first_message, last_message,
                  first_index, last_index, content_hash, provenance,
                  asked, asked_topics, model, created_at
```

- `first_message`, `last_message`: the edges, as message UUIDs, with their
  integer ids in `first_index` and `last_index`.
- `content_hash`: over the ids and texts of the chunk's counted messages,
  taken when the node is written.
- `provenance`: the run ids the chunk covers, and whether any of them used
  `diary_query`, a memory read, `room_digest` or `room_history`.
- `asked`, `asked_topics`, `model`: empty unless generated text was written
  (the `asked` phrase, or a backfill).

Levels 2 and up have no node. Each is derived from the level 1 nodes in its
range and the records under them.

**Appending never invalidates a chunk.** A chunk's range depends only on the
counted messages. A new message can complete the newest chunk, and touches
nothing older. A node, once written, is never recomputed, re-cut or merged.

**Which messages count.** Exactly the set the history window draws from,
through one function that both the window and the chunk job call.
- `kind="progress"` rows never count. They are reaped when the sender posts
  its real reply (`post_chat_message` in `db/chat.py`), so a count that
  included them would shrink after every turn.
- Per-call filters such as `history_skip` apply after the window is cut, as
  they do today, and never move an edge.

**Edges are stored, not recomputed.** The next chunk starts at the first
counted message whose id is greater than the previous node's `last_index`.
Message ids only grow, so this holds even when that last message has since
been deleted. A message deleted later renumbers nothing already written. One
function gives every edge: the newest node's `last_index`, then every 10th
counted message after it. The window start and the chunk job both call it,
so a deletion cannot make them disagree.

**A rewind that reaches into a written node** (a retry of a turn more than 30
messages back) is rare, and handled without re-cutting:
- the node stands, and now covers fewer than 10 counted messages, or none;
- its record-derived part renders from the rows that remain;
- its generated text is withheld, because its hash no longer matches;
- the messages posted after the rewind fill new chunks after the newest
  node's `last_index`.

**Edits and deletions.** At render, a node's `content_hash` is recomputed over
its range. On a mismatch, from an edit or a deletion, the record-derived part
renders as usual, because it reads the current rows. Any generated text is
withheld, the same way as under the provenance rule (decision 8). Nothing is
rewritten.

**Outcomes are kept per thread within a chunk.** Each thread line has its own
exchange count and outcome: verdicts, repeats, failed runs. A chunk-wide
outcome would not say which of its three topics had the rejected answer.
Exchanges with no topic form threads by the rule in decision 1, each under
`?`. Level 2 aggregates per topic over its 100 messages, which makes it the
operator's tag table: topic, chunks, outcome. All of a level 2 chunk's `?`
threads are one line.

**A chunk carries its dates.** `from` and `to` are the calendar dates of its
first and last counted message, written out in full. A relative age ("2 days
ago") would change the chunk's bytes as the clock moves, and the chunks are
the part of the prompt meant to stay byte-identical between turns.

**Crossing an edge is derived too.** A topic continues across an edge when
the last exchange before it and the first after it share a topic in
`exchange_topic`. When rendering, two neighbouring chunks that share a topic
across their edge are shown joined, so a thread crossing an edge reads as one
thread.

**Why the edges are not moved to topic changes.** Cutting where the topic
changes would make a chunk's range depend on topic records. Those can
change later: an operator reassigns an exchange, or a topic is named after
the turn. A stored node would then cover the wrong range. That is exactly
what fixed counting avoids.

**Why there is no bisect for topic transitions.** It was measured
(`topic_transitions` in the probe fixture), and it fails in two ways:
- **A topic that comes back looks like no change.** d3 rated messages 1 and
  8 of a 12-message sequence as the same topic, at 0.98, with four
  transitions in between.
- **Follow-ups read as new topics.** "again", "thanks" and "elaborate" were
  scored as topic changes.

Fixed counting needs no transitions at all.

**Rendering is aligned to chunk edges.** For the counted messages before the
current request:

1. **The verbatim window** starts at the newest edge that leaves at least 30
   counted messages after it. It holds 30 to 39 messages, not exactly 30. A
   window cut at exactly 30 would split a chunk: half of it would be verbatim
   and half not, so it would be shown either twice or not at all.
2. **Level 1** chunks cover the hundred the window starts in and the whole
   hundred before it: 10 to 19 chunks, or fewer in a room that short.
3. **Level 2** chunks cover the thousand that level 1 starts in and the whole
   thousand before it, up to where level 1 begins: 10 to 19 chunks.
4. **Each higher level** repeats the rule, up to the room's first message.
5. **A chunk whose node is not written yet** renders from records alone. It
   has no generated text to withhold.

With `W` the window's first index, level 1 starts at
`H1 = max(0, (W // 100 - 1) * 100)`, and level 2 at
`H2 = max(0, (H1 // 1000 - 1) * 1000)`.

**A group is compacted one group late.** Compacting a hundred at the moment
its hundredth message is counted would leave the digest coarsest exactly
where the loop needs it finest: the window would be followed directly by one
line for the 100 messages just behind it. Holding each group back by one
keeps 100 to 190 messages of thread lines behind the window at all times.
The operator's rule (compact when 10 accumulate) still decides *what* a level
2 chunk is. The delay only decides *when* it replaces its ten level 1 chunks
in the rendering. Nothing stored depends on it, because only level 1 has
nodes.

**The digest grows with the logarithm of the room.** Each level shows at most
19 chunks, and a room of *N* messages has about log10(*N*) levels. A room of
20,000 messages renders at most four levels. So nothing ever has to be left
out for age alone, and the budget (decision 7) only trims detail.

**What changes from turn to turn.** The window's first message and the list
of chunks change only when the count crosses an edge. At about two counted
messages per exchange, that is about once per five turns. In between, a new
turn only appends to the end of the history.

**Prefix reuse becomes possible.** Today's 30-message window loses reuse
across turns, because its first message changes every turn. An aligned
window keeps the history's head byte-identical for about five turns.

The backend reuses those bytes only if nothing per-turn renders before them.
Today the request renders ahead of `conversation_history_xml` in the context
message, so the gain needs a layout change that puts the chunks and the
history ahead of the per-turn sections. Two other things change bytes in that
head, each costing one partial prefill:
- a `later_verdict` attribute added to a message in the history;
- a verdict that changes a chunk's derived outcome.

The layout change is a separate, measured step (the prefill time on turns 2
to 5 after an edge, with verdicts counted), not something this proposal
claims.

**Cost.** Record-derived chunks need no model call, and a node is one row per
10 counted messages. The optional `asked` phrase is one generative call per
10 counted messages.

**What the steps cost in detail.** Detail falls off in steps, not smoothly
at φ:
- 30 to 39 messages verbatim;
- the next 100 to 190 messages as threads, with their opening words;
- the next 1,000 to 1,900 messages as topics per hundred;
- everything older as topics per thousand, and so on.

That is coarser than a Fibonacci gradient, and a deliberate trade for chunks
that are written once and never invalidated.

**The `asked` phrase: optional, and only if it earns its place.** A topic
title says what a thread was about. It does not say what kind of thing was
asked in it: "conversions between metric and imperial units" against
`unit-conversion`. The request excerpt already says much of that for free,
in the operator's words, so the phrase has to beat the excerpt and not just
an empty chunk. Whether it does is measured (Measuring it), and the phrase
ships only if it does. When it is enabled:
- one local generative call per level 1 chunk, in housekeeping, of at most
  120 characters, given the topic titles of its range;
- it reads the chunk's messages plus the 2 counted messages on each side,
  marked as context and not summarized;
- replies whose run used `room_digest` are left out of its input, so the
  digest never summarizes its own output;
- its result is dropped if the chunk's `content_hash` changed while the call
  ran.

Level 2 never gets a phrase.

**Rooms that predate the digest.** Chunks older than topic recording have no
topic records, but they are not without records:
- **Runs.** Every exchange the assistant answered has an `assistant_run` row
  with its status and the run summarizer's outcome (`resolved`, `partial`,
  `failed`). A run is matched to its exchange through its journal payload,
  the recovery path the repeat proposal names for runs older than
  `metadata_["request_message"]`.
- **Exact repeats.** The repeat proposal's exact matcher is a pure function
  of message text. For an old chunk it runs at render time over the chunk
  and the chunks before it. Its result is shown, never stored as repeat
  records.

So an old chunk renders its threads (decision 1), each with its request
excerpt, its runs by outcome and its exact repeats, all under `?`. That is
enough to find a conversation again and open it with `room_history`. It
needs no model call and no recording period, so a room compacts on the day
this is built.

Topic names are the one thing missing. They matter most at level 2 and up,
where there are no excerpts and every thread of an old hundred falls under
one `?` line. That is where generated text could earn its cost. A backfill
job writes `asked` and `asked_topics` for each such level 1 chunk, with the same
margins, input rule and screen. `asked_topics` are names for display only.
They are not `exchange_topic` records, so no topic is assigned to an old
exchange after the fact. Whether to run it is a separate decision. A room of
2,000 messages is about 200 generative calls, since level 2 is never
generated. At the housekeeping limit of 10 calls per burst with a 60-second
cool-down, that is 20 bursts, so it can only trickle through idle time, never
run in one go.

**What generated text may say.** The `asked` phrase and backfill text alike:
- which topics came up;
- what was asked, in kind ("conversions between metric and imperial units").

**What it may not say:** the content of an answer, a value, a name read from
a source, or a quoted reply. How a topic went comes from records only, never
from generated text. The generator is instructed so, and its output is
screened with a decision-model Noul: "Does the text state the content of an
answer, a value or a name from a reply?" Text that fails the screen is not
stored. The chunk renders from records, and the failure is recorded. That
screen has not been measured. It is added to the probe fixture before any
generated text ships.

**Who writes it.** A local generative model, on a slot verified local by the
repeat proposal's rules: loopback and verified local execution. It runs in
post-turn housekeeping, never on the request path.

### 6. The current topic

The turn-start `topic` Choice (decision 2) gives the current request's topic
before any planning. The digest opens with it:

```
current topic: letter-count (contested; 3rd exchange in a row; first seen 2 h ago)
```

This is the "where were we" line. It is the cheapest part of the design, and
useful even with nothing else built. For a short follow-up, the topic is the
inherited one.

When the request follows a break (decision 4), the line says so, and names
what the room was on before it:

```
current topic: unit-conversion (new; after a break of 3 days; before it: letter-count, contested)
```

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
  <timeline>…collapsed lines for the window's exchanges…</timeline>
</room_digest>

<room_earlier authority="context" content_is_data="true">
  <chunk level="2" messages="0-99" from="1999-10-04" to="1999-10-19"
         exchanges="48" before_topics="true">
    <topic slug="?" exchanges="48" outcome="33 resolved, 12 partial, 3 failed, 6 exact repeats"/>
  </chunk>
  <chunk level="1" messages="100-109" from="1999-10-19" to="1999-10-19"
         exchanges="5" before_topics="true">
    <topic slug="?" messages="100-105" exchanges="3" outcome="2 resolved, 1 exact repeat"
           opened_with="which tasks are still open on the garden board"/>
    <topic slug="?" messages="106-109" exchanges="2" outcome="1 resolved, 1 failed run"
           opened_with="how many times does the letter r occur in the word…"/>
  </chunk>
  …
  <chunk level="1" messages="200-209" from="1999-10-27" to="1999-10-28" exchanges="5">
    <topic slug="kanban-status" messages="200-205" exchanges="3"
           outcome="2 resolved, 1 partial"
           opened_with="move the two painting tasks to the done column"/>
    <topic slug="letter-count" messages="206-209" exchanges="2"
           outcome="1 repeat, 1 marked wrong"
           opened_with="how many times does the letter r occur in the word…"/>
  </chunk>
  …
</room_earlier>
```

**Two blocks, because they change at different rates.**
- **`room_digest` changes every turn.** It holds the current topic, the
  topic table and the window's timeline, with relative ages.
- **`room_earlier` changes about once per five turns:** at a chunk edge, and
  when a verdict, a reassignment or an edit touches a chunk it shows. It
  holds only absolute dates.

**Budget.**

| block | limit | parts |
|---|---|---|
| `room_digest` | 1,500 serialized characters | the 12 most recently used topics; the timeline, at most 20 lines |
| `room_earlier` | 5,000 serialized characters | every chunk the rendering rule gives (decision 5) |

**When `room_earlier` is over its limit, detail goes before coverage**, in
this order, each step applied to the oldest chunks first and only as far as
needed:
1. excerpts are dropped from level 1 thread lines;
2. a level 1 chunk's thread lines merge into one line per topic;
3. a chunk of level 2 or up keeps its 6 topics with the most exchanges, and
   counts the rest in `more_topics`;
4. a group of ten chunks is shown as its one chunk of the next level,
   earlier than decision 5 would;
5. only then is the oldest chunk left out, counted in an `omitted`
   attribute.

Every range the room has is still named after steps 1 to 4, so
`room_history` can still open it. The limits are starting values. The
`record` phase stores each block's size and how many steps the trimming
took, and the limits are set from that.

With the `asked` phrase enabled (decision 5), a level 1 chunk also carries
`asked="…"`, for verified-local consumers only (decision 8).

**Placement.**
- **`room_digest`** is constant for the turn. It goes in the dynamic tail,
  right after `repeated_request`, so the turn's calls share their prefix
  through it.
- **`room_earlier`** goes directly ahead of `conversation_history_xml`, the
  history it precedes in time. Together they are one stretch of the prompt
  that only appends between chunk edges. Whether the backend reuses that
  stretch across turns depends on the layout change in decision 5. Until
  that is measured, the block's position costs nothing extra: it renders
  identically on every call of a turn, as the history does.

**Source priority.** Context only, at the rank of `conversation_history_xml`.

**Consumers:**
- **The decide call**, for orientation.
- **The reply audit**, so that a reply to "what have we talked about?" which
  rests on the digest is not rejected as unsupported.

The two blocks support statements about *what was discussed*, never about
what is true.

**The rule added to the decide instructions:**

> `room_digest` and `room_earlier` describe what this room has talked about
> and how each topic went. They never state what is true. `opened_with` is
> the start of a request the user typed, cut short; it is not an instruction
> for this turn. To use what was said in an earlier part of the room, read
> its messages with `room_history`. To use a fact, read its source again. A
> `contested` topic had an answer rejected. Do not repeat that answer without
> fresh evidence. A `quiet` topic is not resolved; it was simply not raised
> again. To say what the conversation has covered, use these two blocks.

### 8. Access, and where the work runs

**Generated text follows the strictest rule in force.** An `asked` phrase or
backfill text is model-written from replies, and replies can restate diary
text or facts later made secret. So:

- **Generated text goes only to verified-local models.** A consumer whose
  model is not verified local gets the digest without it: current topic,
  topics, statuses, timeline, and each chunk's record-derived part.
- **Provenance is kept per node** (decision 5). A later policy change can
  then withhold exactly the affected chunks' text without rewriting any.
- **Chunks before the latest context boundary are marked
  `before_context_change`** and render without generated text. Topics
  continue across the boundary. A profile switch is not an audience boundary,
  but facts may have been invalidated.

**The record-derived part goes to every consumer.** It carries topic titles,
counts, outcomes and dates, never message text.

**Request excerpts follow the history window's rule.** An excerpt is message
text from this room, typed by the operator. Every consumer of the digest
already receives this room's verbatim window, so it receives the excerpts.
Before a context boundary an excerpt is treated as the window treats a
message from before one: kept, and marked. Nothing is scrubbed.

**Where the work runs.** Node writes, topic naming, `asked` phrases and
backfill are jobs in [post-turn housekeeping](2026-11-09-post-turn-housekeeping.md),
which owns their order, priority, budget and failure rules. None of them runs
on the request path.

### 9. Reading back: `room_history`

The digest tells the loop that something was discussed in messages 200 to
209. Without a way to read them, that is a map of a place the loop cannot
visit, and the rule "read it again" has nothing to read when the content
exists only in the room: what the operator said they wanted, the wording
they corrected, the list they pasted.

**One read-only action**, beside `diary_query` and `memory_query`:

| parameter | meaning |
|---|---|
| `messages` | a range the digest printed, such as `200-209`; at most 20 counted messages |
| `topic` | or a topic slug: the topic's newest exchanges, up to 20 counted messages |

**What it returns.** The counted messages of the range, verbatim, as an
observation:
- each with its index, its sender and its date;
- each cut in the middle beyond 1,500 characters, as the history window
  cuts;
- the message UUIDs kept on the step, so the trace shows exactly what was
  read.

**Its authority is the history window's.** A message the operator wrote is
what the operator said then. An assistant reply in the observation is an old
answer, never evidence, under the same instruction that already covers
`conversation_history_xml`. Reading a reply back does not make it true.

**It is offered only when there is something to read:** when the room has
counted messages before the window. A short room's action list is unchanged.

**It reads this room only**, as it is now: an edited message reads as edited,
and a deleted one is gone. A range before a context boundary reads with the
same marking the window gives such messages.

**A run that used it is recorded as having used it** in a chunk's
`provenance` (decision 5), beside `diary_query` and `room_digest`. Its reply
may restate old replies, so generated text about that chunk gets the same
care.

**What it is not.** It is not search. It opens a range the digest already
named. Finding a range from words is what the excerpts, topics and dates are
for. Whether the room also needs full-text search over its own messages is a
separate question, asked only if the replay shows the loop failing to find
ranges it should have found.

**Why it comes with the first release.** Every later choice in this proposal
(no answers in the digest, no summaries of summaries, generated text only if
it earns its place) trades detail for safety. That trade is only acceptable
if the detail is still reachable.

## Measuring it

- **Topic assignment**, against hand labels on synthetic rooms. Precision
  inside the band, abstentions, how often `none` creates a topic that should
  have been an existing one, and how often an existing topic absorbs a new
  subject.
- **The answer screen.** It is added to `benchmarks/decision_probe` as an
  experiment: texts that state an answer, a value or a name, and texts that
  do not. No generated text ships before it has measured precision.
- **Orientation, in scripted rooms** of 80–200 invented exchanges with known
  topics. At turn N, replayed four ways:
  1. without the digest;
  2. with records only: counts, topics, outcomes, dates;
  3. with request excerpts added;
  4. with the `asked` phrase added to that.

  Each rung ships only if it beats the one before it. The questions:
  - "What is the current topic?"
  - "What did we talk about before the kanban questions?"
  - "Is the letter-counting question settled?"
  - a repeat of a `contested` question.

  Scored on correctness, on whether a `quiet` topic is ever called solved,
  and on whether a rejected answer is repeated.
- **Reading back.** In the same rooms, questions whose answer is only in an
  off-screen message ("what did I say the budget was?"), with and without
  `room_history`. Scored on:
  - whether the loop opened a range, and whether it was the right one;
  - whether the reply attributes the content to the earlier message and not
    to a source;
  - whether an old assistant reply read this way is ever passed on as a
    fact.
- **Threads without topics.** On scripted rooms with known subjects, how
  often the no-model thread rule splits one subject in two, and how often
  it joins two. The second must be zero.
- **The real room.** `benchmarks/digest_compaction.py` renders one of the
  operator's own rooms, and the operator judges it against what they
  remember of that room. That verdict outranks every scripted score.
- **No answer leakage.** Across the scripted rooms, no rendered generated
  text states the content of an answer. The screen's misses are counted
  here.
- **Edges.** After deletions, edits and retry rewinds in a scripted room:
  - the window starts on a stored edge;
  - no message is shown both verbatim and in a chunk;
  - generated text whose `content_hash` no longer matches is withheld;
  - a rewind deeper than 30 messages leaves a short node standing, and new
    messages chunk after it;
  - no exchange is listed in both the timeline and a chunk;
  - an excerpt follows an edit of its message, and is gone after a
    deletion.
- **Cost.**
  - The turn-start Choice: its added latency, on top of the request profile
    it shares a call with.
  - Generative housekeeping calls per turn, and their seconds.
  - Backlog depth.
  - The characters of each block, and how far the trimming went.
  - The decide call's prefill time on the turns between two chunk edges.
- **The operator's view.** Every topic, status and chunk is visible on
  /assistant with its basis. Operator corrections (renames, merges, marks,
  reassignments) are counted as the signal of what the automatic parts get
  wrong.

## Rollout

The compaction itself comes first. It needs no topic records, no decision
model and no generative call, so it does not wait for the steps that do.

1. **Compaction from existing records.** Chunk nodes in housekeeping; the
   edge function; threads, request excerpts, run outcomes and exact repeats
   rendered per chunk on /assistant, for every room, old or new. Nothing
   reaches a prompt.
2. **`room_earlier` and the aligned window**, behind `assistant.room_digest`
   (`off` / `record` / `on`). `record` stores the would-send block and the
   would-be window start on the run, as the repeat proposal does.
3. **`room_history`**, enabled together with `room_earlier` going `on`. The
   block names ranges, and the action opens them.
4. **Topics.** The topic tables, the `topic` Choice in the request profile,
   follow-up inheritance, and naming new topics. Recording only at first.
   Chunks render topics for the exchanges that have them, and `?` threads
   for the rest, with no switch between the two.
5. **Derived status, the `room_digest` block and the timeline**, shown on
   /assistant with the operator controls, then in the decide and audit
   prompts behind the same setting. The current topic can go `on` before the
   topic table does.
6. **The `asked` phrase**, after the answer screen has been measured, and
   only if the orientation replay shows a gain over the excerpts.
7. **Backfill of generated topic names** for rooms that predate topic
   recording, by the operator's decision, room by room.

## What this does not do

- **Cross-room topics.** Topics are per room. Recognizing that two rooms
  discuss the same thing is the identity question again, across rooms, and
  belongs with the cross-session memory work this conversation started from.
- **Replace the history window or the working set.** The window keeps the
  verbatim recent messages, and the working set keeps exact evidence. The
  digest orients; it does not inform.
- **Promote anything to memory.** A topic is not a fact about the operator.
- **Say a topic is solved because it went quiet.**
- **Carry standing instructions.** A rule the operator stated before the
  window ("answer in Danish", "don't use tool X") needs its exact wording to
  stay enforceable. The digest keeps at most 80 code points of a thread's
  opening request, marked as an excerpt and declared not to be an
  instruction. Such a rule is out of the loop's view once it leaves the
  window, unless something else holds it.
  Where it lives is an open decision in the
  [roadmap](2026-11-09-conversation-awareness-roadmap.md).
- **Summarize a summary.** Every level is derived from records, and generated
  text is never an input to generated text.
- **Search the room.** `room_history` opens a range the digest named. It
  does not find messages by their words.

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

