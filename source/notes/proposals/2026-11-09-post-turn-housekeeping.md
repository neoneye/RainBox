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
process. The proposals in this series add six more:

| job | from | model | when it matters |
|---|---|---|---|
| run summary | exists | generative | before the inspector is read |
| reply checks on the delivered reply | repeat, decision 6 | `repeat_check` | diagnostics |
| identity experiment | repeat, decision 1 | `repeat_check` + `repeat_confirm` | diagnostics |
| verdict detection | verdicts, decision 2 | `repeat_confirm` | before the next turn, ideally |
| hedge flags | verdicts, decision 2 | `repeat_confirm` | before the next turn, ideally |
| topic naming, after a turn-start `none` | digest, decision 2 | generative, short | before the next turn, ideally |
| digest chunk summaries | digest, decision 5 | generative | eventually |

Three things go wrong if each job is simply enqueued to a role of its own:

- **They compete with the next turn for the GPU.** The supervisor runs one
  process per role, in parallel across roles. A summarizer call can already
  overlap the next turn's decide call today. Seven jobs from three proposals
  would overlap it routinely, slowing the turn the operator is waiting on.
- **Heat.** This laptop has been repaired twice after heat damage. Seven
  uncoordinated jobs per turn put a load on it that nobody chose.
- **Order.** A verdict must be detected before the digest derives topic
  status from it. A topic must be named before the chunk that contains it is
  summarized. Independent roles give no order.

## The design

### 1. One housekeeping role, one queue, ordered per room

A new `housekeeping` role replaces the separate post-turn enqueues. When a
run reaches a terminal state, the assistant enqueues **one** item:
`{"run_uuid", "room_uuid", "request_message"}`. The housekeeping worker
expands it into that run's jobs, in a fixed order:

1. **Run summary.** The existing summarizer logic, moved behind this role.
2. **Fast detectors**, in one request each: reply checks on the delivered
   reply.
3. **Careful detectors**: verdict detection, hedge flags, and the identity
   experiment.
4. **Topic naming**, when the turn-start topic Choice said `none`.
5. **Digest chunks** that became summarizable. At most one generative chunk
   call per run.

**One worker, one job at a time.** Jobs for different rooms interleave by
run age, oldest first. Jobs of one run always run in the order above, and runs
of one room in the order they finished. This is the dependency order: status
reads verdicts, and chunks read topics.

**Each job is idempotent.** It is keyed by
`(job_type, run_uuid, subject, job_version)`. A job whose key already has a
result is skipped, so a crash, a restart, or a duplicate enqueue never runs
the same work twice.

### 2. Never while a turn is in progress

Before starting each job, the worker checks for **turn activity**: any
`assistant_run` with status `running`, or any pending inbox item for an
assistant role. If there is activity, it waits, polling every 2 seconds,
and starts the job only once the system is idle. A job already running when
a turn starts is not killed. Every job has a deadline (decision 4) short
enough that the overlap is bounded.

**Fast, on-path work stays on the path.** The turn-start request profile and
the draft checklist are part of the turn, not housekeeping. The foundation
caps them at two decision calls per turn.

### 3. A heat and time budget

- **Generative housekeeping is budgeted:** at most 10 minutes of generative
  model time per rolling hour. Decision-model calls are cheap and are not
  counted.
- **When the budget is exhausted, generative jobs wait** and decision-model
  jobs continue. Digest chunks are the first to defer, because they are the
  only job nobody is waiting for.
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
| a digest chunk | 120 s |

- **A failed or timed-out job is recorded** with its reason, and retried
  once at the next idle period. After that it is final, and nothing falls
  back to another model.
- **What a downstream job does without its input** is defined by the
  feature, never guessed. A chunk whose topics are missing renders as a
  topic-less chunk. A status without a verdict job's result simply has fewer
  signals.
- **Switching a feature off** cancels its pending jobs before they start.
  Results already written stay.

### 5. Shown where it happened

On /assistant, a run's page gains an **After the turn** group below its
timeline. It lists each housekeeping job:
- its start time and duration;
- its model;
- its outcome (`done`, `skipped: already done`, `deferred: budget`,
  `deferred: thermal`, `failed: reason`);
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
  being done. Verdicts and topic naming should normally be done before the
  operator's next message.
- **Budget use.** The generative minutes per hour, and how often jobs were
  deferred for budget or heat.

## Rollout

1. The `housekeeping` role, with the run summary moved into it, unchanged in
   behaviour. That is the gate, the order and the after-the-turn view, with
   one job.
2. Each feature's jobs join as that feature reaches its recording phase, in
   the roadmap's order.
3. The budget and thermal pause, before the first generative job beyond the
   summary: topic naming or digest chunks.

## What this does not do

- **Schedule cron jobs or kanban workers.** They have their own roles. If
  they also turn out to compete with turns, the same activity gate can
  extend to them, decided separately.
- **Make housekeeping results visible to the turn that produced them.**
  They serve later turns and the operator.
