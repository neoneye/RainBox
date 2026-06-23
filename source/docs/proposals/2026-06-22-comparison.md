# RainBox comparison: personal assistant direction

Date: 2026-06-22

## Purpose

Compare RainBox with PlanExe, Pi, BeeAI Framework, Hermes, OpenClaw, OpenHands,
OpenCode, Supermemory, Mem0, Honcho, and MemPalace for the goal:

> Own personal assistant for my needs.

This is not a README-only scan. For RainBox I read the local architecture docs,
status proposals, and implementation files around agents, memory, cron, tools,
and the assistant write surface. For PlanExe I read the local PlanExe checkout, including its worker
pipeline, hosted UI, database worker, MCP cloud server, shared database models,
and agent instructions. For external projects I used official docs, repo structure/code
surfaces, docs indexes, and, where relevant, architecture papers or policy
pages. The external landscape changes quickly, so treat this as a decision memo
dated above, not a permanent market survey.

## Verification status (updated 2026-06-23)

This document was fact-checked against the RainBox source tree and against the
external projects' public repos/docs on 2026-06-21, with MemPalace added and
checked against its public repo/README on 2026-06-22, and BeeAI Framework added
from the local checkout on 2026-06-23.

- **RainBox claims:** every internal claim below was confirmed against code, with
  file/line evidence. See "RainBox baseline" for the concrete pointers added
  during this pass. Two claims needed sharpening (not correction): the
  *log-and-undo* tier and the *MCP* surface — both are clarified inline.
