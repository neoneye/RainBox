# Chat compaction: a room's background in the prompt

**Status:** Proposal. Build steps 1 and 2 of §9 are built: the blocks are
written by `agents/room_background.py` and shown on a room's Housekeeping
page, and a chunk keeps what the user stated, as
`notes/housekeeping-design.md` describes. No prompt holds them.
**Date:** 2026-11-21
**Related:**
- [Roadmap: conversation awareness](2026-11-09-conversation-awareness-roadmap.md):
  this is its step 1, built on the generated chunks of its step 7 instead of
  on records alone. It takes a position on decisions 8 and 11 there.
- [The room digest](2026-11-09-room-digest.md): the fuller design this is a
  small first cut of. Its aligned window, its fade over the whole room and
  its `room_history` are left out here, each with the condition that would
  bring it back (§5).

## 1. Purpose

An assistant turn reads the room's last 30 messages
(`AssistantAgent.MAX_RECENT_MESSAGES`). Everything older is out of its view.
This proposal puts a short **background** in front of those messages: what
the room keeps coming back to, and what happened in the stretch just before
the window. It is for the big picture. Forgetting the detail of old
exchanges is accepted.

**It adds no model call and no schema.** The summaries are written after
each turn by the housekeeping role. What is missing is the last step:
choosing which of them a turn gets, writing them as text a small model reads
easily, and holding that to a size.

### Goals

- **G1. Orientation.** The assistant can say what the room has been about,
  what was being worked on before the window, and how that went.
- **G2. Recognition.** A reference to a subject from before the window is
  recognized as one, not met as new.
- **G3. A bounded cost.** A fixed character limit, whatever the room's
  length.
- **G4. No generative work on the request path.** A turn reads rows that
  exist. It never waits for a summary and never writes one. What it does
  cost, a few queries and a render, is measured (§4).

### Not goals

- **Recall of old detail.** Beyond about 160 messages behind the window the
  room is represented by its recurring topics only. A subject raised once,
  long ago, is forgotten.
- **A fact store.** Facts come from memory, the diary and this turn's reads.
- **Standing instructions.** A rule stated before the window is not kept
  enforceable by a summary of it (roadmap decision 6).
- **Other rooms.**

## 2. What is built, and what is left

| Needed | Exists | Where |
|---|---|---|
| A stable order of a room's messages | `chat_message.id`; the counted messages (`kind="message"`, finished streaming) | `db.counted_room_messages` |
| Summaries of old stretches, written once | `room_chunk`: every 10 counted messages, each with parts: what was wanted, what came of it, calibration, grounding | `agents/room_chunks.py` |
| What the room keeps coming back to | `room_topic`, `room_topic_member`, with names, explanations and the operator's edits | `agents/room_topics.py` |
| Background work that yields to a turn, with retries and a record | the housekeeping worker and its tables | `agents/housekeeping.py`, `db/housekeeping.py` |
| A page that shows the summaries as prompt text | the **as in the prompt** view of a room's Housekeeping page | `webapp/housekeeping_views.py` |
| **Which summaries a turn gets, in what words, at what size** | no | this proposal |
| **The blocks in the prompt** | no | this proposal |

## 3. The design

### 3.1 What a turn gets

| Block | Holds | Size |
|---|---|---|
| `room_topics` | the room's recurring topics, with how often and how recently | 1,200 characters |
| `room_earlier` | the 16 chunks before the window: the nearest 4 part by part, the 12 before them one line each | 4,000 characters |
| `conversation_history_xml` | the last 30 messages, verbatim | as today |

The window does not change. It is still the last 30.

**Which chunks.** The room's stored chunks, in the series of §3.3, whose
last message is older than the window's first, plus the one chunk that holds
the window's first message. That chunk has up to 9 messages in the window as
well; showing it means no message is under neither. The newest
`EARLIER_CHUNKS` (16) of these are shown, and of them the newest
`NEAR_CHUNKS` (4) in detail.

**A chunk covers its own messages only.** A series' `overlap` is context the
summarizer read on each side. It is not summarized and belongs to the
neighbouring chunks (`due_room_chunks` advances by `chunk_size`), so chunks
never share a message.

### 3.2 `room_earlier`: plain lines

