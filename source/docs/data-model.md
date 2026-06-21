# Data Model

## Purpose

This document maps the important Postgres tables and relationships in
`rainbox`.

The schema is created by `db.create_all()` from SQLAlchemy models in the `db/` package.

## Supervisor Tables

### `inbox`

Pending work for agents.

Key fields:

- `agent_uuid`
- `enqueued_at`
- `payload`

### `journal`

Durable task history.

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

Named parameter overrides for a base model config.

### `model_group`

Named fallback group. Can require function-calling models.

### `model_group_member`

Priority-ordered members of a model group. A member can reference a base config
or an override.

### `agent_model_binding`

Maps code-defined agent UUIDs to model groups.

## Chat Tables

### `chat_user`

Human and agent identities.

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

Room messages.

Key fields:

- `room_uuid`
- `sender_uuid`
- `text`
- `content_type`: `markdown`, `json`
- `kind`: `message`, `debug-memory`, `debug-query`, `debug-router`, `progress`,
  `thinking`, etc.

Only `kind="message"` rows are user-facing prompt/chat content. Diagnostic rows
are for operators and audit.

### `workspace_shell_state`

Per-room working directory for the deterministic workspace shell agent.

## Conversation Tables

### `conversation_run`

State for bounded persona-to-persona conversations managed by the
`conversation` agent.

Key fields:

- `uuid`
- `room_uuid`
- `status`: `running`, `paused`, `finished`, `stopped`, `failed`
- `participants`
- `turn`, `active_turn`
- `turn_policy`
- `stop_requested`

## Assistant Tables

### `assistant_run`

Durable trace header for one assistant turn.

Key fields:

- `uuid`
- `room_uuid`
- `agent_uuid`
- `journal_id`
- `status`
- `step_limit`
- `started_at`, `finished_at`
- `final_summary`
- `metadata`

### `assistant_step`

One persisted assistant loop step. Steps are written before/after actions so a
run remains inspectable even if the process dies.

Key fields:

- `run_id`
- `step_index`
- `phase`: `planned`, `running`, `observed`, `failed`, `final`, `control`
- `action`
- `reason`
- `args`
- `observation_preview`
- `error`
- `model_group_uuid`
- `model_uuid`

### `assistant_control`

Pending runtime controls for an assistant run, such as stop or redirect.

Key fields:

- `run_id`
- `command`: `stop`, `redirect`
- `payload`
- `state`: `pending`, `applied`, `ignored`
- `requested_by_uuid`
- `applied_at`
- `note`

### `assistant_write_intent`

Controlled write proposals created by the assistant. Confirm-tier writes are
executed only through the confirmation API, not directly by model output.

Key fields:

- `uuid`
- `run_id`
- `step_index`
- `capability_name`
- `payload_hash`
- `payload`
- `preview_text`
- `state`
- `room_uuid`
- `agent_uuid`
- `result`
- `error`

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

- `uuid`, `name`, `enabled`, `folder_uuid`, `position`
- `cron_expr`, `timezone`: `localtime` | `UTC`
- `action_type`: `message`, `command`, `backup`
- `target` / `message` (message jobs), `command` (command jobs; for a `backup`
  job, an optional destination overriding the `backup.repo` setting)
- `next_run_at`, `last_fired_at`

### `cron_run`

One row per firing (trigger, timestamp), for history/logs.

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
- `supersedes_uuid`
- `expires_at`

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

### `memory_embedding`

Auxiliary pgvector embeddings for active memory claims, used by hybrid memory
retrieval.

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
- UUIDs are native Postgres UUIDs.
- Timestamps are timezone-aware.
- Flask-Admin is the current primary inspection UI for most tables.
