# Muted cron jobs and the schedule they keep

**Date:** 2026-08-05
**Status:** Proposal. Nothing was changed — production is exactly as it was found.

## The finding

A job named **"Orig"** sits in production: `* * * * *`, `action_type=command`,
`command=ls`, `enabled=true`, created 2026-06-06, `next_run_at` frozen at
2026-06-06 03:57, `last_fired_at` **never**. An every-minute job that has not
fired in two months.

It is not misbehaving. It lives in the folder **"testing"**, which is disabled.
`_cron_job_effective_enabled` (`db/cron.py:576`) requires the job *and every
ancestor folder* to be enabled, so `cron_tick` skips it — and skips it with a
`continue` placed **before** any of the scheduling branches, so its
`next_run_at` was never advanced past the moment the folder was muted.

Everything here is working as written. What follows is about whether "as
written" is what we want.

## What is genuinely fine

Worth stating plainly, because it narrows the problem:

- **The `/cron` UI is already correct.** `cronNextRunCell` (`static/cron.js:211`)
  checks `cronJobLive(r)` first and renders `—` for a job muted by its own or an
  ancestor toggle. The operator never sees a stale date on the page. Its comment
  even says it shows "why it WON'T fire instead".
- **Skipping muted jobs is right.** A disabled folder should mute everything
  under it; that is the feature.
- **Not replaying two months of missed minutes is right.** `cron_tick` fires at
  most once per due job and advances to the next future slot. Nobody wants 86,400
  catch-up runs.

## The two things that are not fine

**1. Un-muting fires immediately, however old the schedule is.**

Enable the "testing" folder and, on the next tick, `Orig` has
`next_run_at <= now` — so it fires straight away, running `ls`, before its first
real slot. Then every minute after. The operator's action was "un-hide this
folder"; the consequence is "start executing a two-month-old command now".

This *is* consistent with how global pause behaves — the tick's docstring calls
it deliberate: "resume behaves like wake-from-sleep: each due job catches up with
at most one fire". That reasoning is sound for a pause measured in minutes, where
catch-up is the whole point. It transfers badly to a mute measured in months,
where the missed slot has no meaning left.

**2. The stored row contradicts itself outside the UI.**

`enabled=true` with a `next_run_at` two months in the past reads, in `psql` or
Flask-Admin, as a broken scheduler. The page knows better; the row does not. That
is how this was found in the first place — while looking for something that might
be consuming resources.

## The design question

**Should a muted job keep a schedule, and should un-muting replay the slot it
missed?**

## Options

### A. Advance the schedule while muted, never fire (recommended)

In `cron_tick`, when `_cron_job_effective_enabled` is false and `next_run_at`
has passed, roll it forward to the next future slot and continue — do not fire,
do not create a run row, do not post a cron event.

This is not a new idea; it is the branch immediately below, applied one case
earlier. Draft jobs already do exactly this: *"Draft: roll the schedule forward
silently (no run row, no event spam); it starts firing once its action is filled
in."* A muted job is the same shape — present, scheduled, deliberately not
running yet.

- Un-muting fires at the next genuine slot, never retroactively.
- `next_run_at` keeps a truthful meaning: *when this would fire if you un-muted
  it*. The row stops contradicting itself.
- Cost is one `UPDATE` per muted job per its own interval — for `* * * * *`,
  one write a minute. Negligible, and it stops as soon as the row is ahead of
  `now`.
- One branch in one function. No schema change, no new state, no transition
  detection.

Deliberate divergence: **global pause keeps its catch-up behavior**, untouched.
Pause is short and an operator is standing there; folder-mute is structural and
open-ended. If that split later feels wrong, the pause side is the one to
revisit, not this.

### B. Clear `next_run_at` when a job becomes muted

Null it at the moment of muting; the existing `next_run_at is None` branch then
schedules it on un-mute *without firing* — the semantics we want, from code that
already exists.

The problem is where the hook goes. There is no folder-toggle endpoint: folder
`enabled` rides the whole-tree PUT (`cron_save_tree`, `db/cron.py:399`), so this
needs old-vs-new transition detection inside a bulk upsert, for every folder,
including transitively-affected descendants. More machinery than A, in the more
dangerous function, to reach the same outcome. It also throws away the "when
would this have fired" information that A preserves.

### C. Leave the mechanism; make the row legible

Keep the behavior; teach Flask-Admin and the tree payload to expose *effective*
enablement, so a row muted by an ancestor says so wherever it is read.

Worth doing regardless as a small improvement, but on its own it documents the
surprise rather than removing it. Un-muting still runs a two-month-old `ls`.

## Recommendation

**A, plus the reporting half of C.**

A is a handful of lines mirroring a decision the codebase already made for
drafts, and it removes the only behavior here that can actually surprise
someone. C's reporting half — surfacing effective-enabled where rows are read
raw — is cheap and prevents the next person from re-deriving this whole
investigation from a suspicious `psql` row.

Testing worth writing with it: a muted job whose slot passes advances and does
not fire; un-muting it fires at the next real slot rather than immediately; a
job muted by an *ancestor* (not its own flag) behaves identically; and global
pause still catches up on resume, so A did not leak into it.

## The specific job

`Orig` is a test artifact — `command=ls`, in a folder called "testing", never
fired, untouched since 2026-07-07. **Delete it.**

Note what happens if it is simply left: it is harmless *only while "testing"
stays disabled*. Enabling that folder — for an unrelated reason, months from
now — starts an every-minute `ls`, and under option A it would still start
firing every minute, just not retroactively. A is about removing the surprise
*timing*, not about keeping dead jobs safe.

This was not deleted, because it is production data and the ask was for a
document.

## Open questions

- **Should un-muting be announced?** The cron room already gets events for
  skips. "3 jobs resumed under 'testing'" on un-mute would make the consequence
  visible at the moment of the click, which is where it matters. Cheap; possibly
  noise. I lean toward doing it only when the un-mute actually un-mutes
  something.
- **Should a job be allowed to outlive its folder's purpose at all?** A "muted
  for 60+ days" indicator on `/cron` — or a nudge to delete — would have
  surfaced `Orig` long before a resource-usage hunt did. That is a bigger
  feature than this proposal and probably wants its own.
- **Does anything else read `next_run_at` raw** and assume it is live? Grep
  before changing it: `cron_job_health` computes "the next 3 upcoming fire
  times" and would, for a muted job, be describing fires that will not happen.