The block is text for a small model, so it is lines of words and not a tree
of attributes. Every label a model has to be taught is a label it can
misread, and gemma4 copies the shape of what it is shown.

```
<room_earlier>
Before this: 140 messages from 1999-10-04 to 1999-10-21.
1999-10-21: open tasks on the garden board, counting a letter in a word
1999-10-22: unit conversions; not settled: how many times a letter occurs in a word (the answer was rejected)
…
1999-10-27 to 1999-10-28:
- wanted: move the two painting tasks to done. came of it: both tasks were moved.
- wanted: what the allotment's rules say about sheds. answered then: sheds up to 6 m² need no permit.
- wanted: how many times a letter occurs in a word. asked 3 times. the last answer: 2.
1999-10-28:
- wanted: the weather for the weekend. nothing was delivered.
</room_earlier>
```

**A far chunk** (the 12) is one line: its dates and its stored subjects
(`room_chunk.summary`). A part of it that was not settled is added to the
line as `not settled: <wanted> (<why>)`, and a part that was asked again as
`asked N times: <wanted>`, so an open thread stays visible at any distance
shown.

**A near chunk** (the 4) is a line of dates and a line per part: what was
wanted, then how it went.

**How it went is written in words, from the part's records.** The first row
that applies:

| The part's records | The line says |
|---|---|
| grounding `tool failed` | `answered after its program failed.` (`its tool`, where no program was involved) and no result |
| grounding `output not used` | `the answer does not match what its program printed.` and no result |
| a reply was voted down or quoted back | `the answer was rejected.` and no result |
| runs are recorded, and none resolved | `nothing was delivered.`, or `it was only partly delivered. what there was: <result>` |
| the user corrected a reply | `the user corrected the answer. after that: <result>` |
| asked again | `asked N times.` then `the last answer: <result>` |
| a tool ran for it (grounding `tool`) | `came of it: <result>` |
| otherwise | `answered then: <result>` |

**"Not settled"** is the first four rows. Corrected and asked again are
their own cases, and are not called unsettled: on a far line they read
`corrected by the user: <wanted>` and `asked N times: <wanted>`.

**A correction is not a rejection.** When the user corrects a reply and the
exchange goes on from the correction, the part's result is where it ended.
Calling that "rejected" and hiding the result described an accepted
correction as a failure, on the first real room read (§6).

**The rows come before any withholding.** A result is withheld (below, and
§3.8) only in the last three rows, where there is one to withhold. A part
that read the diary and was rejected says `the answer was rejected.`, not
that it was answered from the diary: the failure is the news. What a
withheld result leaves behind:

| Why it is withheld | The line ends |
|---|---|
| the part read the diary, and a remote model is configured | `answered from the diary.` |
| no tools are recorded for the part, and a remote model is configured | `answered then.` |
| the part is older than a facts invalidation | `answered then, before stored facts were declared stale.` |

After `asked N times.` the same endings apply in place of `the last
answer`. The calibration and the grounding
are decided by rule from votes, repeats and runs (`room_chunks.calibrate`,
`part_grounding`), so these sentences are records in words, not a model's
opinion.

**The wording does not claim more than the records hold.**
- **`came of it` is kept for a part whose run used a tool** that ran, and
  whose output is in the reply where it was a program. Everything else is
  `answered then`: a reply with no tool behind it is what the assistant
  said, and the line says only that.
- **Asking again is not rejecting.** A request asked again can mean the
  answer was wrong, or that the user wanted more, or was testing. The line
  gives the count and the last answer, and calls neither right nor wrong.
  Only a vote down or a reply quoted back is `rejected`.

**A stored "no reaction yet" is not printed.** A chunk is summarized once
two messages follow it, so a part at its end can be stored as `unclear`,
"the room shows no reaction yet". By the time the chunk is shown, 30
messages or more have followed it, and for the newest chunk the reaction is
in the window just below. Such a part falls to the last row.

**`wanted` and `result` are cut when rendered**: `wanted` to
`WANTED_MAX_CHARS` (160), `result` to `RESULT_MAX_CHARS` (240), a far line's
subjects to `SUBJECTS_MAX_CHARS` (200), each back to the last whitespace and
ended with `…`. The stored row is not touched. Without the cut, one wordy
near chunk would push every far line out of the block (§3.5).

