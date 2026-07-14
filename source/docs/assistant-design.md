# Assistant — design

**Status: built and running.** The assistant (`agents/assistant.py`,
`AssistantAgent`, agent name `assistant` in `agents/config.py`) is a
rainbox-owned ReAct loop: one structured model decision per step, validated and
dispatched by code, with a durable per-step trace, risk-tiered writes, operator
controls, and an undo ledger. It answers in `/chat` rooms it is a member of and
is inspectable at `/assistant`.

The design stance throughout: **models propose, code disposes.** The model can
only ever name an action from a code-owned registry; what that action is
allowed to do — its arguments, its tier, its output budget, whether it needs
operator confirmation — is decided by code, never by prompt text.

## The loop

`handle()` runs a bounded loop (`STEP_LIMIT = 6`). Each iteration:

1. **Controls** — apply any pending operator `stop`/`redirect` at the step
   boundary (see [Controls](#controls-stop--redirect)).
2. **Decide** — one grammar-constrained structured call
   (`_decide_next_step`, via the model-group fallback machinery of
   `ModelGroupAgent._structured_completion`) returns an
   `AssistantStepDecision`: `{reason, action, args}`. `reason` is an
   operator-facing audit note shown in the trace, not hidden chain-of-thought.
3. **Validate** — `_validate_decision` checks the action against the effective
   capability set: unknown/disabled/non-prompt-exposed actions, missing
   required args, and unknown args are all rejected. A rejection records a
   `failed` step and feeds the error back via the scratchpad; the loop
   continues.
4. **Dispatch** — terminal actions (`reply`, `ask_clarifying_question`) post
   the chat message and finish the run. Reads and log-and-undo writes execute
   immediately. Confirm-tier writes are **proposed**, never executed inline.
5. **Observe** — the action's `AssistantObservation{ok, text, data}` is capped
   (per-capability `output_cap_chars`), persisted on the step row, and appended
   to the scratchpad for the next decision.

Running out of steps posts a "couldn't complete this within the step limit"
message with a link to the run's inspector page and finishes the run
`stopped`. Any exception marks the run `failed` (against the step it died on)
and re-raises so the journal records the failure too. It also posts a visible
`kind="notice"` failure message with the reason and run link. A notice is
operational output, not conversation history, and atomically clears the
assistant's lingering progress rows in that room.

**Triggering.** A human post in a room enqueues every responder agent in it
(`webapp/chat_api.py::_maybe_trigger_chat_agents`), which also posts the
"working on it" progress bubble — at enqueue time, because the assistant runs
in a freshly spawned process and the operator would otherwise stare at nothing
during spawn+import. The payload carries `room_uuid` and the triggering
`message_uuid` (used as evidence provenance for memory writes). Every terminal
reply, stop message, step-limit message, or failure notice clears that progress
bubble through `db.post_chat_message`'s terminal-kind transaction.

## Prompt assembly

- **System prompt** = `ASSISTANT_SYSTEM_PROMPT` + the **action catalog**
  generated from the capability registry (only `prompt_exposed` capabilities
  appear). The static part encodes the behavioral rules: one step at a time,
  read before answering (transcript answers are stale; `truncateN` facts have
  a uuid escape hatch), fix reported errors rather than resubmitting, never
  invent placeholder values, and never claim a write that didn't run
  (anti-fabrication).
- **User prompt**, in order: the current **local time** (so relative reminders
  resolve in the operator's zone, not UTC), the **user-profile block**
  (query-independent operator self-model — see
  `memory-architecture.md` §User Profile Block), the **skill block** (active
  procedural skills retrieved for the latest human message; candidates are
  inert), the transcript (`kind == "message"` rows only, newest
  `MAX_RECENT_MESSAGES = 30`), the **scratchpad** of steps taken this turn
  (tail-capped at `MAX_SCRATCHPAD_CHARS = 5000`), and the step counter.
  Profile and skill blocks are best-effort — a retrieval failure never breaks
  the turn.
- **Facts-invalidation marker.** Before the first step, if
  `qa.facts_invalidated_at` changed since the last marker in this room (a
  shield toggle or Q&A repopulate), the assistant posts a one-time visible
  notice telling the model to re-check facts instead of reusing earlier
  answers; dedup is by the exact timestamp in the marker's `meta`. See
  `qa-system.md`.

## The capability registry

`CAPABILITIES` maps each `AssistantActionName` to a `Capability` record:
family, LLM-facing `description` (usage caveats + arg schema) vs operator-facing
`summary`, required/optional args, read/write flags, **tier**
(`log_and_undo` | `confirm` | None for reads), `dry_run`, `output_cap_chars`,
`enabled`, and `prompt_exposed`. Both the prompt catalog and dispatch are
generated from this single object, so disabling a capability removes it from
prompt **and** dispatch at once.

The operator can turn capabilities off at runtime via the
`assistant.disabled_capabilities` setting (a JSON list of names, editable on
`/settings`); `capability_report()` exposes the effective set for inspection.
Internal capabilities (`prompt_exposed=False`) are undo inverses: the model
can never request them — validation rejects them — and they are dispatched
only by `undo_write_intent`.

| Capability | Family | Tier | Undo |
|---|---|---|---|
| `reply`, `ask_clarifying_question` | conversation | terminal | — |
| `query_memory` | memory | read | — |
| `workspace_read_command` | workspace | read | — |
| `kanban_read` | kanban | read | — |
| `kanban_query` | kanban | read | — |
| `find_uuid` | lookup | read | — |
| `remember` | memory | log-and-undo | `reject_memory_candidate` (internal) |
| `forget_memory` | memory | log-and-undo | `reactivate_memory` (internal) |
| `activate_memory` | memory | **confirm** | — |
| `kanban_task_column` | kanban | log-and-undo | inverse move (position-aware) |
| `kanban_task_change_board` | kanban | log-and-undo | inverse board move (board-aware) |
| `kanban_task_complete` | kanban | log-and-undo | move back to prior column |
| `kanban_task_comment` | kanban | log-and-undo | `↩ retracted:` comment |
| `kanban_task_create` | kanban | log-and-undo | `kanban_task_delete` (internal) |
| `kanban_board_create` | kanban | log-and-undo | `kanban_board_delete` (internal) |
| `set_reminder` | cron | **confirm** (dry-run) | — |
| `edit_file` | workspace | **confirm** (dry-run diff) | — |
| `propose_skill` | skill | log-and-undo | `skill_delete` (internal) |
| `activate_skill` | skill | **confirm** | — |

## Read actions

- **`query_memory`** — hybrid retrieval over curated seed Q&A (static +
  dynamic handlers) and active memory claims, tiered user-overlay → upstream →
  claims, fenced as untrusted data, with per-fact (1200 chars, tagged
  `truncateN`) and total (11000 chars) budgets and a `{"uuid": ...}` mode to
  read one fact in full. Seed fact lines carry the entry's `path` as a tag
  (e.g. `seed/upstream, dynamic, system.uptime_host`) so look-alike answers
  stay tellable apart. Details in `qa-system.md` and `memory-architecture.md`.
- **`workspace_read_command`** — one allowlisted, non-shell argv run in the
  workspace root (`tools/command_policy.validate_command` +
  `tools/workspace_command_runner`). The policy excludes interpreters,
  mutation, and network tools, so it stays a file-inspection reader.
- **`kanban_read`** — a task's detail + 10 recent events, a board's JSON
  serialization (`kanban_board_llm_json`), or the folder tree of boards; every
  observation is JSON. Reading writes no events (unlike worker operations).
  See `kanban-design.md`.
- **`kanban_query`** — find kanban boards, folders, and tasks BY NAME via
  `db.kanban_find_by_name`: exact, substring, and fuzzy (typo-tolerant)
  matching over board/folder names and task titles, returning a ranked JSON
  candidate list in `find_uuid`'s shape (kind, name, FULL uuid, parents, page
  url) — the name-side complement to `find_uuid`, for when the operator says
  "the chores board" and the model needs its uuid. See `kanban-design.md`.
- **`find_uuid`** — resolve a uuid the model isn't sure about (a fragment,
  a typo'd paste) across every uuid-bearing table via `db.find_uuid`: each
  JSON match carries kind, name, parent chain, page url, and the FULL uuid to
  use in subsequent actions — so a weak model never has to guess an id. The
  same lookup backs the operator's `/find` page. See `find-uuid-design.md`.

## Write tiers

Two tiers, two safety models:

### Log-and-undo (execute now, reversible)

The write executes immediately and is recorded in the ledger
(`assistant_write_intent`) as a row created **atomically in `completed`** —
never `proposed`, so it can never be confirm-executed into a duplicate. The
row's `result.undo` carries the inverse op (`{capability, payload}`) that
`undo_write_intent` replays. Guard rails:

- **Position-aware undo** — a move-undo carries `expect_column` and refuses if
  the task has since moved elsewhere.
- **State-guarded inverses** — undo of `remember` refuses if the claim is no
  longer candidate/active; undo of `forget` refuses unless still `rejected`;
  undo of `propose_skill` deletes only a still-pending candidate. An undo can
  never clobber a state that changed since the write.
- **Append-only surfaces retract, not erase** — a comment's undo posts
  `↩ retracted: …` (which itself needs no further undo).
- **No-op writes are not recorded** — a `remember` that dedupes into an
  existing claim (`noop`) has nothing to undo, so no ledger row.
- **Duplicate-write block** — the loop keeps a signature set
  (`action:sorted-args`) of writes completed this run; an identical re-issue
  is not replayed, and the model is steered to `reply`.
- After any successful write the scratchpad steers the model to confirm via
  `reply` rather than keep acting, and every write's relative link
  (`/kanban?id=…`, `/memory?id=…`, `/cron?id=…`) is appended to the final
  reply so the operator can jump to what changed.

### Confirm (propose now, execute only on approval)

`_propose_write` records an `assistant_write_intent` in state `proposed` and
returns an observation telling the model its job for this request is over.
The terminal reply carries the proposal in the chat message's `meta`
(`{write_intent, capability, step_link}`), which `/chat` renders as a
confirm/reject card.

- **Dry-run previews** — a `dry_run` capability computes its preview by
  running the action with `ctx.dry_run=True` (must not mutate): `set_reminder`
  resolves the fire time; `edit_file` renders the unified diff. Bad input
  fails at preview time → no proposal is recorded. The dry-run can pin
  execution-time guards into the stored payload via `confirm_payload` —
  `edit_file` stores `base_sha` (SHA-256 of the previewed file) and execution
  refuses if the file changed since the preview.
- **Execution** (`agents/assistant_writes.py::execute_write_intent`) is the
  *only* path that runs a proposed write: it walks
  `proposed → confirmed → executing → completed | failed`, verifies the
  stored `payload_hash` still matches, and runs the capability's executor
  against the **stored** payload — the assistant cannot mutate what was
  approved. It refuses non-`proposed` intents and non-confirm-tier
  capabilities. `reject_write_intent` declines a proposal.
- `edit_file` is additionally confined by `resolve_workspace_path` (rejects
  traversal/sensitive/escape paths) and a 100 KB size cap on both old and new
  content.

### Undo

`undo_write_intent` replays a completed intent's stored inverse and marks the
intent `undone`. One-shot by design: only `completed` intents with an `undo`
record qualify; there is no redo.

## Trace

Every run is durable in `assistant_run` / `assistant_step` (see
`data-model.md` for the columns):

- A run row opens **before** anything else, so a crash anywhere is recorded.
- A normal action step is **one mutable row**: inserted at `phase="running"`
  *before* the action executes (a kill mid-action leaves a durable row), then
  settled in place to `observed`/`failed` with the capped
  `observation_preview` and the full `{ok, text, data}` observation JSONB.
- Terminal-only rows (`final`, a `failed` validation, a crash, `control`) are
  single inserts.
- Each step stores the exact decide-call prompts (`system_prompt`,
  `user_prompt`), raw `model_response`, the model used, token counts,
  `duration_ms`, and the
  `requested_at`/`created_at`/`settled_at` timestamps.
- Before dispatching a decide call, the run's `metadata.active_call` checkpoint
  stores its step index, exact system/user prompts, request time, model group,
  and an attempt list. Each attempt adds the resolved model name/UUID,
  configured timeout, start time, latest partial reasoning/response (flushed at
  most once per second), and failure when applicable. The checkpoint
  is removed only after the resulting step is durable. This covers the window
  where no `assistant_step` exists yet because the model has not returned.
- Each step also stores the model's native `reasoning` ("thinking") channel,
  captured via instrumentation while the structured output streams (the
  structured wrapper drops it from the parsed result). A reasoning model's
  thinking shows on the /assistant step ("model reasoning", collapsed) and is
  posted into the room as a `kind="thinking"` bubble; a non-reasoning model
  emits no reasoning channel, so nothing is stored or shown. On a decide-call
  crash (e.g. a timeout mid-think) the failed step keeps the partial
  reasoning.
- The journal `result` is a short summary plus pointers
  (`assistant_run_uuid`, step count) — the tables are the trace, the journal
  is not.

After every terminal state the assistant stores an immediate deterministic
failure digest when applicable, then enqueues the
**`assistant_run_summarizer`** agent (off the critical path), which makes one
structured call over the trace and stores a `{trigger, obstacles[], outcome}`
digest on `assistant_run.summary` for the inspector. The deterministic digest
means a failed run is useful even if the summarizer model is unavailable; a
later successful summarizer call may replace it. The summarizer posts no chat
and enqueues nothing, so it can never summarize itself.

## Failure recovery

There are two terminal failure paths:

- **Handled exception** — `_fail_run` records a failed step with the latest
  prompts, model UUID, and partial reasoning; marks the run `failed`; stores the
  fallback summary; posts the operational failure notice; and re-raises so
  `Agent.run()` marks the journal failed. A structured stream timeout therefore
  remains visible as the step error rather than becoming a silent exit.
- **Worker interruption** — the supervisor tracks the journal currently owned
  by each child. EOF, watchdog kill, or supervisor shutdown calls
  `recover_interrupted_assistant_run`; startup applies the same recovery to
  `running`/`stopping` runs left by the previous supervisor. Recovery turns an
  open action row into `failed`, or materializes `metadata.active_call` as a
  failed step with the exact prompts/model/configured timeout. It then marks
  the run `killed`, fails the journal, stores the fallback summary, and posts
  the failure notice.

The supervisor liveness clock is explicitly refreshed at every assistant step
boundary and by streamed model-progress checkpoints. Its 60-second guard is
therefore scoped to the active step, not accumulated from the beginning of the
run. The provider's configured structured-stream timeout is independently
restarted for each model attempt in each step.

Failure notices carry `meta.assistant_failure_run_uuid`, making notice creation
idempotent per run. They use `kind="notice"`: visible in `/chat`, excluded from
the assistant prompt (`kind == "message"` is the conversation), and terminal
for progress cleanup. The notice includes a deep link to `/assistant`, where
the failed step exposes the full model request and error.

## Controls (stop / redirect)

Operators steer an in-flight run via `assistant_control` rows, applied at each
step boundary:

- **stop** — records a `control` trace step, posts "Stopped at your request.",
  finishes the run `stopped`, and marks other pending controls `ignored`.
- **redirect** — folds the instruction into the scratchpad so the next step
  sees it; prior steps are never touched.

Endpoints: `GET /chat/api/assistant/runs/<uuid>` (live run state for the chat
UI), `POST …/stop`, `POST …/redirect`, `POST …/resummarize`, and
`POST /chat/api/assistant/write-intents/<uuid>/confirm|reject|undo`.

> **Control-plane caveat.** None of these endpoints authenticate the caller;
> `confirmed_by_uuid` is filled from the seeded human user without proof. This
> is Finding 4 of `proposals/2026-06-25-security-review-mitigations.md`
> (open): the confirm-tier state machine is sound, but *who may confirm* is
> currently anyone who can reach localhost.

## Inspector pages

- **`/assistant`** (`webapp/assistant_views.py`) — the run inspector: a run's
  dashboard (status, duration, tokens), the step timeline with decisions,
  prompts, observations, and linked write intents, plus a markdown export at
  `/assistant/<run>/markdown`. Deep-linked as `/assistant?id=<run-uuid>`
  (chat replies and the step-limit message link here).
- **`/assistant-overview`** (`webapp/assistant_overview_views.py` +
  `static/assistant-overview.js`) — a searchable, sortable, paginated table of
  all runs, each row linking into the inspector.

## Testing

The single live-model seam is `_decide_next_step`; tests drive the loop with
scripted decisions from `agents/assistant_fakes.py`. Coverage:
`agents/test_assistant.py` (loop, validation, trace),
`agents/test_assistant_actions.py` (read actions),
`agents/test_assistant_writes.py` (tiers, intents, undo),
`agents/test_assistant_control.py` (stop/redirect),
`agents/test_assistant_remember_candidate.py`, `test_assistant_skills.py`,
`test_assistant_profile.py`, `test_assistant_facts_marker.py`,
`test_assistant_progress.py`, `test_kanban_query.py`,
`test_kanban_move_action.py`,
`test_kanban_change_board.py`, `test_kanban_writes_s2.py`,
`test_kanban_create.py`, `test_kanban_create_board.py` (kanban capabilities incl. the locked
prompt-exposed surface), `db/test_assistant_trace.py`,
`db/test_assistant_write_intent.py`, `db/test_assistant_control.py`, and the
webapp `test_assistant_*` suites for the endpoints and pages.

## See also

- `data-model.md` — the `assistant_run`/`assistant_step`/`assistant_control`/
  `assistant_write_intent` schema.
- `skills-design.md` — the skills the assistant retrieves, proposes, and
  activates.
- `memory-architecture.md` — the memory trust model behind
  `remember`/`forget`/`activate_memory` and the profile block.
- `kanban-design.md` — the kanban capability family and why it sits outside
  the worker authority model.
- `qa-system.md` — the Q&A knowledge base behind `query_memory`.
- `proposals/2026-06-25-security-review-mitigations.md` — Finding 4 (the
  unauthenticated confirm boundary).
