# Housekeeping — design

Housekeeping is the work that follows a turn. It has two jobs: the run
summary, and the room chunk summary. The role, its queue and its page are
built so that later jobs (detectors) join the same queue.

The two summaries are different things:
- **The run summary** describes one assistant run: what triggered it, the
  obstacles in its steps, and its outcome.
- **The room chunk summary** describes a stretch of a room's messages: what
  was asked, in kind, and topic names. It is the compaction of a long room.

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

**A series per pair of settings.** A chunk belongs to the series of its
`(chunk_size, overlap)`. Changing either setting starts a new series from the
room's first message, beside the old one. Both stay readable on the room's
page, which is how two sizes are compared.

**When jobs are queued.**
- When a run ends, the next 2 due chunks of its room, if
  `housekeeping.room_summaries` is on (the default).
- When the operator presses **Summarize the rest** on a room's page, every
  due chunk of the current series, up to 200.

**The call.** One plain-text call per chunk on the `assistant.room_summarizer`
slot, which falls back to `assistant.default`. The prompt holds the chunk, the
context on each side marked as not to be described, and the topic names
earlier chunks of the series used. The reply is two labelled lines:

```
asked: conversions between units, and tasks on a board
topics: unit-conversion, kanban
```

- `asked` says what kind of thing was asked, in at most 120 characters. The
  prompt forbids stating an answer, a value or a name from a reply.
- `topics` are one to four names, reused from earlier chunks when the subject
  is the same.
- A reply with no `asked:` line is sent back to the model once with the
  reason. Anything else off-format is stored in `problems` and shown.

**Local models only.** The call reads the room's own messages, so members of
the group that are not on a loopback endpoint, or are `:cloud` names, are left
out. With none left the job fails and says so.

**What is stored.** One `room_chunk` row per chunk, written once: its series
and `seq`, its edges as message ids, a `content_hash` of its messages, `asked`,
`topics`, `problems`, the model and the prompt version. The page recomputes
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
| `room_uuid`, `chunk_size`, `overlap`, `seq` | the room, the series, and the chunk's place in it |
| `first_message_id`, `last_message_id`, `message_count` | its edges and size |
| `content_hash` | over its messages' ids and texts when it was written |
| `asked`, `topics`, `problems` | what the model wrote, and where its reply strayed from the format |
| `model`, `prompt_version`, `job_uuid` | what wrote it |

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
- One card per chunk: its messages' numbers and dates, `asked`, the topics,
  the model, a link to the job's prompt and reply, and the messages
  themselves, folded.
- A chip per series the room has chunks in.
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
