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

`handle()` runs a bounded loop (`STEP_LIMIT = 6`). With the
`assistant.acceptance_criteria` switch on, a code-driven **step 0** precedes
the loop: one structured call establishes the reply's constraints before any
work happens (see [Acceptance criteria](#acceptance-criteria)); it consumes
none of the step limit. Each loop iteration:

1. **Controls** — apply any pending operator `stop`/`redirect` at the step
   boundary (see [Controls](#controls-stop--redirect)).
2. **Decide** — one grammar-constrained structured call
   (`_decide_next_step`, via the model-group fallback machinery of
   `ModelGroupAgent._structured_completion`) returns an
   `AssistantStepDecision`: `{reason, action, args}` (`args` is forced into
   the schema's `required` list — a non-required field simply gets omitted by
   the model). `reason` is an operator-facing audit note shown in the trace,
   not hidden chain-of-thought. `reply` takes number-prefixed args —
   `{"1_specification": ..., "2_message": ..., "3_audit": ...}`, all
   required — where the prefixes spell the writing order: first the
   reply's constraints (response language mirrors the operator's message;
   units, separators, date format), then the answer text obeying them,
   then a skeptical self-audit of that text against the specification,
   `user_settings_json` and the formatting guide (see
   `profile-guidance.md`).
3. **Validate** — `_validate_decision` checks the action against the effective
   capability set: unknown/disabled/non-prompt-exposed actions, missing
   required args, and unknown args are all rejected. Reply args written out
   of prefix order are also rejected — checked in both representations:
   the parsed args dict (json insertion order) and the provider's raw
   response text (the authority when the structured-output parser
   normalizes key order). Every reply logs a `reply order check:` line
   (dict key order, raw-text key positions) so an order escape is
   diagnosable from the app log. A rejection records a `failed` step and
   feeds the error back via the scratchpad; the loop continues.
4. **Dispatch** — terminal actions (`reply`, `ask_clarifying_question`) post
   the chat message and finish the run — except a `reply` whose `3_audit` is
   anything but `OK`: the self-audit gate bounces it as a rejected step (the
   audit text flows back through the scratchpad so the model fixes the
   message), capped at `MAX_AUDIT_REJECTIONS = 2` per run so a
   never-approving audit cannot burn the step limit. The gate also
   re-checks the prefix order from the decision's own args — a second,
   plumbing-free enforcement layer at the last moment before the message
   posts. Reads and log-and-undo writes execute immediately. Confirm-tier
   writes are **proposed**, never executed inline.
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
- **User prompt** — the sections are emitted as top-level sibling tags (no
  root wrapper: models recognize the start/end tags without a single-rooted
  document, and a wrapper would cost one indentation level on every line;
  each section is still individually ElementTree-escaped XML). The task
  leads the prompt — with the request buried at the bottom under a long
  profile/history, weaker models answered the surrounding context instead of
  the request — and the supporting context follows. In order: the
  **current request** (a bare `<current_request>` tag, no attributes: the
  section order carries the emphasis and the time anchor is
  current_local_time at the end), the **acceptance criteria**
  (`<acceptance_criteria_json>`, directly after the request so the request
  and its constraints travel together — present only when the
  `assistant.acceptance_criteria` switch is on and the step-0 call
  succeeded; see [Acceptance criteria](#acceptance-criteria)), the
  transcript (`kind == "message"` rows
  only, newest `MAX_RECENT_MESSAGES = 30`), the **scratchpad** of steps
  taken this turn (each step renders its action, the decision's stated
  reason, the args, and the observation — a rejected step reads as the full
  decision it was, not an anonymous failure; tail-capped at
  `MAX_SCRATCHPAD_CHARS = 5000`), the step
  counter (`decision_request`), the **user settings**
  (`<user_settings_json>` — `profile.current`'s fields as JSON, a bare tag
  with no attributes (the system prompt declares it reference data), no
  preamble and no tree label; opaque enum values such as `number_format`
  carry a code-owned `<key>.comment` entry spelling the convention out), the
  **formatting guide** (`authority="instructions"` — deterministic
  locale directives compiled by `user_profile/formatting.py`; the one
  profile-derived block with instruction authority, justified because every
  imperative sentence is code-owned and every interpolated value passed the
  strict prompt-boundary validation), the **knowledge calibration** block
  (`authority="context"` — self-declared topic rows as JSONL from
  `user_profile/calibration.py`, sharing a 2 700-char guidance budget with
  the formatting guide, formatting admitted first) — these two blocks sit
  behind independent default-off switches (`assistant.formatting_guide`,
  `assistant.knowledge_calibration`), flipped only after each block passes
  its live release gate; see `profile-guidance.md` — the **user-profile
  block** (query-independent operator self-model — see
  `memory-architecture.md` §User Profile Block), the **skill block** (active
  procedural skills retrieved for the latest human message; candidates are
  inert), and the current **local time** (so relative reminders resolve in
  the operator's zone, not UTC).
  All of these are best-effort — a retrieval or formatter failure empties
  only its own block, never the turn.
- **One declared-profile context snapshot per turn.**
  `user_profile.current_profile_context()` reads `profile.current`,
  `qa.facts_invalidated_at`, and `profile.current_changed_at` in one
  statement and resolves the profile once; the room marker and all three
  declared blocks render from that snapshot, and the handle path performs no
  second settings lookup — a switch committed mid-turn applies wholly to the
  next turn, never mixing two people or showing a new profile without its
  switch notice. The live eval harness reuses the same construction through
  `build_turn_prompts` with an eval-only profile override.
- **Context-invalidation marker.** Before the first step, if either pending
  cause — a facts/Q&A invalidation (`qa.facts_invalidated_at`) or a
  `profile.current` switch (`profile.current_changed_at`) — has not been
  acknowledged in this room, the assistant posts one visible notice: the
  generic re-check-facts text for a facts-only event, a tailored notice for
  a profile switch, or a combined notice when distinct events are both
  pending. The two stamps are written independently (`set_current_profile`
  never touches the facts stamp — a switch changes the declared-profile
  blocks, not the Q&A base), so a Q&A event followed by a switch still
  surfaces as combined in either order. The marker's `meta` checkpoints both
  current stamps (`context_invalidation`, `facts_invalidation`,
  `profile_context_changed`, `profile_switch_uuid`), each acknowledged
  independently — several changes before a room runs coalesce into one
  marker. Legacy markers carrying only
  `facts_invalidation` stay recognized. The marker is operator-facing: it is
  demoted behind the operator's message and filtered from model history (the
  freshly assembled profile blocks are the model-side signal). Switching the
  active profile preserves room history — it is a soft signal, never
  redaction, and not an audience boundary. See `qa-system.md`.

## Acceptance criteria

Behind the `assistant.acceptance_criteria` switch (default off), a
code-driven **step 0** establishes the reply's constraints before the decide
loop starts — enforced by the loop, so the model cannot skip or forget it.
One structured call returns an `AcceptanceCriteria`:

- `response_language` — with the reason, e.g. `"en-US (mirrors the current
  message)"`. The operator's CURRENT message alone decides; the assistant's
  own earlier replies are never a language reference (a prior reply in the
  wrong language is an error to correct, not continuity to preserve).
- `processing` — preferences that steer the WORK (the target unit for an
  ambiguous conversion, the timezone for a reminder).
- `formatting` — preferences that steer the FINAL message (separators, date
  format, temperature unit, spelling).
- `assumptions` — every ambiguity resolved by a settings-based assumption,
  stated so the operator can spot a wrong one. Assumptions are made only
  where the settings provide a default; otherwise the ambiguity is recorded
  as unresolved and the normal `ask_clarifying_question` path handles it.

The call has its own small persona prompt
(`ACCEPTANCE_CRITERIA_SYSTEM_PROMPT`, not the assistant's working prompt);
profile languages enter it only through the prompt-boundary validation in
`user_profile/formatting.py`. Inputs: the current request, the last few
**operator** messages (`ACCEPTANCE_CRITERIA_MAX_MESSAGES = 6`,
`assistant_messages="omitted"` — operator messages carry the
language-continuity signal, assistant replies are exactly the wrong anchor),
`user_settings_json`, and the formatting guide rendered from the criteria
snapshot profile regardless of the `assistant.formatting_guide` switch
(which gates only the decide-prompt injection). NOT the action catalog —
the call plans constraints, not actions.

The result renders as a bare `<acceptance_criteria_json>` section directly
after `<current_request>` in every decide step. Its authority lives in one
code-owned system-prompt sentence, and `_system_prompt()` swaps the
source-priority block for a variant ranking `acceptance_criteria_json`
directly below `current_request` — both only while the switch is on, so a
switched-off run's prompts are byte-identical to the feature-less baseline.
The second-opinion reviewer sees the same section next to its
`current_request` (a program converting to yards should fail review when
the criteria say meters).

**Revision — the criteria are current state, not a step-0 snapshot:**

- **Code-driven refresh**: a write capability flagged
  `revises_acceptance_criteria` (none today — `memory_remember` only creates
  an inert candidate; the flag is claimed by future profile/settings write
  capabilities) triggers a loop-enforced re-run after its write succeeds:
  one fresh `current_profile_context()` snapshot, and ALL settings-derived
  blocks plus the criteria re-render from it together.
- **Model-requested**: the `acceptance_criteria` catalog action (loop-run
  like the terminals, `action=None`; offered only while the switch is on)
  revises for changes only the model can see. It costs a decide step — the
  right incentive against reflexive re-speccing — the revision call receives
  the prior criteria and the run's observations, and a revision reproducing
  the prior criteria is reported as the no-op it is.

Only the latest criteria render: a revision **replaces** the injected
section, never appends. Every code-driven call is its own trace row
(`action="acceptance_criteria"`, prompts and latency persisted) outside
`step_limit`; a model-requested revision is an ordinary decision whose inner
call — prompts, model, usage, raw response — rides in `observation.data`.
Fail-open: a failed call logs, records a failed step row, injects nothing,
and the run proceeds exactly as with the switch off. Design rationale and
rollout plan: `proposals/2026-07-23-reply-acceptance-criteria.md`.

## The capability registry

`CAPABILITIES` maps each `AssistantActionName` to a `Capability` record:
family, LLM-facing `description` (usage caveats + arg schema) vs operator-facing
`summary`, required/optional args, read/write flags, **tier**
(`log_and_undo` | `confirm` | None for reads), `dry_run`, `output_cap_chars`,
`enabled`, and `prompt_exposed`. Both the prompt catalog and dispatch are
generated from this single object, so disabling a capability removes it from
prompt **and** dispatch at once.

Action names follow `<family>_<verb>` (`memory_query`, `kanban_task_column`),
and each family's members sit contiguously in `AssistantActionName` — the
prompt catalog renders in enum order, so this is what groups related actions
next to each other in the system prompt. A new action goes inside its family
block, not at the end of the enum.

The operator can turn capabilities off at runtime via the
`assistant.disabled_capabilities` setting (a JSON list of names, editable on
`/settings`); `capability_report()` exposes the effective set for inspection.
Internal capabilities (`prompt_exposed=False`) are undo inverses: the model
can never request them — validation rejects them — and they are dispatched
only by `undo_write_intent`.

| Capability | Family | Tier | Undo |
|---|---|---|---|
| `reply`, `ask_clarifying_question` | conversation | terminal | — |
| `acceptance_criteria` | conversation | loop-run (switch-gated) | — (derived state) |
| `memory_query` | memory | read | — |
| `memory_remember` | memory | log-and-undo | `memory_reject_candidate` (internal) |
| `memory_activate` | memory | **confirm** | — |
| `memory_forget` | memory | log-and-undo | `memory_reactivate` (internal) |
| `workspace_read_command` | workspace | read | — |
| `find_uuid` | lookup | read | — |
| `python_run` | python | compute | — |
| `kanban_read` | kanban | read | — |
| `kanban_query` | kanban | read | — |
| `kanban_task_column` | kanban | log-and-undo | inverse move (position-aware) |
| `kanban_task_change_board` | kanban | log-and-undo | inverse board move (board-aware) |
| `kanban_task_complete` | kanban | log-and-undo | move back to prior column |
| `kanban_task_comment` | kanban | log-and-undo | `↩ retracted:` comment |
| `kanban_task_create` | kanban | log-and-undo | `kanban_task_delete` (internal) |
| `kanban_task_set_title`, `kanban_task_set_description` | kanban | log-and-undo | same capability, previous value (text-guarded) |
| `kanban_board_create` | kanban | log-and-undo | `kanban_board_delete` (internal) |
| `kanban_board_set_name`, `kanban_board_set_description` | kanban | log-and-undo | same capability, previous value (text-guarded) |
| `kanban_folder_set_name` | kanban | log-and-undo | same capability, previous value (text-guarded) |
| `set_reminder` | cron | **confirm** (dry-run) | — |
| `edit_file` | workspace | **confirm** (dry-run diff) | — |
| `propose_skill` | skill | log-and-undo | `skill_delete` (internal) |
| `activate_skill` | skill | **confirm** | — |

## Read actions

- **`memory_query`** — hybrid retrieval over curated seed Q&A (static +
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
- **`python_run`** — run a small self-contained Python program in a Pyodide
  (WebAssembly) sandbox (`tools/python_sandbox`): exact math, string
  manipulation, and similar pure compute. A fresh `node runner.mjs` process
  per job. Imports: the standard library plus a curated allowlist
  (`allowed_packages.mjs`: numpy, sympy, mpmath) — the runner loads only the
  allowlisted packages the code imports, from a wheel cache warmed at
  `npm install` (postinstall), so jobs stay offline. Everything else is
  blocked: other packages, network, the host filesystem, and the host
  environment (sanitized `jsglobals`, nulled pyodide escape hatches, minimal
  env). The parent kills the job past 30s CPU (`RLIMIT_CPU`), 100 MB memory
  growth above the post-load baseline (RSS polling), or 60s wall clock.
  Touches no operator data. Gated by the second-opinion review (next
  section). Needs node + a one-time `npm install` in
  `tools/python_sandbox` (`tools.doctor` checks); otherwise the model sees a
  `blocked:` observation. Design spec:
  `docs/superpowers/specs/2026-07-19-python-sandbox-design.md` (repo root).

## Second-opinion gate

Capabilities flagged `second_opinion=True` in the registry (currently only
`python_run`) get an independent LLM review BEFORE dispatch — enforced by the
loop, not prompt discipline, so the deciding model cannot skip it. The
reviewer judges the current request, the decision's `reason`, the deciding
model's reasoning channel, and the program together; a rejection becomes the
step's failed observation (the program never runs, the problems feed back
through the scratchpad, and the exact resubmission is blocked via
`failed_actions`), while an approval dispatches with the full review — verdict,
prompts, the reviewer's reasoning and response — riding in
`observation.data["second_opinion"]` for the trace. Reviewer model: the
`second_opinion` binding on `/agentmodel`, else the assistant's own group.
Fails open (`skipped`/`error` recorded): the gated actions are
side-effect-free compute, so the gate is a quality check, not a security
boundary — write safety stays with the tier system below. Full design:
`second-opinion-design.md`.

## Write tiers

Two tiers, two safety models:

### Log-and-undo (execute now, reversible)

The write executes immediately and is recorded in the ledger
(`assistant_write_intent`) as a row created **atomically in `completed`** —
never `proposed`, so it can never be confirm-executed into a duplicate. The
row's `result.undo` carries the inverse op (`{capability, payload}`) that
`undo_write_intent` replays. Guard rails:

- **Position-aware undo** — a move-undo carries `expect_column` (a board-move
  undo `expect_board`, a field-edit undo `expect_<field>`) and refuses if the
  target has since moved on.
- **State-guarded inverses** — undo of `memory_remember` refuses if the claim is no
  longer candidate/active; undo of `memory_forget` refuses unless still `rejected`;
  undo of `propose_skill` deletes only a still-pending candidate. An undo can
  never clobber a state that changed since the write.
- **Append-only surfaces retract, not erase** — a comment's undo posts
  `↩ retracted: …` (which itself needs no further undo).
- **No-op writes are not recorded** — a `memory_remember` that dedupes into an
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

Intents persist capability names as strings, so rows written before a
capability was renamed still carry its former name. `LEGACY_CAPABILITY_NAMES`
(`agents/assistant_writes.py`) maps former → current name wherever a persisted
name is resolved (confirm-execute and undo), keeping old ledger rows executable
and undoable. Renaming a capability means adding its old name to that map.

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
  `requested_at`/`created_at`/`settled_at` timestamps. When reading key
  ORDER from a step, trust only the text columns (`model_response`): the
  JSONB columns (`args`, observations) are reordered by Postgres —
  length-then-bytes, so reply args always display as
  `3_audit, 2_message, 1_specification` regardless of what the model
  actually wrote.
- Before dispatching a decide call, the run's `metadata.active_call` checkpoint
  stores its step index, exact system/user prompts, request time, model group,
  and an attempt list. Each attempt adds the resolved model name/UUID,
  configured timeout, start time, latest partial reasoning/response (flushed at
  most once per second), and failure when applicable. The checkpoint
  is removed only after the resulting step is durable. This covers the window
  where no `assistant_step` exists yet because the model has not returned.
- Each step also stores an operator-facing debug **`log`** (JSONB list of
  `{label, text, uuid?, href?}` entries): the active profile that drove the
  declared blocks (name, uuid, `/profile` deep link) and both block switch
  states — the first questions when troubleshooting a weird reply. The
  inspector renders it as a collapsed "log" block placed before the model
  request (mirrored in the markdown export); the list is extensible for
  future per-step diagnostics. Debug context never enters the model prompt.
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

  The page updates live while its run is active, riding the same `chat_events`
  SSE stream as /chat (per `chat-frontend-rules.md`: no polling, hidden tab
  stays silent and catches up on refocus). The run/step/checkpoint helpers in
  `db/assistant.py` NOTIFY with `{assistant_run_uuid, event}` — no `room_uuid`,
  so chat clients ignore these payloads — and on an event for the shown run the
  page refetches its own server-rendered HTML (debounced 300ms) and swaps the
  `.as-main` pane in place: one Jinja renderer, no client-side duplicate.
  While the loop is inside a model call, an "in flight" card at the timeline's
  tail shows the streamed partial reasoning/response from the `active_call`
  checkpoint (updated ~1s); the checkpoint is cleared when the step row lands,
  so the card never duplicates a settled step. The card is live-view chrome —
  intentionally absent from the markdown export.
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
`test_kanban_change_board.py`, `test_kanban_set_fields.py`,
`test_kanban_writes_s2.py`,
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
  `memory_remember`/`memory_forget`/`memory_activate` and the profile block.
- `kanban-design.md` — the kanban capability family and why it sits outside
  the worker authority model.
- `second-opinion-design.md` — the pre-execution review gate on `python_run`:
  the reviewer's prompts, verdict, model binding, fail-open policy, and
  inspector rendering.
- `qa-system.md` — the Q&A knowledge base behind `memory_query`.
- `proposals/2026-07-23-reply-acceptance-criteria.md` — the acceptance-criteria
  step's design rationale and rollout plan.
- `proposals/2026-06-25-security-review-mitigations.md` — Finding 4 (the
  unauthenticated confirm boundary).
