# RainBox

`rainbox` is short to type constantly, but long enough to avoid the worst four-letter collision problem. It contains `ai`, hints at `brainbox` without saying “brain,” and still feels like a local machine: a box where experiments, agents, private chats, and background jobs can live.

Rainbox is my local LLM experiment box for running private models, agents, and automation workflows.

Each agent runs in its own child process, so they easily can be killed when they get stuck or timeouts.
Unlike other frameworks where the agents runs in the same process, being fragile and error prone.

A small Python demo of an **OS-process supervisor for AI agents**, built from POSIX primitives without a workflow framework.

A single executable (`main.py`) runs a Flask webserver and an idle-by-default supervisor in the same process. When work shows up in an agent's inbox (Postgres-backed), the supervisor spawns that agent as a child process via `posix_spawn`, hands it an inherited `socketpair` for JSONL communication, and multiplexes it via a `selectors` loop. Each agent drains its inbox, journals each item through `processing → completed`, then exits when idle; the supervisor reaps it and does **not** respawn until new work appears. A heartbeat watchdog SIGKILLs unresponsive processes or broken status channels. Ctrl-C stops both the webserver and the supervisor cleanly.

`dreamer`, `critic`, and `verifier` form a linear pipeline: each agent's `completed` journal row is routed by the supervisor into the next agent's inbox. Clicking **Run demo** in the webapp seeds 5 items into dreamer's inbox; the supervisor wakes dreamer, which produces 5 critic items, which produce 5 verifier items, with full lineage carried in each payload. The roles `followup`, `chat_structured`, `chat_unstructured`, `tool_demo`, `workspace_shell`, `router`, `query`, `query_router`, `query_filter_router`, `mcp`, and `assistant` back the group-chat feature; `workspace_shell` (deterministic command runner) and `query` (embedding-only retriever) skip the LLM completion path, while the others make **real LLM calls** when configured with model groups. The `edit_document*` roles are payload-driven document-editing planners: they consume `{document, instructions}` inbox payloads, return validated patch data in the journal result, and progressively experiment with richer patch schemas, status/comment fields, EOF handling, and reasoning fields. They plan edits but do not apply them directly.

Agents are a small **class hierarchy** (`agents/base.py`): a base `Agent` owns the inbox-drain lifecycle, `ModelGroupAgent` resolves the agent's bound model group, and `StructuredLLMAgent` makes one schema-validated LLM call per item. The `dreamer/critic/verifier` pipeline agents still run a `time.sleep(1)` placeholder, but the chat-backing agents make real calls: `FollowUpClassifierAgent` (`agents/followup.py`) classifies whether a chat message needs a reply, and `StructuredChatAgent` (`agents/chat_structured.py`, role `chat_structured`) reads a chatroom's history, retrieves relevant long-term memories, and posts a reply — both `StructuredLLMAgent` subclasses. Its plain-text sibling `UnstructuredChatAgent` (`agents/chat_unstructured.py`, role `chat_unstructured`) does the same history read and memory retrieval but makes a single *non-structured* completion, so it subclasses `ModelGroupAgent` directly and needs a model group declared "structured output: must not have". `ToolDemoAgent` (`agents/tool_demo.py`) is a sibling that subclasses `ModelGroupAgent` directly and replies via a LlamaIndex `FunctionAgent` equipped with a `multiply` tool, demonstrating the **tool-calling** path instead of structured output. `WorkspaceShellChatAgent` (the `tools/` package, role `workspace_shell`) is a **no-LLM** sibling that subclasses the base `Agent` directly: it parses a chatroom's typed command into an explicit argv with `shlex` and runs it with `subprocess.run(shell=False)` — no shell interpretation at all — then posts the raw output back. Each command has its own validator and every path argument is confined to a workspace directory. It is split into focused modules (`workspace_policy` / `command_policy` / `workspace_command_runner` / `workspace_shell_chat`). `RouterAgent` (`agents/router.py`, role `router`) is another `StructuredLLMAgent` (no FunctionAgent): it reads the same IRC-style transcript and triages the latest message into `{subject, action, reply}` — a 10-20 word summary, whether it needs an action (`no`/`unclear`/`yes`), and a clarification or small-talk reply — posting the reply as a real message and the triage as a `debug-router` row. `QueryAgent` (`agents/query.py`, role `query`) is the no-LLM-completion retriever: it embeds the message through Ollama's OpenAI-compatible embeddings endpoint (`embeddinggemma:300m`), looks the user up against a pgvector-backed Q&A registry (`data/question_answer.jsonl`), and either posts the matched static `answer` or invokes the named dynamic handler in `agents/query_handlers.HANDLERS`. `QueryRouterAgent` (`agents/query_router.py`, role `query_router`) is the crossover: exact alias hits bypass the LLM entirely, and otherwise the top-1 ungated semantic candidate (with handler output materialized) is fed as a hint to a router-style LLM. `QueryFilterRouterAgent` (`agents/query_filter_router.py`, role `query_filter_router`) splits that path further: one LLM call filters top-K candidates for relevance, then a shorter routing LLM produces the reply using only the kept candidates. `MCPAgent` (`agents/mcp.py`, role `mcp`) runs a FunctionAgent with tools loaded from configured MCP servers.

On top of the supervisor, the webapp carries a **local-LLM toolkit** for picking and tuning the model that backs the agents. It talks to registered local providers (LM Studio, Jan, and Ollama), syncs their models into a `model_config` table on startup, and provides pages to ping a model (plain text and structured JSON output), define reusable parameter overrides per model, organize models into priority-ordered groups, bind each agent to one of those groups (the fallback list it should run), and run benchmark suites that score each model/override.

