# Housekeeping — design

Housekeeping is the work that follows a turn. It has two jobs: the run
summary, and the chatroom chunk summary. The role, its queue and its page are
built so that later jobs (detectors) join the same queue.

The two summaries are different things:
- **The assistant summary** describes one assistant run: what triggered it, the
  obstacles in its steps, and its outcome.
- **The chatroom chunk summary** describes a stretch of a room's messages: its
  parts, each with what was wanted, what came of it and how well calibrated
  the assistant was, under the names of its subjects and broader topics. It is the
  compaction of a long room.

The plan for those later jobs, and for a heat budget and a catch-up lock, is
in `proposals/2026-11-09-post-turn-housekeeping.md`. This page describes what
exists.

## Architecture

| Piece | File |
|-------|------|
| The worker: expand, order, gate, kill, retry | `agents/housekeeping.py` |
| The queue and its record | `db/housekeeping.py` |
| `housekeeping_job` / `housekeeping_attempt` | `db/models.py` |
| The Housekeeping page and a job's page | `webapp/housekeeping_views.py` |
| The assistant summary's model call | `agents/assistant_run_summarizer.py` |
| The chunk summary's prompt and parsing | `agents/room_chunks.py` |
| `room_chunk` | `db/models.py` |

## From a run ending to a summary

1. **The assistant enqueues one item** for the `housekeeping` role at every
   terminal state: `{run_uuid, room_uuid, request_message}`. The supervisor
   does the same for a run it recovers.
2. **The supervisor spawns the worker**, as it does any role with inbox work.
3. **The worker expands the item** into that run's jobs. A job is a
   `housekeeping_job` row, identified by
   `(job_type, run_uuid, subject, job_version)`, so the same work asked for
   twice is one row.
4. **The worker runs every waiting job**, not only the ones the item named,
   and exits when none is left.

## Chatroom chunk summaries

A room's **counted messages** are its real messages that have finished
streaming, in id order. Progress and thinking rows do not count.

**Cutting.** The counted messages are cut into chunks of
`housekeeping.room_chunk_size` (default 10), from the room's first message.
A chunk is due once `housekeeping.room_chunk_overlap` (default 2) counted
messages follow it, because the model reads that many messages on each side
as context. The next chunk starts after the newest stored chunk's last
message id, so appending to a room never changes a chunk already cut.

**A series per settings and prompt.** A chunk belongs to the series of its
`(chunk_size, overlap, prompt_version)`. Changing a setting, or a change to
the prompt in code, starts a new series from the room's first message, beside
the old one. Every series stays readable on the room's page, which is how two
sizes or two prompts are compared.

**When jobs are queued.**
- When a run ends, the next 2 due chunks of its room, if
  `housekeeping.room_summaries` is on (the default).
- When the operator presses **Summarize the rest** on a room's page, every
  due chunk of the current series, up to 200.

**The model reads both sides, with the records under each reply.** Every
message keeps its number. A request is shown up to 1,500 characters and a
reply up to 600, cut in the middle. Under an assistant reply stands a line of
signals, when the records have any (`db.attach_reply_signals`):

| signal | from |
|---|---|
| `run resolved` / `partial` / `failed` | the assistant summary's outcome for the reply's run, else the run's status |
| `criteria met` / `criteria not met`, and how many revisions | the reply audit's verdicts for that run: met when the last one was to send the reply |
| `voted up` / `voted down`, with the comment | the user's latest vote on the message |

The signals are records, and the prompt says so. They are what lets a small
model say something about reliability without judging an answer from its own
knowledge.

**The call.** A structured call on the `assistant.room_summarizer` slot,
which falls back to `assistant.default`. A second call then checks the
answer (see **The check** below). The prompt holds the chunk and
the context on each side, marked as not part of it. The reply is a breakdown
in two levels:

