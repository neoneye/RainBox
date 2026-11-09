# Post-turn housekeeping: one queue, in order, never during a turn

**Status:** Proposal. Nothing here is built. The run summarizer exists.
**Date:** 2026-11-09
**Related:**
- [The decision-model foundation](2026-11-09-decision-model-foundation.md):
  the slots, and the residency and heat rules.
- [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md),
  [Later verdicts](2026-11-09-later-verdicts-on-earlier-messages.md) and
  [The room digest](2026-11-09-room-digest.md): each adds work that runs
  after a turn.
- `notes/supervisor-design.md`: roles, workers, and the inbox.

## The problem

Today one job runs after a turn: the run summarizer. The assistant enqueues
it at every terminal state, and the summarizer role drains it in its own
process. The proposals in this series add more:

| job | from | model | when it matters |
|---|---|---|---|
| run summary | exists | generative | when the inspector is read |
| reply checks on the delivered reply | repeat, decision 6 | `repeat_check` | diagnostics |
| identity experiment | repeat, decision 1 | `repeat_check` + `repeat_confirm` | diagnostics |
| verdict detection | verdicts, decision 2 | `repeat_confirm` | before the next turn, ideally |
| hedge flags | verdicts, decision 2 | `repeat_confirm` | before the next turn, ideally |
| topic naming, after a turn-start `none` | digest, decision 2 | generative, short | before the next turn, ideally |
| digest chunk node | digest, decision 5 | none | once the window has passed the chunk |
| digest `asked` phrase, if enabled | digest, decision 5 | generative | eventually |
| digest backfill, if the operator runs it | digest, decision 5 | generative | eventually |

Three things go wrong if each job is simply enqueued to a role of its own:

- **They compete with the next turn for the GPU.** The supervisor runs one
  process per role, in parallel across roles. A summarizer call can already
  overlap the next turn's decide call today. These jobs would overlap it
  routinely, slowing the turn the operator is waiting on.
- **Heat.** This laptop has been repaired twice after heat damage.
  Uncoordinated jobs after every turn put a load on it that nobody chose.
- **Freshness.** Topic status and digest chunks are derived when rendered,
  so the order of jobs never makes them wrong. But a verdict detected after
  the next turn has started is missing from that turn. Jobs the next turn
  reads must not wait behind jobs nobody is waiting for, and independent
  roles give no priority.

## The design

### 1. One housekeeping role, one queue, two tiers

A new `housekeeping` role replaces the separate post-turn enqueues. When a
run reaches a terminal state, the assistant enqueues **one** item:
`{"run_uuid", "room_uuid", "request_message"}`. The housekeeping worker
expands it into that run's jobs, in two tiers.

**Tier 1, for the next turn.** Decision-model calls, one short generative
call, or none, so seconds in total:
1. **Fast detectors**, in one request each: reply checks on the delivered
   reply.
2. **Careful detectors**: verdict detection, hedge flags, and the identity
   experiment.
3. **Topic naming**, when the turn-start topic Choice said `none`.
4. **Digest chunk nodes** that became due. No model call.

**Tier 2, eventually.** Generative calls of up to two minutes:
1. **Run summary.** The existing summarizer logic, moved behind this role.
   It comes first in its tier, so it waits only for tier 1's seconds.
2. **Digest `asked` phrases**, when enabled. At most one per run.
3. **Digest backfill**, when the operator has started it for a room.

**One worker, one job at a time.** Every tier 1 job, in every room, runs
before any tier 2 job. Within a tier, the oldest run goes first, and runs of
one room go in the order they finished. So a backfill or a slow summary never
delays a fresh run's verdicts.

**One dependency is kept.** An `asked` phrase waits for topic naming in its
range, so it can be given the topic titles. Nothing else depends on order,
because status and chunk contents are derived when read.

**Each job is idempotent.** It is keyed by
`(job_type, run_uuid, subject, job_version)`. A job whose key already has a
result is skipped, so a crash, a restart, or a duplicate enqueue never runs
the same work twice.
- **Digest jobs are keyed by their chunk**, as
  `(job_type, room_uuid, first_message, last_message)`, with no version. A
  chunk's node and text are written once, so a new job version applies only
  to chunks not yet written.
- **A generative job drops its result if its input changed while it ran.**
  An `asked` phrase whose chunk's `content_hash` no longer matches is not
  stored, and is recorded as `dropped: input changed`.

### 2. Never while a turn is in progress

Before starting each job, the worker checks for **turn activity**: any
`assistant_run` with status `running`, or any pending inbox item for an
assistant role. If there is activity, it waits, polling every 2 seconds,
and starts the job only once the system is idle.