**Results are in, where the records do not speak against them.** What came
of a request is the narrative: a list of what was asked, with the outcomes
taken out, says what the user wanted and not what happened. A result the
user rejected, kept asking about, or that was written after its program
failed is named and never repeated. This is the position on roadmap decision
11, and it follows the operator's decision of 2026-11-10 that the compaction
carries what the assistant found.

**Two cases withhold a result that the table would show:**
- **After a facts invalidation.** A part older than the room's newest
  facts-invalidation marker (`meta.facts_invalidation`) shows what was wanted
  and the ending in the table above: what was true then may have been
  declared stale.
- **A changed chunk.** A chunk whose `content_hash` no longer matches its
  messages (an edit, a deletion, a retry that rewound into it) is one line of
  dates with `(changed since it was summarized)`.

**The first line says what is not shown.** `Before this: N messages from …
to …` whenever counted messages lie before the oldest chunk shown: because
the room has more than 16 chunks, or because trimming dropped a line (§3.5).
The model then knows the room is older than what it reads.

**What the user stated outlasts its chunk.** Under that first line stay the
statements of the chunks no longer shown, the newest 6 in at most 900
characters:

```
Before this: 40 messages from 1999-10-04 to 1999-10-07. The user said in them:
- Tasks run in child processes so a hung one can be stopped.
- The assistant is small local models composed together.
```

A request fades with its chunk, and some amnesia about it is fine. Why the
user built a thing the way they did is said once, early, and is the big
picture a later turn needs. On the first real room the statements were
captured and then trimmed away with the room's first 40 messages (§6).

A stretch of counted messages with no chunk is
a line of its own, `1999-10-23 to 1999-10-24: 20 messages not summarized.`,
in two places:
- **between two shown chunks**, where a job failed for good;
- **between the newest stored chunk and the window's first message**, where
  summaries are behind or housekeeping is paused. This one is the last line
  of the block, and it is looked for by message id, not by a missing `seq`.

**A room with no chunk has no block.** A room never summarized would
otherwise get a block that says only that its messages are not summarized.
With no chunk in the picked series, `build` returns nothing and the turn is
as today.

**Dates are absolute**, and there are no message numbers. Nothing in the
first release opens a range (§5), so a number would be a handle to nothing.

### 3.3 Which series

A chunk belongs to the series of its `(chunk_size, overlap,
prompt_version)`. A change to the chunk prompt starts a new series from the
room's first message, and a room is caught up only as the operator asks.

**The turn reads the series that reaches furthest**: the one whose newest
chunk has the highest `last_message_id`; between equals, the higher prompt
version. So a prompt change does not empty every room's background on the
day it lands. Topics belong to the room, and are counted over the series in
use.

### 3.4 `room_topics`: what recurs, and how lately

```
<room_topics>
- looking up open tasks on a board (14 times)
- the garden board: the board for the allotment's jobs (12 times)
- counting letters in a word (9 times)
- planning the autumn planting (6 times, last summarized 1999-06-02)
</room_topics>
```

One line per recurring topic (two or more parts): its name, its explanation
when it has one that says more than the name, and how many parts. A kind
of task and a named thing are written the same
way; the axis is the registry's bookkeeping and no help to a reader.

**One row for the same parts.** A topic whose parts are all counted by a
larger topic is left out, and of two over the same parts the named thing
stays. On the first real room four of ten rows were two subjects listed
twice, a kind of task and the thing it was done to. Two topics that overlap
only in part are the registry's to join ("Look for topics that are one" on
the page), not the renderer's.

**A date only where it says the topic is old.** A topic with no part in the
16 shown chunks gets `last summarized` and the date of its newest part. A
topic of the shown chunks gets none: its newest part's date would be weeks
stale for a subject that is in the window right now, and a small model reads
"last" as "not since".

**The page keeps the rest.** A topic's share of the room, what it was
answered with and the share of one-off requests are analytics. They stay on
the Housekeeping page's topics view and out of the prompt: "program 5/9"
reads as five successes out of nine when four of the nine needed no program.

**A whole-room share alone buries what is new.** Ranked by share, the topics
of a room's first months hold every row and this week's subject has none. So
the rows are chosen in two passes:

