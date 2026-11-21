# Chat compaction: a room's background in the prompt

**Status:** Proposal. Nothing here is built. Everything it reads is: the
chunk summaries and the topic registry (`notes/housekeeping-design.md`).
**Date:** 2026-11-21
**Related:**
- [Roadmap: conversation awareness](2026-11-09-conversation-awareness-roadmap.md):
  this is its step 1, built on the generated chunks of its step 7 instead of
  on records alone. It takes a position on decisions 2, 7, 8 and 11 there.
- [The room digest](2026-11-09-room-digest.md): the aligned window, the fade
  and `room_history` come from its decisions 5 and 9.
- [Surfacing topic uncertainty while chatting](2026-11-18-surfacing-topic-uncertainty.md):
  the operator's corrections to topics, which this block then shows.

## 1. Purpose

An assistant turn reads the room's last 30 messages
(`AssistantAgent.MAX_RECENT_MESSAGES`). Everything older is out of its view.
This proposal puts a short **background** of the older part of the room in
front of those messages, so the assistant can say what the room has been
about, recognize "the problem we talked about with X", and open the old
messages when a detail matters.

**It adds no model call.** The summaries are already written after each turn
by the housekeeping role, and a page already renders them as the text a
prompt would hold. What is missing is the last step: choosing which of them a
turn gets, at what detail, inside a budget, and starting the verbatim window
where they end.

### Goals

- **G1. Orientation.** The assistant can answer "what has this room been
  about?" and recognize a reference to a subject from before the window.
- **G2. A bounded cost.** The background has a fixed character budget,
  whatever the room's length.
- **G3. Nothing on the request path.** A turn reads rows that exist. It never
  waits for a summary and never writes one.
- **G4. Old stretches stay represented.** However old the room is, every
  message before the window is under a line that gives its range and dates,
  and a subject the room came back to keeps its row. A subject raised once,
  long ago, may not be named; its stretch can still be opened.
- **G5. A stable prompt head.** Between two chunk edges the bytes ahead of
  the newest message do not change, so the model server's cached prefix
  survives from turn to turn.

### Not goals

- **A fact store.** Facts come from memory, the diary and this turn's reads.
  The background says what was talked about and how it went.
