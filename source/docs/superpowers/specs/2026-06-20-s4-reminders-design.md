# S4 — reminders (confirm-tier + dry-run) — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Implements card **S4** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
the assistant can set a **reminder** that fires a chat message at a chosen time.
This is the **first confirm-tier write family** beyond `activate_memory` and the
**first `Capability.dry_run` user** — it introduces the reusable dry-run preview
protocol that S5 (file patches) will reuse.

## Decisions (made, with rationale)

- **Confirm-tier with a dry-run preview.** Scheduling a future action has real
  blast radius (it will post to the operator later), so the assistant *proposes*
  and the operator confirms. The proposal's preview shows the **resolved fire
  instant** — so the operator catches a wrong date before approving.
- **A reminder is a one-shot cron `message` job.** Reuses the cron subsystem
  (the scheduler S1 warmed up). One-shot needs no scheduler rewrite: a CronJob
  with **empty `cron_expr` + a pre-set `next_run_at`** fires once, then
  `cron_compute_next_run("")` returns `None` so it never refires (verified). A
  tiny `cron_tick` tidy retires such a job (sets `enabled=False`) after its
  single fire so it doesn't linger enabled-but-dead.
- **`when` is an ISO-8601 datetime string** (`datetime.fromisoformat`). A naive
  value is interpreted as **UTC**; the dry-run preview shows the resolved instant
  so any misread is visible before confirm. Natural/relative-time resolution
  ("Friday 9am" → datetime, needs "now" injected into the prompt) is a follow-up.
- **The reminder fires into the conversation room** (`ctx.room_uuid`) as
  `⏰ Reminder: <text>`, authored by the cron system sender.

## The dry-run preview protocol (new, reusable)

- `AssistantActionContext` gains `dry_run: bool = False` (default keeps every
  existing action unchanged).
- `_propose_write`, when `cap.dry_run` is True, calls the action once in
  **dry-run mode** (`replace(ctx, dry_run=True)`) to build the preview:
  - if the dry-run observation is **not ok** (e.g. bad datetime), return it and
    create **no** intent — bad input is caught at propose time;
  - otherwise the intent's `preview_text` is the dry-run observation's text.
- `execute_write_intent` runs the action normally (`dry_run=False`) on confirm.
- Contract: a `dry_run=True` capability's action MUST NOT mutate when
  `ctx.dry_run` — it computes and returns a preview only.

`activate_memory` keeps `dry_run=False`, so its propose path is unchanged.

## `db/cron.py`

```python
def cron_create_one_shot_message(
    *, message: str, fire_at: datetime, target: str = "", name: str = "",
    folder_uuid: UUID | None = None,
) -> CronJob:
    """Create an enabled one-shot 'message' cron job: empty cron_expr + a pre-set
    next_run_at, so it fires once at fire_at and then retires (cron_tick disables
    a fired empty-expr job). `target` is a chatroom uuid string (where to post)."""
    job = CronJob(
        name=name or "Reminder", enabled=True, folder_uuid=folder_uuid,
        cron_expr="", timezone="UTC", action_type="message",
        target=target, message=message, next_run_at=fire_at,
    )
    db.session.add(job)
    db.session.commit()
    return job
```

`cron_tick` tidy — replace the post-fire reschedule (currently
`job.next_run_at = cron_compute_next_run(...)` right after `fire_cron_job(job,
trigger="scheduled")`) with:

```python
            fire_cron_job(job, trigger="scheduled")
            nxt = cron_compute_next_run(job.cron_expr, job.timezone, after=now)
            if nxt is None and not (job.cron_expr or "").strip():
                job.enabled = False  # one-shot (empty expr): retire after its single fire
            else:
                job.next_run_at = nxt
            fired += 1
```

(Only empty-`cron_expr` jobs are retired; a non-empty-but-unparseable recurring
expr keeps its existing dormant behavior. Verified: `cron_compute_next_run("")`
→ `None`.)

## `agents/assistant.py`

**Context** — add the field:

```python
@dataclass(frozen=True)
class AssistantActionContext:
    journal_id: UUID | None
    room_uuid: UUID
    agent_uuid: UUID
    step_index: int
    dry_run: bool = False   # True only inside _propose_write's preview call
```

**Enum** (after the kanban writes):

```python
    SET_REMINDER = "set_reminder"      # confirm-tier (dry-run): schedule a reminder message
```

**Action:**