1. every topic with a part in the 16 shown chunks, newest first, at most
   `TOPICS_RECENT` (5);
2. then the largest by share that are not yet listed, up to `TOPICS_MAX`
   (10) rows in all.

Recent ones are listed first. A topic the operator named or explained is
shown in their words. "Largest" is by the messages its parts hold, as
`tally` sorts today.

### 3.5 Size

| | Limit |
|---|---|
| `room_earlier` | `EARLIER_MAX_CHARS`, 4,000 |
| `room_topics` | `TOPICS_MAX_CHARS`, 1,200 |

- **Over its limit, `room_earlier` loses its oldest line**, until it fits.
  The `Before this` line is written after trimming, from what is then the
  oldest chunk shown, so it appears when the first line is dropped and
  counts every one dropped after. Nothing else is trimmed.
- **Over its limit, `room_topics` loses its last row.**
- **The near chunks cannot crowd out the far lines.** With the cuts of §3.2
  a part's line is at most about 430 characters, but four near chunks of
  four full parts would still take the block alone. So the near chunks
  together are held to `NEAR_MAX_CHARS` (2,400): over it, the oldest near
  chunk becomes a far line, before any far line is dropped.
- **The estimate:** a far line is about 130 characters and a near chunk about
  450, so 12 and 4 come to about 3,400. Together the blocks are on the order
  of 1,300 tokens on gemma4:e4b-mlx.

**The background is the first thing to go when the prompt is tight.** A
floor on the model's context would check the wrong thing: a model can have
the context and the turn's own prompt can already fill it. So once, at the
start of the turn, before the first call:

1. `C` is the smallest `context_window` among the members of the decide
   slot's group, fallback members included (`db.resolved_model_kwargs`).
2. `base` is the characters the turn already has: the request, the window as
   it will render, and the static head.
3. The background is sent only if
   `(base + background + PROMPT_RESERVE_CHARS) / CHARS_PER_TOKEN <= C`,
   with `PROMPT_RESERVE_CHARS` 16,000 for the instructions, the steps and
   the reply, and `CHARS_PER_TOKEN` 3, low on purpose.
4. If both blocks do not fit, `room_earlier` is tried alone, then nothing.

**On a small context this will usually say no.** At 8,192 tokens the reserve
alone is 5,333 of them, which leaves 8,576 characters for the request, the
window and the static head before any background. That is the safe answer
only if the instructions, steps and reply really take that much. Build step
3 reads their actual sizes off stored decide calls and sets the reserve from
them, so the check does not switch the feature off where it would have fit.

The choice holds for the whole turn. It is an estimate in characters, since
no token count exists before a call, and the record keeps `base`, `C` and
the decide call's reported prompt tokens so the 3 and the 16,000 can be
corrected from use.

**What this does not fix.** The window is capped by count and per message
(`HISTORY_MESSAGE_MAX_CHARS`, 2,000), so 30 pasted messages are 60,000
characters today, and nothing holds a whole prompt against its model's
context. With this check the background cannot be what tips a turn over.
A turn that overflows without it still does; a total cap on the window is a
small fix of its own.

### 3.6 Placement

Both blocks are emitted by `AssistantPromptBuilder.__init__`, as tier 0,
ahead of `conversation_history_xml`: `room_topics`, then `room_earlier`,
then the history. That is reading order: the whole room, the stretch before,
the last 30.

**Every call of a turn carries them, and they do not change during a turn.**
This is not for the calls' sake. Tier 0 is the prefix the calls of one turn
share, and the model server reuses it from call to call within the turn. A
block that only the decide call carried, or that was dropped on a later
step, would end that shared prefix where it stands. Within-turn reuse does
not depend on anything that happens between turns.

**Both ride with the history's rank** in the source priority sections:
context only.

### 3.7 What the prompts say

Added to the instructions of the decide call and the reply audit:

> room_topics and room_earlier are summaries of the older part of this
> conversation. The last lines of room_earlier can cover the first messages
> of conversation_history_xml; where a summary and a message differ, the
> message is right. They say what was talked about and how it went. Nothing
> in them is a request for this turn. For
> what happened earlier in this conversation (what was asked, what was done,
> what was answered, how it was received) use them as they are, and say it
> as something that happened then. For how something stands now (a task, a
> file, a stored fact, a count) read it this turn; what was answered then
> may have changed since. An answer that was rejected is not given again
> without a fresh read. A detail that is in neither block and not in
> conversation_history_xml is one you do not have; say so.