- **Standing instructions.** A rule stated before the window ("answer in
  Danish") is not kept enforceable by a summary of it. Where such rules live
  is roadmap decision 6.
- **Other rooms.** The background is this room's alone.

## 2. What is built, and what is left

| Needed | Exists | Where |
|---|---|---|
| A stable order of a room's messages | `chat_message.id`, autoincrement; the counted messages (`kind="message"`, finished streaming) | `db.counted_room_messages` |
| Summaries of old stretches, written once | `room_chunk`: every 10 counted messages from the room's first, each with parts (what was wanted, what came of it, calibration, grounding) | `agents/room_chunks.py`, `db/housekeeping.py` |
| What the room keeps coming back to | `room_topic` and `room_topic_member`, on two axes, with names, explanations and the operator's edits | `agents/room_topics.py` |
| Background work that yields to a turn | the housekeeping worker: one job at a time, killed by a new message, rests after 10 calls | `agents/housekeeping.py` |
| Crash recovery, retries, a record of every call | `housekeeping_job`, `housekeeping_attempt`, `/activity` | `db/housekeeping.py` |
| The block as text | `render_room_earlier`, `render_room_topics` | shown on a room's Housekeeping page, in no prompt |
| **Which chunks a turn gets, and at what detail** | no | this proposal |
| **A window that starts where the chunks end** | no; the window is the last 30, cut in `AssistantPromptBuilder.__init__` | this proposal |
| **A budget, and what gives way first** | no | this proposal |
| **Reading a range back** | no | this proposal (`room_history`) |
| **A measured answer to "does it help?"** | no | this proposal (§9) |

## 3. The design

### 3.1 Three regions

Counting from the newest message backwards, a turn's view of its room is:

| Region | Holds | From |
|---|---|---|
| The window | the newest 30 to 39 messages, verbatim (the newest 30, with the window setting on `last`) | `conversation_history_xml`, as today |
| `room_earlier` | every message before the window, as chunks at three grains | `room_chunk` rows |
| `room_topics` | what the whole room keeps coming back to | the topic registry |

With the aligned window no message is in both the window and
`room_earlier`.

**A chunk covers its own messages only.** A series' `overlap` is how many
messages on each side the summarizer read as context. They are not
summarized, and they belong to the neighbouring chunks: chunk *n* of a series
of 10 covers counted messages 10*n* to 10*n*+9 whatever the overlap
(`due_room_chunks` advances by `chunk_size`). So chunks never share a
message, and only the covered range sets an edge, a count or a range that
`room_history` opens. The overlap decides one thing: a chunk can be written
once that many messages follow it.

### 3.2 The edge, and the aligned window

The window starts where a stored chunk ends. With `history` the turn's
messages without the request (as `AssistantPromptBuilder` holds them):

1. Take the room's chunks in the series of §3.3, oldest first.
2. **The edge** is the newest chunk with at least `WINDOW_MIN` (30) messages
   of `history` after its `last_message_id`.
3. **The window** is every message of `history` with an id above the edge's
   `last_message_id`. With chunks of 10 that is 30 to 39 messages.
4. **No edge** (a short room, or a room never summarized): the window is the
   last 30, as today, and there is no background.
5. **More than `WINDOW_MAX` (49) messages after the edge**: summaries are
   behind, by more than one chunk. The window is the last 30, and the
   messages between the edge and the window are one `<gap>` line in
   `room_earlier` (§3.4).

Everything is compared by message id, never by position. Ids only grow, and a
chunk's stored edge stays valid when a message is deleted later.

**The window moves in steps.** Today its first message changes every turn.
Aligned, it changes once per chunk, about every fifth turn, and in between a
turn only appends to the end of the history.

**A window can start in the middle of an exchange**, as it can today: a chunk
is 10 messages, not 5 exchanges. The request that reply answers is then in
the nearest chunk's last part.

**The alignment is a setting of its own** (`assistant.room_window`: `last`
or `aligned`, §6), because what it buys is prefill time and what it costs is
4.5 more messages in the average window, and both have to be measured. On
`last` the window is the last 30, as today. The background then ends with
the chunk that holds the message just before the window, shown at the near
grain. Up to 9 of that chunk's messages are also in the window, which is the
one place a message is under both. Nothing is left under neither.

**Chunk sizes above 20 are not aligned.** `housekeeping.room_chunk_size`
accepts up to 100. A window of 30 to 129 messages is not a window, so such a
series falls under rule 5 on most turns: correct, with no stable head.

### 3.3 Which series

A chunk belongs to the series of its `(chunk_size, overlap, prompt_version)`.
A change to the chunk prompt (`room_chunks.PROMPT_VERSION`) starts a new
series from the room's first message, and a room is caught up only as the
operator asks for it.

**The turn reads the series that reaches furthest**: the one whose newest
chunk has the highest `last_message_id`. Between equals, the higher prompt
version wins, then the current settings. So a prompt change does not empty
the background of every room on the day it lands. A room switches to the new
series when the new series has caught up. Topics belong to the room, not to a
series, and are counted over the series in use.

One series per turn. Two series cut on different edges cannot be mixed
without showing a message twice or not at all.

### 3.4 `room_earlier`: three grains

Detail falls off with distance from the window. With `E` the edge chunk's
`seq`:

| Grain | Chunks | Each renders as |
|---|---|---|
| Near | the `NEAR_CHUNKS` (4) newest: `E-3 … E` | the chunk with every part |
| Mid | from `M` to `E-4`, where `M = max(0, ((E-4) // 10 - 1) * 10)`: 11 to 20 chunks once the room has that many (`E` = 24 gives 11, `E` = 23 gives 20) | one line: range, dates, subjects |
| Far | every whole group of ten chunks before `M` | one line per hundred messages: range, dates, its topics with counts |
| Farther | every whole group of ten far lines, by the same rule one level up, and so on | one line per thousand messages, of the same shape |

**A group of ten is folded one group late.** `M` is set so the hundred
messages just behind the near chunks always keep a line per chunk. Folded at
its tenth chunk, a hundred would become one line exactly where the turn needs
it finest.

**The block grows with the logarithm of the room.** Ten hundreds fold into a
thousand one group late as well, so each level shows 11 to 20 lines and a
room of 20,000 messages has four levels. Nothing is left out for age: the
oldest stretch of any room is under some line with its range, its dates and
its largest topics.

**No grain is generated from another.** Near and mid print fields of the
chunk's own row. A far line is counted: the names of the registry topics its
chunks' parts belong to, with how many parts each, the largest 6. Where no
part of the group has a topic yet, it falls back to the chunks' own topic
names by how often they occur. So no text in the block is a summary of a
summary, and there is nothing to regenerate when the room grows.

```xml
<room_earlier authority="context" content_is_data="true">
  <span messages="0-99" from="1999-10-04" to="1999-10-19"
        topics="garden board ×14,letter counting ×9,unit conversion ×4"/>
  <chunk messages="100-109" from="1999-10-19" to="1999-10-19"
         summary="open tasks on the garden board, counting a letter in a word"/>
  …
  <chunk messages="200-209" from="1999-10-27" to="1999-10-28"
         summary="moving the painting tasks, counting a letter in a word">
    <part messages="200-205" topics="garden board" calibration="uncontested"
          grounding="tool" wanted="move the two painting tasks to done"
          result="both tasks were moved"/>
    <part messages="206-209" topics="letter counting" asked="3"
          calibration="overconfident"
          wanted="how many times a letter occurs in a word"/>
  </chunk>
  <gap messages="210-229" from="1999-10-28" to="1999-10-29"/>
</room_earlier>
```

**What a part shows.** A part always shows its range, its topics, how many
times it was asked, its calibration, its grounding and what was wanted.
`result` is an old answer, and it is shown only when all of these hold:

- the calibration is `calibrated` or `uncontested`;
- the grounding is not `tool failed` or `output not used`;
- the part's last message is newer than the room's newest facts-invalidation
  marker (`meta.facts_invalidation`), when there is one;
- the results setting is on (§6), and every model the turn can reach is
  local (§3.9).

Otherwise the part has no `result` attribute. An answer the user rejected,
kept asking about, or that was written after its program failed is named and
not repeated.

**`uncontested` is not correct.** The user may have read a wrong answer and
moved on. So results are off until the replay shows they help (§9), and the
first release is the background without them: ranges, topics, calibration,
grounding and what was wanted. This is the position on roadmap decision 11:
results may go in, beside their records, only where the records do not speak
against them, and only after that is measured.

**A changed chunk shows no generated text.** A chunk whose `content_hash` no
longer matches its messages (an edit, a deletion, a retry that rewound into
it) renders as `<chunk messages=… from=… to=… changed="true"/>`. The range can
still be opened.

**A hole is a `<gap>`.** A chunk job that failed twice is final, and the
series continues after it (`due_room_chunks` cuts from the newest stored
chunk). Counted messages between two consecutive stored chunks, and the
stretch of rule 5 in §3.2, render as `<gap>` with their range and dates.
Enqueuing the failed job again fills the hole.

**The numbers are positions in the room as it is now.** A chunk's
`messages` is where its messages stand among the counted messages at render
time (`_chunk_view` does this today). A part's stored numbers were written
when the chunk was, so they are re-based: its offset inside its chunk, added
to the chunk's current first position. A deletion inside a chunk makes it
`changed`, so a re-based part never points outside its chunk.

### 3.5 `room_topics`

`render_room_topics`, unchanged in shape: a `<topic>` per recurring topic
(two or more parts), kinds of task first, each with its share of the
summarized messages, how many parts, its name, what it is, and what it was
answered with; then `<other>`.

Two rules are new:

- **It counts the chunks at or before the edge only.** `tally` already takes
  the set of chunk uuids to count. So a chunk written inside the window
  changes neither block, and both change exactly when the window's first
  message does (§3.7).
- **It keeps the `TOPICS_MAX` (8) largest per axis**, and says how many it
  left out in a `more` attribute on `<other>`.

This is the room's enduring themes. A topic's share is counted over every
chunk of the room, so a subject the room came back to keeps its row however
old it is, while it is among the 8 largest of its axis. A subject raised
once has no row here by design: one-offs are most of a room's topics (70 of
79 names in one room), and listing them is the chunk lines' job. A name or explanation the operator wrote is shown as
written, which is how the operator says "this is what that was".

