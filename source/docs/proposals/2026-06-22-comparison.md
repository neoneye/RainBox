# RainBox comparison: personal assistant direction

Date: 2026-06-22

## Purpose

Compare RainBox with Pi, Hermes, OpenClaw, OpenHands, OpenCode, Supermemory,
Mem0, Honcho, and MemPalace for the goal:

> Own personal assistant for my needs.

This is not a README-only scan. For RainBox I read the local architecture docs,
status proposals, and implementation files around agents, memory, cron, tools,
and the assistant write surface. For external projects I used official docs,
repo structure/code surfaces, docs indexes, and, where relevant, architecture
papers or policy pages. The external landscape changes quickly, so treat this as
a decision memo dated above, not a permanent market survey.

## Verification status (2026-06-22)

This document was fact-checked against the RainBox source tree and against the
external projects' public repos/docs on 2026-06-21, with MemPalace added and
checked against its public repo/README on 2026-06-22.

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

## Executive judgment

RainBox should remain the base if the goal is a personal assistant shaped around
your own operating style, local models, inspectable state, reversible writes, and
experimentation. It is already more aligned with "my private assistant that I can
debug and bend" than Pi, OpenHands, or OpenCode, because those three are
primarily coding-agent/toolkit surfaces rather than whole personal-assistant
systems.

Hermes and OpenClaw are the closest product-level comparators. They are ahead on
"always-on assistant" surfaces: messaging gateways, onboarding, daemon mode,
voice/mobile/channel reach, and packaged skills. RainBox is ahead on narrow,
auditable local authority: Postgres-backed provenance, explicit write tiers,
log-and-undo, dry-run/confirm, model-free tests, and operator-visible eval loops.

The memory products are not direct replacements. They are possible memory engines
or design references. Supermemory looks strongest for broad ingestion/connectors
and user-profile/RAG packaging. Mem0 looks easiest to embed as a general memory
API. Honcho is the most conceptually interesting for personal assistants because
it models changing peers, sessions, and representations, not just retrieved
facts. MemPalace is the closest philosophical match to RainBox of the four: it is
local-first, MIT, verbatim (no summarization), and inspectable, and it reports
strong LongMemEval retrieval with zero API calls — making it the most natural
"better retrieval without giving up ownership" candidate. RainBox should add a
memory-provider seam and benchmark them before outsourcing memory.

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
  capability registry, durable traces (`assistant_run`/`assistant_step`), hybrid
  memory lookup, Q&A lookup, workspace reads, kanban reads, and controlled writes
  (`source/agents/assistant.py`). The hybrid retrieval blends vector similarity
  (0.55), Postgres full-text rank (0.30), and an entity boost (0.15) in
  `source/memory/retrieval.py:retrieve_memories_hybrid`.
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
| Pi | Minimal terminal coding harness, reusable TypeScript agent core, unified LLM API, TUI/extensibility | Not a durable personal assistant; no built-in permission system; coding-first | Event-stream agent loop, provider API, extension/package model, supply-chain discipline |
| Hermes | Self-improving personal agent, messaging gateway, skills, cron, subagents, pluggable memory providers | Large capability surface; harder to audit; less tailored to RainBox's provenance-first model | Gateway, daemon setup, skill lifecycle, provider/memory pluggability |
| OpenClaw | Always-on multi-channel personal assistant, local-first gateway, voice/mobile/canvas, broad channel support | Host-access risk, huge remote-input attack surface, complex ecosystem | DM pairing/allowlists, channel routing, daemon mode, sandbox posture |
| OpenHands | Software-dev agent platform, sandboxed execution, SDK, cloud/enterprise, GitHub workflows | Not a personal life assistant; optimized for repos and SDLC | Sandbox abstractions, lifecycle controls, security analyzer patterns |
| OpenCode | Fast terminal coding agent, TUI, permissions, MCP, skills, custom tools | Coding-session tool, not a durable personal assistant | Permission grammar, terminal UX, AGENTS/skills ergonomics |
| Supermemory | Managed/self-hostable memory + context stack, user profiles, graph/RAG, connectors, multimodal ingestion | Outsources core memory semantics unless carefully wrapped | Benchmark as a memory backend; copy connector/profile ideas |
| Mem0 | Simple memory API, OSS/managed options, SDKs, MCP/editor integrations, graph memory | Less opinionated about full assistant governance; managed path is recommended | Use as easiest drop-in memory baseline for evals |
| Honcho | Reasoning-first peer/session memory, representations, multi-peer modeling, self-host FastAPI | Async reasoning latency; AGPL; less direct claim-level provenance control | Strong candidate for user/agent model experiments |
| MemPalace | Local-first verbatim memory, strong LongMemEval retrieval with zero API calls, MIT, wings/rooms/drawers + temporal KG, 33 MCP tools | Retrieval store, not an assistant governance layer; no claim/evidence provenance model; benchmark numbers self-reported | Closest values match; benchmark as a local retrieval backend behind the adapter |

