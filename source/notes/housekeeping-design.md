# Housekeeping — design

Housekeeping is the work that follows a turn. It has two jobs: the run
summary, and the room chunk summary. The role, its queue and its page are
built so that later jobs (detectors) join the same queue.

The two summaries are different things:
- **The run summary** describes one assistant run: what triggered it, the
  obstacles in its steps, and its outcome.
- **The room chunk summary** describes a stretch of a room's messages: its
  parts, each with what was wanted, what came of it and how well calibrated
  the assistant was, under one broader phrase and broader topics. It is the
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
| The run summary's model call | `agents/assistant_run_summarizer.py` |
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

## Room chunk summaries

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
| `run resolved` / `partial` / `failed` | the run summary's outcome for the reply's run, else the run's status |
| `criteria met` / `criteria not met`, and how many revisions | the reply audit's verdicts for that run: met when the last one was to send the reply |
| `voted up` / `voted down`, with the comment | the human's latest vote on the message |

The signals are records, and the prompt says so. They are what lets a small
model say something about reliability without judging an answer from its own
knowledge.

**The call.** One structured call per chunk on the `assistant.room_summarizer`
slot, which falls back to `assistant.default`. The prompt holds the chunk and
the context on each side, marked as not part of it. The reply is a breakdown
in two levels:

| level | fields |
|---|---|
| each part | `first_message`, `last_message`, `summary`, `result`, `human_reaction`, `topics` (one or two) |
| the chunk | `summary`, `topics` (one to three, broader) |

A part is a run of consecutive messages about one subject. The parts come
first in the schema, so the chunk's phrase is written with them in view.

- **`summary`** is what was wanted.
- **`result`** is what came of it: the one main thing found or concluded, or
  that nothing was.
- **`human_reaction`** is a choice: `moved_on`, `asked_again`, `corrected`,
  `pushed_for_more`, `thanked`, `not_shown`. It is something the model can
  read off the messages.

**Calibration is decided by rule, not by the model** (`room_chunks.calibrate`).
The model supplies what it is good at: the result, and the human's reaction.
Asked for the calibration too, it called an answer that was repeated
unchanged while the human kept asking "calibrated", with the evidence in its
own prompt. The rule reads the part's reactions, the text of its requests,
and the records. The first line that applies decides:

| when | calibration | basis shown |
|---|---|---|
| a reply was voted down | overconfident | a reply was voted down |
| the human corrected a reply | overconfident | the human corrected a reply |
| asked again, and the first run did not resolve it but the last did | underconfident | it delivered only after being asked again |
| asked again, and a run resolved it | overconfident | it answered, and the human kept asking |
| asked again, and nothing was delivered | unclear | asked again, and nothing was delivered |
| a reply was voted up | calibrated | a reply was voted up |
| no reaction in the chunk | unclear | the chunk shows no reaction |
| the human went on, and the last run resolved it (or no run is recorded) | calibrated | the human went on without contesting it |
| the human went on, and the last run did not resolve it | unclear | the human went on, and the last run was … |

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
`signals`, counted by code: repeated and bare requests, read from their
text, and from the records of its replies the runs by outcome, the first and
the last run's outcome, replies that did not meet their criteria, revisions,
votes up and down. The page shows the calibration in
colour with its basis, the reactions the model read, and these counts in
grey. A result is an old answer, and it is never shown without them.

**What code settles after the reply** (`room_chunks.settle`):
- Topic names are normalized to lowercase words joined by hyphens.
- A part whose message numbers lie outside the chunk keeps its text and loses
  its numbers.
- Neighbouring parts with the same first topic are joined into one, which
  records how many it stands for in `requests`. The joined part keeps the
  first phrase, the result of the last (where the subject ended up), and
  every reaction along the way.
- Each part is extended to the message before the next part, and the last to
  the chunk's end, so the parts cover the chunk.
- Each part's `signals` are counted, and its calibration is decided.
- Anything else off (too many topics, an over-long or empty phrase, no parts)
  is listed in `problems` and shown with the chunk.