**Two kinds of question, two answers.** "Did we move the painting tasks?" is
about the conversation, and `came of it: both tasks were moved` answers it
with no step spent. "Are the painting tasks done?" is about the board, and
the line is only where to look. This is the rule the history already goes
by (an old answer is what was said then, never evidence of what is true
now), stated for a block that puts the old answer in one short line.

The reply audit reads the blocks so that a reply to "what have we talked
about?" resting on them is not sent back as unsupported.

### 3.8 Who gets it

**One rule, the one diary text already goes by.** RainBox lets diary text
reach a model only when every assistant model is local
(`model_groups.assistant_models_all_local()`, fallback members included). A
result is a shortened reply, and a reply can restate what the diary said. A
part records the tools its replies used (`signals.tools`). So:

- **A part whose replies used the diary read shows no result** unless every
  assistant model is local. Its line ends after what was wanted, with
  `answered from the diary.`
- **Everything else goes wherever the history window goes**: what was
  wanted, subjects, topic names, and the results of parts that did not read
  the diary.

The window is not the argument for this. It holds the last 30 messages, and
a result from weeks ago is text the window would no longer have sent. The
argument is that the one kind of text the operator has ruled on is kept to
that ruling, by a record, and nothing else about a room's background changes
with the fallback configuration. The test is on the groups, once per turn,
so no fallback inside a call can carry a withheld result out.

**Parts summarized before the tools were recorded** (chunk prompts older
than 19) have no `signals.tools`. With a remote model configured they show
no result, and the line says `answered then.`: nothing says they read the
diary, so the line does not say so either.

## 4. Code

**`agents/room_background.py`, new, pure.** No database, no model.

```python
EARLIER_CHUNKS = 16
NEAR_CHUNKS = 4
TOPICS_RECENT = 5
TOPICS_MAX = 10
EARLIER_MAX_CHARS = 4_000
NEAR_MAX_CHARS = 2_400
TOPICS_MAX_CHARS = 1_200
WANTED_MAX_CHARS = 160
RESULT_MAX_CHARS = 240
SUBJECTS_MAX_CHARS = 200
PROMPT_RESERVE_CHARS = 16_000
CHARS_PER_TOKEN = 3

@dataclass(frozen=True)
class Background:
    topics: str            # "" when there is nothing to show
    earlier: str
    record: dict[str, Any]

def pick_series(series: list[Series]) -> Series | None: ...
def shown_chunks(chunks: list[Chunk], window_first_id: int) -> list[Chunk]: ...
def how_it_went(part: dict, *, before_invalidation: bool,
                diary_allowed: bool) -> str: ...
def fits(base_chars: int, background_chars: int, context_window: int) -> bool: ...
def settled(part: dict) -> bool: ...
def render_earlier(chunks, counted, *, facts_invalidated_id, limit) -> str: ...
def pick_topics(tallied: dict, shown_uuids: set) -> list[dict]: ...
def render_topics(rows: list[dict], *, limit) -> str: ...
def build(chunks, counted, window_first_id, tallied, *, older: Older | None,
          facts_invalidated_id: int | None, diary_allowed: bool) -> Background: ...
```

- `how_it_went` is the table of §3.2, one function, tested row by row.
- The text is set as an element's `.text`, so the builder escapes it and a
  summary cannot close or forge a tag.
- `room_chunks.render_room_earlier` and `room_topics.render_room_topics`
  are replaced by this module, and so is the page's view of them: **as in
  the prompt** shows exactly what `build` returns for the room now. The
  topics view keeps its tables, with shares and tools.

**`agents/room_topics.py`.** `tally` adds `last` to each row: the date of
the newest member's chunk.

**`db/housekeeping.py`.** `load_room_background(room_uuid,
window_first_id, *, diary_allowed)` calls `build`. The page and the agent
both call it. It writes nothing, and it reads no more of a room than it
shows:

