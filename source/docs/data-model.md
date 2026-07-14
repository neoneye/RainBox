# Data Model

## Purpose

This document maps the important Postgres tables and relationships in
`rainbox`.

The schema is created by `db.create_all()` from the SQLAlchemy models in
`db/models.py`. There is no Alembic: schema evolution is ad-hoc idempotent
helpers run from `init_db()` on startup (add-column-if-missing, conditional
constraint updates, one-time backfills). See Finding 5 of
`proposals/2026-06-25-security-review-mitigations.md` for the plan to move to
explicit migrations.

## Supervisor Tables

### `inbox`

Pending work for agents — the input side of the queue. Rows are ephemeral:
`take_item` pops the oldest row for an agent, deletes it, and opens a `journal`
row in `processing`.

Key fields:

- `agent_uuid`
- `enqueued_at`
- `payload`

### `journal`

Durable task history: the record of work after it leaves the inbox. The row
mutates through its lifecycle; for richer agents the `result` only points at a
fuller per-step record (e.g. the `assistant_run` trace beneath it).

`id` is a **UUID** (the `journal_id` threaded across the codebase), so it is
globally unique and self-describing — but not monotonic: "oldest first" must
order by `started_at`/`enqueued_at`, never by `id`.

Key fields:

- `inbox_id`
- `agent_uuid`
- `state`: `processing`, `completed`, `failed`, `stopped`
- `payload`
- `result`
- `routed_at`

The supervisor routes completed, unrouted journal rows to downstream agents
based on `agents/config.py`.

## Model Configuration Tables

### `model_config`

Synced model rows from every registered LLM provider (LM Studio, Jan,
…). Keyed by `(provider, model_name)`. Rows are not deleted when models
disappear from a provider; `available` changes instead. See
[llm-providers.md](llm-providers.md) for the sync contract.

### `model_config_override`

Named parameter overrides for a base model config (`overrides` JSONB,
shallow-merged over the base `arguments`). An unnamed override renders a
synthesized label derived from its effective arguments (e.g. `t0.5 c32k tool`).

### `model_group`

Named fallback group. Carries three per-capability membership constraints —
`function_calling_constraint`, `structured_output_constraint`,
`reasoning_constraint` — each a tri-state: `dont_care`, `must_have`,
`must_not_have`.

### `model_group_member`

Priority-ordered members of a model group. A member can reference a base config
or an override.

### `agent_model_binding`

Maps code-defined agent UUIDs to model groups.

## Chat Tables

### `chat_user`

Human and agent identities (`user_type`: `human` | `agent`). Exactly one human
exists (no sign-up/auth); agent rows reuse the code-defined agent UUIDs from
`agents/config.py`.

### `chatroom`

Chat rooms.

Key fields:

- `uuid`
- `name`
- `created_by`
- `folder_uuid`
- `position`

### `chatroom_folder`

Folder tree for organizing chat rooms in the left panel.

### `chatroom_member`

Room membership.

### `chat_message`

Room messages. The autoincrement `id` doubles as the ordering / incremental-fetch
cursor (clients ask for messages `after` their last id).

Key fields:

- `room_uuid`
- `sender_uuid`
- `text`
- `content_type`: `markdown`, `json`
- `kind`: `message`, `debug-memory`, `debug-query`, `debug-router`, `progress`,
  `thinking`, etc.
- `meta`: JSONB structured attachment for interactive messages — e.g. a
  confirm-tier write proposal stores `{write_intent, capability, step_link}` so
  chat can render confirm/reject controls; the facts-invalidation marker stores
  its timestamp here.
- `streaming`: true while the row's `text` grows in place token-by-token
  (flipped false on the final flush; the UI shows a live cursor and withholds
  feedback buttons while true).

Only `kind="message"` rows are user-facing prompt/chat content. Diagnostic rows
are for operators and audit.

### `workspace_shell_state`

Per-room working directory (plus an `env` column kept at the fixed baseline)
for the deterministic workspace shell agent, so `cd` survives between messages
and restarts.

## Conversation Tables

### `conversation_run`

State for bounded persona-to-persona conversations managed by the
`conversation` agent.

The transcript stays in `chat_message`; this row is the only mutable runtime
state the feature adds. Two compare-and-set guards (`tick_count` for manual
ticks; `last_speaker_journal_id`/`turn`/`active_turn` for routed completions)
keep the turn loop idempotent under double-delivery and restarts.