## Product fit by category

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
- Strong if the priority is retrieval quality over your own conversation history
  with zero external API calls, especially with a pgvector backend that could sit
  alongside RainBox's existing Postgres.
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
state in local Postgres and is designed around operator inspection. OpenClaw and
Hermes can also run locally, but their broader channel/tool surfaces make the
trust boundary wider. Pi, OpenHands, and OpenCode are local-capable but oriented
toward code workspaces. Supermemory/Mem0/Honcho can be managed or self-hosted
depending on product and license, but outsourcing memory changes the trust
model. MemPalace is the exception among the memory products: it is local-first by
default and only sends data out if you explicitly point it at a remote
Qdrant/pgvector, so it preserves the ownership posture RainBox cares about.

### Personal-assistant UX

Hermes and OpenClaw are ahead. RainBox has a useful web chat and Telegram
service, but not yet a "talk to it anywhere" experience. OpenClaw's pairing and
channel breadth are especially relevant. Hermes's CLI/gateway continuity is also
relevant. Pi, OpenHands, and OpenCode are not broad personal-assistant UX
products; Pi is closer to a minimal terminal coding harness plus embeddable
agent toolkit.

### Autonomy and writes

RainBox is unusually strong here because it treats writes as first-class
governed operations. The assistant can write memory, skills, kanban, reminders,
and files, but with clear risk tiers, traces, undo, and dry-run/confirm. OpenClaw
and Hermes appear more product-capable, but that broader autonomy is also the
risk. OpenHands/OpenCode have mature code-edit autonomy, but that is repo-focused
rather than personal-life focused.

### Memory

RainBox's memory is safe, inspectable, and provenance-rich. Supermemory, Mem0,
Honcho, and MemPalace are more specialized and likely stronger on large-scale
retrieval, cross-session benchmark performance, graph/reasoning, connectors, and
packaged SDKs. Honcho is the best conceptual complement for user/agent modeling.
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

OpenCode has a good allow/ask/deny permission model. OpenHands has strong
sandbox/workspace architecture. Pi has good documented containerization patterns
but no built-in permission system. OpenClaw has DM pairing and non-main
sandboxing, but its main-session host access is a major risk if exposed
casually. Hermes claims command approval and container isolation, but its broad
gateway should be treated with the same caution.

## Recommended direction for RainBox

### 1. Keep RainBox as the personal base

Do not pivot to a generic agent framework. RainBox's moat is that it is already
your own assistant substrate: local models, Postgres state, inspectable memory,
evals, kanban, cron, feedback, and controlled writes. That is closer to the
stated goal than a polished but opaque assistant.

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

### 5. Use Pi/OpenCode/OpenHands for coding, not identity

Pi, OpenCode, and OpenHands are useful tools and design references. RainBox can
borrow Pi's event-stream runtime ideas, OpenCode's permission ergonomics, and
OpenHands' sandbox/lifecycle architecture. It should not become primarily a
coding agent. For your goal, coding is one skill of the personal assistant, not
the identity of the assistant.

## Practical decision matrix

If the goal is:

- "I want a self-hosted personal agent now": try Hermes or OpenClaw and compare
  daily use friction.
- "I want a minimal terminal coding harness": try Pi.
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
  style gateway + Honcho/Supermemory/Mem0 adapter benchmark.

## Suggested next RainBox work

1. Runtime dashboard (already in RainBox backlog).
2. Telegram/gateway hardening: pairing, allowlist, read-only default, write
   authority gates.
3. Memory adapter interface and benchmark harness.
4. Honcho proof-of-concept for room/agent/user representations.
5. Supermemory or Mem0 proof-of-concept for document/project ingestion.
   MemPalace proof-of-concept for local verbatim retrieval on the existing
   Postgres (pgvector backend), validating its LongMemEval claims on your data.
6. Read-only MCP adapters through RainBox's existing `Capability.adapter` seam.
7. Voice UX later; do not add it before the gateway safety model is clear.

## Bottom line

RainBox is not behind because it lacks agent hype features. It is behind on
assistant availability and onboarding. Its core governance is already strong.

The best path is not replacement. It is:

```text
RainBox as authority and audit log
+ Hermes/OpenClaw-style gateway and channel UX
+ optional memory-provider adapter
+ Pi/OpenCode/OpenHands-inspired runtime, permission, and sandbox hardening
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

External:

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