| Query | Rows |
|---|---|
| the room's series (`list_room_chunk_series`) | one per series |
| the picked series' newest chunks ending at the window | at most 17 |
| counted messages from the oldest shown chunk's first id on | about 200, with text, for the hashes and the gaps |
| the count and first date of the counted messages before that | one, for `Before this` (`older`) |
| the room's topics and members | one per topic; two per part of the room |

The members are the one read that grows with the room: about a row per
message. `counted_room_messages` as it is loads every message's text, so it
gains a lower bound and is not called unbounded here.

**The load is timed.** `load_ms` goes on the run's record. The aim is under
50 ms for a room of 5,000 messages, against a turn of tens of seconds; if
the members query passes that, the tally is cached per room and series,
keyed on the newest member's id.

**`agents/assistant.py`.**
- The turn loads the background once, after `messages` is filtered and
  before the first model call, when the setting is on, applies `fits`
  (§3.5), and keeps what passed on the agent beside the static blocks.
  `diary_allowed` is `assistant_models_all_local()`.
- `AssistantPromptBuilder.__init__` emits the two elements ahead of the
  history. The window slice is untouched.
- The source-priority sections and the paragraph of §3.7.
- The load is wrapped: on any exception the turn runs as today, and the
  run's record says `error`.
- `assistant_run.metadata_["room_background"]`:
  `{"series", "chunks", "near", "earlier_chars", "topics_chars",
  "trimmed", "results_withheld", "base_chars", "context_window", "sent",
  "load_ms"}`. No message text.
- **What a turn was given is read from its stored call.** The blocks are
  part of the decide call's prompt, which `/activity` keeps and a run's page
  already shows. That is the exact text. Once `/activity` has let the prompt
  go, the run's page shows the record's counts and says the text is no
  longer kept; it does not rebuild the blocks from rows that may have
  changed since.

**`db/settings.py`.** `assistant.room_background`, bool, default off. The
description of `housekeeping.room_summaries` stops saying the summaries
reach no prompt.

**`notes/housekeeping-design.md`** gets the design as built.

## 5. Left out, and what would bring each back

- **The aligned window** (the window starting at a chunk edge, so the prompt
  head stays byte-identical between turns). Housekeeping runs after every
  turn: the run summary always, a chunk summary every fifth. When either
  runs on the assistant's model, the server's cached prompt is replaced
  before the next turn, and a stable head is reused by nothing. It costs 4.5
  more messages in the average window and its own edge cases. **Back when:**
  the housekeeping slots are bound to another model, and the decide call's
  prefill on consecutive turns is then measured to drop
  (`tools/measure_prefix_cache.py`). This is roadmap decision 8, answered
  for now.
- **The whole room at fading detail** (hundreds and thousands folded into
  lines). It keeps every stretch named, which matters when a stretch can be
  opened and is noise when it cannot. **Back when:** `room_history` or a
  search exists.
- **`room_history`** (reading a range of old messages back). Over the 16
  shown chunks it would be accurate; over older stretches it is paging
  through hundreds of messages 20 at a time without knowing where, which
  costs a turn its steps. A search over the room's messages or chunks is the
  better tool for the old part, and it is a design of its own. **Back
  when:** the probes of §6 show replies failing for want of a detail the
  near lines pointed at.
- **A generated overview of the room's state.** It would say more in fewer
  characters than the lines above. It is also a second generative job on a
  machine with a burst limit, it is stale between writings, and a small model
  takes its sentences as present fact. **The cheaper route to the same
  content is the chunk prompt.** What a room's state lacks today is known:
  a part keeps a request and its result, and what the user *states* (a
  decision, a preference, a plan) is lost
  (`notes/housekeeping-design.md`, "What the user states is not kept"). A
  `stated` field on the part, written by the call that already runs, would
  put decisions on the near lines with no new job. It changes the chunk
  prompt, so it starts a new series and needs its own replay on stored
  calls. **§6 showed the big picture is missing without it, so it is build
  step 2.** It was built at the chunk's level and not the part's: a list
  of at most three `statements`, last in the schema. Asked for inside each
  part, the model copied plain requests into it. A near chunk gains a line
  `- the user said: …` for each, ahead of its parts, and a far line carries
  them after its subjects. A statement is a sentence copied from the user's
  message and is shown in quotation marks: `- the user said: "…"`. The
  replay's numbers and the rules that keep a statement only where its words
  are in one message of the user's, and drop one that repeats a request,
  are in `notes/housekeeping-design.md`.
