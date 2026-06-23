# Run-summarizer agent — design (2026-06-23)

**Status:** ✅ implemented (`agents/assistant_run_summarizer.py`; suite green,
1239 passed). Adds a dedicated agent that summarizes a completed assistant run
(what triggered it + obstacles hit) off the critical path, and surfaces the
summary in the `/assistant` left panel.

## Goal

After the `AssistantAgent` finishes a run, a separate agent produces a short
structured summary — a one-line **trigger**, a list of **obstacles** seen across
the steps, and a one-word **outcome** — stored on the run and shown in the
`/assistant` runs list. The summarization must never block the assistant's reply
to the operator.

## Decisions (made, with rationale)

- **A dedicated agent `assistant_run_summarizer`** in the registry (`agents/config.py`),
  `requires_structured_output: True`. That flag makes the `/agent_models` page
  offer it **only structured-output model groups** — the operator's model-config
  override is structured-output-only, enforced by the existing page (no new
  gating code). New `ASSISTANT_RUN_SUMMARIZER_UUID`.
- **Subclass `StructuredLLMAgent`** (the `followup` agent's base): one structured
  call per inbox item, fixed `system_prompt` + Pydantic `response_model`, with the
  `_structured_call` seam tests already monkeypatch. `agents/assistant_run_summarizer.py`,
  wired into `agents/__main__.py`'s class map as `"assistant_run_summarizer"`.
- **Off the critical path via the existing queue.** When the assistant reaches a
  terminal state it calls `db.enqueue(ASSISTANT_RUN_SUMMARIZER_UUID, {"run_uuid": …})` — a
  non-blocking inbox insert; the assistant returns to the operator immediately and
  the **supervisor** runs the summarizer in its own process. Chosen over a cron
  sweep (more moving parts, adds latency) and an in-process post-reply thread
  (shares the assistant's process/timeout). Best-effort: a failed enqueue is
  logged, never raised.
- **Summarize all terminal runs.** The enqueue fires at every terminal point —
  finished reply, step-limit, operator-stop, and failure — via one helper
  (`_request_summary`). Obstacles matter most on the runs that went wrong.
- **Structured schema** (`RunSummary`):
  - `trigger: str` — one line: what the operator asked / what kicked off the run.
  - `obstacles: list[str]` — problems hit across steps (failed actions, retries,
    blocks); `[]` when the run was smooth.
  - `outcome: Literal["resolved","partial","failed"]` — one word, for the row tint.
- **Storage: a new nullable `summary` JSONB column on `assistant_run`** (additive
  migration via `_add_column_if_missing`). The summarizer writes
  `{trigger, obstacles, outcome, summarized_at}`; `null` = not-yet-summarized.
  Chosen over overloading `metadata_` for a clear, queryable field the list view
  selects directly.
- **No recursion.** The summarizer is a different agent (`ASSISTANT_RUN_SUMMARIZER_UUID`),
  creates no `assistant_run` rows, and enqueues nothing — it cannot summarize
  itself.

## Components

**`agents/assistant_run_summarizer.py`**
- `RunSummary(BaseModel)` — the schema above.
- `RUN_SUMMARIZER_SYSTEM_PROMPT` — instructs: read the trigger + the per-step
  digest, output the schema only; obstacles = concrete problems (a `failed` step,
  an error, a no-op/blocked action), not normal successful steps.
- `AssistantRunSummarizerAgent(StructuredLLMAgent)` — overrides `handle(journal_id,
  payload)`: resolve the run by `run_uuid` (`db.get_assistant_run_by_uuid`); if
  missing, return `{ok: False}`. Build the user prompt from the trigger
  (`db.get_run_trigger_message`) + a compact per-step digest (index, action,
  phase, error) over `db.list_assistant_steps`. One `_structured_call`. Persist
  via `db.set_run_summary(run, {...})`. Return `{ok: True, response: …}`.

**`agents/config.py`** — `ASSISTANT_RUN_SUMMARIZER_UUID` + a `assistant_run_summarizer` entry
(`requires_structured_output: True`, `next: None`).

**`agents/__main__.py`** — import + register `"assistant_run_summarizer": AssistantRunSummarizerAgent`.

**`db/models.py` + migration** — `AssistantRun.summary: Mapped[dict | None]`
(JSONB); `_add_column_if_missing("assistant_run", "summary", "summary JSONB")`.

**`db/assistant.py`** — `set_run_summary(run, summary: dict)` (stamps
`summarized_at`, commits). `list_assistant_runs` already returns the rows (now
carrying `summary`).

**`agents/assistant.py`** — `_request_summary(run)` (best-effort enqueue), called
at the four terminal points.

**`webapp/assistant_views.py`** — runs-list row: when `r.summary`, show the
`trigger` (truncated) and, if `obstacles`, an amber "⚠ N" badge; `outcome` tints
the row; otherwise a faint "summarizing…". Detail pane: a summary block (trigger,
outcome, obstacles list) near the top.

## Error handling

- Summarizer model failure (no group bound / all candidates fail / invalid
  output) → `handle` lets it raise so the journal records `failed`; the run stays
  unsummarized (`summary` null). The assistant is unaffected.
- Enqueue failure in the assistant → logged, swallowed; the reply already shipped.

## Testing (model-free)

- `agents/test_assistant_run_summarizer.py`: monkeypatch `_structured_call` to return a
  scripted `RunSummary`; seed a run + steps (incl. a `failed` step); assert
  `handle` writes `run.summary` with the trigger/obstacles/outcome; a missing
  `run_uuid` returns `{ok: False}` and writes nothing.
- `agents/test_assistant.py`: after a terminal run (reply, and a failure path),
  assert an `Inbox` row exists for `ASSISTANT_RUN_SUMMARIZER_UUID` carrying the run uuid.
- `agents/test_capability_registry.py` (or a config test): `assistant_run_summarizer` has
  `requires_structured_output: True`.
- `db/test_assistant_trace.py`: `set_run_summary` round-trips and stamps
  `summarized_at`.
- `webapp/test_assistant_views.py`: a run with a seeded `summary` renders the
  trigger + obstacle badge in the list; an unsummarized run shows "summarizing…".

## Out of scope

- Re-summarizing on demand / a "re-summarize" button (the supervisor processes
  each enqueue once; backfilling old runs is a later sweep if wanted).
- Editing a summary; streaming/live summary updates.
- Per-model attribution in the stored summary (the structured-call seam doesn't
  surface the winning model; the bound group is visible on `/agent_models`).

## Acceptance

A finished (or failed/stopped) assistant run gets, shortly after and without
delaying the reply, a stored `summary{trigger, obstacles, outcome}` produced by
the `assistant_run_summarizer` agent via a structured-output-only model override, and the
`/assistant` runs list shows the trigger + an obstacle indicator per run. Suite
green, model-free.