The webapp also hosts a **group chat** (`/chat`): one human operator and the agents converse in rooms. Rooms, messages, users, and membership are persisted in Postgres; new messages are pushed to the browser over **Server-Sent Events** (no polling) via Postgres `LISTEN/NOTIFY`. When the operator posts in a room a responder agent (`chat_structured`, `chat_unstructured`, `tool_demo`, `workspace_shell`, `router`, `query`, `query_router`, `query_filter_router`, `mcp`, or `assistant`) belongs to, a job is enqueued and the agent replies live (rendered as markdown, or as a JSON code block when it answers with JSON). Agent replies can be upvoted/downvoted; that feedback is stored, can be promoted into eval cases, and can be linked back to retrieval telemetry. See [As a loop for running AI agents](#as-a-loop-for-running-ai-agents) for an assessment of what's real and what's still placeholder.

## Setup

`main.py` talks to Postgres via Flask-SQLAlchemy, so you need the venv installed and a Postgres database created.

Install Python deps:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Re-activate with `source venv/bin/activate` in any new shell. Deactivate with `deactivate`.

Create the Postgres databases (one-time):

```
createdb rainbox_production   # the app's real data
createdb rainbox_claude       # tests run here (never touches production)
```

By default the app connects to `postgresql+psycopg://localhost/rainbox_production`. To point elsewhere, set `DATABASE_URL`:

```
export DATABASE_URL=postgresql+psycopg://user:pass@host/dbname
```

Tests are pinned to `rainbox_claude` by `rainbox/conftest.py` (override with `RAINBOX_TEST_DATABASE_URL`), so running `pytest` can never read or mutate production data — even if `DATABASE_URL` points at production in your shell.

Install the **pgvector** Postgres extension (used by `QueryAgent` for vector similarity over the Q&A knowledge base):

```
brew install pgvector
psql rainbox -c 'CREATE EXTENSION vector;'
```

The Homebrew bottle currently ships builds only for the two newest Postgres majors (17 and 18 at the time of writing). If your `pg_config --version` is older (e.g. PostgreSQL 16), build pgvector from source against the right `pg_config`:

```
git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git
cd pgvector
make        # uses pg_config from PATH; set PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config to override
make install
psql rainbox -c 'CREATE EXTENSION vector;'
```

Verify with `psql rainbox -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"` — you should see one row.

The schema is created automatically on first startup via `db.create_all()`.

**Local model providers.** Run whichever provider(s) you want to use for chat models: LM Studio on `127.0.0.1:1234`, Jan on `127.0.0.1:1337`, and/or Ollama on `127.0.0.1:11434`. Also run Ollama with the embedding model used by Q&A and memory embeddings:

- one or more chat-capable models in a provider that the bound agents' model groups will reference on `/agentmodel`, and
- the Ollama embeddings model **`embeddinggemma:300m`** (768-dim) — `QueryAgent` and memory embeddings use Ollama's OpenAI-compatible `/v1/embeddings` endpoint to embed Q&A, active memory claims, and incoming queries.

## Run

One command:

```
python3 main.py
```

That starts the webserver on `http://127.0.0.1:5000` **and** the supervisor (in a background daemon thread). The supervisor is idle until work shows up — no agent processes are spawned at startup.

Startup also reconciles the `model_config` table with every registered provider (availability, file sizes where available, and the `is_function_calling_model` capability flag for newly-discovered models). To also refresh **existing** rows' capability arguments from provider-reported capabilities, run a one-shot sync that exits without starting the server:

```
python3 main.py --force-model-sync
```

In a browser:

- `http://127.0.0.1:5000/` — index, with a **Run demo** button (resets demo data and seeds 5 dreamer tasks). The supervisor's next tick (within ~5 s when idle — it backs off its poll when there's no work — then ~1 s while the pipeline is active) spawns dreamer, which drains and feeds critic, which feeds verifier.
- `http://127.0.0.1:5000/agent/<role>` — per-agent page with a JSON form to enqueue a single message into that agent's inbox.
- `http://127.0.0.1:5000/model` — split-view tree of synced model configs and their overrides; create/test/delete overrides here. Each config/override has a **Test connection** section with three probes: **Test chat** (a plain completion — system "answer with 'pong'", user "ping" — that passes if the reply contains "pong", and shows the full reply); **Test structured output** (a JSON-schema ping via `as_structured_llm`); and **Test function calling** (enabled only when `is_function_calling_model` is true) which runs a `FunctionAgent` that must forward a random number to a `send_number` tool, verifying function calling actually works. The probes run in place via a JSON endpoint (`/model/api/test`). A base config's detail page also shows provider-specific **Model info** when the provider exposes native metadata, with distinct messages for a deleted-but-reachable model vs an unreachable server.
- `http://127.0.0.1:5000/modelgroups` — define named, priority-ordered groups of models/overrides (`Edit priority list` opens `/modelgrouppriorities`). A group can carry a **function-calling constraint** (a checkbox on the new-group form): then only models/overrides that resolve to `is_function_calling_model=true` may be members (the priority editor disables the rest, and the server rejects them).
- `http://127.0.0.1:5000/agentmodel` — bind each code-defined agent to a model group (the prioritized fallback list it should run); editable without code changes. Agents that need tool calls (those with `requires_function_calling` in `agents/config.py`, e.g. `tool_demo` and `mcp`) are only offered function-calling groups, so they can't be misconfigured onto a structured-output-only group.
- `http://127.0.0.1:5000/benchmark_basic` — run the benchmark suite per model/override with live progress and a score column.
- `http://127.0.0.1:5000/benchmark_kanban` — the kanban 2×2 decision matrix (markdown vs JSON context × structured output vs function calling). Verdict 2026-06-11: markdown + structured output won on both reliability and speed (see docs/kanban-design.md).
- `http://127.0.0.1:5000/benchmark_editdocument` — per-model scoreboard for `EditDocumentAgentV1` through `EditDocumentAgentV6`. Picker switches between the six agents. Columns are the seeded tests in `benchmarks/editdocument.py` (currently `append_task`, `remove_task`, `check_task`); rows are every available model in the `/model` tree. Each cell runs the agent end-to-end with the target model pinned (no fallback) and scores by byte-for-byte equality between the applied patches and a reference expected document. Cell details reveal expected / applied / patches plus any agent status/comment/thinking fields the selected version reports.
- `http://127.0.0.1:5000/chat` — group chat between the human operator and the agents. Split view: rooms on the left (create one and pick which agents join), messages on the right, live via SSE. The active room is remembered in a `?room=<uuid>` URL parameter. Agent messages have feedback buttons; feedback is persisted and can feed the eval loop.
- `http://127.0.0.1:5000/admin/` — Flask-Admin: browse the `inbox`, `journal`, model tables, chat tables, memory tables (`memory_claim`, `memory_evidence`), feedback/eval tables (`feedback_event`, `eval_case`, `eval_run`, `eval_result`), and retrieval telemetry (`retrieval_event`) directly.

Current architecture/operator docs:

- [`docs/memory-architecture.md`](docs/memory-architecture.md) — memory provenance, retrieval, feedback, evals, and directions.
- [`docs/relevance-telemetry.md`](docs/relevance-telemetry.md) — retrieval event semantics and telemetry limits.
- [`docs/eval-loop.md`](docs/eval-loop.md) — feedback-to-eval architecture.
- [`docs/eval-playbook.md`](docs/eval-playbook.md) — practical eval workflow.
- [`docs/memory-commands.md`](docs/memory-commands.md) — user-facing memory command reference.
- [`docs/operator-guide.md`](docs/operator-guide.md) — day-to-day app operation.
- [`docs/data-model.md`](docs/data-model.md) — table map for supervisor, chat, memory, telemetry, eval, config, and cron data.
- [`docs/backup.md`](docs/backup.md) — encrypted database backups (age + zstd), scheduling, remote git upload, and restore.
- [`docs/find-uuid-design.md`](docs/find-uuid-design.md) — the cross-table uuid resolver behind `/find` and the assistant's `find_uuid` action: exact/substring/fuzzy/mention matching, sources, ranking, Q&A shields.
- [`docs/supervisor-design.md`](docs/supervisor-design.md) — the core runtime: inbox→journal queue, spawn-on-demand agents, heartbeat watchdog, recovery, routing.
- [`docs/settings-design.md`](docs/settings-design.md) — typed operator settings: registry, DB → env → default provenance, the /settings page.
- [`docs/git-design.md`](docs/git-design.md) — the /git page: repo pointers, guarded tree save, read-only inspection.
- [`docs/profile-design.md`](docs/profile-design.md) — person profiles: field registry, sparse data JSONB, locale templates.
- [`docs/evals-design.md`](docs/evals-design.md) — the evals framework internals: case model, scoring, gate/optimizer/monitor mechanics.