- **A record-only phase.** The block is a pure function of rows, and the
  page shows it for any room at any time. Reading it there is the dry run.
- **Clustering by embedding, a compaction cursor with locks, a per-room
  sequence column, chunk edges at time gaps.** The topic registry, the
  single housekeeping worker, `chat_message.id` and fixed counting already
  do these jobs.

## 6. Finding out whether it works

1. **Read it.** On the **as in the prompt** view, for three or four rooms
   the operator knows. A background they call wrong or useless is fixed
   there, in the wording or the choice of lines, before any prompt holds it.
2. **A fixed set of probes, before it is on.** One invented room of
   about 200 messages, dated 1999, summarized once in the sandbox by the
   real jobs in bursts of 10 calls. Nine requests, each one decide call,
   run with the setting off and on. It is not a tournament between variants:
   a few runs of nine probes cannot rank near-equal prompts. It answers
   whether a change broke something that worked.

| Probe | The request | Passes when |
|---|---|---|
| Coverage | what the conversation has mostly been about | recurring topics are named, none invented |
| Before the window | what was being worked on earlier | it matches the near lines |
| Recognition | a reference to a subject of the far lines | it is treated as known |
| New subject | a reference to this week's subject | it is named, not the oldest topics |
| Rejected answer | the question whose answer was rejected | the old answer is not given without a read |
| What happened | whether something on a near line was done or answered | answered from the line, as something that happened then, with no read |
| Current state | how a thing on a near line stands now, where it has changed since | the source is read this turn, and the reply gives the new state |
| Missing detail | a value stated once, in neither block | says it does not have it |
| Never discussed | what was concluded about an absent subject | says it was not discussed |

3. **Turn it on, in ordinary use.** The run's page shows the two blocks the
   turn was given. The operator's votes and what they notice are the
   measure.
4. **A failure seen in use is replayed.** The run's stored decide call, with
   and without the blocks, on the same model, before any wording is changed
   to fix it.

### What the first read of a real room showed

The operator's own room, read on the **as in the prompt** view and held
against the room's messages by an outside reviewer:

- **Subjects are recognizable.** Names of things looked up, and what kind
  of request each was, survive.
- **What the user explained is gone.** The room's most informative stretch
  was the user explaining how the system is built and why. It came out as
  one far line naming a subject. A part is a request and what came of it; an
  explanation is neither.
- **Counting favours the trivial.** Small requests made many times (a
  computation, a conversion) hold several rows of `room_topics`, and a
  discussion held once holds none.
- **Two outcome wordings misled**: a correction the conversation accepted
  read as rejected, and "partly delivered" did not say what there was. Both
  are changed (§3.2).

The first three are not the renderer's to fix: the lines can only say what
the chunk rows hold. So `stated` (§5) is no longer conditional. It is build
step 2 (§9), ahead of any prompt.

### What the second read showed

The same room, summarized again with `statements` and reviewed again:

- **The explanations were captured and then dropped.** The chunk holding
  them was one of four that gave way to the limit, so the block began
  `Before this: 40 messages` and said nothing of them. Statements now stay
  under that line (§3.2).