```python
def _action_set_reminder(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Confirm-tier write: schedule a one-shot reminder that posts a chat message
    at `when`. In dry-run (propose) it resolves the time and previews without
    creating anything; on real execution it creates the one-shot cron job."""
    text = str(args.get("text", "")).strip()
    raw_when = str(args.get("when", "")).strip()
    try:
        fire_at = datetime.fromisoformat(raw_when)
    except ValueError:
        return AssistantObservation(
            ok=False, text=f"invalid 'when' (use ISO-8601, e.g. 2026-06-27T09:00): {raw_when!r}"
        )
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)
    when_str = fire_at.isoformat()
    if ctx.dry_run:
        return AssistantObservation(
            ok=True, text=f"Would remind you at {when_str}: {text}",
            data={"fire_at": when_str},
        )
    job = db.cron_create_one_shot_message(
        message=f"⏰ Reminder: {text}", fire_at=fire_at, target=str(ctx.room_uuid),
        name=f"Reminder: {text[:40]}",
    )
    return AssistantObservation(
        ok=True, text=f"Reminder set for {when_str}: {text}",
        data={"cron_job_uuid": str(job.uuid), "fire_at": when_str},
    )
```

(`UUID`, `datetime`, `UTC` are imported in assistant.py — verify; add
`from datetime import UTC, datetime` if missing.)

**Registry entry:**

```python
    AssistantActionName.SET_REMINDER: Capability(
        name=AssistantActionName.SET_REMINDER, family="cron",
        description=('schedule a reminder that messages you at a time; needs your '
                     'confirmation. args: {"text": "...", "when": "ISO-8601 datetime"}'),
        required_args=("text", "when"), action=_action_set_reminder,
        read=False, write=True, tier="confirm", dry_run=True,
    ),
```

**`_propose_write`** — build the preview via the dry-run action when `cap.dry_run`:

```python
    def _propose_write(self, ctx, decision, cap):
        """...existing docstring..."""
        preview = f"{cap.name.value}: {json.dumps(decision.args, sort_keys=True)}"
        if cap.dry_run:
            dry = self._dispatch_action(
                replace(ctx, dry_run=True), decision
            )
            if not dry.ok:
                return dry  # bad input → no proposal
            preview = dry.text
        intent = db.create_write_intent(
            run_id=self._run.id, step_index=ctx.step_index,
            capability_name=cap.name.value, payload=decision.args,
            preview_text=preview, room_uuid=ctx.room_uuid, agent_uuid=ctx.agent_uuid,
        )
        return AssistantObservation(
            ok=True,
            text=(f"Proposed (awaiting your confirmation): {preview}. "
                  f"Confirm intent {intent.uuid} to apply."),
            data={"write_intent_uuid": str(intent.uuid), "state": "proposed"},
        )
```

(`replace` from `dataclasses` — add to the existing `from dataclasses import …`.
`_dispatch_action` already exists and runs `cap.action(ctx, decision.args)`.)

## `agents/test_assistant_fakes.py`

Add `"set_reminder"` to the locked action surface.

## Tests (TDD, model-free) — `agents/test_reminders.py` (new)

1. **dry-run previews without creating a job:** `_action_set_reminder` with
   `ctx.dry_run=True` returns `ok=True` text "Would remind you at …" and creates
   no CronJob.
2. **real execution creates a one-shot job:** with `dry_run=False`, a CronJob
   exists with `action_type="message"`, empty `cron_expr`, `next_run_at == fire_at`,
   `target == room_uuid`, message prefixed `⏰ Reminder:`.
3. **bad datetime → ok=False** (both dry-run and real).
4. **propose path uses the dry-run preview:** drive the loop with a scripted
   `set_reminder`; the proposed `assistant_write_intent.preview_text` starts
   "Would remind you at" and **no** CronJob is created yet (confirm-tier doesn't
   execute inline).
5. **confirm executes and creates the job:** `execute_write_intent(intent)` →
   the one-shot CronJob now exists; intent → `completed`.
6. **one-shot fires once then retires:** a one-shot job with `next_run_at` in the
   past fires on `cron_tick` (posts the reminder to the room) and is then
   `enabled=False` with no future run.
7. **capability flags:** `set_reminder` is `tier="confirm"`, `dry_run=True`,
   `write=True`; surface lock updated.

## Done when

- The assistant proposes a reminder with a preview showing the resolved time;
  the operator confirms to schedule it; an unconfirmed reminder never schedules.
- The reminder fires once at its time (posting `⏰ Reminder: …` to the room) and
  the job retires.
- Bad datetimes are rejected at propose time.
- Model-free tests (fake clock via `cron_tick(now=…)`) cover all of the above;
  full affected suite green.

## Out of scope (follow-ups)

- Natural/relative time ("in 2 hours", "Friday 9am") — needs current-time
  injection into the assistant prompt and a parser; absolute ISO only for now.
- Edit/cancel of a pending reminder (a future cron-write capability or the
  existing cron admin UI).
- Recurring reminders (this is one-shot only).