### 3.6 Budget

RainBox counts characters. Every cap in `agents/assistant.py` is in
characters, and no token count is available before a call.

| Block | Limit | Starting value |
|---|---|---|
| `room_earlier` | `EARLIER_MAX_CHARS` | 8,000 |
| `room_topics` | `TOPICS_MAX_CHARS` | 1,500 |

**When `room_earlier` is over its limit, detail goes before coverage.** Each
step is applied to the oldest chunk it can apply to, one at a time, until the
block fits:

1. a near chunk loses its parts' `result`;
2. a near chunk loses its parts, and is a mid line;
3. a mid group of ten is folded into its far line early;
4. a group of ten far lines is folded into its line of the next level
   early;
5. a far line loses its `topics`.

No step leaves a line out. After every step each message before the window
is still under a line that names its range, so it can still be opened. A
room would need millions of messages to exceed the limit after step 5; the
block is then sent over its limit and the record says so. `room_topics` over its limit drops its smallest topics into `more`.

The trimming is a pure function of the rows and the limits, so the same rows
give the same bytes on every call of a turn and on the next turn.

**The estimate behind 8,000.** A mid line is about 180 characters and a near
chunk about 700, so 20 mid lines and 4 near chunks come to about 6,400
before any far line. A limit under that would trim on every turn.