| level | fields |
|---|---|
| each part | `first_message`, `last_message`, `summary`, `result`, `follow_up`, `topics` (one or two) |
| the chunk | `subjects` (the name of each part's subject), `topics` (one to three, broader) |

**Every prompt numbers its chunk from m1.** A chunk's messages are m1 to
m10 wherever the chunk stands in its room, and the context on each side has
no numbers, since no answer refers to it. Counted from the room's first
message the numbers grow with the room, and a small model handles a long
number worse than a short one. The model answers in the prompt's numbers.
Code puts the room's in their place (`room_chunks.room_numbers`) before
anything is settled, so what is stored and what the pages show is counted
from the room's first message. The check and a rewrite are given the stored
parts in the prompt's numbers again, and a message number in the check's
reasoning is stored as the room's.

A part is a run of consecutive messages about one subject. A request that
is repeated, retried or corrected stays in its part. The same question about
another thing or another value is a part of its own, because it has an
answer of its own. The parts come first in the schema, so the chunk's subjects
are named with them in view.

- **`subjects`** are names, at most five words each, and not sentences.
  Code joins them with commas into the chunk's stored `summary`. Asked for
  a phrase over the chunk, the model wrapped the names in words that said
  nothing: that details were requested, that questions were asked. A list
  leaves it nowhere to put them.
- **`summary`** is what was wanted.
- **`result`** is what came of it: the one main thing found or concluded, or
  that nothing was.
- **`follow_up`** is a choice: `corrected`, `retried`, `thanked`, `none`.
  It is what the user's messages in the part say about the replies, and
  the only part of the user's reaction left to the model.

**What the user did after each reply is read by rule** (`room_chunks.react`),
and stored as the part's `reactions`, in order. Whether a message follows a
reply, where it stands and whether it repeats a request can all be read off
the messages. Asked for the reaction, the model answered for the end of the
chunk and not for the part: it gave `not_shown` to parts in the middle of a
chunk, and `moved_on` to a part with a retry inside it.

**Whether a follow-up asks again is put to a decision model**
(`llm/systemone.py`, slot `assistant.repeat_check`). The text rule sees a
request typed again; it does not see one asked again in other words. Before
the summary call, the worker asks the decision model about every user
message that follows a reply to an earlier request of the chunk
(`room_chunks.follow_ups`), and about the first user message after the
chunk, so a request asked again across a chunk's edge is seen by the chunk
that holds the request:

- **One request per follow-up, of two user messages.** The request and
  the user's next message are given as a text, each inside a tag of its
  own (`<message1>`, `<message2>`), cut as the summary's prompt cuts them,
  with angle brackets escaped so a message cannot close its tag. The
  assistant's reply between them is not given. The question is a choice,
  "Does message2 ask again for what message1 asked for?", with three
  options: `duplicate` (yes, in the same or almost the same words), `same`
  (yes, in different words, or by asking for another attempt), `different`
  (no, it asks for something else). The follow-up asks again when
  `duplicate` and `same` hold at least half of the probability between
  them.
- **Its yes makes the message `asked_again`**, inside a part or after it,
  and joins a part that opens with such a message to the part before. The
  text rule's yes stands whatever the model said.
- **Each part lists the messages the model called asking again**, with the
  probability and which of the two ways it was, as `asked_again_at`. The chunk's `audit.repeat_check` says
  which model was asked, of how many follow-ups, and how many it answered.
- **A follow-up that is the request word for word is not put to the model.**
  It asks again, with probability 1 and the way `duplicate`, and
  `audit.repeat_check.identical` counts these. `asked` and `answered` count
  only the follow-ups the model was given.
- **Unbound, timed out or refused, a follow-up is read by rule alone.** The
  slot has no fallback to `assistant.default`: a generative model cannot
  answer this API. The model must have its weights on this machine.
- **The slot takes a SystemOne model group only.** On `/modelgroup` a new
  group's first choice is SystemOne. "Must have" admits only models the
  provider reports with the `decision` capability (`model_config.systemone`,
  observed at every sync), and fixes structured output to "Must have",
  since such a model's output is always structured. On `/agentmodel` a
  SystemOne group is offered to a decision slot and to no other agent.
- **Every request is a `decision_call` row**: the question, the
  probability, the label and the bands that gave it, the model's digest, and
  a digest of the state. The state's text is not stored in that row.
- **A request that reached the model is also a row on `/activity`**, under
  the slot's name as its caller: the state and each question as they were
  sent, and the reply as it came back. That row holds the state's text for
  as long as `/activity` keeps a prompt. The `decision_call` row names it
  (`llm_call_uuid`), and the job's page links each request to it. A request
  that was never sent (no model bound, a state too long, a model that is
  not local) has no such row.
- **The job's page shows each request as its row on `/activity` shows
  it**: each message that was sent under its role (the state, then each
  question), and the response as it came back. The line above them carries
  the message's number, the verdict and its probability; these come from
  the `decision_call` row, and the number from the state rebuilt from the
  room's messages and matched to the call by the digest. A request with no
  row on `/activity` says so, and shows the rebuilt state when the
  messages still match.
- **These calls do not count toward the rest.** Each is a fraction of a
  second of scoring, and no text is generated.

Measured on one room with `d1:3b`, on 16 follow-ups that asked again and
41 that did not, labelled by hand before the run: the question caught 14
and called none of the others one, in about a fifth of a second each.

- **With the reply between the two messages** it caught 15 and called 1.
  One run cannot tell these apart, so the reply is left out: the input is
  smaller, and a reply can carry text from elsewhere.
- **With the three user messages before it as well**, and one question
  per earlier message, it found one request asked again across another
  subject, and tied 2 to 4 messages to a request they did not repeat. A
  short nudge or retry is taken to repeat whatever stands a few messages
  back. So only the message just before is asked about.
- **A yes-or-no question** over an object with the keys request, reply and
  next_message caught 12 of 15 and called none.
- **Asked whether the reply "did not do what was asked"**, the same model
  separated the two no better than chance. `d3:4b` said yes to nearly
  everything at 0.5 and took eight times as long. A generative call on
  gemma4:e4b-mlx judging the same follow-ups was no better, at several
  seconds each.

**What the tags do and do not do.** They mark where each message begins
and ends. They do not make the model deaf to what a message says: a
sentence planted in the next message, saying that it repeats the request,
moved nearly every unrelated follow-up to "asks again", with tags as
without. That message is the user's own, and the answer is about it. The
reply, which could carry planted text from elsewhere, is not in the input.
What a wrong answer costs is one part's reaction and calibration on a page:
nothing acts on it.

One entry for each user message inside the part after its first request,
where the decision model did not answer:

| the message | reaction |
|---|---|
| the only such message in the part, and the model read `retried` | `asked_again` |
| at most 3 words, and the model read `retried` in the part | `asked_again` |
| at most 3 words, and the model read `thanked` | `thanked` |
| at most 3 words otherwise | `pushed_for_more` |
| an earlier request of the part again, by its text | `asked_again` |
| anything else | `pushed_for_more`: it is in the part, so on its subject |

Then one entry for the first user message after the part, looked for in
the rest of the chunk and in the context after it:

| the message | reaction |
|---|---|
| there is none: the room ends there | `not_shown` |
| a request of the part again, by its text | `asked_again` |
| it puts words of a reply in the part between quotation marks | `questioned` |
| anything else | `moved_on` |

**A reply quoted back is negative feedback.** A user who quotes the
assistant's wording in the next message is not done with the answer: they
are finding out what it meant or where it went wrong. Words the user used
first are theirs, and quoting them does not count. This is the only doubt
that is read, and it is read from the quotation marks. Asked whether a part
opens with doubt about the reply before it, the model said so of three new
subjects in one room of 14 chunks, and not of the one message that did.

`corrected` is the model's reading, and stands before the last entry. The
model's `retried` counts in two places. On a message of a few words: such a
message has no request of its own, so it is a retry, a thanks or a nudge,
and telling these apart takes reading. And on a part's only follow-up: the
reading can then mean no other message. Where a part has several longer
follow-ups the model's `retried` counts for nothing, because it does not say
which one, and it says so of a new question on the same subject.

**Calibration is decided by rule, not by the model** (`room_chunks.calibrate`).
The model supplies what it is good at: the result. Asked for the calibration too, it called an answer that was repeated
unchanged while the user kept asking "calibrated", with the evidence in its
own prompt. The rule reads the part's reactions and the records. The first
line that applies decides:

| when | calibration | basis shown |
|---|---|---|
| a reply was voted down | overconfident | a reply was voted down |
| the user corrected a reply | overconfident | the user corrected a reply |
| the next message quoted the reply back | overconfident | the user's next message questioned the reply |
| asked again, and the first run did not resolve it but the last did | underconfident | it delivered only after being asked again |
| asked again, and a run resolved it | overconfident | it answered, and the user kept asking |
| asked again, and nothing was delivered | unclear | asked again, and nothing was delivered |
| a reply was voted up | calibrated | a reply was voted up |
| the room ends after the part | unclear | the room shows no reaction yet |
| the user went on, and the last run resolved it (or no run is recorded) | uncontested | the user went on, and did not say whether it was right |
| the user went on, and the last run did not resolve it | unclear | the user went on, and the last run was … |

**"Asked again" is read from the text, not taken from the model**
(`room_chunks.count_repeats`). The model calls a new question on the same
subject asking again, and a stricter wording of the prompt did not change
that. So a part counts as asked again when either holds:
- **A request in it repeats an earlier one in it.** The same words; the
  earlier request with up to 8 words added; or the two at least 92% alike
  letter for letter with the spaces taken out, which a slip of the keys
  leaves them. Two requests with different numbers in them are never a
  repeat, so the same question about another value is not one.
- **It has a bare follow-up and the model read it as asking again.** A
  follow-up of at most 3 words carries no request of its own. Whether it is
  a retry or a thanks is the one thing taken from the model.

Several different questions joined into one part are not asking again.

A part never delivered is `unclear`: that is a failure, and not a matter of
confidence.

**Where the rule is still wrong.** It tells underconfident from overconfident
by how the first and the last run ended, and that comes from the run
summary. A reply that said "nothing found" is often recorded as resolved, so
a request answered only on the retry can come out overconfident.

**A result is stored beside what is known about it.** Each part also gets
**Uncontested is not calibrated.** A reply can be wrong and get no answer:
the user reads it, sees it is muddled, and asks about something else. So
going on in silence is its own label, shown in grey, and "calibrated" is
kept for the one case where the user said the answer was right: a vote up.
The user's verdict reaches a part through the vote on the reply. A vote
down makes it overconfident, a vote up calibrated.

`signals`, counted by code: repeated and bare requests, read from their
text, and from the records of its replies the runs by outcome, the first and
the last run's outcome, replies that did not meet their criteria, revisions,
votes up and down. The page shows the calibration in
colour with its basis, the reactions, and these counts in grey. A run of
the same reaction is written once with its count. A result is an old
answer, and it is never shown without them.

**What code settles after the reply** (`room_chunks.settle`):
- Topic names are normalized to lowercase words joined by hyphens.
- A part whose message numbers lie outside the chunk keeps its text and loses
  its numbers.
- Each part is extended to the message before the next part, and the last to
  the chunk's end, so the parts cover the chunk.
- A part that opens with the request before it asked again is joined to that
  part. "Asked again" is read as the calibration rule reads it: the opening
  request repeats one in the part before, by its text; or it is at most 3
  words and the model read `retried` in it or in the part before it. The
  joined part keeps the first phrase and the result of the last (where the
  request ended up).
- A shared topic name does not join two parts. The model gives unrelated
  questions one generic topic, and joined they would show the first
  question beside the answer to the last.
- Each part's `signals` are counted, its `reactions` are read, and its
  calibration is decided.
- `requests` is how many times the part's request was made: one, and one
  more for each `asked_again` inside the part. The page shows it as ×N.
- Anything else off (too many topics, an over-long or empty phrase, no parts)
  is listed in `problems` and shown with the chunk.

**The check** (`room_chunks.AUDIT_PROMPT`, `settle_audit`). A second
structured call, on the same local models, reads the chunk's own prompt
followed by the summary as it was stored: each part with its message
numbers, what was wanted, what came of it, and how many times it was asked.
For each part it writes its reasoning and then answers two questions with
true or false:

| answer | question |
|---|---|
| `request_matches` | does the "wanted" line say what the user asked in these messages |
| `result_matches` | does the "came of it" line answer that same request, from a reply in these messages |

- **The reasoning comes first**, so the answers are given with it in view,
  and so there is something to tell a rewrite. It is stored cut to 600
  characters.
- **A false request or result fails the part.** The reactions are not put
  to the check. They are read by rule; and when they were the model's, the
  check disagreed with itself on them from one run to the next.
- **A failed part has the chunk written once more**
  (`room_chunks.build_retry_prompt`). The prompt is the chunk's own, followed
  by the first answer as it was stored and the check's reasoning for each
  failed part. The second answer is settled and checked like the first, and
  it is the one stored, whatever its check says. So a chunk is two calls as
  a rule and four at most.
- **Each stored part carries the check of itself** as `audit`: `{request,
  result, reasoning}`. The row's `audit` says how many times the
  chunk was written and, when twice, what the first answer had in the parts
  that failed.
- **A check or a rewrite that no model delivers** leaves the answer there
  is, and the row's `audit.error` says which of the two it was.
- **The check has its own caller label**, `assistant.room_auditor`. The
  job's page shows each check as a row of its timeline, as it does each
  summary.

**Why questions per part, and not a score.** Tried on 14 chunks of one
room, on gemma4:e4b-mlx:
- **The model scoring its own summary in the same reply** gave the middle of
  a six-point scale to 13 of 14, among them a part that paired a request
  with the answer to another.
- **A second call giving one score for the chunk** found one of three badly
  wrong chunks, and marked a sound one. Run again, it changed its score on
  5 of 13 chunks. With its reasoning written first it found none of the
  three: it confirmed each line against some message, and did not ask
  whether the request and the result belonged together.
- **A second call answering per part** failed a request or a result only
  where the two did not belong together, in every run.

**The prompt has no sample answers and no list of earlier topic names.** A
small model copies both. With a list of earlier names to reuse, it chose
from the list whether or not a name fitted, and returned the list's
placeholder word as a topic. So topic names are not shared between chunks:
the same subject can get two names in two chunks. Recognizing them as one
topic is a later step, and not this call's job.

**What it does well and badly**, on gemma4:e4b-mlx, 4 to 8 seconds for the
summary call; the check writes more and takes longer:
- **Parts, results and topics are good.** Ranges are right and the topics
  name the subject.
- **The reactions hold where they are read by rule.** No part inside a
  room is left without a reaction, and a request typed again is always
  seen.
- **`follow_up` is read unevenly.** `corrected` was right both times it was
  given in one room of 14 chunks. `thanked` was given six times where
  nobody thanked; none of them stood, because no message of a few words
  followed. `retried` was right three times out of four: the fourth turned
  a nudge for more into asking again.
- **Doubt without a quotation is not seen.** A next message that probes
  the reply in its own words reads as moving on, and the part stays
  uncontested until the reply is voted on.
- **The decision model takes a nudge for more as asking again.** A
  follow-up of a word or two that asks for more detail scores above 0.5.
- **It misses a retry that names something new**, such as a retry with a
  date added.
- **What the user states is not kept.** A part holds a request and its
  result. When the user explains something, the part's result is the
  user's own sentence or the last reply, and the rest is lost.
- **A correction in a later chunk is not seen.** The part it corrects stays
  as it was written, because a chunk is summarized once and alone.
- **The chunk's subjects carry no filler**, in all 14 chunks of one room.
  Many are the user's request word for word, which is longer than a name.
- **A part's phrase sometimes still begins with who spoke**, though the
  prompt asks otherwise.

**Local models only.** The call reads the room's own messages, so members of
the group that are not on a loopback endpoint, or are `:cloud` names, are left
out. With none left the job fails and says so.

**What is stored.** One `room_chunk` row per chunk, written once: its series
and `seq`, its edges as message ids, a `content_hash` of its messages,
`summary`, `topics`, `parts`, `problems`, `audit`, the model and the prompt
version. The page recomputes
the hash and marks a chunk whose messages have changed since.

**Where it goes.** The summaries are shown on the room's page. They reach no
prompt.

**A rest after ten calls.** The worker counts its model calls, of any job
type, and a chunk's check and rewrite each count as one. After 10 in a row it rests for 60 seconds before the next, and the
waiting job shows as resting. A new message still stops the call in progress.

## Order

One worker runs one job at a time.
- Every tier 1 job runs before any tier 2 job. Tier 1 is for results the next
  turn reads. Tier 2 is generative work that is read eventually. The run
  summary is tier 2.
- Within a tier, a job whose run's page was opened goes first
  (`bumped_at`), then the oldest.

Opening `/assistant?id=<run>` sets `bumped_at` on that run's unfinished jobs.
Someone is looking for the summary at that moment.

## Giving way to a turn

**Housekeeping starts when nothing else is working.** Before each job the
worker asks `db.turn_activity()` every 2 seconds and waits while it is true.
Turn activity is any of:
- an inbox item for any role other than `housekeeping`;
- an assistant run in `running` or `stopping`;
- a journal row in `processing` for any other role.

A run or journal row older than 30 minutes does not count. Both are settled
when their worker ends, so an older one is a row a dead process left behind.

**A new message kills the model call in progress.** While a job runs, a
`TurnWatch` thread asks the same question every 2 seconds. When the answer is
yes and the main thread is inside the model call, it signals the main thread
(`SIGUSR1`), whose handler raises `Preempted`. The stream is closed on the way
out, so the model server stops generating. The attempt is recorded as
`killed: turn`, the job becomes `deferred`, and it runs again when the system
is idle. There is no cap on kills and no back-off.

Two details make the kill safe:
- **`Preempted` is a `BaseException`.** The model-fallback loop treats any
  `Exception` as one model failing and tries the next. A kill must not do
  that.
- **Only the model call is interruptible.** The summarizer wraps its call in
  `call_guard`, which the worker sets to `TurnWatch.interruptible`. Database
  writes made from inside the call (the reply so far) run under
  `TurnWatch.shielded`, so the signal never lands inside a commit. A turn seen
  during a shielded write raises when the write is done.

## Failures

- **A failed attempt is retried once**, at once, since the system is idle.
  After a second failure the job is `final` with `failed: <reason>`. Nothing
  falls back to another job or another slot.
- **A final failure can be enqueued again** from the job's page. Its earlier
  attempts stay.
- **Refresh summary** on a run's page asks for the job again with `force`,
  which puts a finished job back in the queue. The new call is a new attempt
  of the same job.
- **A run that no longer exists** makes its job `final` with
  `skipped: run not found`.

## Nothing is lost when a process dies

The inbox item is a hint. The rows say what is due.
- **A job left `running`** by a dead worker goes back to `waiting`, and its
  open attempt is closed as `failed` with the reason. The worker does this
  when it starts, and the supervisor does it at startup.
- **The sweep** creates a summary job for every run that ended in the last 24
  hours with no summary and no job, at most 10 per pass. It runs each time
  the worker handles an item, and at supervisor startup. So an enqueue lost
  in a crash delays a summary and does not lose it.
- **At startup the supervisor wakes the worker** if any job is waiting.

## The tables

`housekeeping_job`: one row per job.

| column | meaning |
|---|---|
| `job_type`, `tier` | what it is, and which tier it runs in |
| `run_uuid`, `room_uuid`, `subject`, `job_version` | what it is for |
| `state` | `waiting`, `running`, `deferred`, `final` |
| `outcome` | for a final job: `done`, `skipped: …`, `failed: …` |
| `deferred_reason` | `turn` after a kill |
| `retries` | failures already retried; a kill is not one |
| `bumped_at` | set when the run's page is opened |
| `created_at`, `finished_at` | |

`housekeeping_attempt`: one row per try, appended.

| column | meaning |
|---|---|
| `job_uuid`, `started_at`, `ended_at` | |
| `result` | NULL while running; `done`, `skipped`, `failed`, `killed: turn` |
| `model`, `model_ms` | the model that answered, and the attempt's wall time |
| `llm_call_uuid` | the stored call, which holds the prompt and the reply |
| `response_preview` | the reply so far, written every 2 seconds while the call streams |
| `error` | for a failed attempt |

`room_chunk`: one row per summarized chunk.

| column | meaning |
|---|---|
| `room_uuid`, `chunk_size`, `overlap`, `prompt_version`, `seq` | the room, the series, and the chunk's place in it |
| `first_message_id`, `last_message_id`, `message_count` | its edges and size |
| `content_hash` | over its messages' ids and texts when it was written |
| `summary`, `topics` | the chunk's subjects, joined, and broader topics |
| `parts` | its parts: `{first_message, last_message, summary, topics, requests}`, and from prompt 3 `result`, the reactions, `calibration`, `signals`; from prompt 4 `calibration_basis`; from prompt 6 `audit`; from prompt 7 the reactions are read by rule; from prompt 12 `asked_again_at`; NULL for a chunk of the first prompt |
| `problems` | what was off in the reply, and in the check's reply |
| `audit` | the check: `{attempts, rewritten, error}`, and `repeat_check`, what the decision model was asked; NULL for a chunk written before the check existed |
| `model`, `job_uuid` | what wrote it |

The prompt and the reply are never copied. A job's page reads them from the
attempt's `llm_call` row: for a chunk, the summary that was kept.

## The pages

**Assistant → Housekeeping** (`/assistant/housekeeping`).
- The top line says what is running now and for how long, how many jobs
  are pending, and how many have failed.
- The log is one line per job, newest first: when, job, chatroom and
  message range, model time and attempts, outcome. A line names no model
  and holds no links: the whole line opens the job. A job that is done has
  no outcome pill. A pill is for what is still going or went wrong.
- It filters by outcome, job type and room.
- **Pause** and **Resume**, at the end of the top line. Pause sets
  `housekeeping.paused`. The job that is running finishes. After that the
  queue gives out one kind of job only: the assistant summary, which is the
  last step of a run and what the run's page waits for. Every other job
  stays pending, and new ones are still queued. Resume clears the setting
  and wakes the worker, which carries on from where the queue stood.

**A job** (`/assistant/housekeeping/<uuid>`).
- What it is for, with links to the room, its chunks, the run and the
  request. Its heading says on hover what that kind of job does and where
  its result is read.
- A timeline, as the one at the top of a run's page on `/assistant`: one
  row for each thing that happened, in the order they began, with a bar
  where it took time. The rows are each attempt (a mark at its start: its
  time is on the timeline as the calls it made; the first is named `start`,
  a later one `attempt 2` and so on), each request to the
  decision model (`duplicate m<n>`, after the message it is about), each
  model call the attempt made (`db.housekeeping_attempt_calls`), named
  `summary` or `check`, and the call still running. A chunk job that wrote
  its chunk has `result` as the last row, a mark where the job finished. A chunk's attempt makes
  two to four model calls: a second summary and check when the check had
  the summary written again.
- Under the timeline, the row that is selected, in full. A click on a row
  selects it, and the address then names it (`#ev-<key>`), so a row can be
  linked to. The selection holds across the page's live reload. The page
  opens on the call still running, else on the first model call.
  - An attempt: how it ended, when it began, and its error.
  - The start of a chunk job also lists the chunk's messages in a table:
    each one's number, its uuid as a link to it in the chatroom, who wrote
    it (`user` or `agent`), and its text. A long text shows its beginning
    and unfolds.
  - A model call: a link to the call on `/activity`, the call's figures as
    a call's meta line reads on `/assistant` (model, tokens in and out,
    cached, prefill, decode, throughput, duration), its prompts, and its
    reply.
  - A request to the decision model: the verdict, and what its row on
    `/activity` shows.
  - The call still running: its reply so far.
  - The result: the chunk the job wrote (`db.job_room_chunk`), as the
    room's page shows a chunk, with a link to it there.