**A generative call running when a turn starts is cancelled.** The worker
keeps polling every 2 seconds while a call runs. On turn activity it closes
its request to the model server, records `deferred: turn`, and requeues the
job. That is safe because every job is idempotent. The builder confirms that
closing the request stops generation on the server, not only in the client.

**Cancellation is capped at two per job.** While the operator chats actively,
a turn can start before a summary finishes, again and again. Each cancelled
attempt has already spent its heat, so endless cancelling costs more than
finishing. After two cancellations, the third attempt runs to completion even
if a turn starts, and its deadline (decision 4) bounds the overlap. Each
cancellation is recorded with the model time it wasted.

A decision-model request is allowed to finish. Its 4-second deadline
(decision 4) bounds the overlap.

**Fast, on-path work stays on the path.** The turn-start request profile and
the draft checklist are part of the turn, not housekeeping. The foundation
caps them at two decision calls per turn.

### 3. A heat and time budget

- **Generative calls run in bursts of at most 10, then cool down for 60
  seconds.** The operator's measure of what this machine takes without
  overheating is 10 to 15 calls in a row, followed by a 60-second sleep, so
  the burst uses the low end. On top of that, generative housekeeping uses
  at most 10 minutes of model time per rolling hour. Decision-model calls
  do not count against this budget, but their seconds are recorded in the
  same ledger, because they run on the same GPU.
- **When the budget is exhausted, generative jobs wait** and decision-model
  jobs continue. Digest backfill defers first, then `asked` phrases, because
  nobody is waiting for either.
- **Thermal pressure pauses all generative housekeeping.** macOS reports a
  CPU speed limit and thermal warnings (`pmset -g therm`, or
  `NSProcessInfo.thermalState` through a small helper). When either
  indicates throttling, generative jobs pause until it clears. On this
  machine `pmset -g therm` currently reports "No thermal warning level has
  been recorded". Its throttled format has to be confirmed by the builder
  under load.

### 4. Deadlines, failures, and no cascades

| job | deadline |
|---|---|
| run summary | 120 s (today's behaviour) |
| a decision-model request | 4 s, including load and queue |
| topic naming | 30 s |
| a digest chunk node | no model; one database write |
| an `asked` phrase, or one backfill chunk | 120 s |

- **A failed or timed-out job is recorded** with its reason, and retried
  once at the next idle period. After that it is final, and nothing falls
  back to another model. The operator can re-enqueue a final failure from
  /assistant. A job cancelled for a turn (decision 2) is requeued, and that
  does not count as a retry.
- **What a downstream job does without its input** is defined by the
  feature, never guessed. A chunk with no `asked` phrase renders from
  records alone. A status without a verdict job's result simply has fewer
  signals.
- **Switching a feature off** cancels its pending jobs before they start.
  Results already written stay.

### 5. Shown where it happened

On /assistant, a run's page gains an **After the turn** group below its
timeline. It lists each housekeeping job:
- its start time and duration;
- its model;
- its outcome (`done`, `skipped: already done`, `deferred: budget`,
  `deferred: thermal`, `deferred: turn`, `dropped: input changed`,
  `failed: reason`);
- links to what it wrote.

These are later events, so they are shown after the turn's own rows and
dated, never inside its steps.

The run list gets a small indicator for runs whose housekeeping is still
pending or failed. The global housekeeping state (queue depth, budget used
this hour, thermal state) gets one line on the /assistant index page.

## Measuring it

- **Interference.** The decide call's prefill and total time, with
  housekeeping on and off, over a week of normal use. The target is no
  measurable difference.
- **Lag.** For each job type, the time from the run finishing to the job
  being done. Tier 1 should normally be done before the operator's next
  message.
- **Cancellations.** How often a generative call is cancelled for a turn,
  and how much model time that wastes.
- **Budget use.** The generative minutes per hour, and how often jobs were
  deferred for budget or heat.

## Rollout

1. The `housekeeping` role, with the run summary moved into it, unchanged in
   behaviour. That is the gate, the order and the after-the-turn view, with
   one job.
2. Each feature's jobs join as that feature reaches its recording phase, in
   the roadmap's order.
3. The budget and thermal pause, before the first generative job beyond the
   summary: topic naming, `asked` phrases or backfill.

## What this does not do

- **Schedule cron jobs or kanban workers.** They have their own roles. If
  they also turn out to compete with turns, the same activity gate can
  extend to them, decided separately.
- **Make housekeeping results visible to the turn that produced them.**
  They serve later turns and the operator.