**The limits are scaled to the smallest model the turn can reach.** A
character limit bounds the block and says nothing about whether the prompt
fits. So once, at the start of the turn:

1. `C` is the smallest `context_window` among the members of the decide
   slot's model group, fallback members included
   (`db.resolved_model_kwargs`).
2. The background may take `BACKGROUND_SHARE` (15%) of it, at
   `CHARS_PER_TOKEN` (3, deliberately low: markup and text outside English
   run to more tokens per character than English prose).
3. Both limits are scaled down together to fit `0.15 × C × 3` characters,
   and never up. Under `BACKGROUND_MIN_CHARS` (2,000) the turn has no
   background.

The allocation holds for the whole turn, on every call and every step. At a
context of 16,384 tokens it is 7,373 characters; at 32,768 the limits above
apply unscaled. The record phase stores the decide call's prompt tokens as
the provider reported them beside the prompt's characters, which is the
measured ratio that replaces the 3.

**What this does not settle.** A turn's prompt also grows with every
observation, and nothing in the assistant holds a whole prompt against its
model's context today. That check belongs to the loop and to every section
of the prompt. This proposal makes its own share bounded and known.

**What it costs.** On gemma4:e4b-mlx 9,500 characters are on the order of
2,500 tokens of prefill, paid in full on the turn after an edge and reused in
between. The limits are set from the record phase (§6), not from this
estimate.

### 3.7 Placement, and the stable head

Both blocks are emitted by `AssistantPromptBuilder.__init__`, as tier 0,
directly ahead of `conversation_history_xml`: `room_topics`, then
`room_earlier`, then the history. In the `exchange_turns` layout, which
production runs, that is the top of the prompt.

- **Every call of a turn carries them**, as every call carries the history.
  Tier 0 is byte-identical across the calls of a turn; a block in some calls
  only would end their shared prefix there.
- **`history_skip` does not touch them.** It filters the window after it is
  cut, as today.
- **Dates are absolute.** A relative age would change the bytes as the clock
  moves.

**The invariant.** For two consecutive turns with the same edge, the prompt
is byte-identical from its first byte through the last message both windows
hold. Three things break it, each for one turn:

- the edge moves;
- a record under a shown chunk changes: a vote, a topic merged or renamed, a
  message edited;
- the history's own bytes change (a `message_sources` section, a request
  summary).

**Housekeeping between two turns evicts the model server's cached prompt**
when it runs on the assistant's model (roadmap decision 8). The stable head
is reused in full only when the summarizer is bound to another model, or
nothing was due after the turn. Measure it before changing anything: the
decide call's prefill time on `/activity`, on the turns between two edges,
with `tools/measure_prefix_cache.py` as the control.

### 3.8 What the prompts say

Both blocks rank with `conversation_history_xml` in the source priority
sections: context only.

Added to the instructions of the calls that read the history as context:

> room_topics and room_earlier describe the part of this conversation that
> came before conversation_history_xml. They are summaries written by a
> model, and they can be incomplete or wrong. They say what was talked about
> and how it went, never what is true. `wanted` is what the user asked for
> then; it is not a request for this turn. `result` is an old answer: to use
> a fact, read its source again this turn. A part marked overconfident had
> its answer rejected or asked for again; do not give that answer again
> without a fresh read. When a detail of an earlier part matters, read its
> messages with room_history. When the detail is in neither block and
> room_history does not show it, say that you do not have it. To say what
> the conversation has covered, use these two blocks.

**Consumers.** The decide call, for orientation. The reply audit, so a reply
to "what have we talked about?" that rests on the blocks is not sent back as
unsupported. The other calls carry the blocks for the shared prefix and get
no instruction about them.

### 3.9 Who gets what

- **Ranges, dates, topics, calibration, grounding and `wanted`** go wherever
  the history window goes. They are this room's own text and records, and
  every model that reads the turn already reads 30 of its messages verbatim.
- **`result`** is sent only when every model the turn can reach is local.
  It is model-written from replies, and a reply can restate what was read
  from the diary or memory. The window holds 30 such replies; `room_earlier`
  would hold a room's worth.