**The prompt has no sample answers and no list of earlier topic names.** A
small model copies both. With a list of earlier names to reuse, it chose
from the list whether or not a name fitted, and returned the list's
placeholder word as a topic. So topic names are not shared between chunks:
the same subject can get two names in two chunks. Recognizing them as one
topic is a later step, and not this call's job.

**What it does well and badly**, on gemma4:e4b-mlx, 4 to 8 seconds a chunk:
- **Parts, results and topics are good.** Ranges are right and the topics
  name the subject.
- **The reaction is read unevenly.** `corrected` and `moved_on` are usually
  right; `asked_again` is not, which is why the rule reads repeats from the
  text.
- **A correction in a later chunk is not seen.** The part it corrects stays
  as it was written, because a chunk is summarized once and alone.
- **The chunk's own phrase is weak** when its parts are unrelated.
- **Phrases still begin with who spoke**, though the prompt asks otherwise.

**Local models only.** The call reads the room's own messages, so members of
the group that are not on a loopback endpoint, or are `:cloud` names, are left
out. With none left the job fails and says so.

**What is stored.** One `room_chunk` row per chunk, written once: its series
and `seq`, its edges as message ids, a `content_hash` of its messages,
`summary`, `topics`, `parts`, `problems`, the model and the prompt version. The page recomputes
the hash and marks a chunk whose messages have changed since.

**Where it goes.** The summaries are shown on the room's page. They reach no
prompt.

**A rest after ten calls.** The worker counts its model calls, of any job
type. After 10 in a row it rests for 60 seconds before the next, and the
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
| `summary`, `topics` | the chunk's phrase and broader topics |
| `parts` | its parts: `{first_message, last_message, summary, topics, requests}`, and from prompt 3 `result`, the reactions, `calibration`, `signals`; from prompt 4 `calibration_basis`; NULL for a chunk of the first prompt |
| `problems` | what was off in the reply |
| `model`, `job_uuid` | what wrote it |

The prompt and the reply are never copied. A job's page reads them from the
attempt's `llm_call` row.

## The pages

**Assistant → Housekeeping** (`/assistant/housekeeping`).
- The top line says what is running now and for how long, how many jobs wait
  in each tier, and how many have failed.
- The log is one line per job, newest first: when, job, room and run, model,
  model time and attempts, outcome.
- It filters by outcome, job type and room.

**A job** (`/assistant/housekeeping/<uuid>`).
- What it is for, with links to the room, the run and the request.
- Every attempt in order: how it ended, its model and time, its prompts, and
  its reply. A running attempt shows the reply so far.
- For a final failure, an Enqueue again button.

**A room's chunks** (`/assistant/housekeeping/room/<uuid>`), reached from
the `chunks` link beside a room's name in the log.
- How many messages the room has, how many are summarized, and how many
  chunks are queued or due.
- One card per chunk: its messages' numbers and dates, its phrase and
  topics, the model, a link to the job's prompt and reply, and the messages
  themselves, folded.
- Under it, each part: its message range, what was wanted, what came of it,
  the calibration in colour with its basis, the reactions the model read,
  and the records in grey.
- A chip per series the room has chunks in, earlier prompts included.
- **Summarize the rest**, with the number of model calls it will make.

**A run's page** has an **After the turn** group below its timeline, one line
per job, each linking to the job's page. The run list says `summary failed`
for a run whose summary job failed.

**Staying current.** Both housekeeping pages poll
`/assistant/housekeeping/api/revision` every 2 seconds and reload their own
content when the value moves. Open prompt blocks stay open across a reload.

## Adding a job type

1. Add it to `HOUSEKEEPING_JOB_TYPES` in `db/housekeeping.py`, with its tier
   and the label the page prints.
2. Create its rows in `HousekeepingAgent._expand`, or in the sweep when the
   records alone say it is due.
3. Add its runner to `HousekeepingAgent._RUNNERS`. A runner gets the job, the
   attempt and the `TurnWatch`, wraps its model call in
   `watch.interruptible()`, and returns the outcome text.