Key fields:

- `id` (UUID primary key)
- `room_uuid`
- `status`: `running`, `paused`, `finished`, `stopped`, `failed`
- `participants`
- `turn`, `tick_count`, `active_turn`, `active_speaker_uuid`
- `turn_policy`, `budget` (JSONB)
- `retry_count`, `stop_requested`, `reason`

## Assistant Tables

### `assistant_run`

Durable trace header for one assistant turn — the queryable source of truth
(`journal.result` holds only a short summary). Identity is the `uuid` primary
key (no integer surrogate); children reference it via `run_uuid`.

Key fields:

- `uuid` (primary key)
- `room_uuid`
- `agent_uuid`
- `journal_id`
- `status`: `running`, `stopping`, `finished`, `stopped`, `failed`, `killed`
- `step_limit`
- `started_at`, `finished_at`
- `final_summary`
- `metadata` (run/model diagnostics). While a decide call is in flight,
  `metadata.active_call` checkpoints `{step_index, system_prompt, user_prompt,
  requested_at, model_group_uuid, attempts[]}`. Attempt entries retain the
  resolved model UUID/name, configured timeout, timestamps, and any error so a
  killed worker can be reconstructed before an `assistant_step` exists. The
  checkpoint is cleared after step persistence or interruption recovery.
- `summary`: a post-completion JSONB digest (`{trigger, obstacles[], outcome,
  summarized_at}`); failures receive a deterministic digest immediately, while
  the off-path `assistant_run_summarizer` may later replace it with a
  model-produced digest

### `assistant_step`

One logical assistant loop step as **a single mutable row**: a normal action
step is inserted at `phase="running"` (so a crash mid-action leaves a durable
row) and updated in place to its terminal phase; terminal-only steps (`final`,
failed validation, `control`) are a single insert. The step's `uuid` is stable
for its whole life, so write intents can reference the producing step.

Key fields:

- `run_uuid`, `step_index`
- `phase`: `planned`, `running`, `observed`, `failed`, `final`, `control` —
  the step's *current state*, not a per-transition log
- `action`, `reason`, `args`
- `system_prompt`, `user_prompt`: the exact decide-call prompt
- `reasoning`: the model's native thinking channel from the decide call
  (captured via instrumentation while the structured output streamed); NULL
  for a non-reasoning model. Distinct from `reason`, the schema's short
  operator-facing audit note
- `model_response`: raw provider content from the decide call; on interruption,
  the most recently checkpointed partial structured response
- `observation_preview` (capped, model-facing) and `observation` (the full
  `{ok, text, data}` JSONB — the authoritative function-result record)
- `error`
- `model_group_uuid`, `model_uuid`
- `input_tokens`, `output_tokens`, `duration_ms`: the decide call's usage/latency
- `requested_at`, `created_at`, `settled_at`: model request → response →
  observation recorded

An interrupted decide call is recovered into a terminal `failed` step from the
run-level `active_call` checkpoint. Its `action` may be NULL because the worker
vanished before a structured decision existed, while its prompts, model,
configured-timeout error, request time, and measured elapsed duration remain
inspectable.

### `assistant_control`

Pending runtime controls for an assistant run; the loop polls at each step
boundary (`stop` ends the run cleanly, `redirect` injects a new instruction).

Key fields:

- `run_uuid`
- `command`: `stop`, `redirect`
- `payload`
- `state`: `pending`, `applied`, `ignored`
- `requested_by_uuid`
- `applied_at`
- `note`

### `assistant_write_intent`

Controlled write proposals created by the assistant. Confirm-tier writes are
executed only through the confirmation API, not directly by model output; the
payload is bound by `payload_hash` so a confirmed intent executes exactly what
was previewed. Log-and-undo writes do not use this table.

State machine: `proposed` → `confirmed` → `executing` → `completed` | `failed`,
plus `rejected` (operator declined) and `undone` (a completed write reverted).

Key fields:

- `uuid`
- `run_uuid`, `step_uuid` (the producing step)
- `capability_name`
- `payload_hash`, `payload`, `preview_text`
- `state`
- `room_uuid`, `agent_uuid`
- `result`, `error`
- `confirmed_at`, `executed_at`, `completed_at`, `confirmed_by_uuid`

## Configuration Tables

### `app_setting`