To see the `chat_structured` agent reply, assign it a model group at `/agentmodel`, keep its provider running, and post a message in a room it's a member of (`main.py` must be running so the supervisor can spawn it).

Press **Ctrl-C** in the terminal to stop. The SIGINT handler asks the webserver to shut down, signals the supervisor to wind down, and SIGKILLs any in-flight agents.

## How it's structured

- `main.py` — entrypoint. Starts the Flask webserver on the main thread (via `werkzeug.serving.make_server`) and the supervisor on a non-daemon background thread. Installs SIGINT/SIGTERM handlers for clean shutdown. Owns the `spawn()` helper and the supervisor's `selectors` loop. The `--force-model-sync` flag runs the provider model sync (refreshing existing rows' capability flag) and exits without starting the server.
- `webapp/` — Flask app as a package, split by feature so no single file is large (see [The `webapp/` package](#the-webapp-package) below). Defines the `app` object that `main.py` imports.
- `agents/` — the child agent process (`python -m agents`) **and** the agent class hierarchy. `Agent` (in `agents/base.py`) owns the inbox-drain lifecycle (pop item → journal `processing` → `handle()` → `completed`/`failed` → emit socket status → exit when idle), with `setup()`/`handle()` hooks. `ModelGroupAgent` resolves the agent's bound model group in `setup()`. `StructuredLLMAgent` makes one schema-validated LLM call per item (system prompt + a per-item user prompt → a Pydantic model, falling back through the group's models). `agents/__main__.py` reads the socket config and picks the subclass for the role via a small registry (`chat_structured → StructuredChatAgent`, `chat_unstructured → UnstructuredChatAgent`, `followup → FollowUpClassifierAgent`, `tool_demo → ToolDemoAgent`, `workspace_shell → WorkspaceShellChatAgent`, `router → RouterAgent`, `query → QueryAgent`, `query_router → QueryRouterAgent`, `query_filter_router → QueryFilterRouterAgent`, `edit_document* → EditDocumentAgent*`, `mcp → MCPAgent`, else `ModelGroupAgent`). Takes `--socket-fd` and nothing else.
- `agents/config.py` — the role declarations + pipeline topology.
- `agents/followup.py` — `FollowUpClassifierAgent`, a `StructuredLLMAgent` that classifies a single chat message (no history) as needing a reply (`needs_response ∈ {"yes","maybe","no"}`).
- `agents/chat_structured.py` — `StructuredChatAgent` (role `chat_structured`), a `StructuredLLMAgent` that filters diagnostic rows out of the chat history, retrieves relevant memory claims, renders an IRC-style transcript with an optional memory context block, asks the model for `{reply_format, reply_content}` (`reply_format ∈ {"markdown","json"}`), normalizes JSON replies, and posts the result back into the room.
- `agents/chat_unstructured.py` — `UnstructuredChatAgent` (role `chat_unstructured`), a plain-text sibling that shares the same transcript formatting (`chat/transcript.format_history`) and memory retrieval but makes a single non-structured completion instead of structured output. It subclasses `ModelGroupAgent` directly and requires a model group declared "structured output: must not have".
- `agents/router.py` — `RouterAgent`, a `StructuredLLMAgent` that reuses the shared transcript formatter (`chat/transcript.format_history`) that, instead of replying, triages the latest message into `{subject, action}` — a 10-20 word summary and whether it needs an action (`action ∈ {"no","unclear","yes"}`) — via structured output (no `FunctionAgent`/tools), posting the decision back into the room as a JSON message. Bind it to a model group on `/agentmodel`.
- `agents/query.py` / `agents/query_handlers.py` — `QueryAgent` (role `query`), a **no-LLM-completion** chat agent that answers from a small JSONL Q&A registry via pgvector similarity. Reads `data/question_answer.jsonl`, embeds each question alternate with Ollama's `embeddinggemma:300m` model (OpenAI-compatible `/v1/embeddings`) into the `data_seed_memory` pgvector table on first use, then per-message: an exact-alias lookup first (verbatim phrases like `git branch` skip the embedding call entirely), then top-K cosine retrieval aggregated by `qa_id` with `MIN_SCORE`/`MIN_MARGIN` gates — if confident it posts the static `answer` or invokes the named dynamic handler in `agents/query_handlers.HANDLERS` (system health, datetime, git status/log/remote, gpu info, todo grep, outdated deps, project list, etc.), otherwise it posts an "I don't have a confident match" fallback. Dynamic handlers receive a `QueryContext` (room_uuid, query, payload, agent_uuid). The base JSONL holds only **generic, publishable** entries; PII and instance-specific persona entries (assistant name, operator identity/contact) live in the operator overlay `<customize.dir>/question_answer.jsonl` (the `customize.dir` setting on `/settings`, env fallback `RAINBOX_CUSTOMIZE_DIR`), which overrides base entries by id. After editing it, press "Repopulate Q&A memory" on `/settings` — no restart needed. (`QUERY_AGENT_REBUILD_KB=1` still forces a repopulate on startup.) No model group needed — it only embeds.
- `agents/query_router.py` — `QueryRouterAgent` (role `query_router`), a crossover of QueryAgent + RouterAgent: exact alias hits skip the LLM entirely, and otherwise the top-1 ungated semantic candidate (with the dynamic handler's actual output materialized) is fed as a hint to a `StructuredLLMAgent` (router-style `{subject, action, reply}`) so the LLM produces a conversational answer with the KB as context. Has its own system prompt with few-shot examples for replying to the bot's own prior question without parroting it.
- `agents/query_filter_router.py` — `QueryFilterRouterAgent` (role `query_filter_router`), a two-stage LLM pipeline: an LLM filter call returns the subset of top-K candidates whose qa_ids directly address the user (hallucinated qa_ids are dropped), `_resolve_match` runs only for the kept candidates (so handlers don't fire for rejected matches), and a shorter router LLM produces the reply assuming pre-filtered relevance. Subclasses `ModelGroupAgent` (not `StructuredLLMAgent`) so it can drive two structured calls with different schemas in one `handle()`. Adds one extra LLM call vs `query_router` in exchange for tighter relevance and simpler prompts.
- `agents/edit_document_v1.py` — `EditDocumentAgent` (role `edit_document`), a `StructuredLLMAgent` that consumes a `{document, instructions}` inbox payload and returns a validated list of non-overlapping `replace_lines` patches in the journal `result`. Does not apply the patches; the caller does. Single op vocabulary, fail-fast validation; invalid LLM output falls through to the next model in the bound group via the existing `_structured_call` fallback.
- `agents/edit_document_v2.py` — `EditDocumentAgentV2` (role `edit_document_v2`), a payload-driven sibling of `EditDocumentAgent`. Same `{document, instructions}` inbox payload and same `replace_lines` patch encoding, but the response also carries a `status` (`done`/`partial`/`unclear`) and a non-empty `comment`. Fully self-contained: the `Patch`/`validate_patches`/`render_document_with_line_numbers` from v1 are duplicated here (per the `shell_v1`/`shell_v2` precedent) rather than imported.
- `agents/edit_document_v3.py` — `EditDocumentAgentV3` (role `edit_document_v3`), a third payload-driven sibling. Differs from v1/v2 in the LLM-facing patch language: four high-level ops as a Pydantic discriminated union, normalized internally to the same `replace_lines` form. Fully self-contained; reuses `agents/patch_apply.apply_patches` (shared) for the post-normalization apply step.
- `agents/edit_document_v4.py` / `agents/edit_document_v5.py` / `agents/edit_document_v6.py` — later document-editing siblings that continue the patch-schema experiments: logical EOF handling, duplicated experimental baselines, and a v6 leading `reasoning` field before patches.
- `agents/tool_demo.py` — `ToolDemoAgent`, a `ModelGroupAgent` (not a structured call) that feeds the same IRC-style transcript to a LlamaIndex `FunctionAgent` with a `multiply` tool, lets the model call the tool, and posts the reply back into the room. `FunctionAgent` is async, so `handle()` bridges it with `asyncio.run`; the model still comes from the bound group with the same priority-fallback. It requires its bound group to have the function-calling constraint (so members are guaranteed tool-capable) — it reads that flag in `setup()` and fails fast with an actionable message if absent, rather than forcing `is_function_calling_model` on per call.
- `agents/mcp.py` — `MCPAgent` (role `mcp`), a chat-backed `FunctionAgent` that sources tools from MCP servers configured in `mcp.json`; requires a function-calling model group. Private servers (API keys) go in `<customize.dir>/mcp.json`, which merges over the base file per server name.
- `memory/ops.py` / `memory/retrieval.py` — explicit memory commands (`remember`, `forget`, `confirm`, `correct`, recall/explain commands), deterministic memory retrieval, prompt memory formatting, and the `debug-memory` audit row.
- `evals/runner.py` / `evals/compare.py` / `evals/optimizer.py` / `evals/monitor.py` — feedback/eval loop: run deterministic eval cases, compare candidates against baselines, try bounded candidate configs, and sample recent production chat outputs.
- `tools/` — `WorkspaceShellChatAgent` (role `workspace_shell`), a **no-LLM, no-`bash`** chat agent split into focused modules: it parses each human message into an explicit argv with `shlex.split` and runs it via `subprocess.run(argv, shell=False)`, so shell metacharacters (`|`, `>`, `$()`, `;`, `&&`, `*`, `$VAR`) have no special meaning — they are rejected for clarity. Each command has its own validator (`ls`, `pwd`, `cd`, `cat`, `head`, `tail`, `grep`, `wc`, `date`, `find`, `stat`, `file`), `cd`/`pwd` are implemented in Python, and **every path argument must resolve inside the workspace** (`SHELL_CWD`) — sensitive files (`.env`, `id_rsa`, `.ssh/…`, …) are blocked even within it. Only the per-room `cwd` persists (in `workspace_shell_state`); the environment is always the fixed `SHELL_ENV` baseline. Add the `workspace_shell` user to a room on `/chat`; it needs no model group. The package layers as `workspace_policy.py` (path confinement) ← `command_policy.py` (parsing + allowlist + validators) ← `workspace_command_runner.py` (execution + `cd`/`pwd` builtins) ← `workspace_shell_chat.py` (the agent). Less convenient than a real shell — no pipelines, redirection, or globbing — but far easier to reason about; use separate messages (`cd src`, then `ls`) instead of `cd src && ls`.
- `db/` — Flask-SQLAlchemy models, queue + model-management + chat API, app factory, memory/provenance helpers, feedback helpers, retrieval telemetry helpers, and eval case/run/result helpers (`import db` is the facade).
- `llm/` — provider-backed LlamaIndex connectivity: model construction through LM Studio, Jan, or Ollama; a `ThinkingAwareOpenAILike` subclass that recovers JSON from `reasoning_content` for OpenAI-compatible providers; native Ollama wrapping; and the `/model` test probes — `test_chat` (plain completion), `test_structured_output` (`PingResponse` schema), and `test_tool_call` (`FunctionAgent` + `send_number` tool).
- `benchmarks/basic.py` — benchmark definitions that resolve a model config or override and score per-trial results: base64 decode/encode, reverse string, and reverse list (structured-output trials), plus two function-calling tests via a LlamaIndex `FunctionAgent`: `tool_order` gives the model three tools (`func1`, `func2`, `func3`) and checks it invokes all three in the order requested that trial — each trial uses a different one of the 6 permutations (shuffled; 5 of the 6 at the default 5 trials); `tool_route` gives three tools (`random`, `func1`, `func2`) where `random` returns the name of a function and the model must then call exactly that one (a data-dependent dispatch).
- `benchmarks/runner.py` — background orchestrator that walks the model tree, warms up each model once, runs every benchmark, and maintains the live state dict the `/benchmark_basic` page polls. The kanban 2×2 matrix (`benchmarks/kanban.py`) runs as its own spec set on `/benchmark_kanban`.
- `benchmarks/editdocument.py` / `benchmarks/editdocument_runner.py` — second benchmark stack, scoped to document-editing agents. Mirrors `benchmarks/basic.py`/`benchmarks/runner.py` shape.
- `agents/patch_apply.py` — `apply_patches(document, patches) -> str`. Materializes a list of `replace_lines` patches (the encoding `validate_patches` accepts) into the post-edit document. Standalone helper used by the benchmark.

`main.py` launches `python -m agents` via `posix_spawn`; the agent's socket fd is inherited across the exec. Each process pushes a Flask app context to get `db.session` access; each agent only touches journal rows tagged with its own `uuid`, so there is no cross-agent contention.

### The `webapp/` package

The Flask app is a package split by feature so no single file gets large. It uses a shared-`app` layout (not blueprints), so the `url_for('func_name')` calls inside templates work without blueprint prefixes. Each module owns its routes and the HTML templates they render:

- `webapp/core.py` — builds `app`, runs `init_db`, syncs `model_config` from every registered provider on startup, creates the benchmark runner, and registers the Flask-Admin views. Imported first; everything else registers routes against its `app`.
- `webapp/pages.py` — `/`, `/demo`, `/agent/<name>`.
- `webapp/models_views.py` — `/model`, override create/test/delete, the per-override "usecase" presets (structured-output vs tool-use, which set `is_function_calling_model`/`should_use_structured_outputs`), and the in-place **Test chat** / **Test structured output** / **Test function calling** probes (JSON endpoint `/model/api/test`).
- `webapp/model_group_views.py` — `/modelgroups*` and `/modelgrouppriorities`.
- `webapp/agent_views.py` — `/agentmodel` (bind each agent to a model group).
- `webapp/benchmark_views.py` — `/benchmark_basic*`.
- `webapp/chat_views.py` — `/chat`, the single-page chat UI (client-side markdown via `marked`+`DOMPurify`, JSON syntax highlighting via `highlight.js`). Served with `Cache-Control: no-store` so frontend changes show on a normal reload.
- `webapp/chat_api.py` — the chat JSON API (`/chat/api/rooms`, `/chat/api/agents`, `/chat/api/rooms/<uuid>/messages`, `/chat/api/messages/<uuid>/feedback`) and the SSE endpoint `/chat/stream` (a dedicated `psycopg` connection that `LISTEN`s and forwards `NOTIFY` events). Also enqueues a job for each responder agent (`chat_structured`, `chat_unstructured`, `tool_demo`, `workspace_shell`, `router`, `query`, `query_router`, `query_filter_router`, `mcp`) that belongs to a room when a human posts in it (the enqueue payload carries the triggering message's uuid so `workspace_shell` runs that exact command). Feedback capture writes `FeedbackEvent` rows and downvotes can write `RetrievalEvent(stage='downvoted')` rows for same-turn retrieval context.
- `webapp/__init__.py` — imports `core` (which builds `app`), then imports the view modules to register their routes, and re-exports `app` so `from webapp import app` still works.

### `main.py` (entrypoint)

The process generates an ephemeral `root_uuid` and starts two pieces of work:

1. **Supervisor thread** — runs `supervisor_loop()`. Pushes a Flask app context for its lifetime so it can use `db.session`.
2. **Webserver** — `werkzeug.serving.make_server(...).serve_forever()` on the main thread.

A SIGINT/SIGTERM handler sets a `threading.Event` (`stop_event`) and calls `server.shutdown()` from a helper thread. When `serve_forever()` returns, the `finally` block joins the supervisor thread (which sees `stop_event` set, exits its loop, and SIGKILLs any remaining agents).

**Spawning an agent** (helper used when an agent's inbox becomes non-empty):

1. Create a `socket.socketpair()` — one end for the parent, one for the agent.
2. Mark the agent end inheritable (`os.set_inheritable(fd, True)`) so `posix_spawn` doesn't close it on exec.
3. `os.posix_spawn` runs `python -m agents` with `--socket-fd <agent_end_fd>`.
4. Close the agent end in the parent — only the agent should hold it now.
5. Send the config message `{"name": <role>, **params}\n` over the socket (UUIDs serialized as strings via `json.dumps(default=str)`). The agent reads it as the first JSONL line.

**Supervisor loop** (runs while `stop_event` is not set):

1. **Routing pass:** scan the journal for rows in `state='completed' AND routed_at IS NULL`. For each, look up the source agent's `next` uuid in `agents/config`. If non-null, enqueue a lineage payload into that next agent's inbox. Mark the row routed regardless (terminal rows get marked too, so they aren't re-scanned).
2. **Wake-up pass:** call `db.agent_uuids_with_work()` (returns the set of agent UUIDs whose inbox is non-empty). For each role in `agents/config` whose UUID is in that set and whose agent is not currently running, `spawn()` it and register its socket on the `selectors`.
3. **Socket drain:** `sel.select(timeout=1.0)` blocks in the kernel for up to one tick. For each readable socket, `recv` once, append to its buffer, split on `\n`, log each JSON message, and update the agent's `last_heartbeat`. An empty `recv` (EOF) marks the agent as dead.
4. **Watchdog:** if `now - last_heartbeat > HEARTBEAT_TIMEOUT` (60 s) for any alive agent, log a warning, `SIGKILL` it, and mark it dead. A background heartbeat runs during `handle()`, including model calls; the timeout therefore catches a dead process or broken status channel rather than ordinary slow inference.
5. **Reap:** for each dead agent, `waitpid`, fail its active journal, and recover any interrupted assistant run before unregistering and closing its socket. Recovery persists the active model-call diagnostics and posts a terminal chat notice. Do **not** respawn — the next tick's wake-up pass will re-spawn if work appears.

**Startup / shutdown:** startup recovers assistant runs left `running`/`stopping` by a previous supervisor. On shutdown the loop `SIGKILL`s remaining agents, recovers their active work, reaps them, closes sockets, and returns.

### `agents/__main__.py`

`main()` bootstraps the process:

1. Parse `--socket-fd`, wrap it as a `socket.socket(fileno=...)`.
2. Read from the socket until the first `\n`, then parse that line as the config (uuid as string, name, role params).
3. Push a Flask app context for the lifetime of the process (gives `db.session` access to Postgres).
4. Pick the agent class for the role (`chat_structured → StructuredChatAgent`, `chat_unstructured → UnstructuredChatAgent`, `followup → FollowUpClassifierAgent`, `tool_demo → ToolDemoAgent`, `workspace_shell → WorkspaceShellChatAgent`, `router → RouterAgent`, `query → QueryAgent`, `query_router → QueryRouterAgent`, `query_filter_router → QueryFilterRouterAgent`, `edit_document* → EditDocumentAgent*`, `mcp → MCPAgent`, else `ModelGroupAgent`) and call `agent_instance.run()`.
5. Close the socket and exit.

`Agent.run()` owns the lifecycle so subclasses don't have to:

- `setup()` once (e.g. `ModelGroupAgent` resolves the bound model group here).
- Loop: `db.take_item(agent_uuid)` atomically pops the oldest inbox row and inserts a journal row as `processing` (returns `(journal_id, payload)` or `None`). On `None`, send `{"status": "idle"}` and exit. Otherwise send `{"status": "processing", ...}`, call `handle(journal_id, payload)`, journal the returned dict as `completed` (or `failed` on exception), and send the matching status.

Subclasses only override `handle()` (and optionally `setup()`):

- `Agent.handle` / `ModelGroupAgent.handle` — placeholders (`time.sleep(1)`); `ModelGroupAgent` records which models it *would* try.
- `StructuredLLMAgent.handle` — builds two messages (a fixed system prompt + a per-item `user_prompt(payload)`), tries each model in the group in priority order via `as_structured_llm(...)`, and returns the parsed Pydantic model. `StructuredChatAgent` and `FollowUpClassifierAgent` build on this.

The agent never holds an item outside the database — `take_item` moves it from inbox to journal in a single transaction. If a worker exits mid-task, the supervisor marks the journal failed. Assistant runs additionally checkpoint an in-flight model request before dispatch, allowing recovery to retain its exact prompts, model, configured timeout, and failure reason.

## Heartbeat / kill / respawn behavior

- **Healthy agent**: emits state transitions plus a background heartbeat every 20 seconds during `handle()`. When the inbox is empty it sends `{"status": "idle"}` and exits; the parent sees EOF and reaps it. **No automatic respawn** — the supervisor's next wake-up pass spawns it again only if its inbox becomes non-empty.
- **Dead or disconnected agent** (no message for > 60 s): the watchdog sends `SIGKILL`, `waitpid` reaps it, and the supervisor fails its active journal. An interrupted assistant run becomes `killed`, gets a deterministic failure summary, turns its active model-call checkpoint into a failed timeline step, and posts an operational chat notice that clears its stale progress bubble. Startup performs the same recovery for runs abandoned by a previous supervisor process.
- **Shutdown** (Ctrl-C): the SIGINT/SIGTERM handler sets `stop_event` and shuts the webserver. The supervisor loop exits, `SIGKILL`s any remaining agents, and the main thread returns.

## Wire format

Each socket message is a single JSON object terminated by `\n` (JSONL). The parent buffers partial reads and splits on `\n` so multiple messages per `recv` work correctly.

The first message on every agent socket is the config, sent parent → agent (UUIDs serialized as strings):

```
{"uuid": "<uuid>", "name": "<role>", "description": "...", "next": "<next-uuid>" }
```

After that, the agent sends status messages parent ← agent at each transition:

```
{"status": "processing", "journal_id": 7, "payload": {"task": "dreamer_task_2"}}
{"status": "completed",  "journal_id": 7}
{"status": "idle"}
```

## Pipeline routing

The supervisor's loop includes a routing pass that turns one agent's `completed` journal row into the next agent's inbox row. Topology lives in `agents/config` as a `next: UUID | None` field per role:

```
dreamer.next = CRITIC_UUID
critic.next  = VERIFIER_UUID
verifier.next = None       # terminal
```

Each enqueued payload carries lineage:

```json
{
  "from": "dreamer",
  "from_journal_id": 42,
  "input": <the source row's input payload>,
  "result": <the source row's result>
}
```

So the verifier sees a doubly-nested payload that includes the original dreamer task and the critic's transformation of it — every stage's input/output is recoverable from the latest payload alone.

A `routed_at timestamptz` column on `journal` marks rows the supervisor has already processed; the routing query is `WHERE state='completed' AND routed_at IS NULL`. Each route step (`enqueue(next_uuid, ...)` + `mark_routed(...)`) is two commits, so on a crash between them a duplicate inbox item is possible — easy follow-up if needed.

## Postgres layer

Tables in the `rainbox` database (created on demand via `db.create_all()`). The supervisor/pipeline uses two:

```
inbox(id, agent_uuid uuid, enqueued_at timestamptz, payload text)
journal(id, inbox_id int, agent_uuid uuid, enqueued_at timestamptz, started_at timestamptz,
        updated_at timestamptz, state text, payload text, result text, routed_at timestamptz)
```

The model-tooling layer adds five more (native `uuid` keys, `jsonb` blobs, `timestamptz`):

```
model_config(id, uuid, model_name, arguments jsonb, available bool, size_bytes bigint, created_at, updated_at)
model_config_override(id, uuid, display_name, model_config_uuid -> model_config.uuid, overrides jsonb, created_at, updated_at)
model_group(id, uuid, name, requires_function_calling bool, created_at, updated_at)
model_group_member(id, group_uuid -> model_group.uuid, position, member_uuid)
agent_model_binding(id, agent_uuid unique, model_group_uuid -> model_group.uuid, created_at, updated_at)
```

`model_config` rows are synced from providers on startup but never deleted when a model disappears (only the `available` flag flips), so a past run's exact parameters stay recoverable by uuid. The sync refreshes the observational `size_bytes` and sets `is_function_calling_model` (from native capability metadata when available) on **new** rows; an existing row's `arguments` blob is otherwise left immutable, except via the explicit `python3 main.py --force-model-sync` which opts in to refreshing the capability flag on existing rows too. A `model_group_member.member_uuid` references either a `model_config` or a `model_config_override`, resolved override-first.

`agent_model_binding` links a code-defined agent (its `uuid` from `agents/config.py`) to the `model_group` it should run — the prioritized fallback list, e.g. a fast/low-quality vs a slow/high-quality strategy. Agents and their pipeline topology stay in code; only this model-to-agent assignment lives in the database, editable at runtime via `/agentmodel`. `init_db` seeds one unassigned row per code agent; `model_group_uuid` is nullable, with `ON DELETE SET NULL` so deleting a group just unassigns the agents that used it. `model_group.requires_function_calling` is a membership constraint: when set, only function-calling models/overrides may join, and `/agentmodel` only offers such groups to agents that declare `requires_function_calling`.

The group-chat feature adds five more:

```
chat_user(id, uuid, name, user_type text CHECK IN ('human','agent'), created_at)
chatroom(id, uuid, name, created_by uuid, created_at)
chatroom_member(id, room_uuid -> chatroom.uuid, user_uuid -> chat_user.uuid)  -- unique(room_uuid, user_uuid)
chat_message(id, uuid, room_uuid -> chatroom.uuid, sender_uuid uuid, text,
             content_type text, kind text, created_at)
workspace_shell_state(room_uuid -> chatroom.uuid, cwd text, env jsonb, updated_at)
```

`init_db` seeds exactly one `human` operator plus one `agent` `chat_user` per `agents/config` entry (reusing each agent's uuid), and a starter `general` room if none exist. Rooms have explicit membership (`chatroom_member`); a message's `content_type` is `markdown` or `json` — agent messages carry the agent's declared `reply_format`, human messages are auto-classified (valid JSON → `json`, else `markdown`). `chat_message.id` doubles as the incremental-fetch cursor (clients ask for messages `after` their last id). Posting a message runs `pg_notify('chat_events', {room_uuid, message_id})` in the same transaction, which the `/chat/stream` SSE endpoint forwards to connected browsers.

`chat_message.kind` separates user-facing messages from operator/debug rows. Normal chat rows use `kind='message'`; diagnostics such as `debug-memory`, `debug-query`, `debug-router`, `progress`, and `thinking` stay in the room for inspection but are filtered out of `StructuredChatAgent` prompts and folded in the UI.

The memory layer adds two provenance-oriented tables:

```
memory_claim(id, uuid, agent_uuid, scope, room_uuid, kind, subject, predicate,
             object, text, confidence, status, sensitivity, supersedes_uuid,
             created_at, updated_at, expires_at)
memory_evidence(id, uuid, memory_uuid -> memory_claim.uuid, provenance,
                source_type, source_id, excerpt, created_by_uuid, created_at)
```

`memory_claim` is the canonical remembered belief. `memory_evidence` is the audit trail; multiple evidence rows can attach to one claim, so a model-inferred candidate can later gain user-confirmed evidence without losing the original provenance. Retrieval only considers active, non-expired claims and excludes `secret` sensitivity unless explicitly allowed.

The feedback, telemetry, and eval loop add five more:

```
feedback_event(id, uuid, room_uuid, message_uuid, agent_uuid, rating, comment,
               created_by_uuid, created_at, metadata jsonb)
retrieval_event(id, uuid, target_type, target_id, stage, query, room_uuid,
                agent_uuid, journal_id, source, retrieval_rank,
                retrieval_score, filter_label, metadata jsonb, created_at)
eval_case(id, uuid, source_feedback_uuid, name, case_type, split, input jsonb,
          expected jsonb, rubric jsonb, status, created_at, updated_at)
eval_run(id, uuid, name, agent_role, config jsonb, started_at, finished_at,
         summary jsonb, is_baseline)
eval_result(id, uuid, eval_run_uuid -> eval_run.uuid,
            eval_case_uuid -> eval_case.uuid, score, passed, details jsonb,
            created_at)
```

`feedback_event` captures up/down feedback on user-facing agent replies and snapshots enough same-turn context to promote the feedback into an eval case later. `retrieval_event` is an event log for Q&A and memory retrieval decisions (`retrieved`, `accepted`, `rejected`, `used`, `downvoted`); counters are derived from it, not stored as mutable fields. `eval_case` / `eval_run` / `eval_result` are the deterministic benchmark loop used by `evals/runner.py`, `evals/compare.py`, and `evals/optimizer.py`.

- `agent_uuid` uses the native Postgres `uuid` type (16-byte). Python side uses `uuid.UUID`.
- All timestamps are `timestamp with time zone` (`timestamptz`). Python stores tz-aware `datetime.now(UTC)`.
- `inbox.enqueued_at` has a column-level default (`datetime.now(UTC)`); other timestamps are set explicitly by the helpers.
- `payload` and `result` are JSON-encoded strings in `text` columns (not `jsonb`).
- `state` is constrained to `processing`, `completed`, `failed`, `stopped` via a `CHECK` constraint (`journal_state_check`). The Python side uses `Literal["processing","completed","failed","stopped"]`.
- Indexes: `inbox(agent_uuid, id)`, `journal(agent_uuid, id)`, and `journal(state, routed_at)` for the routing query.

Helpers in `db/` (all use `db.session` under a pushed Flask app context — no connection argument):

- `init_db(app)` — `db.create_all()` inside an app context.
- `reset_demo_data()` — wipe both tables (used by the `/demo` button).
- `enqueue(agent_uuid, payload)` — append to an agent's inbox.
- `take_item(agent_uuid)` — atomic pop from inbox + insert into journal as `processing`. Returns `(journal_id, payload)` or `None`.
- `journal_update(journal_id, state, result=None)` — transition a journal row to `completed` / `failed` / `stopped`, optionally attaching a JSON result.
- `fetch_unrouted_completed()` — list journal rows that completed but haven't been routed yet (the supervisor's routing-pass query).
- `mark_routed(journal_id)` — set `routed_at = now`, used after enqueueing to the next agent or to skip terminal rows.
- `agent_uuids_with_work()` — set of agent UUIDs whose inbox is non-empty (used by the supervisor's wake-up pass).

The demo only exercises `processing → completed`; `failed` and `stopped` exist for the obvious extensions (exception during work → `failed`; supervisor cancellation → `stopped`).

## Layout

| Package | What lives there |
|---|---|
| `main.py` | entrypoint: webserver + supervisor + signal handling |
| `agents/` | the agent child process (`python -m agents`), base class hierarchy, and every role implementation |
| `db/` | Flask-SQLAlchemy models and all Postgres helpers (`import db` is the facade) |
| `webapp/` | Flask app package, split by feature |
| `chat/` | transcript formatting and streaming-reply helpers shared by chat agents |
| `memory/` | explicit memory commands and deterministic retrieval |
| `evals/` | eval runner, baseline comparison, optimizer, production monitor |
| `benchmarks/` | benchmark definitions, runners, and subprocess workers |
| `llm/` | provider-backed LlamaIndex connectivity and the /model test worker |
| `backup/` | encrypted DB backup (`python -m backup.dump`) and remote upload |
| `providers/` | LM Studio / Ollama / Jan provider registry |
| `tools/` | the no-LLM workspace_shell command runner |
| `data/` | the base Q&A knowledge file (`question_answer.jsonl`) |
| `voice_tts_kokoro/`, `whisper_service/`, `telegram_service/` | standalone processes with their own venvs (TTS, STT, Telegram bridge) — the core talks to/with them over HTTP only |

Tests are colocated inside each package next to the modules they test (`<pkg>/test_*.py`); the root `conftest.py` pins every pytest run to the `rainbox_claude` database.

## As a loop for running AI agents

A candid assessment of this code as the basis for an AI-agent system.

### What's right (OS-level)

- **Process isolation per agent.** Most agent frameworks are in-process Python — one tool hang or OOM and the whole supervisor goes. This design doesn't have that failure class.
- **Watchdog + `SIGKILL`** is the correct shape for LLM workloads where infinite loops and stuck subprocess tools are real concerns.
- **Low idle footprint** — `select` blocks in the kernel until the next deadline or socket event, and the supervisor only spawns agents when work appears. Inbox discovery is poll-based, so an idle supervisor still wakes on a timer; it backs that off to `IDLE_TICK_TIMEOUT` (5 s) when there are no live agents or pending work, keeping at-rest CPU minimal. (A `LISTEN/NOTIFY` wakeup on the inbox — as chat already uses — would take it to truly zero; see follow-ups.)
- **Small surface area** — POSIX primitives, no workflow framework, the whole thing is readable end-to-end.

### What's right (runtime layer)

- **Real persistence.** Inbox + journal in Postgres means agent state survives crashes, supervisor restarts, and machine reboots. That's the difference between "demo" and "could actually run a workload." `take_item` being atomic is the load-bearing detail: a crash leaves a durable `processing` row until the supervisor transitions it to `failed`, never a lost item.
- **Schema-enforced state machine.** The journal `state` column is constrained at the schema level (`CHECK (state IN (...))`) and at the Python level (`Literal["processing","completed","failed","stopped"]`). That's a real type-safe lifecycle, not just a convention.
- **Operator interface.** The webapp + Flask-Admin lets a human inspect tables and enqueue work without restarting the supervisor. The difference between "I can run this" and "I can operate this."
- **Type-checked.** Annotations throughout + `pyrightconfig.json` means a static check catches schema/code drift early.

### What's still missing

1. **LLM is wired for the chat/tool/document agents, not the demo pipeline.** `StructuredChatAgent`, `UnstructuredChatAgent`, `FollowUpClassifierAgent`, router variants, query-router variants, document-editing agents, and tool agents make real provider-backed LLM calls when configured. The `dreamer/critic/verifier` pipeline agents still run the `time.sleep(1)` placeholder — swapping them over is now a matter of giving each a `StructuredLLMAgent` subclass with its own system prompt, not new plumbing.
2. **Heartbeat detects liveness, not semantic progress.** The background thread keeps a process alive during long model calls, but it also keeps heartbeating if `handle()` is deadlocked while the process itself remains healthy. Per-call model timeouts are the primary bound for that case; the supervisor watchdog covers process or status-channel failure.
3. **Retry / backoff policy.** The journal *can* hold `failed` rows (and `handle()` exceptions now produce them), but nothing reads them. A real system needs "if `failed` and attempts < N, re-enqueue with attempts+1 and an exponential delay."
4. **Bidirectional protocol post-config.** The agent reads its inbox directly from Postgres and doesn't react to anything the supervisor sends after the initial config message. Cancellation and live steering still need a socket-side protocol.
5. **Routing topology is hardcoded linear.** `next: UUID | None` only expresses a single successor. Real workflows need fanout (one role feeds many), fanin (many feed one), conditional routing (route based on result content), and stop conditions (verifier might loop back to dreamer if confidence is low).

### Where it sits in the landscape

- **vs LangGraph / CrewAI / AutoGen:** the inverse — those handle DAG + LLM, this handles OS isolation + durability. They compose: each spawned process here could host one LangGraph graph.
- **vs Celery:** Celery has queue + retries + result store + workflow chaining. This now has all four (inbox + on-demand spawn + journal + the routing producer), in a few hundred lines and a single Postgres database instead of a Redis/RabbitMQ broker.
- **vs Temporal:** same shape (durable workflow state, signals, history, replay) at a much smaller scale. Temporal's "history is the source of truth" is exactly what the journal table is here — just without the distributed-system machinery.

### One-line judgement

This behaves like a multi-agent system: durable, type-checked, observable through Flask-Admin, with end-to-end lineage carried in each routed payload, real structured LLM calls behind the chat agents, a live group-chat UI over SSE, and a single Ctrl-C-stoppable entrypoint. What's left to make it useful for broader LLM workloads is moving the `dreamer/critic/verifier` pipeline off its `time.sleep(1)` placeholder onto the same `StructuredLLMAgent` path, adding semantic-progress detection beyond liveness heartbeats, and growing the routing topology beyond linear successor.

### Where to go from here

Four paths, depending on what you want to learn or use:

- **Path A — Put the pipeline on the LLM.** The plumbing now exists: `StructuredLLMAgent` makes a real structured provider-backed call, and `StructuredChatAgent`/`FollowUpClassifierAgent` already use it. Give `dreamer`, `critic`, and `verifier` their own `StructuredLLMAgent` subclasses with role-specific system prompts (dreamer generates ideas, critic critiques, verifier verifies) and register them in `agents/__main__.py`'s class registry. The infrastructure (model-group binding, fallback, journaling, the raised heartbeat) is already in place, so this is mostly prompt design now.
- **Path B — Conditional routing and feedback loops.** Today `next` is a single uuid. The next architectural leap is making it conditional on the result: verifier returns `{"verdict": "approved"}` → terminate; `{"verdict": "rework", "feedback": "..."}` → enqueue back into dreamer with the feedback in the payload. Now you have an actual agentic loop, not just a linear assembly line. Need: termination conditions (max iterations) and cycle detection.
- **Path C — Survivability.** Retry/backoff for `failed` rows (attempts column, exponential backoff), cancellation protocol over the socket (the supervisor can ask an agent to abort a specific journal_id), recovery on supervisor restart (resume `processing` rows from where you left off). Pure systems work, no API keys required.
- **Path D — Operator tools.** Dashboard view in the webapp showing the live pipeline (counts per stage, in-flight items, throughput); cost tracking once Path A lands; manual "re-route this row" / "cancel this row" buttons in Flask-Admin.

**Recommended order: A → B.** A makes the pipeline real on the same path the chat agents already use, and exposes gaps you'd otherwise design for hypothetically. B is where this stops being a linear workflow runtime and becomes a fully agentic system — the conditional loop is the conceptual centerpiece of the genre (LangGraph, CrewAI, AutoGen all converge on this). C and D are valuable but secondary; do them when the workload starts demanding them, not before.

### Memory

For real LLM agents, *memory* is what makes consecutive tasks better than the sum of one-shot prompts. This repo now has a first-class memory layer rather than just a design sketch.

The implemented memory model is provenance-first:

- `memory_claim` stores the remembered belief: text, optional subject/predicate/object, scope, kind, lifecycle status, confidence, sensitivity, and expiry.
- `memory_evidence` stores how the belief became known. Supported provenance values are `observed_from_source`, `inferred_by_model`, `confirmed_by_user`, and `imported_from_transcript`.
- A claim can have multiple evidence rows. Confirmation adds evidence; it does not erase the original source.
- Claim lifecycle is explicit: `candidate`, `active`, `superseded`, `rejected`, `expired`.
- Sensitivity is explicit: `public`, `private`, `secret`. Normal retrieval excludes `secret`.

Explicit memory commands are handled by `memory/ops.py` and routed through `QueryAgent` before the Q&A/vector path initializes:

- `remember that ...`
- `forget ...`
- `confirm that ...`
- `correct that OLD -> NEW`
- `what do you remember?`
- `what do you remember about ...`
- `why do you remember ...`
- `which memories did you use?`

`StructuredChatAgent` is the first runtime consumer. For each chat turn it:

1. filters room history down to real `kind='message'` rows,
2. retrieves active, non-expired, non-secret memory claims by deterministic token overlap,
3. prepends a compact `Relevant remembered facts:` block to the prompt,
4. records a folded `debug-memory` row for audit,
5. writes `RetrievalEvent` rows for `retrieved` and `used` memory claims.

Feedback closes the loop. Agent messages can be upvoted or downvoted. Feedback is stored in `feedback_event`; same-turn `debug-memory` / `debug-query` context is snapshotted, and downvotes write `retrieval_event(stage='downvoted')` rows for involved memory claims or Q&A entries when diagnostic context is available. The diagnostic lookup is scoped to the rated turn so a later no-memory answer does not accidentally downvote an earlier memory.

The eval loop makes memory behavior measurable:

- `FeedbackEvent` rows can be promoted into `EvalCase` rows.
- `evals/runner.py` runs deterministic `chat_reply` and `memory_retrieval` cases.
- `evals/compare.py` compares candidate runs against baselines and gates regressions.
- `evals/optimizer.py` tries bounded candidate configs such as `memory_retrieval_limit`.
- `evals/monitor.py` samples recent production chat outputs into eval runs.

So the current shape is:

```
real chat -> memory retrieval -> feedback -> eval case -> eval run -> comparison/gate -> safer change
```

This is still early. Retrieval is lexical, not semantic; attribution can say a memory entered the prompt, not that it truly caused the final wording; automatic candidate extraction is not implemented; and eval comparison still needs strict case-set hardening so candidates cannot add unmatched easy cases to inflate their means. The detailed architecture and next steps live in [`docs/memory-architecture.md`](docs/memory-architecture.md).