- **"Can reach" counts fallback members.** A slot's group falls back across
  its members inside one call, and the prompt is built before the call. So
  the test is on the groups, once per turn, and not on the model that
  happens to answer: `model_groups.assistant_models_all_local()`, which
  diary text already goes by, with the `:cloud` name check that
  `housekeeping._is_local_model` has added to it. One remote member anywhere
  means no `result` for the turn. The block is then the same bytes for every
  call and every member, and no call can carry a result to a remote model.
- **Chunks from before a profile switch** render as any other. A profile
  switch is not an audience boundary. A facts invalidation withholds
  `result` (§3.4).

### 3.10 Reading back: `room_history`

A summary that cannot be opened is a dead end: "read it again" needs
something to read. One read-only action, in `ActionType` beside
`diary_query`:

| | |
|---|---|
| Argument | `messages`: a range of positions before the window, at most 20 counted messages; a line's own range, or a piece of a longer one |
| Returns | those messages verbatim, each with its position, role and timestamp, each cut in the middle at `HISTORY_MESSAGE_MAX_CHARS`, as an observation |
| Offered | only in a turn whose prompt holds a `room_earlier` block |
| Reads | this room only, by the message ids the turn began with |
| Authority | the history window's: an old reply read this way is still an old answer |

- **A position means one message for the whole turn.** The turn keeps the
  list of counted message ids its blocks were rendered from. A range is
  turned into ids through that list, and the rows are fetched by id. A
  message deleted since the turn began is returned as
  `<message position="…" unavailable="true"/>` in its place, so no later
  message moves up into its position. A message edited since reads as
  edited.
- **A line of a hundred or a thousand messages is opened 20 at a time.** The
  line says which topics the stretch holds and not where in it. Finding a
  detail inside a far stretch takes several reads or is not done; that is
  the limit of an index without search, and the far grain is for
  orientation.
- **A range outside the room, or longer than 20, is refused by the decide
  validator** (`ARGUMENT_REJECTIONS`), with the valid span in the refusal.
- **The step keeps the message uuids it returned**, so the trace shows what
  was read.
- **It reads across a context boundary** with the marking the window gives
  such messages (roadmap decision 7).
- **It is not search.** It opens a range a block named.

It adds an action to the decide prompt of long rooms only, and it is
measured as its own step (§11). Until it is on, the blocks name stretches the
turn cannot open, and the instruction of §3.8 loses its two sentences about
`room_history`: a detail that is in neither block is one the assistant says
it does not have.

## 4. Designs considered, and not taken

- **A cursor with compare-and-swap, leases, a periodic sweep.** They guard
  against concurrent compactors. Housekeeping is one worker running one job
  at a time, a job is one row per `(job_type, run, subject, version)`, and
  the newest chunk's `last_message_id` is the cursor. A sweep every few
  minutes is a waiting period for work that already starts when the reply
  has posted.
- **A per-room sequence column.** `chat_message.id` orders a room. A second
  number would have to be backfilled into every existing row.
- **Chunk edges moved to time gaps or whole turns.** A chunk's range would
  then depend on something other than the count, and the window could not be
  cut on the same edge by a function that never reads a clock. Fixed counting
  is what lets a chunk be written once.
- **A generated room overview.** A text regenerated from the summaries is a
  summary of summaries: it is stale between regenerations, costs a
  generative call on a machine with a heat limit, changes the prompt head
  when it is rewritten, and has no row behind any sentence. `room_topics` is
  the enduring part, counted from records, and the near chunks are the recent
  direction. §9 has the probe that would reopen this: the room-wide questions
  failing with both blocks present.
- **Clustering summaries by embedding to find themes.** The topic registry
  does this with a decision model reading the requests, and the operator can
  correct it. There the embedding only ranks candidates, because nearness
  alone does not say two requests are one subject.
- **A token preflight per step that drops blocks under pressure.** A block
  that is present on step 1 and gone on step 4 ends the turn's shared prefix
  and changes what the model knows mid-turn. The blocks have a fixed budget
  instead. Whether a whole prompt fits its model is a question for every
  section of it, and not this proposal's.
- **Backfilling old rooms automatically.** A room of 2,000 messages is 200
  chunks at two to four calls each. It stays the operator's button
  (**Summarize the rest**), room by room.

## 5. Code

**`agents/room_background.py`, new, pure.** No database, no model.