- A prompt and a reply are the text blocks a pane on `/assistant` is made
  of (`webapp/text_blocks.py`): a prompt is folded under its character
  count, every block has a copy button, a long one is cut to a few lines
  with a toggle, and a reply that is JSON has the raw and pretty switch.
- A chunk job asks the decision model at the start of an attempt, before
  its own model call. A chunk's last request is compared with the next
  message from the user, which lies after the chunk's range; that request
  is marked "after the chunk".
- For a final failure, an Enqueue again button.

**A room's chunks** (`/assistant/housekeeping/room/<uuid>`), reached from
the `chunks` link beside the room's name on a job's page.
- How many messages the room has, how many are summarized, and how many
  chunks are queued or due.
- One card per chunk: its messages' numbers and dates, its phrase and
  topics, and the messages themselves, folded. The card names no model. An
  inspect link in its corner opens the job that wrote it: the models,
  the prompts and replies, and what the decision model was asked.
- A chunk's heading links to the chunk itself, in the series shown:
  `?size=…&overlap=…&prompt=…#chunk-N`. The chunk the address names is
  outlined.
- Under it, each part: its message range, what was wanted, what came of it,
  the calibration in colour with its basis, the reactions, and the records
  in grey. A part the check still doubts says which of its request and
  result, and why.