Operator configuration, addressed by `key` (e.g. `backup.repo`). The code-side
registry in `db/settings.py` is the source of truth; this table only persists
values. Reads resolve **DB value → env var → registry default**. Edited at
`/settings`, read-only in Flask-Admin. See
[the configuration proposal](proposals/2026-06-07-user-configuration-in-postgres.md).

Key fields:

- `key` (unique; no `uuid` — rows are key-addressed)
- `value`: text, `NULL` = unset (falls through to env/default); empty string also
  counts as unset for `string`/`json` types
- `value_type`: `string`, `bool`, `int`, `json` — a registry-owned cache,
  reconciled on startup
- `secret`: when true the value is env-only (never stored here) and redacted in
  the UI/logs
- `description`

## Cron Tables

Back the `/cron` scheduler. The seeded daily database backup is a `cron_job` with
`action_type="backup"`.

### `cron_folder`

Folder tree for organizing jobs; a folder (and its subtree) can be disabled.

### `cron_job`

A scheduled job. Key fields:

- `uuid`, `name`, `description`, `enabled`, `folder_uuid`, `position`
- `cron_expr`, `timezone`: `localtime` | `UTC`
- `action_type`: `message`, `command`, `backup`, `memory_sync`
- `target` / `message` (message jobs), `command` (command jobs; for a `backup`
  job, an optional destination overriding the `backup.repo` setting)
- `max_retries`: auto-refire (trigger `retry`) up to N times after an error
  outcome; 0 = off
- `origin_run_uuid`, `origin_step_uuid`: provenance for jobs the assistant
  created (e.g. a reminder via `set_reminder`); NULL for manual jobs
- `next_run_at`, `last_fired_at`

### `cron_run`

One row per firing — the logs. The outcome lands on the same row: in-process
actions set `status` synchronously; async command fires stay `pending` until
the workspace-shell agent writes back (the tick sweeps abandoned runs to
`error`).

Key fields:

- `cron_uuid`, `trigger` (`scheduled`, `manual`, `retry`, …), `fired_at`
- `status`: `pending`, `ok`, `error`; `finished_at`, `error`
- `debug`: true for a dry-run firing
- `journal_id`: the workspace-shell journal row holding a command's full output

## Kanban Tables

### `kanban_board`

Board metadata and folder placement.

### `kanban_board_folder`

Folder tree for organizing boards.

### `kanban_column`

Columns inside a board.

### `kanban_task`

Cards/tasks. Key fields include board/column UUIDs, assigned `agent_uuid`,
lease fields (`claimed_by`, `claimed_at`, `claim_expires_at`), title,
description, and position.

### `kanban_task_event`

Append-only task audit trail: created/moved/claimed/progress/done/failed and
other event details.

## Memory Tables

### `memory_claim`

The canonical remembered belief.

Key fields:

- `uuid`
- `agent_uuid`
- `scope`: `global`, `agent`, `room`, `project`
- `room_uuid`
- `kind`: `fact`, `preference`, `project_decision`, `procedure`,
  `episode_summary`
- `subject`, `predicate`, `object`
- `text`
- `confidence`
- `status`: `candidate`, `active`, `superseded`, `rejected`, `expired`
- `sensitivity`: `public`, `private`, `secret`
- `supersedes_uuid`, `conflicts_with_uuid`
- `expires_at`
- Trust-hardening fields: `epistemic_confidence`, `retrieval_strength`,
  `support_count`, and the normalized identity keys `subj_pred_key` /
  `value_key` / `key_version` (used for conflict detection and tombstone
  matching)

### `memory_evidence`

Provenance for memory claims.

Key fields:

- `memory_uuid`
- `provenance`: `observed_from_source`, `inferred_by_model`,
  `confirmed_by_user`, `imported_from_transcript`
- `source_type`: `chat_message`, `journal`, `file`, `api`, `manual`,
  `transcript`
- `source_id`
- `excerpt`
- `created_by_uuid`

One memory claim can have many evidence rows.

### `memory_rejected_value`

A tombstone: a `(scope, room/agent, subj_pred_key, value_key)` combination that
was rejected or superseded and must not silently return. Snapshots the rejected
claim's text and evidence summary so a later suppression stays explainable even
if the original rows change.

Key fields:

- `scope`, `agent_uuid`, `room_uuid` (part of the uniqueness key — a room/agent
  tombstone is scoped; only a global tombstone applies everywhere)
- `subj_pred_key`, `value_key`
- `claim_text`, `evidence_summary`, `reason`
- `created_from_uuid`, `created_by_uuid`
- `hit_count`, `last_hit_at`