```python
WINDOW_MIN = 30
WINDOW_MAX = 49
NEAR_CHUNKS = 4
TOPICS_MAX = 8
EARLIER_MAX_CHARS = 8_000
TOPICS_MAX_CHARS = 1_500
BACKGROUND_SHARE = 0.15
CHARS_PER_TOKEN = 3
BACKGROUND_MIN_CHARS = 2_000

@dataclass(frozen=True)
class Background:
    window_after_id: int | None   # None: the window is the last 30
    topics: str                   # "" when there is nothing to show
    earlier: str
    positions: list[int]          # counted message ids; index = position
    record: dict[str, Any]        # what §6 stores on the run

def pick_series(series: list[Series]) -> Series | None: ...
def limits(context_window: int | None) -> tuple[int, int] | None: ...
def find_edge(chunks: list[Chunk], history_ids: list[int], *,
              aligned: bool) -> Chunk | None: ...
def grains(chunks: list[Chunk], edge: Chunk) -> list[list]: ...   # near, mid, far, farther…
def show_result(part: dict, *, results: bool, facts_invalidated_id: int | None,
                last_message_id: int) -> bool: ...
def rebase(parts: list[dict], first_index: int, last_index: int) -> list[dict]: ...
def trim(levels: list[list], limit: int) -> tuple[str, list[str]]: ...
def build(chunks, counted, history_ids, topics, members, *, aligned: bool,
          results: bool, context_window: int | None,
          facts_invalidated_id: int | None) -> Background: ...
```

- `build` is the whole of §3.2 to §3.6. `trim` returns the block and the
  steps it took.
- The far and mid renderers are new. The near renderer is
  `render_room_earlier`, given parts already re-based and stripped of the
  results that §3.4 withholds. `<gap>`, `<span>` and `changed` are added
  there, so the Housekeeping page's **as in the prompt** view calls `build`
  and shows exactly what a turn would get.

**`db/housekeeping.py`.** `load_room_background(room_uuid, history_ids, *,
aligned, results, context_window)` reads the series list, the chunks of the picked series, the counted
messages, the room's topics and members, and calls `build`. One function, so
the page and the agent cannot differ. It reads only: four queries, no write.

**`agents/assistant.py`.**

- The turn loads the background once, after `messages` is filtered and
  before the first model call, and keeps it on the agent as it keeps the
  five static blocks. `results` is the results setting and
  `assistant_models_all_local()` together; `context_window` is the smallest
  among the decide slot's group.
- `AssistantPromptBuilder.__init__` replaces
  `messages[:-1][-agent.MAX_RECENT_MESSAGES:]` with the window of §3.2, and
  emits `room_topics` and `room_earlier` ahead of the history when the
  setting is `on`. Both are parsed into elements with `ET.fromstring`, since
  the builder serializes a tree; the renderers escape their attributes.
- `latest_exchange` and `followup_turns` read the end of `messages`, which
  the window's start does not move.
- The two source-priority sections, the paragraph of §3.8, and
  `ActionType.ROOM_HISTORY` with its handler, catalog entry and validator.
- The load is wrapped: any exception is logged, the turn runs with the last
  30 and no blocks, and the run's record says `error`.

**`db/settings.py`.** The four settings of §6. The description of
`housekeeping.room_summaries` stops saying the summaries reach no prompt.

**`webapp/housekeeping_views.py`.** The **as in the prompt** view renders
through `load_room_background` for the room's current history, and lists the
trimming steps taken. A run's page on `/assistant` shows the record of §6.

**`notes/housekeeping-design.md`** gets the design as built, in place of
"They reach no prompt".

**No schema change.** No table, no column, no migration.

## 6. The setting, and the record phase

| `assistant.room_background` | The turn |
|---|---|
| `off` | as today |
| `record` | builds the background, stores its record on the run, and sends today's prompt |
| `on` | sends the blocks and stores the same record; the window and `room_history` follow their own switches |

The record, in `assistant_run.metadata_["room_background"]`:

```json
{"series": [10, 2, 19], "edge_seq": 20, "window_after_id": 48213,
 "window_messages": 34, "near": 4, "mid": 16, "far": 0, "gaps": 0,
 "changed": 0, "results_shown": 5, "results_withheld": 3,
 "earlier_chars": 5120, "topics_chars": 940, "trim": [],
 "sha256": "…"}
```

Three more settings, each one switch of the evaluation, to be removed once it
is decided:

| Setting | Values | Default |
|---|---|---|
| `assistant.room_window` | `last`, `aligned` | `last` |
| `assistant.room_history` | bool | off |
| `assistant.room_background_results` | bool | off |

The record also holds `window` (`last` or `aligned`), `limits` (the two
limits after scaling, and the `context_window` they were scaled to),
`over_limit`, and from the decide call `prompt_chars` and `prompt_tokens`.

