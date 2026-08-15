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

`handle()` runs a bounded loop (`STEP_LIMIT = 6`). Its first model-facing
activity is the
[`response_language_classifier`](#response-language-classifier-experiment).
A code-driven **step 0** then precedes the loop: one structured call
establishes the reply's constraints before any work happens (see
[Acceptance criteria](#acceptance-criteria)); neither preliminary call
consumes the step limit. Each loop iteration:

1. **Controls** — apply any pending operator `stop`/`redirect` at the step
   boundary (see [Controls](#controls-stop--redirect)).
2. **Decide** — one grammar-constrained structured call
   (`_decide_next_step`, via the model-group fallback machinery of
   `ModelGroupAgent._structured_completion`) returns an
   `AssistantStepDecision`: `{reason, action, args}` (`args` is forced into
   the schema's `required` list — a non-required field simply gets omitted by
   the model). `reason` is an operator-facing audit note shown in the trace,
   not hidden chain-of-thought. `reply` takes one argument —
   `{"message": ...}`, the answer text in the language of the operator's
   current message. It carries neither a constraints argument (the
   acceptance-criteria step establishes those before the work) nor an audit
   argument (a separate reviewer runs after the message exists, step 4).
3. **Validate** — `_validate_decision` checks the action against the effective
   capability set: unknown/disabled/non-prompt-exposed actions, missing
   required args, and unknown args are all rejected. A rejection records a
   `failed` step and feeds the error back via the scratchpad; the loop
   continues.
4. **Dispatch** — terminal actions (`reply`, `ask_clarifying_question`) post
   the chat message and finish the run — except a `reply`, which is first
   sent to the **reply audit**: a separate model call that reads the
   finished message against the request, the turn's constraints, the
   operator settings, the turn's observations and how the message addresses
   the user (an opening that credits the answer's source instead of giving
   it is a defect — the auditor previously read such an opening as diligence
   and blessed it, so the one check that could have caught the habit was
   reinforcing it; the carve-out is fenced off from tone nitpicking, since
   the auditor is otherwise barred from style complaints), and returns a typed
   `{problems, reason, verdict}` — findings first, so the auditor states what
   it found before committing to a call (the ordering `SecondOpinionVerdict`
   already used; with `reason` leading, local models answered it with the
   verdict word itself). `problems` is one string, a line per defect, rather
   than a list of typed objects: the nested shape asked a small local model to
   hold a container, a per-item schema and two required keys in mind while it
   was also judging the reply. A `revise` verdict bounces the reply as a
   rejected step (the problems flow back through the scratchpad so the model
   fixes the message), capped at `MAX_AUDIT_REJECTIONS = 2` per run so a
   never-approving auditor cannot burn the step limit. The audit resolves
   its model through the dedicated `reply_audit` binding, else the
   assistant's own group; it fails open, so an unbound or unreachable
   auditor sends the message. Every verdict lands in its own `reply_audit`
   trace row with its model, duration and prompts. A clarifying question is
   not audited. Reads and log-and-undo writes execute immediately.
   Confirm-tier writes are **proposed**, never executed inline.
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
  invent placeholder values, never claim a write that didn't run
  (anti-fabrication), and **answer without narrating the retrieval** — no
  opening that credits memory, records or a lookup before the answer, and no
  imitating the shape of one's own earlier replies. That last clause is load
  bearing: an opening the model adopts once appears in every assistant turn
  of `conversation_history_xml` thereafter, and the model reads its own habit
  as the room's convention. The rule never quotes an offending opening —
  a phrase in a prompt is a phrase the model emits. Voice beyond this belongs
  to `assistant_persona`, which the system prompt does not compete with; this
  rule is about a reply that withholds the answer, not about tone.
- **User prompt** — the sections are emitted as top-level sibling tags (no
  root wrapper: models recognize the start/end tags without a single-rooted
  document, and a wrapper would cost one indentation level on every line;
  each section is still individually ElementTree-escaped XML). The task
  leads the prompt — with the request buried at the bottom under a long
  profile/history, weaker models answered the surrounding context instead of
  the request — and the supporting context follows. In order: the
  **current request** (`<current_user_request>`, bare unless it was shortened:
  the section order carries the emphasis and the time anchor is
  current_local_time at the end; see
  [Long requests](#long-requests) for the shortening attributes and the
  `<current_user_request_summary_markdown>` section that follows the request
  when they are present), the **response-language classification**
  (a bare `<reply_language_markdown>`: the suffix states the format and the
  system prompt names the section as reference data, so neither a `format` nor
  an `authority` attribute repeats what is already said —
  directly after the request; reason and a score-free language list ranked by
  confidence), the **acceptance criteria**
  (`<acceptance_criteria_markdown>`, directly after the language block so the
  request and its constraints travel together — present when the step-0 call
  succeeded; see [Acceptance criteria](#acceptance-criteria)), the
  transcript (`<conversation_history_xml>`, `kind == "message"` rows
  only, newest `MAX_RECENT_MESSAGES = 30`; bare except for
  `assistant_messages="omitted_after_fresh_read"`, which the system prompt
  reads to explain the gap it describes; a message shortened by
  [Long requests](#long-requests) carries that section's attributes and may be
  followed by a `<message_summary_markdown>` sibling), the **scratchpad** of steps
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

## Long requests

The request is the one prompt section whose size the operator sets directly —
a pasted log or backtrace is a request too — and it renders into every prompt
of every step plus the criteria, classifier, second-opinion and audit calls,
so an uncapped paste is multiplied by the whole turn.

Past `CURRENT_REQUEST_MAX_CHARS = 8000` the request travels with its **middle**
dropped: `_truncate_middle` keeps half the budget from each end and writes the
dropped count into the seam. Both ends, because both carry content — a pasted
log opens with the command and closes with the failure — and the marker is in
band rather than only in the tag's attributes because the model reads the
section as prose, and without it a backtrace appears to step from one frame
straight to an unrelated one. Characters, not bytes: every other cap here
counts characters, and slicing UTF-8 by byte splits codepoints.

A shortened request carries `truncated="middle"`, `original_chars` and
`included_chars`. Those are code-owned facts, so they live in attributes; the
description of what was dropped is model-written, so it renders as its own
`<current_user_request_summary_markdown>` section directly after the request
— named off the request so the tie between the two is visible in the tag
rather than only in the prose — with its
authority declared in a code-owned system-prompt paragraph
(`TRUNCATED_REQUEST_SECTION`, carried by every prompt that renders the
request). `_append_current_user_request` is the single renderer all five
builders call, so the cap, the attributes and the summary cannot drift apart
between the decide loop and the calls that judge its output.

The description comes from a code-driven **summary call** that runs before
everything else on the turn — before the language classifier, whose own prompt
carries the request. It reads at `REQUEST_SUMMARY_INPUT_MAX_CHARS = 60 000`,
far above the prompt cap because this call exists to see what the others
cannot; its own middle is dropped the same way past that, and its system
prompt tells it to report the gap rather than describe what it never saw. It
makes one structured call (`RequestSummary`: `content_type`, `summary`,
`key_details`) on the assistant's own model group, records its own trace row
outside the step budget, and fails open — a failed or unbound call leaves the
turn running on the shortened request alone. Requests inside the cap skip it
entirely: latency on the rare turn, not on all of them.

The reviewer and the auditor both judge the reply against the request, so both
are told a shortening is not itself a defect: the reply was written against the
same shortened copy, and a part that falls in the dropped middle is not
something it failed to cover.

History messages get the same cut at a tighter
`HISTORY_MESSAGE_MAX_CHARS = 2000` — an old message is context rather than the
task, and a paste from an earlier turn would otherwise arrive in every prompt
of every later turn.

**The description outlives its turn.** A successful summary call writes its
Markdown to the triggering message's `meta.request_summary_markdown`, and
`_append_prompt_message` replays it as a `<message_summary_markdown>` sibling
directly after that message in `conversation_history_xml` — the same adjacency
the top-level pair uses, and `<message>` stays a leaf so every message renders
the same shape. Without it the description died with the turn: three long
pastes followed by "retry" left the model reading three shortened messages and
nothing about what was cut from any of them, while the summaries that had
already been computed and paid for sat unread on the runs that wrote them.
The message is the right home rather than the run — every turn already reads
the room's messages, so the replay costs no extra query and does not depend on
finding the run that produced it. The write is additive (no other `meta` key is
touched, no NOTIFY, nothing a chat client renders changes) and best-effort:
losing it costs later turns the description, never this turn. A message with no
stored summary — posted before this existed, or summarized by a call that
failed — renders shortened and undescribed, as it did before.

The path is lossy by design for now: the dropped middle is gone for the run.
Giving the assistant tools to grep and page through the full text — turning
"find the real exception in this log" into a program it writes rather than a
summarization it trusts — is the next step, not part of this.

## Response-language classifier experiment

Before skill retrieval, acceptance criteria and the decide loop, the assistant
runs one narrow structured classifier. It returns:

- `reason`: a short operator-facing explanation of the evidence, which also
  carries any omission or uncertainty the classifier is aware of;
- `languages`: BCP-47 `{code, score}` rows, where score uses PlanExe's
  `1=strong negative, 2=weak negative, 3=neutral, 4=weak positive,
  5=strong positive` scale.

The request includes the current operator message, up to six earlier messages
of either role and every validated `languages.rows` entry (tag, level, stance,
note). The assistant's earlier turns are in there because they are the only
record of what language the conversation has actually been running in — real
evidence for the one call whose job is to decide the language. The
anti-perpetuation guard that omission used to provide is now a stated
precedence instead: a previous wrong-language reply loses to the current
request, and the disagreement goes in `reason`. The prompt explicitly separates
the language of the
reply's narration from languages appearing as quoted examples: a request for
multilingual phrases with English explanations remains an English reply unless
the operator asks for a genuinely multilingual answer. A broad requested
language selects the family, while a compatible `prefer` profile row supplies
the exact variant: “translate to English” with preferred `en-GB` is classified
as `en-GB`, not broadened to `en`. Every declared code must be copied and
scored exactly.

The output boundary performs one narrow, observable repair for scorer models
that reason about the correct variant but still emit its broad parent tag: it
refines that tag to the single compatible preferred variant (or sole compatible
non-avoid variant) and appends a description of the repair to `reason`. It
never invents a score for an omitted row; omissions are appended to `reason`
too. This keeps the useful classification exact without hiding upstream
model-quality failures.

The structured output is persisted as a `response_language_classifier` trace
row with the prompts, model response, scores, model identity and
duration. Code also renders it as compact Markdown for every later assistant
model call: reason, then language tags sorted by descending score (ties retain
the classifier's original order). Numeric scores are omitted from the
Markdown; the system prompt explains that ordering carries confidence and that
not every scored candidate must appear in the reply:

```xml
<reply_language_markdown>
## Reason
...

## Languages - highest confidence first
- `en-GB`
- `da`
</reply_language_markdown>
```

The dedicated binding-only `response_language_classifier` role allows
scorer-model comparisons on `/agentmodel`; when unbound it falls back to the
assistant's group. If neither binding has a usable group, no Markdown block is
added. Call failures are traced and fail open.

**Status (2026-07-26):** live exploratory use is very close to the operator's
target. Multilingual-content prompts and translation intent classify well, and
a broad target now retains the compatible preferred profile dialect (`English`
with preferred `en-GB` becomes `en-GB`). The remaining gate is a repeatable
edge-case corpus and confirmation that the ranked Markdown reliably controls
downstream language delivery; further prompt redesign is not currently
indicated.

This deliberately supersedes using the broad acceptance-criteria call for
language routing. The classifier has one responsibility, a typed multilingual
result, a dedicated model binding and an independently inspectable trace. It
does not repeat deterministic locale settings or inject example dialect
vocabulary. The acceptance-criteria experiments remain useful evidence: they
showed that broad planning added latency and another authority surface without
a measured locale improvement. A correct classification and faithful delivery
by the reply model are separate metrics; the classifier can correctly choose
`en-GB` even when a weaker reply model later mixes British and American forms.

The Markdown is a downstream context projection, not a lossless replacement
for the structured result. Scores stay in the trace so classifier models can be
evaluated; the ranked list saves prompt tokens and avoids asking later models
to reinterpret Likert values. It currently has no inclusion threshold, so
tests must establish whether its reason and ordering are sufficient for
genuinely multilingual and low-confidence cases.

## Acceptance criteria

On every turn a code-driven **step 0** establishes the reply's constraints
before the decide loop starts — enforced by the loop, so the model cannot skip or forget it.
One structured call returns an `AcceptanceCriteria`:

- `processing` — preferences that steer the WORK (the target unit for an
  ambiguous conversion, the timezone for a reminder).
- `formatting` — preferences that steer the FINAL message (separators, date
  format, temperature unit, spelling). The system prompt directs the call
  through the formatting guide line by line: the criteria are what the reply
  is checked against, so a preference omitted here is one nobody verifies.
- `assumptions` — every ambiguity resolved by a settings-based assumption,
  stated so the operator can spot a wrong one. Assumptions are made only
  where the settings provide a default; otherwise the ambiguity is recorded
  as unresolved and the normal `ask_clarifying_question` path handles it.

Each is a required, non-empty **string**, not a list. A list of terse
fragments invites one fragment and an empty sibling: a call that has already
read the formatting guide reasons that the guide applies itself later and
returns `[]` for `formatting`, which then reaches the second-opinion reviewer
as "no formatting constraints." `min_length=1` closes that exit — a field with
nothing to carry must say so, which the operator can check, where a blank
field cannot be told apart from an oversight. For the same reason the system
prompt carries no worked example: a copyable one gets emitted verbatim in
place of criteria derived from the actual request.

Response language is deliberately absent. The preceding
`reply_language_markdown` from the dedicated classifier is injected directly
into decide and second-opinion prompts; the broader criteria call does not
reinterpret or duplicate it.

The call has its own small persona prompt
(`ACCEPTANCE_CRITERIA_SYSTEM_PROMPT`, not the assistant's working prompt);
Inputs: the current request, the last few messages of either role
(`ACCEPTANCE_CRITERIA_MAX_MESSAGES = 6`) — how the assistant has been
formatting and phrasing its replies is exactly the continuity these criteria
establish — plus `user_settings_json` and the formatting
guide rendered from the criteria snapshot profile regardless of the
`assistant.formatting_guide` switch (which gates only the decide-prompt
injection). NOT the action catalog — the call plans constraints, not actions.

The result renders as an `<acceptance_criteria_markdown>` section directly
after `<current_user_request>` in every decide step: a Markdown projection of
the structured result, since local models read Markdown faster than the equivalent
JSON (rainbox's own benchmarks) and nothing downstream parses the section back.
The parsed object stays the authority and is what the trace row records — the
same split the response-language classifier uses. Each field collapses to one
line, so a model-written criterion cannot forge a heading into the section that
holds it. Its authority lives in one code-owned system-prompt sentence, and
`_system_prompt()` swaps the source-priority block for a variant ranking
`acceptance_criteria_markdown` directly below `current_user_request`.
The second-opinion reviewer sees the same section next to its
`current_user_request` (a program converting to yards should fail review when
the criteria say meters).

**Revision — the criteria are current state, not a step-0 snapshot:**

- **Code-driven refresh**: a write capability flagged
  `revises_acceptance_criteria` (none today — `memory_remember` only creates
  an inert candidate; the flag is claimed by future profile/settings write
  capabilities) triggers a loop-enforced re-run after its write succeeds:
  one fresh `current_profile_context()` snapshot, and ALL settings-derived
  blocks plus the criteria re-render from it together.
- **Model-requested**: the `acceptance_criteria` catalog action (loop-run
  like the terminals, `action=None`) revises for changes only the model can
  see. It costs a decide step — the
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
| `acceptance_criteria` | conversation | loop-run | — (derived state) |
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
  `requested_at`/`created_at`/`settled_at` timestamps. The JSONB columns
  (`args`, observations) are reordered by Postgres — length-then-bytes — so
  never read key order from them; the text columns (`model_response`) hold
  what the model actually wrote.
- Before dispatching a decide call, the run's `metadata.active_call` checkpoint
  stores its step index, exact system/user prompts, request time, model group,
  and an attempt list. Each attempt adds the resolved model name/UUID,
  configured timeout, start time, latest partial reasoning/response (flushed at
  most once per second), and failure when applicable. The checkpoint
  is removed only after the resulting step is durable. This covers the window
  where no `assistant_step` exists yet because the model has not returned.
- A model that returns an unusable response — one that arrived whole and then
  failed the schema or a caller's validator — is asked again up to
  `ModelGroupAgent.REJECTED_RESPONSE_RETRIES` (3) times before the group falls
  back to the next candidate. Each retry appends, after the unchanged prompt,
  every response rejected so far (as assistant turns) and why each was
  rejected (as a `<rejected_response>` user turn), so
  the model can see what was wrong with what it wrote. Every retry is its own
  attempt entry, so the trace shows each rejected response beside its reason.
  A call that never produced a response (timeout, transport error, empty
  stream) is not retried — it falls straight through to the next candidate.
- An action may record a **`timing`** block in its observation `data`, and
  `memory_query` does: `{phases: [{name, ms, started_at}], embeddings: {count,
  ms, chars, models, calls, dropped}}`. The phases are `claim retrieval`,
  `seed KB load`, `recall filter`, `seed retrieval`/`seed fallback` — timed in
  `finally`, so a phase that raised still reports what it spent. The
  embeddings come from `llm.capture_embeddings()`, a dispatcher handler that
  times every embedding call made anywhere inside the action (the embedder is
  reached from claim vector search, the seed KB's populate and its retrieve).
  The /assistant page renders the phases as a table under the action result
  and the embedder's totals beneath them; each embedding call is also a row in
  the run's model-call waterfall (kind `embedding`), where it lands on the same
  wall-clock as the LLM calls and usually explains the gaps between them. Any
  action that records the same block gets both renderings for free.
- Embedder time is counted apart from LLM time everywhere: `assistant_run_stats`
  reports `embedding_calls`/`embedding_ms` beside the token totals rather than
  inside them (the embedder produces no tokens, so folding its seconds in would
  drag throughput down against work it never did), and the dashboard splits the
  run's wall-clock into model / embed / action. embeddinggemma shares the local
  runtime with the assistant's own model, so retrieval that embeds can evict
  what the last decide call warmed — that cost is visible rather than buried in
  "action" time.
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
  thinking shows on the /assistant step ("model reasoning", collapsed); it is
  not a chat row (see the progress row below). A non-reasoning model emits no
  reasoning channel, so nothing is stored or shown. On a decide-call crash
  (e.g. a timeout mid-think) the failed step keeps the partial reasoning.
- Every call a run makes is a step row, including the ones it could not
  make: with no model group bound the language classifier and the
  acceptance-criteria call record a `skipped` row (see `data-model.md`
  §assistant_step) rather than vanishing, keeping the trace a complete account
  of the turn. The per-step debug `log` is assembled before the first model
  call so those rows carry it too.
- The journal `result` is a short summary plus pointers
  (`assistant_run_uuid`, step count) — the tables are the trace, the journal
  is not.

A run narrates itself to the room through **one** `kind="progress"` row,
rewritten in place as it works (`_set_activity` → `_publish_progress` →
`db.upsert_progress`): which step it is on, what it is doing, what it has cost
(`db.assistant_run_stats` — LLM calls, in/out tokens, throughput), and a link
to `/assistant`. The row also carries `meta.assistant_run_uuid`, which /chat
uses to send a click anywhere on the bubble to that run — and under the status
text the bubble counts up ("Worked for 21s", "Worked for 5m 43s") from the
row's `created_at`, which is why a turn that reuses the row a dead run left
behind restarts that clock (`db.upsert_progress(restart=True)`). It replaced a
`thinking` bubble and a `debug-assistant` bubble
per step, which buried the conversation under a dozen rows per turn while the
same state was already on the step rows. Like any progress row it is reaped
when the real reply lands, so a finished turn leaves the answer, not the
bookkeeping — but every terminal post (reply, clarifying question, stop
message, failure notice) carries `meta.assistant_run_uuid`, which /chat renders
as an `Inspect ↗` entry in the row's kebab menu. A reply worth questioning is exactly
when the trace is wanted, and the bubble that linked to it is gone by then. The
pointer rides in `meta`, never in the text: the text is the answer, it is what
Copy yields, and it is what the model reads back as conversation next turn.

After every terminal state the assistant stores an immediate deterministic
failure digest when applicable, then enqueues the
**`assistant_run_summarizer`** agent (off the critical path), which makes one
structured call over the trace and stores a `{trigger, obstacles[], outcome}`
digest on `assistant_run.summary` for the inspector. The deterministic digest
means a failed run is useful even if the summarizer model is unavailable; a
later successful summarizer call may replace it. The summarizer posts no chat
and enqueues nothing, so it can never summarize itself.

`outcome` is what the /assistant and /assistant-overview chips read: anything
but `resolved` shows as **Unresolved**, so a mis-graded `partial` is
indistinguishable from a run that failed. The delivered reply is the evidence
that field is judged on, so it reaches the summarizer shortened from the middle
(`agents.base.truncate_middle`, 2000 chars) rather than cut from the end — an
answer's closing lines are where it says whether it answered — and the system
prompt tells the model what the marker means, so a shortened reply is never
itself a reason to lower the outcome.

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

  A **Model calls** card above the timeline is the run's profile: one bar per
  model call, placed on the run's wall-clock span and scaled by its duration,
  so the gaps between bars are the time no model was working. The dashboard
  counts the same calls. Both read `_llm_calls`, which is deliberately not a
  count of step rows — three calls ride inside something else and would
  otherwise be invisible, their seconds booking as "action" time: the
  second-opinion review (its own table), the acceptance-criteria revision's
  inner call, and the memory recall filter's scorer (both in a step's
  observation payload, with `requested_at` + `usage`). A call with no recorded
  start is placed at its row's end minus its duration.

  Timeline rows are numbered by their position ("Step 3 of 4"), not by
  `step_index` — the code-driven rows share the decide index they sit beside,
  so numbering by it repeated one number several times over. The decide-loop
  index stays in the anchor's tooltip. A code-driven row is marked `warm-up`
  (its call went out before the first decide call: the response-language
  classifier, the acceptance-criteria step 0) or `follow-up` (after: the reply
  audit, a mid-run criteria refresh), so the real ReAct steps are scannable
  between them. Those rows show no decision dump and no action call — neither
  happened — and their result is dropped when it only repeats the response
  above, which for such a call is the same content twice.

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
  all runs, each row linking into the inspector. The Steps column is a bare
  count of step rows — the same number /assistant's timeline numbers, warm-up
  and follow-up calls included, so one run never reports two different counts.
  While a run is still working it has no digest, so its Summary cell carries
  "Step N" instead: the overview's only progress readout. No denominator —
  `step_limit` bounds decide steps while this counts every row, so a long run
  read "Step 8 of 6".

## Testing

The single live-model seam is `_decide_next_step`; tests drive the loop with
scripted decisions from `agents/assistant_fakes.py`. Coverage:
`agents/test_assistant.py` (loop, validation, trace),
`agents/test_assistant_actions.py` (read actions),
`agents/test_assistant_writes.py` (tiers, intents, undo),
`agents/test_assistant_control.py` (stop/redirect),
`agents/test_assistant_long_request.py` (the middle cut, the summary call),
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