- **External claims:** the original eight projects exist as described and the load-bearing
  facts checked out — Pi's four `@earendil-works/*` packages and its
  "no built-in permission system" posture; Hermes's gateway, `hermes claw
  migrate`, and pluggable memory providers (Honcho/Mem0/Supermemory); OpenClaw's
  channel list, DM pairing, and `non-main` sandbox default; OpenHands arxiv IDs
  2407.16741 (ICLR 2025) and 2511.03690 (SDK paper); OpenCode's allow/ask/deny
  permission model with `.env` denied by default; Supermemory, Mem0, and Honcho's
  feature surfaces, plus Honcho's AGPL-3.0 license and Mem0's arxiv 2504.19413.
- **Minor external nuances** worth a footnote: Pi's exact event-name list
  (`agent_start`/`turn_start`/`turn_end`/`agent_end`) is consistent with the
  documented hook/event system but is not quoted verbatim in the top-level
  README; Mem0's graph memory was dropped from the v3 OSS rewrite while remaining
  on the hosted Platform, so "graph memory" is edition-dependent for
  self-hosters.
- **MemPalace (added 2026-06-22):** verified against the public repo/README. The
  load-bearing facts checked out — MIT license, local-first/no-cloud default,
  verbatim storage with "does not summarize, extract, or paraphrase", the
  wings/rooms/drawers hierarchy, 33 MCP tools, the temporal entity-relationship
  knowledge graph on local SQLite, pluggable backends (ChromaDB default,
  sqlite_exact, Qdrant, pgvector), and the LongMemEval numbers as *self-reported
  in the README* (96.6% R@5 raw, 98.4% hybrid v4, ≥99% with LLM rerank). Those
  benchmark figures are the project's own claims, not independently reproduced
  here.
- **BeeAI Framework (added 2026-06-23):** verified against the local BeeAI
  Framework checkout. The relevant code surfaces are
  the Python package (`python/beeai_framework`) and TypeScript package
  (`typescript/src`): `RunContext`/`Emitter`, `ChatModel`, `Tool`,
  `RequirementAgent`, `Workflow`, memory classes, MCP/A2A/ACP/OpenAI/AgentStack
  serving adapters, filesystem/code tools, and provider adapters. BeeAI is
  Apache-2.0, Python package version `0.1.81`, and includes an IBM legal notice
  saying the code was contributed as open source rather than as a maintained IBM
  product.
- **PlanExe (added 2026-06-23):** verified against the local PlanExe
  checkout. The relevant code surfaces are
  `worker_plan/worker_plan_internal/plan/run_plan_pipeline.py`,
  `worker_plan/worker_plan_internal/plan/nodes/full_plan_pipeline.py`, the
  diagnostics modules (`redline_gate`, `premise_attack`, `constraint_checker`,
  `prompt_adherence`), the report assembly node, `database_api/model_planitem.py`,
  `worker_plan_database/app.py`, `frontend_multi_user/src`, and `mcp_cloud`.
  PlanExe is best read as a long-horizon planning artifact factory with an MCP
  service boundary, not as a general personal assistant.

## Executive judgment

RainBox should remain the base if the goal is a personal assistant shaped around
your own operating style, local models, inspectable state, reversible writes, and
experimentation. It is already more aligned with "my private assistant that I can
debug and bend" than Pi, BeeAI, OpenHands, OpenCode, or PlanExe, because those
are primarily framework, coding-agent, or specialized planning surfaces rather
than whole personal-assistant systems.

Hermes and OpenClaw are the closest product-level comparators. They are ahead on
"always-on assistant" surfaces: messaging gateways, onboarding, daemon mode,
voice/mobile/channel reach, and packaged skills. BeeAI is a different kind of
comparator: a reusable agent framework with stronger runtime abstractions,
provider/tool contracts, observability, and protocol adapters. PlanExe is also a
different kind of comparator: a specialized long-horizon planning system that
turns one detailed objective into a multi-stage, inspectable artifact bundle.
RainBox is ahead on narrow, auditable local authority: Postgres-backed
provenance, explicit write tiers, log-and-undo, dry-run/confirm, model-free
tests, and operator-visible eval loops.

The memory products are not direct replacements. They are possible memory engines
or design references. Supermemory looks strongest for broad ingestion/connectors
and user-profile/RAG packaging. Mem0 looks easiest to embed as a general memory
API. Honcho is the most conceptually interesting for personal assistants because
it models changing peers, sessions, and representations, not just retrieved
facts. MemPalace is the closest philosophical match to RainBox of the four: it is
local-first, MIT, verbatim (no summarization), and inspectable, and it reports
strong LongMemEval retrieval with zero API calls - making it the most natural
"better retrieval without giving up ownership" candidate. RainBox should add a
memory-provider seam and benchmark them before outsourcing memory.

PlanExe should not be merged into RainBox as another chat agent. Treat it as a
long-horizon planning module that RainBox can call, inspect, critique, and turn
into living execution state. The most valuable integration is: RainBox drafts and
remembers context, calls PlanExe when the task deserves a full planning pipeline,
ingests the resulting WBS/risks/governance/assumptions into RainBox kanban and
memory, then uses RainBox agents to verify, monitor, and update the plan over
time.

## RainBox baseline

RainBox today is a local, Postgres-backed assistant workbench:

- Local model providers: LM Studio, Jan, Ollama, model configs, model groups,
  structured-output/tool-call probes, and benchmarks.
- A Flask web UI with group chat, rooms, memberships, SSE via Postgres
  `LISTEN/NOTIFY`, feedback buttons, admin views, model pages, cron pages, and
  kanban.
- Process-isolated agents spawned by the supervisor via `os.posix_spawn` over a
  `socketpair`, with JSONL status/heartbeat messages and a watchdog that
  `SIGKILL`s on a missed heartbeat (`HEARTBEAT_TIMEOUT = 60.0s`, `source/main.py`).
- Multiple chat responder agents — the registry in `source/agents/__main__.py`
  lists ~19 agent types — including structured/unstructured chat, router, Q&A,
  query-router/filter-router, workspace shell, MCP agent, tool demo, and the
  newer `assistant`.
- Memory tables with claims, evidence, embeddings, lifecycle status, scope,
  sensitivity, expiry, provenance, retrieval telemetry, debug rows, and feedback
  hooks (`MemoryClaim`, `MemoryEvidence`, `MemoryEmbedding`, `RetrievalEvent`,
  `FeedbackEvent` in `source/db/models.py`).
- A bounded assistant loop (`STEP_LIMIT = 6` ReAct steps) with a code-owned
  capability registry, durable traces (`assistant_run`/`assistant_step` — one
  mutable row per step, opened at `running` and settled in place, so each step is
  individually addressable and a write it produces is FK-linked back to it via
  `assistant_write_intent.step_uuid`), hybrid memory lookup, Q&A lookup, workspace
  reads, kanban reads, and controlled writes (`source/agents/assistant.py`). The
  hybrid retrieval blends vector similarity
  (0.55), Postgres full-text rank (0.30), and an entity boost (0.15) in
  `source/memory/retrieval.py:retrieve_memories_hybrid`.
- Embeddings run fully locally on Ollama with `embeddinggemma:300m` (768-dim, the
  `MemoryEmbedding.embedding` pgvector column), so retrieval needs no external API
  calls — the same zero-API-call posture MemPalace advertises. Each embedding row
  stores its `model_name`/`embed_dim`, so swapping embedders re-embeds per claim
  (lazily on write plus a daily backfill cron) rather than corrupting the table
  (`source/memory/embeddings.py`).
- A separate Q&A seed-memory path embeds the curated
  `source/data/question_answer.jsonl` registry into a pgvector-backed
  `data_seed_memory` table and answers with a two-stage lookup — exact-alias match
  first, then semantic top-K — rebuildable without a restart via `rebuild_kb()`
  (the `/settings` "Repopulate Q&A memory" button), in
  `source/memory/seed_memory.py`.
- Write families already implemented: memory, skills, kanban, reminders (cron),
  and file edits. Writes are tiered as log-and-undo (execute immediately,
  reversible) or confirm (dry-run preview → operator approval). The
  `AssistantWriteIntent` state machine (proposed → confirmed → executing →
  completed/failed, with a `payload_hash` binding) governs the *confirm* tier;
  log-and-undo writes run inline and carry their own inverse, so they do not use
  that table.
- Evals and monitoring: feedback can be promoted into eval cases
  (`promote_feedback_to_eval_case`, downvotes default to a `regression` split);
  deterministic runners score candidates against baselines without a live LLM and
  gate on regressions (`source/evals/runner.py`, `source/evals/compare.py`).
- Encrypted backup support (age public-key + zstd, fail-closed, optional git
  push — `source/docs/backup.md`) and a `rainbox doctor` operator health check
  (`source/tools/doctor.py`) probing embedder reachability, model groups,
  capabilities, skills, and MCP.

RainBox's current weakness is not depth of internal mechanics. It is product
surface and integration reach:

- No mature always-on gateway comparable to OpenClaw/Hermes.
- Telegram exists as a service, but channel UX is not yet the primary product
  shape.
- Runtime dashboard UI is still missing (the backend endpoints —
  `/stop`, `/redirect`, write-intent `confirm/reject/undo` — already exist; only
  the operator-facing view is unbuilt, tracked as S7).
- *Governed* external-system/MCP adapters are intentionally deferred. Note the
  nuance: a standalone `MCPAgent` (`source/agents/mcp.py`, loading servers from
  `mcp.json`) already runs MCP tools directly as a chat agent. What is deferred
  is the *assistant adapter boundary* (S9 read-only, S10 write-capable) that
  would route MCP through the capability registry and write-intent ledger — the
  `Capability.adapter` field exists but is unused until that lands.
- Memory is inspectable and safe, but not yet as feature-rich as dedicated
  memory platforms for ingestion, graph reasoning, connectors, or long-context
  benchmarks.
- Setup is developer/operator-oriented, not casual-user friendly.

## Comparison table

| Project | Best at | Weak for your goal | What RainBox should learn |
|---|---|---|---|
| RainBox | Local ownership, inspectable state, reversible writes, eval loop, local model experiments | Fewer channels, less polished onboarding, no mature mobile/voice surface | Keep as the control plane; improve gateway and dashboard |
| PlanExe | Long-horizon planning pipeline, structured artifact bundle, MCP-facing job lifecycle, explicit critique/check stages | Not an always-on personal assistant; produces static plan artifacts unless another system turns them into living state | Treat planning as a staged, resumable artifact factory; import WBS/risks/assumptions into RainBox execution loops |
| Pi | Minimal terminal coding harness, reusable TypeScript agent core, unified LLM API, TUI/extensibility | Not a durable personal assistant; no built-in permission system; coding-first | Event-stream agent loop, provider API, extension/package model, supply-chain discipline |
| BeeAI Framework | Reusable Python/TypeScript agent runtime, provider abstraction, typed tools, nested events/middleware, workflow/protocol adapters | Framework rather than assistant product; shallow long-term memory; no RainBox-style durable governance/write ledger | Run/event/middleware model, structured tool/backend contract, RequirementAgent rules, MCP/A2A/OpenAI serving adapters |
| Hermes | Self-improving personal agent, messaging gateway, skills, cron, subagents, pluggable memory providers | Large capability surface; harder to audit; less tailored to RainBox's provenance-first model | Gateway, daemon setup, skill lifecycle, provider/memory pluggability |
| OpenClaw | Always-on multi-channel personal assistant, local-first gateway, voice/mobile/canvas, broad channel support | Host-access risk, huge remote-input attack surface, complex ecosystem | DM pairing/allowlists, channel routing, daemon mode, sandbox posture |
| OpenHands | Software-dev agent platform, sandboxed execution, SDK, cloud/enterprise, GitHub workflows | Not a personal life assistant; optimized for repos and SDLC | Sandbox abstractions, lifecycle controls, security analyzer patterns |
| OpenCode | Fast terminal coding agent, TUI, permissions, MCP, skills, custom tools | Coding-session tool, not a durable personal assistant | Permission grammar, terminal UX, AGENTS/skills ergonomics |
| Supermemory | Managed/self-hostable memory + context stack, user profiles, graph/RAG, connectors, multimodal ingestion | Outsources core memory semantics unless carefully wrapped | Benchmark as a memory backend; copy connector/profile ideas |
| Mem0 | Simple memory API, OSS/managed options, SDKs, MCP/editor integrations, graph memory | Less opinionated about full assistant governance; managed path is recommended | Use as easiest drop-in memory baseline for evals |
| Honcho | Reasoning-first peer/session memory, representations, multi-peer modeling, self-host FastAPI | Async reasoning latency; AGPL; less direct claim-level provenance control | Strong candidate for user/agent model experiments |
| MemPalace | Local-first verbatim memory, strong LongMemEval retrieval with zero API calls, MIT, wings/rooms/drawers + temporal KG, 33 MCP tools | Retrieval store, not an assistant governance layer; no claim/evidence provenance model; benchmark numbers self-reported | Closest values match; benchmark as a local retrieval backend behind the adapter |

## Product fit by category

### PlanExe

PlanExe is your long-horizon planning system. In code it is less like a chatbot
and more like a planning artifact factory: a submitted objective becomes a
`PlanItem` row, a worker claims that row, a Luigi pipeline expands the objective
through dozens of typed stages, and the system stores the final HTML report, zip
bundle, progress, usage artifacts, and failure diagnostics back onto the plan
record. The hosted UI and MCP server are front doors to that same job lifecycle.

How it works in code:

- `database_api.model_planitem.PlanItem` is the durable plan/task record. It
  carries the prompt, public state (`pending`, `processing`, `completed`,
  `failed`, `stopped`), progress fields, stop flags, model/profile parameters,
  report HTML, downloadable zip snapshot, internal `track_activity.jsonl`, usage
  overview JSON, and failure diagnostics.
- `mcp_cloud` exposes the agent boundary: `example_plans`, `example_prompts`,
  `model_profiles`, `plan_create`, `plan_status`, `plan_file_info`,
  `plan_list`, `plan_stop`, `plan_retry`, `plan_resume`, and feedback. Tool
  handlers validate input with Pydantic, run sync DB work through
  `asyncio.to_thread`, return structured content, and keep plan ids as PlanItem
  UUIDs.
- `worker_plan_database/app.py` is the queue worker. It polls Postgres for
  pending `PlanItem` rows, marks a plan processing, creates a run directory,
  sets token/user/API-key context, runs the pipeline, writes progress after each
  completed stage, honors stop requests at task boundaries, stores sanitized
  artifacts, and handles billing/progress events.
- `worker_plan/worker_plan_internal/plan/run_plan_pipeline.py` defines
  `PlanTask`, `ExecutePipeline`, progress calculation from expected output
  files, model fallback, usage metrics, logging, stop flags, and the Luigi build
  invocation. This is the framework; individual planning stages live under
  `worker_plan/worker_plan_internal/plan/nodes/`.
- `nodes/full_plan_pipeline.py` is the stage map. It wires prompt screening,
  constraint extraction, safety/redline checks, premise attack, purpose/domain
  classification, levers, scenarios, assumptions, governance, resources, team,
  SWOT, expert review, data collection, documents, WBS, pitch, dependencies,
  durations, schedule, review, executive summary, Q&A, premortem, self-audit,
  prompt adherence, and final report assembly.
- `nodes/report.py` is the assembly point: it pulls the intermediate markdown,
  CSV, and HTML outputs into a single interactive HTML report plus markdown
  report. The zip contains the intermediate files that fed the report.

The important point: PlanExe is not just "generate a plan with an LLM." It is a
fixed, inspectable workflow where each stage writes an artifact that can be
examined, reused, retried, and compared. The pipeline has explicit self-critique
stages: `redline_gate` classifies unsafe ideas; `premise_attack` runs multiple
lenses over the premise; `extract_constraints` and `constraint_checker` try to
keep downstream stages from violating user constraints; `prompt_adherence`
extracts original directives and scores final artifacts against them;
`premortem`, `self_audit`, `expert_review`, and `review_plan` stress-test the
plan from different angles.

The closest RainBox equivalents are clear:

- PlanExe `PlanItem` resembles RainBox `Journal` plus `AssistantRun` at job
  scale: both persist lifecycle, progress, and failure state. PlanExe is coarser
  but better for long-running artifact jobs; RainBox is finer-grained and better
  for step-by-step assistant actions.
- PlanExe pipeline stage outputs resemble RainBox assistant steps, eval cases,
  and write intents: they create reviewable breadcrumbs. PlanExe's breadcrumbs
  are files in a run directory; RainBox's are relational rows linked to chat,
  memory, kanban, and write approval.
- PlanExe's `plan_status` and SSE completion detector resemble the RainBox
  runtime dashboard RainBox still needs. RainBox should copy the simple public
  state contract and recent-output/stall visibility, but make it per assistant
  step/action, not just per plan.
- PlanExe's model profiles (`baseline`, `premium`, `frontier`, `custom`) map to
  RainBox's model groups and model configs. RainBox has the richer local model
  workbench; PlanExe has a simpler caller-facing profile selection contract.
- PlanExe's prompt examples are a good product idea: teach callers what a good
  input looks like before they call the expensive tool. RainBox should do the
  same for high-risk capabilities: show examples of good reminder/file/kanban
  requests and require clarification before vague writes.
- PlanExe's WBS, schedule, risks, governance, assumptions, and Q&A outputs are
  natural RainBox ingestion targets. They should become kanban boards/tasks,
  memory claims with provenance, evaluation cases, reminders/checkpoints, and
  monitoring prompts - not just a downloaded HTML report.

Non-obvious implementation details worth carrying over:

- `track_activity.jsonl` is treated as internal-only because it can contain
  provider payloads and secrets. PlanExe stores it separately on `PlanItem` and
  keeps it out of new downloadable zip snapshots. RainBox should apply the same
  discipline to any future model-call/activity ledger.
- PlanExe's stop behavior is cooperative. `plan_stop` can mark intent quickly,
  but a worker may only stop after the current LLM call or stage boundary. The
  UI/API wording distinguishes "stop requested" from actually stopped; RainBox's
  runtime dashboard should do the same.
- Progress is computed by comparing produced files with the expected output file
  set. That is a good low-tech stall detector for long jobs and maps well to
  RainBox background tasks that produce artifacts.
- `worker_plan_database` disposes inherited DB pools after fork to avoid
  corrupted psycopg2 connections between Luigi workers. If RainBox grows
  multiprocessing around Postgres, this is a concrete failure mode to remember.
- PlanExe records token/user/API-key context for LLM calls and incremental
  billing. RainBox does not need billing, but the attribution idea is useful for
  answering "which assistant/tool/model spent time or money on this result?"

Fit:

- Strong if the user asks for a substantial project plan, strategy, business
  launch, technical roadmap, governance model, or multi-month execution path.
- Strong as a tool RainBox can call through MCP once RainBox's read-only MCP
  adapter boundary is ready.
- Strong as a design reference for staged artifact generation, progress
  reporting, retry/resume/stop semantics, and built-in critique stages.
- Weak as an always-on personal assistant. It does not own daily chat context,
  personal memory, reminders, reversible writes, kanban execution, or channel
  routing in the RainBox sense.

Important caution: PlanExe output is structured and useful, but still untrusted.
Its own README says budgets, timelines, legal/regulatory details, and mitigations
need verification. RainBox should treat PlanExe artifacts like high-quality
source material from an LLM pipeline: ingest with provenance, extract claims,
turn tasks into reviewable execution state, and run follow-up verification before
acting on them.

RainBox takeaway: make PlanExe a specialist tool behind RainBox, not a
replacement. RainBox should know when a request is "long-horizon planning" and
route it to PlanExe; then it should own the living execution layer: memory,
kanban, reminders, evidence checks, drift monitoring, and operator-approved
updates.

### Pi

Pi Agent Harness is a minimal terminal coding harness and TypeScript agent
toolkit. Its repo is split into packages for:

- `@earendil-works/pi-coding-agent`: interactive coding-agent CLI;
- `@earendil-works/pi-agent-core`: stateful agent runtime with tool execution,
  event streaming, steering, follow-up queues, and state management;
- `@earendil-works/pi-ai`: unified multi-provider LLM API with model discovery,
  tool-call support, token/cost tracking, image/thinking support, and
  provider/OAuth handling;
- `@earendil-works/pi-tui`: terminal UI library.

The Pi docs describe the project as intentionally small at the core and extended
through TypeScript extensions, skills, prompt templates, themes, and Pi packages.
The coding-agent README says Pi skips some built-in product features, such as
subagents and plan mode, in favor of extensibility or third-party packages.

The most important non-README detail is the agent-core event model. Pi's agent
runtime emits `agent_start`, `turn_start`, message start/update/end events,
tool execution start/update/end events, `turn_end`, and `agent_end`. Tool
execution can be parallel or sequential, `beforeToolCall` can block execution
after argument validation, `afterToolCall` can postprocess results, and callers
can steer or queue follow-up messages while a run is active. (The
`beforeToolCall`/`afterToolCall` hooks and the turn/tool lifecycle are documented;
the exact event-name list above is consistent with that system but is not all
quoted verbatim in the top-level README.)

Fit:

- Strong if the goal is a compact coding-agent CLI or a reusable TypeScript
  agent/LLM runtime.
- Good reference for streaming event design, tool preflight hooks, provider
  abstraction, custom extensions, and terminal UX.
- Weak if the goal is a durable personal assistant with memory provenance,
  reminders, kanban, multi-channel messaging, or governed non-code writes.

Safety note: Pi explicitly does not include a built-in permission system for
restricting filesystem, process, network, or credential access. By default it
runs with the permissions of the launching process. Its documented answer is to
containerize or route tools into Gondolin, Docker, or OpenShell. That is a very
different safety posture from RainBox's current narrow workspace shell and
write-intent ledger.

RainBox takeaway: Pi is useful as an agent-runtime and TUI reference, not as the
personal-assistant product model. The strongest ideas to borrow are the event
stream, provider API, TypeScript extension/package model, and supply-chain
hardening. Do not copy the default all-permissions tool posture into RainBox.

### BeeAI Framework

BeeAI Framework is not a personal assistant in the same sense as RainBox,
Hermes, or OpenClaw. It is a library stack for building agents and multi-agent
systems in Python and TypeScript. The local checkout shows two mature API
surfaces rather than one app: Python under `python/beeai_framework` and
TypeScript under `typescript/src`. The project gives you agent classes, a common
chat/embedding backend contract, schema-validated tools, event emitters,
middleware, short-term memory strategies, workflow composition, caching, and
serving adapters for protocols such as MCP, A2A, ACP, AgentStack, and
OpenAI-compatible APIs.

How it works in code:

- The unit of execution is `Runnable.run(...)`, which returns a `Run`. A `Run` is
  awaitable and async-iterable, so callers can either wait for the final output
  or consume live events.
- `RunContext.enter(...)` creates a run id, group id, parent id, abort signal,
  context dict, and child `Emitter`. Nested runs pipe their events into the
  parent, giving agents, tools, models, workflows, and middleware one shared
  trace tree.
- `ChatModel` is the provider abstraction. Provider-specific adapters implement
  `_create` and `_create_stream`; the base class handles tool-choice support,
  retries, streaming token events, output validation, structured-output schemas,
  malformed tool-call repair, prompt caching hooks, usage/cost accumulation, and
  provider/model parsing such as `ollama:granite3.3:8b`.
- `Tool` is schema-first: each tool exposes a Pydantic input schema, validates
  input before execution, emits start/success/error/retry/finish events, supports
  retry options, and can cache results. The decorator path can turn a normal
  Python function into a tool by deriving a schema from its signature.
- `RequirementAgent` is BeeAI's most interesting agent design for RainBox. It
  runs an LLM/tool loop, but before each model call a reasoner computes rules for
  the available tools. Requirements can force a tool at a step, hide or deny a
  tool, prevent stopping until a minimum invocation is met, block repeated tool
  cycles, and add ask-permission handlers by intercepting tool start events.
- `HandoffTool` turns another agent/runnable into a tool, cloning the target when
  possible and copying the relevant non-system messages. That is BeeAI's
  multi-agent delegation primitive.
- `Workflow` is a typed state machine over a Pydantic state object. Each step can
  mutate state and route to next/prev/self/start/end or a named step, while
  emitting workflow events.
- Serving is adapter-oriented. The same runnable/agent can be registered behind
  an MCP server, OpenAI chat-completions/responses API, A2A executor, ACP/Zed
  integration, AgentStack server, or Watsonx Orchestrate surface. The adapters
  generally clone the runnable per request and initialize per-session memory.

The closest RainBox equivalents are clear:

- RainBox `AssistantAgent` has a code-owned `AssistantActionName` enum and
  `CAPABILITIES` registry. BeeAI has the more general `Tool` abstraction and
  `Requirement` rules. RainBox's registry is better for audited personal writes;
  BeeAI's schema/event contract is better as a reusable runtime surface.
- RainBox `assistant_run`/`assistant_step` tables are durable traces. BeeAI
  traces are in-process event trees. RainBox should keep Postgres as the source
  of truth, but borrow BeeAI's nested run ids and uniform event vocabulary for
  live dashboard streaming.
- RainBox provider wrappers are tuned for LM Studio, Jan, and Ollama model
  experiments. BeeAI's `ChatModel` contract is broader and cleaner for
  structured outputs, tool-call fallback, retries, streaming, usage/cost, and
  prompt caching. RainBox could adopt the shape without giving up its local-model
  workbench.
- RainBox workspace reads are deliberately narrow: allowlisted commands,
  `shell=False`, fixed environment, no interpreters/network/mutation. BeeAI's
  `ShellTool` is cleaner API-wise because it accepts argv directly and delegates
  to a backend, but it is broader authority unless wrapped by RainBox policy.
- RainBox file writes are confirm-tier, dry-run diff, then approved execution via
  `AssistantWriteIntent`. BeeAI's `FileEditTool` returns a diff and guards exact
  replacement counts, but it writes during the tool call. RainBox should copy the
  exact-replace guard and diff shape, not the direct-write authority.
- RainBox's standalone `MCPAgent` loads MCP tools into a LlamaIndex
  `FunctionAgent`. BeeAI has both sides: `MCPTool` wraps remote MCP tools as
  typed BeeAI tools, and `MCPServer` exposes BeeAI tools/runnables as MCP. That
  is a better adapter model for RainBox's deferred S9/S10 assistant boundary.
- RainBox memory is claim/evidence/provenance memory with lifecycle, sensitivity,
  embeddings, retrieval telemetry, and feedback hooks. BeeAI memory is mostly
  conversation-buffer memory (`UnconstrainedMemory`, `SlidingMemory`,
  `SummarizeMemory`, `TokenMemory`). BeeAI is not a replacement for RainBox
  memory, though its token/sliding/summarizing buffers could help with prompt
  packing.

Fit:

- Strong if RainBox needs a better internal runtime contract: nested events,
  middleware, typed run contexts, typed tools, provider abstraction, and protocol
  adapters.
- Strong as a source of implementation patterns for a runtime dashboard: every
  nested model/tool/workflow event already has trace metadata in BeeAI.
- Strong reference for exposing RainBox capabilities through MCP/A2A/OpenAI-like
  protocols without hand-building every adapter from scratch.
- Weak as a direct personal assistant. It has no durable Postgres-backed personal
  state, no rooms/memberships/kanban/reminders product surface, no provenance
  memory, no write-intent approval ledger, and no RainBox-specific operator
  workflow.

Important caution: BeeAI's abstractions can make powerful tools feel uniform,
which is useful for developers and dangerous for a personal assistant. RainBox
should not hand BeeAI tools directly to the model with broad filesystem, shell,
network, or MCP authority. Put RainBox's capability registry and write-intent
ledger in front of them.

RainBox takeaway: BeeAI is the best framework reference in this comparison. Do
not replace RainBox with it. Borrow the runtime ideas: `Run`/`RunContext`, nested
emitters, middleware, tool schemas, backend contract, RequirementAgent-style
rules, handoff tools, and protocol serving. Keep RainBox-owned persistence,
provenance, memory lifecycle, write tiers, and operator approval.

### Hermes

Hermes Agent is the strongest direct comparison if judged as "self-hosted
personal agent that grows with you." Its repo and docs describe a terminal UI,
messaging gateway, many model providers, skills, persistent memory, cron,
subagents, multiple terminal backends, and migration from OpenClaw.

What Hermes appears ahead on:

- First-class messaging gateway across Telegram, Discord, Slack, WhatsApp,
  Signal, CLI, and related surfaces.
- Self-improving skills and memory as product pillars.
- More mature daemon/onboarding story.
- Terminal backends including local, Docker, SSH, Modal/Daytona-style remote
  persistence.
- Built-in migration from OpenClaw and compatibility with memory providers like
  Honcho/Supermemory.

What RainBox has that Hermes may not match:

- Smaller, more explicit capability registry with write tiers.
- Log-and-undo/dry-run-confirm write ledger.
- Postgres-visible memory provenance with evidence rows and retrieval telemetry.
- Model-free tests around assistant contracts and write safety.
- A local model workbench tuned for comparing your own LM Studio/Jan/Ollama
  models.

RainBox takeaway: copy Hermes's product shell, not necessarily its internal
governance. RainBox should become easier to run as an always-on gateway and
should support pluggable memory providers, but should keep its trace-before-action
and reversible-write contracts.

### OpenClaw

OpenClaw is the other closest personal assistant comparator. Its repo presents a
local-first gateway for a personal AI assistant across many channels: WhatsApp,
Telegram, Slack, Discord, Signal, iMessage, IRC, Matrix, Teams, LINE, WeChat, and
others. It has daemon onboarding, DM pairing, channel routing, voice, canvas,
apps/nodes, tools, skills, and cron. Its README also states the important risk:
main-session tools run on the host by default, while non-main/group sessions can
be sandboxed.

What OpenClaw appears ahead on:

- Multi-channel assistant experience.
- Pairing/allowlist policy for inbound DMs.
- Mobile/voice/canvas surfaces.
- Install/update/doctor/onboarding path.
- Channel-to-agent routing.

Risks and limits:

- A multi-channel remote-input gateway is a large attack surface.
- Host tools on the main session are convenient but high-blast-radius.
- The project is much larger and more operationally complex than RainBox.

RainBox takeaway: OpenClaw's channel/security posture is the most relevant
reference. RainBox should adopt explicit DM pairing/allowlists before making
Telegram or other messaging surfaces powerful. Do not copy broad host-level
autonomy without RainBox's write-intent ledger and confirm gates.

### OpenHands

OpenHands is primarily a software engineering agent platform, not a personal
assistant. Its docs describe Agent Canvas, CLI, Cloud, Enterprise, and a Software
Agent SDK. The SDK docs index emphasizes agent loops, workspace isolation,
sandboxes, MCP, tools, persistence, pause/resume, sub-agent delegation,
observability, security/action confirmation, and remote agent servers. The papers
frame OpenHands as an agent that writes code, interacts with a command line, and
browses the web in sandboxed environments.

Fit:

- Excellent reference for coding-agent architecture, sandbox execution, remote
  workspaces, GitHub workflows, lifecycle control, and SDK design.
- Not a full personal assistant for memory, relationships, reminders, personal
  preferences, and day-to-day channels.

RainBox takeaway: use OpenHands as a sandbox/security/lifecycle reference if
RainBox grows external tools. It is not the product direction.

### OpenCode

OpenCode is an open-source AI coding agent available as terminal UI, desktop app,
and IDE extension. Its docs show a rich tool system: bash, edit/write/read, grep,
glob, LSP, apply_patch, skills, todo, webfetch/search, questions, custom tools,
and MCP. Its permission model supports allow/ask/deny globally and per-agent,
with granular patterns for bash, edit paths, external directories, and defaults
such as denying `.env` reads.

Fit:

- Very good coding-session assistant.
- Good reference for a compact permission grammar and terminal UX.
- Not a durable personal assistant by itself.

RainBox takeaway: RainBox's workspace shell is intentionally narrower and safer.
OpenCode shows how to make permissions usable when the tool surface grows. If
RainBox adds richer file/code capabilities, copy the permission ergonomics, not
the default permissiveness.

## Memory products

### Supermemory

Supermemory positions itself as long-term/short-term memory and context
infrastructure for AI agents. Its docs describe ingestion for text,
conversations, files, images, docs, PDFs, and videos; a semantic understanding
graph; learned user memory; user profiles; RAG; metadata filtering; contextual
chunking; connectors; MCP; and self-hosting with a local/offline binary.

Fit:

- Strong if RainBox should ingest a lot of external material quickly: email,
  drive docs, PDFs, videos, code, project artifacts.
- Strong if you want a memory service with profiles and RAG as productized
  primitives.
- Risky if it replaces RainBox's inspectable claim/evidence model wholesale.

RainBox takeaway: evaluate Supermemory as an optional memory backend behind a
RainBox adapter. Keep RainBox's local provenance and sensitivity gates in front
of it.

### Mem0

Mem0 is a memory layer for LLM agents with managed and self-hosted options. The
docs distinguish Platform (`MemoryClient`) and OSS (`Memory`) and show simple
add/search/get/update/delete operations. The docs index covers graph memory,
entity-scoped memory, filters, advanced retrieval, temporal reasoning, custom
instructions, feedback, webhooks, MCP, editor integrations, and a local companion
with Ollama. The Mem0 paper is arxiv 2504.19413. Caveat for self-hosters: graph
memory was dropped from the v3 OSS rewrite and currently lives on the hosted
Platform, so "graph memory" is edition-dependent — relevant if RainBox wants
graph reasoning without the managed path.

Fit:

- Easiest baseline for "give my agent memory" via SDK/API.
- Good integration ecosystem.
- Good candidate for a benchmark adapter because its CRUD API maps cleanly to
  RainBox memory operations.

Limits:

- It is a memory component, not an assistant governance layer.
- Managed is the recommended path in their docs; OSS exists but needs provider,
  embedder, vector store, and graph-store choices.

RainBox takeaway: use Mem0 as the simplest external-memory baseline. If RainBox's
own memory retrieval falls behind, Mem0 can validate whether the gap is in
retrieval quality or in assistant behavior.

### Honcho

Honcho is an open-source memory library with a managed service. Its docs describe
workspaces, peers, sessions, messages, background reasoning, peer
representations, peer cards, conclusions, session context, hybrid search, file
upload, SDKs, MCP/client integrations, and self-hosting via Docker/FastAPI. The
important difference is peer-centric reasoning: humans, agents, groups, projects,
and ideas are modeled as changing entities.

Fit:

- Best conceptual match for a personal assistant that should understand you,
  other agents, projects, relationships, and "what one peer knows about another."
- Strong fit for multi-agent RainBox rooms, because RainBox already has agents,
  rooms, memberships, and conversation history.
- Useful for long-running personality/user-model experiments.

Limits:

- Background reasoning means eventual consistency; new messages may not be
  reflected immediately.
- AGPL-3.0 matters if RainBox ever distributes a combined server.
- It may be less directly auditable than RainBox's explicit claim/evidence rows
  unless wrapped carefully.

RainBox takeaway: Honcho is the most interesting optional memory backend for
"personal assistant for my needs." It should be tested as an augmenting
representation layer, not as the only source of truth.

### MemPalace

MemPalace is an MIT-licensed, local-first AI memory system. Its README frames it
as verbatim conversation memory with semantic search: "MemPalace stores your
conversation history as verbatim text and retrieves it with semantic search. It
does not summarize, extract, or paraphrase." It organizes memory into a
three-level hierarchy — *wings* (people and projects), *rooms* (topics), and
*drawers* (original content) — so searches are scoped rather than flat-corpus. On
top of the verbatim store it adds a temporal entity-relationship knowledge graph
with validity windows (add/query/invalidate/timeline) backed by local SQLite.

The retrieval story is staged: raw semantic search (reported 96.6% R@5 on
LongMemEval, no API keys/LLM), a hybrid v4 pipeline with keyword/temporal boosting
(reported 98.4% R@5), and optional LLM reranking (reported ≥99%). Those numbers
are self-reported in the README, not reproduced here. Storage backends are
pluggable: ChromaDB by default, `sqlite_exact` for vector-correctness validation,
and Qdrant or pgvector as explicit opt-ins — and the README is careful that
pointing Qdrant/pgvector at a non-local service sends verbatim drawer text there
"never the default."

Integration is unusually deep for the local-first niche: 33 MCP tools covering
palace reads/writes, knowledge-graph ops, cross-wing navigation, drawer
management, and agent diaries; auto-save hooks for Claude Code, Codex CLI, and
Cursor that snapshot transcripts before context compression; and per-agent
isolation where "each specialist agent gets its own wing and diary," discoverable
at runtime via `mempalace_list_agents`. It is Python 3.9+, installs as a CLI (`uv
tool install mempalace` / `pipx`), and uses `embeddinggemma-300m` (multilingual)
or `all-MiniLM-L6-v2` (English) embeddings.

Fit:

- Best values match of the memory products: local-only by default, MIT, verbatim
  (no lossy summarization), and inspectable — the same instincts RainBox already
  has.
- Strong if the priority is retrieval quality over your own conversation history.
  RainBox already embeds locally with zero API calls (`embeddinggemma:300m` on
  Ollama), so the draw is MemPalace's *retrieval pipeline*, not API independence —
  and its pgvector backend could sit alongside RainBox's existing Postgres.
- Conceptually adjacent to RainBox's own rooms/agents: wings/rooms/drawers and
  per-agent diaries map naturally onto RainBox rooms, agents, and memberships.

Limits:

- It is a retrieval store, not an assistant governance layer. It has no
  claim/evidence/lifecycle provenance model, no write tiers, and no
  dry-run/confirm — RainBox would keep all of that in front of it.
- Verbatim storage of full transcripts is a different sensitivity posture than
  RainBox's scoped claims with sensitivity gates; the drawer contents would need
  to inherit RainBox's secret/sensitivity handling.
- Benchmark numbers are the project's own claims on one dataset (LongMemEval);
  they need to be reproduced on your data before being trusted.

RainBox takeaway: MemPalace is the strongest candidate to benchmark as a local
retrieval backend behind RainBox's memory-provider seam, because it shares
RainBox's ownership/inspectability values instead of fighting them. Its pgvector
backend means it can reuse the existing Postgres deployment. Borrow the
wings/rooms/drawers scoping idea and the verbatim-plus-temporal-KG split as design
references even if it is not adopted as the backend. Keep RainBox's claim/evidence
provenance and sensitivity gates in front of any verbatim drawer store.

## Dimension-by-dimension comparison

### Data ownership

RainBox is best aligned with local/private ownership today. It stores the real
state in local Postgres and is designed around operator inspection. PlanExe is
self-hostable and stores plan state in Postgres plus run-directory artifacts, but
it is organized around plan jobs rather than personal assistant state. BeeAI is
local/self-hostable as a framework, but it does not itself define durable
personal state; ownership depends on the app and adapters built with it.
OpenClaw and Hermes can also run locally, but their broader channel/tool
surfaces make the trust boundary wider. Pi, OpenHands, and OpenCode are
local-capable but oriented toward code workspaces. Supermemory/Mem0/Honcho can
be managed or self-hosted depending on product and license, but outsourcing
memory changes the trust model. MemPalace is the exception among the memory
products: it is local-first by default and only sends data out if you explicitly
point it at a remote Qdrant/pgvector, so it preserves the ownership posture
RainBox cares about.

### Personal-assistant UX

Hermes and OpenClaw are ahead. RainBox has a useful web chat and Telegram
service, but not yet a "talk to it anywhere" experience. OpenClaw's pairing and
channel breadth are especially relevant. Hermes's CLI/gateway continuity is also
relevant. Pi, BeeAI, OpenHands, and OpenCode are not broad personal-assistant UX
products; Pi is closer to a minimal terminal coding harness plus embeddable
agent toolkit, BeeAI is a framework/runtime rather than an end-user shell, and
PlanExe is a specialist planning service rather than a daily assistant shell.

### Autonomy and writes

RainBox is unusually strong here because it treats writes as first-class
governed operations. The assistant can write memory, skills, kanban, reminders,
and files, but with clear risk tiers, traces, undo, and dry-run/confirm. PlanExe
has strong job-level lifecycle controls (pending/processing/completed/failed/
stopped, retry/resume/stop, progress, artifacts), but it does not govern
personal-assistant writes. OpenClaw and Hermes appear more product-capable, but
that broader autonomy is also the risk. OpenHands/OpenCode have mature code-edit
autonomy, but that is repo-focused rather than personal-life focused. BeeAI has
useful ask-permission requirements and tool rules, but no durable write-intent
ledger; RainBox's approval model should remain authoritative.

### Memory

RainBox's memory is safe, inspectable, and provenance-rich. BeeAI's memory
classes are useful prompt-window buffers, not long-term personal memory.
Supermemory, Mem0, Honcho, and MemPalace are more specialized and likely
stronger on large-scale retrieval, cross-session benchmark performance,
graph/reasoning, connectors, and packaged SDKs. Honcho is the best conceptual complement for user/agent modeling.
Supermemory is the strongest ingestion/context stack. Mem0 is the simplest
integration baseline. MemPalace is the strongest local-first verbatim retriever
and the closest to RainBox's ownership/inspectability values, with self-reported
LongMemEval recall that — if it holds on your data — would set a high retrieval
bar to beat natively.

### Safety

RainBox's safety stance is narrow authority plus auditability:

- code-owned capabilities;
- model-free tests;
- path confinement for workspace reads/edits;
- memory sensitivity filters;
- explicit write intents;
- reversible internal writes;
- confirm-tier high-blast-radius writes.

PlanExe has a different safety lesson: critique and verification should be
explicit stages, not afterthoughts. Its redline, premise attack, constraint
checking, expert review, premortem, self-audit, and prompt-adherence stages are
worth copying for RainBox's bigger actions. OpenCode has a good allow/ask/deny
permission model. BeeAI has useful RequirementAgent rules and ask-permission
hooks, but those are runtime controls rather than durable governance. OpenHands
has strong sandbox/workspace architecture. Pi has good documented
containerization patterns but no built-in permission system. OpenClaw has DM
pairing and non-main sandboxing, but its main-session host access is a major
risk if exposed casually. Hermes claims command approval and container
isolation, but its broad gateway should be treated with the same caution.

## Recommended direction for RainBox

### 1. Keep RainBox as the personal base

Do not pivot to a generic agent framework, including BeeAI. RainBox's moat is
that it is already your own assistant substrate: local models, Postgres state,
inspectable memory, evals, kanban, cron, feedback, and controlled writes. That
is closer to the stated goal than a polished but opaque assistant or a clean
runtime library.

### 2. Build the missing product shell

The next meaningful gap is not another internal memory schema. It is making the
assistant present where you actually talk:

1. Make Telegram/gateway usage first-class.
2. Add pairing/allowlist policy before giving remote chats write authority.
3. Add a daemon/service story and `doctor`/status visibility.
4. Add the runtime dashboard promised in the backlog: active run, PID, step,
   current action/model, heartbeat age, stop/redirect/kill/retry, and write
   intent buttons.

Hermes/OpenClaw are the references here.

### 3. Add a memory-provider seam, then benchmark

Do not replace RainBox memory blindly. Add an adapter interface with at least:

- `store_turn(room, participants, messages)`
- `remember_claim(text, scope, sensitivity, provenance)`
- `search(query, room, agent, project, include_secret=False)`
- `profile(entity, budget)`
- `explain(memory_id)`

Then test:

- RainBox native only
- RainBox + Honcho representations
- RainBox + Supermemory context/profile
- RainBox + Mem0 memory search
- RainBox + MemPalace local verbatim retrieval (pgvector backend on the existing
  Postgres)

Use the existing eval loop and feedback telemetry. The question is not "which
memory product is better in general"; it is "which one improves my assistant
without making it less inspectable or less safe."

### 4. Keep writes RainBox-owned

Even if external memory or channels are added, writes should still go through
RainBox's capability registry and write-intent ledger. External systems should be
adapters behind RainBox actions, not model-invoked free-for-all tools.

Near-term safe order:

1. Read-only external/MCP adapters.
2. Confirm-tier external writes with dry-run previews.
3. Log-and-undo only for internal writes where RainBox owns the inverse.

### 5. Treat PlanExe as RainBox's long-horizon planning tool

RainBox should integrate PlanExe through the same governed adapter path planned
for MCP/external systems. The useful workflow is not "chat with PlanExe." It is:

1. RainBox recognizes that a request deserves a full planning pipeline.
2. RainBox drafts a PlanExe-quality prompt using remembered context, constraints,
   budget, stakeholders, success criteria, and banned approaches.
3. The operator approves that prompt before `plan_create`.
4. RainBox polls `plan_status` or subscribes to the SSE completion detector.
5. RainBox ingests the resulting zip/report: WBS -> kanban, risks -> memory and
   monitoring checks, assumptions -> claim/evidence review, schedule -> reminders,
   governance -> decision rules.
6. RainBox continues owning drift monitoring, updates, and write authority.

This is the strongest bridge between your two systems: PlanExe creates the
initial rigorous plan; RainBox keeps it alive.

### 6. Use BeeAI/Pi/OpenCode/OpenHands for runtime and coding ideas, not identity

BeeAI, Pi, OpenCode, and OpenHands are useful tools and design references.
RainBox can borrow BeeAI's run/event/middleware model, tool/backend contracts,
and protocol adapters; Pi's event-stream runtime ideas; OpenCode's permission
ergonomics; and OpenHands' sandbox/lifecycle architecture. It should not become
primarily a framework demo or coding agent. For your goal, coding is one skill
of the personal assistant, not the identity of the assistant.

### 7. Best ideas to pull forward

The best ideas across this comparison are:

- RainBox: keep code-owned capabilities, trace-before-action, reversible internal
  writes, confirm-tier external/high-risk writes, local model experiments, and
  model-free regression tests.
- PlanExe: copy staged artifact generation, explicit critique gates, prompt
  examples before expensive tool calls, progress based on expected artifacts,
  retry/resume/stop semantics, and downloadable intermediate bundles.
- Hermes/OpenClaw: copy always-on gateway ergonomics, onboarding, daemon mode,
  channel routing, and DM pairing/allowlists.
- BeeAI/Pi: copy nested run/event streams, middleware, typed tool/provider
  contracts, and clean protocol adapters.
- OpenCode/OpenHands: copy permission ergonomics, sandbox/workspace lifecycle
  ideas, and operator-visible action confirmation patterns.
- Memory products: benchmark optional memory backends behind RainBox policy; do
  not surrender provenance, sensitivity, or inspection.

Other things easy to miss: PlanExe already encodes a lot of the "AI output is
untrusted" mindset that RainBox wants, but it encodes it as pipeline stages.
RainBox encodes the same value as write governance and memory provenance. The
winning architecture is to combine both: PlanExe-style critique before producing
large artifacts, RainBox-style governance before taking action from them.

## Practical decision matrix

If the goal is:

- "I want a long-horizon plan for a serious project": call PlanExe, then have
  RainBox ingest and manage the result.
- "I want a self-hosted personal agent now": try Hermes or OpenClaw and compare
  daily use friction.
- "I want a minimal terminal coding harness": try Pi.
- "I want a reusable agent runtime/framework": study BeeAI first, especially
  its Python `RunContext`, `Tool`, `ChatModel`, `RequirementAgent`, `Workflow`,
  and serving adapters.
- "I want a coding agent with stronger permission UX": try OpenCode.
- "I want a software-agent platform/SDK": try OpenHands.
- "I want a private assistant that reflects my workflows and can be audited":
  continue RainBox.
- "I want better memory fast": prototype Mem0 first, Supermemory second, Honcho
  for deeper peer/user modeling.
- "I want better memory without giving up local ownership": prototype MemPalace
  (local-first, MIT, verbatim, pgvector backend) and verify its LongMemEval
  numbers on your own data.
- "I want the best long-term personal assistant": RainBox core + OpenClaw/Hermes
  style gateway + PlanExe planning tool + Honcho/Supermemory/Mem0/MemPalace
  adapter benchmark.

## Suggested next RainBox work

1. Runtime dashboard (already in RainBox backlog), with PlanExe-style public
   state, progress, current step, recent outputs, stall detection, and stop/
   retry controls.
2. Read-only MCP adapters through RainBox's existing `Capability.adapter` seam;
   make PlanExe the first serious external tool because its write surface is
   naturally job-scoped.
3. PlanExe ingestion prototype: `plan_create` -> `plan_status` -> zip parse ->
   WBS into kanban, assumptions/risks into memory claims, schedule into
   reminders, prompt-adherence issues into eval cases.
4. Telegram/gateway hardening: pairing, allowlist, read-only default, write
   authority gates.
5. Memory adapter interface and benchmark harness.
6. BeeAI-inspired runtime-event pass: map RainBox assistant steps, provider
   calls, capability dispatch, MCP calls, PlanExe plan jobs, and write-intent
   state transitions onto one nested event stream for the runtime dashboard.
7. Honcho proof-of-concept for room/agent/user representations.
8. Supermemory or Mem0 proof-of-concept for document/project ingestion.
   MemPalace proof-of-concept for local verbatim retrieval on the existing
   Postgres (pgvector backend), validating its LongMemEval claims on your data.
9. Voice UX later; do not add it before the gateway safety model is clear.

## Bottom line

RainBox is not behind because it lacks agent hype features. It is behind on
assistant availability and onboarding. Its core governance is already strong.

The best path is not replacement. It is:

```text
RainBox as authority and audit log
+ PlanExe as long-horizon planning artifact factory
+ Hermes/OpenClaw-style gateway and channel UX
+ optional memory-provider adapter
+ BeeAI/Pi/OpenCode/OpenHands-inspired runtime, permission, and sandbox hardening
```

That combination fits "own personal assistant for my needs" better than any one
external project in isolation.

## Sources checked

RainBox local:

- `source/README.md`
- `source/docs/memory-architecture.md`
- `source/docs/operator-guide.md`
- `source/docs/proposals/2026-06-20-status.md`
- `source/docs/proposals/2026-06-20-improvements-v3.md`
- `source/agents/assistant.py`
- `source/db/models.py`
- `source/agents/mcp.py`
- `source/tools/workspace_command_runner.py`
- `source/tools/command_policy.py`
- `source/docs/proposals/2026-06-09-planexe-personas.md`
- `source/docs/proposals/2026-06-23-status.md`

External / local comparators:

- PlanExe local checkout, especially `README.md`, `AGENTS.md`,
  `worker_plan/AGENTS.md`,
  `worker_plan/worker_plan_internal/plan/run_plan_pipeline.py`,
  `worker_plan/worker_plan_internal/plan/nodes/full_plan_pipeline.py`,
  `worker_plan/worker_plan_internal/plan/nodes/report.py`,
  `worker_plan/worker_plan_internal/diagnostics/redline_gate.py`,
  `worker_plan/worker_plan_internal/diagnostics/premise_attack.py`,
  `worker_plan/worker_plan_internal/diagnostics/constraint_checker.py`,
  `worker_plan/worker_plan_internal/diagnostics/prompt_adherence.py`,
  `database_api/model_planitem.py`, `worker_plan_database/app.py`,
  `mcp_cloud/AGENTS.md`, `mcp_cloud/tool_models.py`,
  `mcp_cloud/handlers.py`, and `mcp_cloud/db_queries.py`.
- BeeAI Framework local checkout, especially `README.md`,
  `python/pyproject.toml`,
  `python/beeai_framework/context.py`,
  `python/beeai_framework/emitter/emitter.py`,
  `python/beeai_framework/runnable.py`,
  `python/beeai_framework/backend/chat.py`,
  `python/beeai_framework/tools/tool.py`,
  `python/beeai_framework/agents/requirement/agent.py`,
  `python/beeai_framework/agents/requirement/_runner.py`,
  `python/beeai_framework/agents/requirement/requirements/`,
  `python/beeai_framework/tools/handoff.py`,
  `python/beeai_framework/tools/mcp/mcp.py`,
  `python/beeai_framework/adapters/mcp/serve/server.py`,
  `python/beeai_framework/adapters/openai/serve/server.py`,
  `python/beeai_framework/workflows/workflow.py`, and `typescript/src`.
- Pi Agent Harness: https://github.com/earendil-works/pi,
  https://pi.dev/docs/latest,
  https://github.com/earendil-works/pi/tree/main/packages/agent,
  https://github.com/earendil-works/pi/tree/main/packages/coding-agent,
  https://github.com/earendil-works/pi/tree/main/packages/ai,
  https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/containerization.md
- Hermes Agent: https://github.com/NousResearch/hermes-agent,
  https://hermes-agent.nousresearch.com/docs
- OpenClaw: https://github.com/openclaw/openclaw, https://openclaw.ai/
- OpenHands: https://docs.openhands.dev/, https://docs.openhands.dev/llms.txt,
  https://arxiv.org/abs/2407.16741,
  https://arxiv.org/abs/2511.03690
- OpenCode: https://opencode.ai/docs/, https://opencode.ai/docs/tools/,
  https://opencode.ai/docs/permissions/, https://opencode.ai/docs/agents/
- Supermemory: https://supermemory.ai/docs,
  https://github.com/supermemoryai/supermemory
- Mem0: https://docs.mem0.ai/, https://docs.mem0.ai/llms.txt,
  https://arxiv.org/abs/2504.19413
- Honcho: https://honcho.dev/docs/v3/documentation/introduction/overview,
  https://github.com/plastic-labs/honcho
- MemPalace: https://github.com/mempalace/mempalace,
  https://raw.githubusercontent.com/mempalace/mempalace/main/README.md