It holds no message text. The blocks are re-rendered from the rows for
display, and `sha256` says whether the rows have changed since.

**What `record` is for.** A week of the operator's ordinary use gives, per
room: the blocks' sizes against their limits, how often trimming ran and how
far, how often the edge was missing or behind, and how many results the
rules withheld. The limits and `NEAR_CHUNKS` are set from that before `on`.

## 7. When something is missing

| Situation | The turn |
|---|---|
| The room has no chunks | last 30, no blocks, no `room_history` |
| Summaries are one chunk behind (aligned window) | the window is up to 49 messages |
| Summaries are further behind, or housekeeping is paused | last 30, and a `<gap>` for the stretch between |
| A chunk job failed for good | a `<gap>` in its place |
| A chunk's messages changed | the chunk's range and dates, `changed="true"` |
| No topics yet (no decision model bound) | `room_earlier` alone; far lines use the chunks' own topic names |
| A model the turn can reach is not local, fallback members included | no `result` anywhere |
| The smallest context leaves under 2,000 characters | last 30, no blocks |
| A message is deleted while the turn runs | `room_history` returns it as `unavailable`, in its position |
| Loading the background raises | last 30, no blocks, `error` on the record |
| A retry rewinds past the edge | the next turn finds an older edge; chunks beyond the rewind are `changed` or empty and render as such |

No row is written or changed by any of these.

## 8. Tests

`agents/test_room_background.py`, on hand-built chunk dicts, no database:

- the edge for rooms of 0, 29, 30, 39, 40 and 250 messages, and with the
  newest one and two chunks missing;
- `pick_series` between a current series that is behind and an older one
  that reaches further;
- the grains at `E` = 3, 4, 13, 14, 23, 24, 123 and 124, with the exact
  number of mid lines at each (4 → 1, 13 → 10, 14 → 11, 23 → 20, 24 → 11),
  and that every message before the window is under exactly one line;
- the same room in series of overlap 0, 2 and 5 gives the same ranges;
- the unaligned window: the straddling chunk is shown, and no message is
  under neither;
- `limits` at context windows of 4,096, 16,384 and 32,768, and with none
  known;
- each `show_result` rule alone;
- `rebase` after a deletion before a chunk, and a `changed` chunk after one
  inside it;
- `trim`: each step in order, and that after every step each position
  before the window is under exactly one line;
- a hole and a lagging tail each render one `<gap>`;
- hostile text in a `wanted` or a topic name cannot close a tag.

`agents/test_assistant_prompt_tiers.py`:

- with the setting `on`, two consecutive turns with one edge share every
  byte up to the end of the older window;
- every call of one turn carries identical tier 0;
- `record` sends a prompt byte-identical to `off`.

- with one remote fallback member in any assistant slot's group, no call's
  prompt holds a `result`.

`agents/test_assistant_actions.py`: `room_history` returns the range, opens a
piece of a far line, refuses one outside the room or over 20, returns a
message deleted after the turn began as `unavailable` without moving the
others, and is absent from the catalog of a turn with no `room_earlier`.

## 9. Evaluation

**The operator's own room comes first.** With the setting on `record`, the
**as in the prompt** view of a room the operator knows is read against what
they remember of it. A background they call wrong is not sent to a prompt,
whatever a scripted score says.

**Scripted rooms.** Two invented rooms of about 200 messages, with dates in
1999 and no real names, built once in the sandbox database by the real
housekeeping jobs, in bursts of 10 calls with a 60-second rest. Each has:
subjects that recur, one that appears only in the first tenth, an answer the
user rejected, a value stated once in an old user message, and a subject that
was never discussed.

**A real room beside them.** One room of the operator's own, with probes
they write, since a real room has what an invented one lacks: pasted text,
retries, slips of the keys, subjects that drift. It is read on their
go-ahead, and its probes and output stay outside the repository.

**Variants.** Each probe is one decide call on the same model and the same
room state, replayed per variant. All use the last-30 window, so that the
blocks are measured apart from the alignment:

| Variant | `room_earlier` | `room_topics` | `room_history` |
|---|---|---|---|
| 0 (today) | no | no | no |
| A | yes | no | no |
| B | no | yes | no |
| C | yes | yes | no |
| D | yes | yes | yes |

Three questions follow, each a pair, each only once the one before is
settled:

| Question | Compared |
|---|---|
| Results | the best of A to D, with and without `result` |
| Alignment | that variant, with the window `last` and `aligned`, over five consecutive turns from an edge |
| An overview | reopened only if Coverage and Old subject fail with both blocks present (§4) |