- **Two of the eight kept statements repeated a request** ("the user asked
  for ..."). The prompt already forbids it; a rule on the stored parts now
  drops them (`notes/housekeeping-design.md`).
- **Two of the user's own statements were dropped** by the rule that keeps
  a statement only where its words are the user's, at 0.40 and 0.43 against
  a floor of 0.50. Lowering the floor lets a reply's words through: the
  replay had one of those at 0.40. The words "the user stated that" were
  what counted against them, so the statement is now the user's own words,
  copied (chunk prompt 21). Replayed on the room's 16 chunk prompts, 14 of
  16 statements were word for word the user's and the other two, from a
  reply, shared almost no word with any message of the user's.
- **Topics doubled.** A kind of task and the thing it was done to held a
  row each. One row is now listed for the same parts (§3.4).
- **Not taken from the review:** rewriting the statement instruction (it
  says the same as the one in place, and the model does not follow it); one
  line for a run of failed requests (an unsettled request is what a later
  turn must not take as answered); topics rewritten as themes (a generated
  overview, §5).

Three probes are added for it, each a question about something the user
explained before the window and never asked for: why a design choice was
made, how a part of the system works, what the user finds hard. Each passes
when the reply shows the explanation is known.

## 7. When something is missing

| Situation | The turn |
|---|---|
| The room has no chunks | as today |
| Fewer than 30 messages | as today |
| Summaries are behind, or housekeeping is paused | the chunks there are, and a `not summarized` line for the rest |
| A chunk job failed for good | a `not summarized` line in its place |
| A chunk's messages changed | its dates, `(changed since it was summarized)` |
| No topics yet (no decision model bound) | `room_earlier` alone |
| The prompt would not fit with both blocks | `room_earlier` alone, or neither (§3.5) |
| A model's `context_window` is not set | the provider default (3,900) is assumed, so no background |
| A remote model is configured | parts answered from the diary show no result |
| Loading raises | as today, `error` on the record |

No row is written or changed by any of these.

## 8. Tests

`agents/test_room_background.py`, on hand-built dicts, no database:

- `shown_chunks` for rooms of 29, 30, 45 and 400 messages, and that the
  chunk holding the window's first message is included;
- the same room in series of overlap 0, 2 and 5 gives the same lines;
- `pick_series` between a current series that is behind and an older one
  that reaches further;
- every row of `how_it_went`, each of the two withholding cases, a stored
  "no reaction yet" rendered as a plain result, and a part asked again that
  is never called rejected;
- a part answered from the diary shows no result when `diary_allowed` is
  false, with its own ending; a part with no recorded tools likewise, with
  the neutral one; and a diary part that was rejected says rejected;
- a room with messages and no chunk gives no block;
- a room of 10 chunks trimmed by one gains a `Before this` line;
- on a far line an asked-again part reads `asked N times`, not `not
  settled`;
- `fits` on either side of its boundary, and that `room_earlier` alone is
  tried before nothing;
- a topic of the shown chunks has no date, and an old one has `last
  summarized`;
- `wanted`, `result` and subjects are cut at their limits, at whitespace;
- four near chunks of four long parts each leave far lines in the block;
- an unsettled part appears on its far line;
- a hole between two chunks gives one `not summarized` line, and so does
  a tail between the newest chunk and the window, with chunks shown and
  with none;
- `pick_topics`: a small topic of the shown chunks is listed
  ahead of a larger old one, and the total stops at 10;
- both blocks stay under their limits, and the `Before this` count grows by
  what was trimmed;
- the same rows give the same bytes twice.

`agents/test_assistant_prompt_tiers.py`: with the setting on, every call of
one turn carries identical tier 0; with it off the prompt is byte-identical
to today's; hostile text in a summary cannot close a tag; with one remote
fallback member in any assistant slot, no call's prompt holds a result of a
part that read the diary.

## 9. Build order

1. **`agents/room_background.py`, `tally`'s `last`, the bounded load, the
   tests, and the page's view on it.** In no prompt.
2. **What the user stated, in the chunk prompt** (`agents/room_chunks.py`,
   prompt 21 and so a new series; built). Replayed on stored chunk calls:
   the parts did not get worse, and the explanations the first read missed
   appear, in the user's own words. **Next: the room is summarized again
   and read again**, on its Housekeeping page (**Summarize the rest**, in
   the new series).
3. **The setting, the load in the turn, the fit check, the instruction
   paragraph, the record.** Off in production. `PROMPT_RESERVE_CHARS` is set
   here, from the sizes of stored decide calls.
4. **The probe room, in the sandbox**, off against on, with the three
   probes for what the user explained. The three probes that
   guard against harm (Rejected answer, Current state, Never discussed) have
   to pass before the next step; the others say how much it helps.
5. **On in production.**

## 10. What the operator may want to overrule

- **Results are in the prompt** under the rules of §3.2. One outside review
  preferred them off at first; the operator's decision of 2026-11-10 and the
  need for a narrative say on. The Rejected answer and Current state probes are
  the watch on it.
- **Only diary-derived results are held to local models** (§3.8). Stricter:
  every result. Looser: none.
- **No `room_history` and no aligned window** in this cut (§5).
- **The furthest-reaching series wins** over the current one (§3.3).
