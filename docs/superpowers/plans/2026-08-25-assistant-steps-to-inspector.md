# Migrating /assistant from steps to the inspector

The page renders a run twice: as a typed event stream (gantt + inspector) and
as a list of step sections below it. The step sections are the older shape —
one row per `assistant_step`, with every kind of detail crammed into it. That
is what does not scale: a new thing a run can do needs new markup in a template
that already knows about everything.

This retires them. Afterwards a run is one stream, and a new kind of row costs
one component.

## What blocks removal today

Everything below is in a step section and has no home in the inspector.
Removal cannot start until each has one.

| in the step section | state |
|---|---|
| write intents: confirm / reject / undo | not in the stream at all |
| write intents with no step (`unlinked`) | nothing to attach to |
| second-opinion verdict, problems, skipped reason, prompts | the review IS an event, but it carries only KPIs |
| skipped steps | `_step_events` returns `[]` for them — they vanish |
| the live in-flight call (`active_call`) | step-shaped, deliberately outside the durable trace |
| the per-step permalink | becomes the new anchor |

Two decisions, taken with the operator:

- **A row's anchor is the identity it already has.** `log_view` mints a stable
  `key` per row for the live refresh. Anchors reuse it rather than minting a
  second identity for the same row — two identities for one thing is how they
  drift.
- **The three orphans all get a home.** Nothing is dropped on the way out,
  including paths with no rows in production today.

## Compatibility: `#step-<uuid>` is a published URL

`db.assistant_step_path()` mints it and six places outside this page link to
it — the chat proposal card, cron provenance, the second-opinion admin view,
two `core.py` row builders, and the uuid lookup. Those links live in the
operator's chat history and cron rows. They must keep resolving.

One step uuid is several rows, so the format needs a documented primary: **the
action event where the step has one, else its call event.** Every external
link means "the step that did this", and the action is the row carrying the
args, the observation and the write intents.

## Order

Payloads first, because both later slices need them. Then anchors, then the
controls, then removal — so the page is never in a state where something is
unreachable.

---

### Slice 0 — close the read-model gaps

`db/assistant_log.py`, plus the components for the new payloads.

1. **A skipped step emits an event.** New kind `skipped`: a call the loop could
   not make. No duration — it cost nothing — so it renders as a tick, and it
   must not reach `assistant_llm_calls`.
2. **The review event carries its review.** `approved`, `problems_text`,
   `skipped`, `error`, and the three prompts move onto the event payload, and a
   component renders them.
3. **The action event carries its write intents.** Keyed by `step_uuid`, so the
   pane that shows what an action did also shows what it wants to write.
4. **Intents with no step get a run-level row.** They belong to the run, so
   they hang off the run's opening `start` event rather than inventing a row.

### Slice 1 — anchors

1. `#ev-<key>` selects that exact row, on load and after a live refresh.
2. `#step-<uuid>` keeps working, resolving to the step's primary row.
3. Every row offers its own link, so any row can be sent to someone.

### Slice 2 — the controls

1. Write-intent confirm / reject / undo render in the action pane. The
   endpoints already exist and are tested; this is relocation, not API work.
2. The live in-flight call becomes a row on the stream rather than a card
   below it.

### Slice 3 — removal

1. Delete the step sections and everything only they used.
2. **Keep the step data in the context.** `_run_markdown` reads `timeline`,
   `reviews`, `unlinked`, `decision_json`, `model_names`, `pending_controls`
   and `verdict` from `ctx`, not from the DOM. Removing the markup is safe;
   removing the data is not.
3. Migrate the tests that assert step markup. They encode real behaviour — an
   earlier attempt at this removal broke 39 of them — so each moves to the
   inspector rather than being deleted.

## Not steps, and untouched

The dashboard, the trigger card, the pending-controls line, and the Verdict
card. None of them is a step section.

## Notes for the implementer

**The staircase is still the contract.** No event contains another. Adding
kinds does not relax it.

**One enumeration.** `assistant_llm_calls` filters `run_events`. A new kind
that is not a model call has to be held out of it, or the run's call count
changes under everything that reads it.

**Deriving, not emitting.** Everything above already exists in a row somewhere.
If a payload is genuinely not recorded, the pane says so rather than guessing.
