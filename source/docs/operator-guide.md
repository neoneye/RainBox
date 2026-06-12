# Operator Guide

## Start The App

Create and activate the venv, then run:

```bash
python3 main.py
```

The web app runs at:

```text
http://127.0.0.1:5000
```

Postgres must be available as `rainbox_production` unless `DATABASE_URL` is set.
Tests run against a separate `rainbox_claude` database (forced by
`rainbox/conftest.py`), so `pytest` never touches production data — create it
once with `createdb rainbox_claude`.
LM Studio should be running on `127.0.0.1:1234` for LLM-backed agents.

## Basic Chat Workflow

1. Open `/chat`.
2. Create or select a room.
3. Add agents to the room.
4. Make sure LLM-backed agents are bound to model groups on `/agent_models`.
5. Post a human message.
6. Watch replies stream over SSE.

Useful responder agents:

- `chat`: normal chat reply with memory retrieval.
- `query`: no-LLM Q&A retriever.
- `query_router`: Q&A hint plus router LLM.
- `query_filter_router`: Q&A retrieval, LLM relevance filter, router reply.
- `workspace_shell`: deterministic workspace-confined command runner.
- `tool_demo`: FunctionAgent demo with a multiply tool.
- `mcp`: FunctionAgent backed by MCP tools.

## Memory Operations

Memory commands are sent as normal chat messages to a room with `query`.

Examples:

```text
remember that I prefer concise technical answers
what do you remember?
what do you remember about technical answers?
why do you remember concise technical answers?
correct that I prefer long answers -> I prefer concise answers
forget concise technical answers
which memories did you use?
```

Memory tables are inspectable in Flask-Admin under the Memory category:

- `MemoryClaim`
- `MemoryEvidence`

## Feedback

Agent messages in `/chat` have feedback buttons.

- Upvotes and downvotes create `FeedbackEvent` rows.
- Downvotes can create `RetrievalEvent(stage='downvoted')` rows for same-turn
  memory/Q&A diagnostic context.
- Feedback rows can be promoted into eval cases from Python/admin workflows.

Inspect feedback in Flask-Admin:

- `FeedbackEvent`
- `EvalCase`
- `EvalRun`
- `EvalResult`

## Running Evals

Run active eval cases:

```bash
venv/bin/python -m evals.runner --active
```

Run a split:

```bash
venv/bin/python -m evals.runner --active --split regression
```

Run a specific case:

```bash
venv/bin/python -m evals.runner --case <eval-case-uuid>
```

Compare two runs:

```bash
venv/bin/python -m evals.compare \
  --baseline <baseline-run-uuid> \
  --candidate <candidate-run-uuid>
```

Sample recent production chat:

```bash
venv/bin/python -m evals.monitor --recent-chat --limit 50
```

## Database Backup

Back up the Postgres database to a compressed, **public-key-encrypted**,
timestamped `.zstd.age` file, on demand or on a schedule. rainbox holds only the
public key, so a compromised host can write backups but never decrypt them.

```bash
age-keygen -o backup-identity.txt          # one-time, offline; note the age1… public key
venv/bin/python -m backup.dump /path/to/backup-repo -r age1ql3z7h9...
```

To also push each backup off-machine, make the backup-repo a git repo with a
remote and set `RAINBOX_BACKUP_GIT_PUSH=1` (the encrypted file is committed and
pushed).

A disabled daily "Database backup" cron job is seeded under the **System**
folder on `/cron`; set the recipient + destination and enable it to run nightly.
Restore with `age -d -i identity | zstd -dc | psql`. Full usage, key setup, and
restore instructions: `docs/backup.md`.

**Where the scheduled backup reads its config:** the cron job resolves the
backup settings (`backup.repo`, `backup.age_recipient`, `backup.git_push`) from
Postgres `app_setting` first, then the matching env var, then the default — so
you can edit them on the **`/settings`** page and they take effect without a
restart (also visible read-only in Flask-Admin under **Config**). The standalone
`python -m backup.dump` CLI is **flags/env
only by design** — it builds no app context and does not read DB settings, so a
manual run ignores values edited in the UI. See `docs/backup.md`.

## Inspecting Telemetry

Use Flask-Admin or direct SQL against `retrieval_event`.

Useful filters:

- `target_type="memory_claim"`
- `target_type="qa_entry"`
- `stage="retrieved"`
- `stage="used"`
- `stage="downvoted"`
- `source="chat_memory_retrieval"`
- `source="query_filter_router"`
- `source="chat_feedback"`

Interpret telemetry as evidence for inspection. Do not automatically delete or
demote memories from counters alone.

## Common Troubleshooting

### Agent Does Not Reply

Check:

- room membership includes the agent.
- the agent has a model group if it needs one.
- LM Studio is running for LLM-backed agents.
- the supervisor process is running.
- Flask-Admin `Inbox` and `Journal` rows for failures.

### QueryAgent Fails

Check:

- pgvector extension is installed.
- LM Studio has `text-embedding-nomic-embed-text-v1.5`.
- `QUERY_AGENT_REBUILD_KB=1` if the JSONL registry changed.

### Tests Cannot Connect To Postgres

In sandboxed runs, localhost Postgres can be blocked. Rerun tests with the
normal approval/escalation path so the process can connect to `localhost:5432`.

## Operational Principle

Prefer inspectable state over hidden automation. The useful path is:

```text
observe -> inspect -> promote to eval -> verify -> tune
```