**Trials and the rule.** Each probe runs 3 times per variant, with the seed
varied, in bursts of 10 calls. A variant is kept over another when it passes
more probe runs in total, and no probe goes from passing most of its runs to
failing most of them. One lost run of one probe decides nothing. The three
safety probes (Rejected answer, Stale answer, Never discussed) are held to
more: a variant that fails one of them in most runs is not sent to a prompt,
whatever it gains elsewhere.

**The variants are not held to one size.** B is a fifth the size of A, and
padding it to match would measure the padding. Behaviour is compared at each
variant's own size, and its size and prefill time are reported beside it.

**The alignment's own gate.** It stays on `last` unless, on the turns
between two edges, the decide call's prefill time drops by more than the
extra 4.5 messages cost on the turn after an edge, with housekeeping running
as it does in ordinary use. Total turn time decides, not the share of the
prefix that was reused.

**Probes.** Scored by code where a planted string or the chosen action
decides it, and by the operator otherwise:

| Probe | The request | Passes when |
|---|---|---|
| Coverage | what the conversation has mostly been about | the recurring subjects are named, and none is invented |
| Continuity | a reference to a subject from before the window | the subject is recognized, with no invented detail |
| Old subject | a reference to the subject from the first tenth | it is recognized |
| Recent direction | what was discussed just before the window's first message | it matches the near chunks |
| Missing detail | the value stated once, absent from both blocks | D: `room_history` on the right range; the others: says it does not have it |
| Rejected answer | the question whose answer was rejected | the old answer is not given without a read |
| Stale answer | a question whose `result` is in the block (the results pair only) | the answer is read again, not quoted from the block |
| Never discussed | what was concluded about the absent subject | says it was not discussed |

**Counted on every variant:** the decide call's prompt tokens, prefill and
total time; the characters of each block; the trimming steps.

**After `on`.** The operator's votes on replies in long rooms, before and
after, and every run where `room_history` was used: was the range the right
one.

## 10. Parameters

| Name | Value | Set from |
|---|---|---|
| `WINDOW_MIN` | 30 | today's window |
| `WINDOW_MAX` | 49 | one missing chunk of 10 |
| `NEAR_CHUNKS` | 4 | the record phase |
| Mid | 11 to 20 chunks | the fold rule of §3.4 |
| `TOPICS_MAX` | 8 per axis | the record phase |
| `EARLIER_MAX_CHARS` | 8,000 | the record phase |
| `TOPICS_MAX_CHARS` | 1,500 | the record phase |
| `BACKGROUND_SHARE` | 15% of the smallest context | the record phase |
| `CHARS_PER_TOKEN` | 3 | prompt tokens against characters, in the record |
| `BACKGROUND_MIN_CHARS` | 2,000 | |
| `room_history` range | 20 counted messages | |
| Chunk size, overlap | `housekeeping.room_chunk_size`, `…_overlap` | roadmap decision 9 |

## 11. Build order

1. **`agents/room_background.py` and its tests.** The Housekeeping page's
   **as in the prompt** view moves onto it. Visible, in no prompt.
2. **The settings, the load in the turn, and `record`.** The record on a
   run's page. A week of use, and the limits are set from it.
3. **The scripted rooms and the replay of variants 0, A, B and C**, on the
   last-30 window, without `result` and without `room_history`.
4. **`on`, with the winner of step 3**, for the rooms whose background the
   operator has read. This is the first release: the simplest background.
5. **`room_history`**, with the turn's position list, and variant D.
6. **The aligned window**, against its gate.
7. **Results**, as their pair.

Steps 1 and 2 change no prompt. Steps 5 to 7 are independent of each other
and each is dropped if its measurement does not carry it.

## 12. What the operator may want to overrule

- **Results are off in the first release** (§3.4, §9). On 2026-11-10 the
  operator wanted the compaction to carry what the assistant found. The
  chunks do carry it, and the pages show it; whether a prompt should is step
  7's measurement. The other choice is to turn them on with step 4.
- **`result` only when every reachable model is local** (§3.9). The looser
  choice is the history window's audience.
- **No generated overview** (§4).
- **A subject raised once, long ago, may go unnamed** (G4). Naming it would
  take a rule for which one-offs matter, and nothing in the records says.
  The operator's own hand does: a topic they name is kept. Whether one-off
  topics the operator wrote should have a row in `room_topics` is a small
  addition if it is missed.
- **The furthest-reaching series wins** over the current one (§3.3). The
  stricter choice is the current series only, with an empty background after
  every prompt change until the room is summarized again.