- The messages the decision model called asking again, each with its
  probability, and under the chunk how many follow-ups were put to the
  decision model and how many it answered.
- Whether the chunk was written once or twice, and for a second writing,
  what the first answer had in the parts the check found wrong.
- A chip per series the room has chunks in, earlier prompts included.
- **Summarize the rest**, with the number of chunks it will write.
- **Show: summaries | as in the prompt.** The second (`&view=prompt`) puts
  the series' chunks into one `<room_earlier>` block of text, in place of the
  cards: a `<chunk>` per chunk with its message range, dates, topics and
  phrase, and inside it a `<part>` per part with its range, topics, how many
  times it was asked, its calibration, what was wanted and what came of it.
  `agents/room_chunks.render_room_earlier` writes it from the stored rows.
  A topic name is stored with hyphens between its words and written here
  with spaces, the names apart by a comma with no space after it.
  The page gives the block's number of characters and a copy button. A
  table above it gives the size of the messages the chunks cover (the
  user's, the replies, both together), then the summaries' sentences
  alone and the whole block, each as a share of that. It is
  how a prompt is going to hold the summaries; no prompt holds them yet.

The room's page is reached from the room itself: the chat sidebar's
**Context** panel lists the pages that show what the assistant is given
about the room, and **Chatroom chunk summaries** is its first entry.

**A run's page** has an **After the turn** group below its timeline, one line
per job that is not done (waiting, running, stopped by a new message, or
failed), each linking to the job's page. A job that is done has no line: its
model call is already a step on the timeline. The run list says
`summary failed` for a run whose summary job failed.

**Staying current.** The housekeeping pages poll
`/assistant/housekeeping/api/revision` every 2 seconds and reload their own
content when the value moves. Open prompt blocks stay open across a reload.
The log and a room's page follow the whole log. A job's page polls with
`?job=<uuid>`, a value over that job and its attempts alone, so it reloads
when its own job moves and not while another one runs. It stops polling
once its job is final: nothing on the page can change then, and a job that
is enqueued again loads the page anew.

## Adding a job type

1. Add it to `HOUSEKEEPING_JOB_TYPES` in `db/housekeeping.py`, with its tier
   and the label the page prints.
2. Create its rows in `HousekeepingAgent._expand`, or in the sweep when the
   records alone say it is due.
3. Add its runner to `HousekeepingAgent._RUNNERS`. A runner gets the job, the
   attempt and the `TurnWatch`, wraps its model call in
   `watch.interruptible()`, and returns the outcome text.