### `memory_embedding`

Auxiliary pgvector embeddings (768-dim) for active memory claims, used by
hybrid memory retrieval. A claim with no row here degrades to lexical-only
retrieval — never an error. Kept separate from `memory_claim` so multiple
models/text-hashes can coexist during rebuilds.

Key fields:

- `memory_uuid`
- `model_name`
- `text_hash`
- `embedding`
- `embed_dim`
- `created_at`, `updated_at`

## Feedback And Telemetry Tables

### `feedback_event`

User feedback on agent replies.

Key fields:

- `room_uuid`
- `message_uuid`
- `agent_uuid`
- `rating`: `upvote`, `downvote`
- `comment`
- `metadata`

Metadata snapshots rated text, latest prior human message, and same-turn
diagnostics when present.

### `retrieval_event`

Append-only retrieval telemetry.

Key fields:

- `target_type`: `qa_entry`, `memory_claim`, `skill`
- `target_id`
- `stage`: `retrieved`, `accepted`, `rejected`, `used`, `downvoted`,
  `considered`, `injected`
- `query`
- `room_uuid`
- `agent_uuid`
- `journal_id`
- `source`
- `retrieval_rank`
- `retrieval_score`
- `filter_label`
- `metadata`

Counters should be derived from this table.

## Git Tables

### `git_folder`

Folder tree for organizing registered repositories.

### `git_repo`

Tracked local Git repositories and their display placement in the Git page.

## Prompt Tables

### `prompt_folder`

Folder tree for organizing stored system prompts on /prompt.

### `prompt`

One version of a system prompt. Versioning is the ancestor chain: cloning is
the only way to make a new version, and the clone's `parent_uuid` points at
the row it was copied from (null = a lineage root). Direct-chat rooms may
link a version via `chatroom.prompt_uuid`.

## Profile Tables

### `profile_folder`

Folder tree for organizing person profiles on /profile.

### `profile`

A person profile: `name` is the standalone tree label; all person fields
live in the sparse `data` JSONB (schema = `profile_fields.PROFILE_FIELDS`;
absent key = unset). Connector-written observations live under
`data["dynamic"]` and survive human-facing saves.

## Eval Tables

### `eval_case`

Benchmark case promoted from feedback or hand-authored.

Key fields:

- `source_feedback_uuid`
- `name`
- `case_type`: `chat_reply`, `memory_retrieval`, `query_answer`,
  `tool_output`
- `split`: `train`, `holdout`, `regression`
- `input`
- `expected`
- `rubric`
- `status`: `candidate`, `active`, `archived`

### `eval_run`

One execution of a set of cases.

Key fields:

- `name`
- `agent_role`
- `config`
- `started_at`
- `finished_at`
- `summary`
- `is_baseline`

### `eval_result`

One case result inside one run.

Key fields:

- `eval_run_uuid`
- `eval_case_uuid`
- `score`
- `passed`
- `details`

## External Tables

The Q&A pgvector table is managed by LlamaIndex/PGVectorStore and is represented
read-only in Flask-Admin as `SeedMemoryKb`.

## Design Notes

- Inbox/journal payloads are JSON-encoded `text`, not JSONB.
- Model configs, memory/eval metadata, and telemetry metadata use JSONB.
- UUIDs are native Postgres UUIDs. Where a UUID is the primary key
  (`journal`, `assistant_run`, `conversation_run`), it is not monotonic —
  ordering uses timestamps, never the id.
- **Folder-tree house style:** tree references (`folder_uuid`, `parent_uuid`)
  and most cross-subsystem references (`agent_uuid`, `cron_uuid`,
  `board_uuid`, …) are plain uuid columns with **no FK constraint** —
  validation is app-side (e.g. `validate_cron_tree`, `validate_kanban_tree`).
  Real FKs with CASCADE are used inside a subsystem where the parent-child
  tie is structural (chat members/messages → room, eval results → run,
  assistant children → run, memory evidence/embeddings → claim).
- Timestamps are timezone-aware.
- Each subsystem has a dedicated page (`/chat`, `/cron`, `/kanban`, `/git`,
  `/memory`, `/assistant`, `/settings`) as its primary UI; Flask-Admin at
  `/admin` is the raw table-level fallback (read-only for `app_setting`,
  `data_seed_memory`, `retrieval_event`, and `memory_embedding`).
