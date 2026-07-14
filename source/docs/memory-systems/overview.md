# Agent Memory Systems Comparative Report

## 1. High-Level Taxonomy

This workspace contains several different answers to "agent memory". They should not be evaluated as one category.

### Library-first memory layer

Repos:

- `mem0`
- `langmem`

These optimize for easy embedding into an existing agent application. The application calls memory tools or SDK functions; the library handles extraction, storage, and retrieval.

Tradeoff: the memory layer is easy to adopt but usually has weak authority over the agent loop. It can store and retrieve, but it cannot guarantee the model calls the right tool at the right time, verifies facts, or uses the recall output safely.

### Agent-runtime memory

Repos:

- `letta`
- `rainbox`

This treats memory as part of the agent runtime. Memory is not merely an external RAG sidecar; it is compiled into or injected into prompt/context, mutated through first-class tools/actions, searched through runtime services, and tied to agent state.

Tradeoff: deeper integration gives better control over behavior, but the memory subsystem becomes coupled to the agent framework, tool execution, message manager, prompt/context assembly, review UI, and compatibility surface.

### Hosted/product memory service

Repos:

- `honcho`
- `supermemory`
- partly `mem0`

These optimize for multi-user, multi-session, product-oriented memory. They expose APIs, SDKs, MCP tools, and background processing.

Tradeoff: the API surface is often much easier to study than the actual decision machinery. In `supermemory`, the most important hosted backend logic is not visible in this checkout. In `mem0`, several advanced capabilities mentioned in docs are managed-platform-only in the OSS code.

### Coding-agent local memory

Repos:

- `engram`
- `mempalace`

This optimizes for a local developer workflow: durable local memory, MCP/tools/hooks, project scopes, exact search, vector search, conflict or dedupe handling, and sync/repair hooks.

Tradeoff: local-first design is operationally simple and inspectable, but it does not solve large-scale hosted ranking, multi-tenant APIs, or rich social/user modeling. `engram` is compact and FTS-oriented; `mempalace` is broader, benchmark-heavy, and vector/hybrid retrieval-oriented.

### Verbatim evidence memory

Repo:

- `mempalace`

MemPalace optimizes for preserving original evidence. It stores raw conversation/file text as drawers and treats extracted/indexed layers as navigation aids rather than the authoritative memory.

Tradeoff: this avoids lossy LLM extraction and preserves auditability, but it creates larger corpora and pushes more work into retrieval, context selection, privacy, and deletion.

### Peer/session representation system

Repo:

- `honcho`

Honcho is not just "save facts and search them". It models workspaces, peers, sessions, messages, observations, and representations. It derives observations from conversation events and serves a working representation back to applications.

Tradeoff: richer domain modeling gives more useful cross-session state, but it requires queueing, derivation, reconciliation, and consistency semantics.

### Operator-governed assistant memory

Repo:

- `rainbox`

RainBox optimizes for memory that an operator can inspect, correct, audit, and evaluate. Its distinctive layer is not the vector ranker; it is the loop from memory claims/evidence to retrieval telemetry, feedback, eval cases, and review UI.

Tradeoff: this works well inside a full assistant product, but it is much heavier than a library and less source-preserving than MemPalace's verbatim drawer model.

### Verification-first memory

Repo:

- `verel`

Verel treats memory as a trust problem. It separates confidence, retrieval strength, and verification state; carries rejected values forward; fences recalled memory as untrusted data; and uses promotion gates for induced rules.

Tradeoff: this is more complex than most systems need for an MVP, but it directly addresses failures that simpler memory systems usually ignore.

## 2. Comparative Matrix

| Repo | Memory unit | Storage backend | Retrieval strategy | Write strategy | Update/delete model | Scoping model | Agent integration | Background processing | Trust/provenance model | Notable strengths | Main risks |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `mem0` | Text fact in vector payload | Vector store plus SQLite history/messages | Semantic, optional keyword/BM25, entity boost, optional rerank | LLM additive extraction, hash dedupe, entity linking | Explicit update/delete APIs; V3 default append-oriented | `user_id`, `agent_id`, `run_id`, filters | Python SDK, tool/API style | Extraction and linking on write | Attribution metadata, history; weak epistemic trust | Practical SDK, pluggable stores, hybrid search | LLM facts can become durable claims without verification |
| `langmem` | Store item, usually JSON memory | LangGraph `BaseStore` | `store.search/asearch` delegated to backend | Tools call create/update/delete/search; extraction via Trustcall | Tool-level CRUD | Namespace templates | LangChain/LangGraph tools | Reflection executor local/remote | Mostly application-defined | Clean primitives, schema-driven extraction | Too low-level to solve memory quality alone |
| `honcho` | Message, document/observation, representation | Postgres/SQLAlchemy, pgvector or vector adapter | Working representation blends semantic, recent, most-derived; message search with windows | Message ingestion plus queued derivation | Soft delete, representation reconciliation | Workspace, peer, session, collection | Hosted API/service model | Deriver queues and workers | Source IDs, derived observations, peer/session provenance | Strong event-to-representation pipeline | Operational complexity; LLM-derived observations still need trust policy |
| `engram` | Observation and prompt records | Local SQLite WAL, FTS5 | FTS5, topic-key lookup, context assembly | MCP `mem_save`, conflict candidate flow, dedupe/update rules | Topic-key updates, duplicate counts, soft delete/sync mutation | Project, scope, session, topic key | MCP tools for coding agents | Sync queue, local conflict workflows | Source/session/project metadata, explicit judgment path | Simple durable local design, inspectable code | Lexical retrieval limits; conflict UX depends on agent behavior |
| `mempalace` | Verbatim drawer chunks, closets, KG triples | Local Chroma default; sqlite_exact, Qdrant, pgvector; SQLite KG | Direct drawer vector search, BM25 rerank, closet boost, metadata filters, FTS fallback | Mine files/convos or MCP add drawer; deterministic IDs; chunk/upsert verbatim text | Delete/update drawers, delete by source, dedup, repair; limited epistemic correction | Palace, wing, room, source file, parent drawer, backend namespace | MCP, CLI, hooks, skills, wake-up stack | Mining, closet/hallway/tunnel computation, repair/sync/backup | Strong source provenance; weak candidate/verified/rejected trust state | Evidence-preserving raw baseline, hybrid retrieval, operational hardening | Raw stores get large/noisy; contradiction resolution mostly outside core recall |
| `rainbox` | Claim, evidence, embedding, retrieval event | Postgres/SQLAlchemy plus pgvector | Hard-filtered hybrid (vector + Postgres full-text + entity boost) for both chat and assistant; profile digest | User commands, assistant actions, review UI; single governed atomic path (`record_belief`); write-time conflict detection; active/candidate flows | Reject/supersede/reactivate/expiry/sensitivity; `MemoryRejectedValue` tombstones block model re-assertion of rejected values; governed atomic correction (`correct_belief`); UI stale-write guards | Global, agent, room, project; sensitivity | Full assistant app: chat prompt (via `build_chat_memory_block`→hybrid), action loop, review UI | Embedding sync/prune, telemetry, feedback/eval loop | Five-actor trust model (3 human/override + 2 model/candidate); rejected-value tombstones; write-time lattice-aware conflict detection; governed atomic correction; fenced prompt injection; claim/evidence provenance and retrieval audit | Operator governance, trust/correction machinery (tombstones + conflict detection + fenced recall + governed writes), telemetry, eval integration | Compact claims may lose source nuance; no automatic candidate extraction; `epistemic_confidence`/`retrieval_strength` columns exist but Tier-1 ranking still uses `confidence` (schema groundwork only); attribution is context-injection, not causal |
| `letta` | Core memory block, archival passage, message | ORM database; passages with embeddings; optional git memory | Archival search, conversation search, compiled core prompt | Agent tools mutate core/archival memory | Append/replace/patch, passage insert, block update | Agent, block labels, files/sources | Deep runtime tool executor integration | Prompt rebuilds, manager services | Block tags/metadata, message timestamps; limited truth model | Clear core/archive/recall separation | Agent can rewrite important memory without strong verification |
| `supermemory` | Document, chunk, memory entry, space | Hosted backend; visible schemas/client only | Hosted search/profile API; SDK uses hybrid settings | API/MCP add memory/document | Version chains, relations, forget API | Space, container tags, org/user/project | SDK, AI SDK tools, MCP | Hosted processing not visible | Rich schema fields and relations; implementation not visible | Product/API surface, document-memory graph | Backend black box; semantic forget needs care |
| `verel` | `MemoryRecord` fact/rule/schema/failure/skill | SQLite local plus backend adapters | Rank blends relevance, retrieval strength, confidence, trust; budgeted recall | Candidate extraction, attested/corroborated promotion | Correction chains, rejected tombstones, decay/prune | Scope lattice | Helpers, MCP, hosted/replicated adapters | Consolidation, promotion gate, replication | Explicit candidate/verified/rejected, provenance, confidence | Best correctness model in set | Complex; may be heavy for product MVP |

## 3. End-to-End Memory Lifecycle Comparison

### Capture

`mem0`, `letta`, `langmem`, and `supermemory` expose direct tool/SDK surfaces for adding memory. `rainbox` captures through explicit memory commands, assistant memory actions, and review UI mutations. `engram` captures via MCP tools and can also store prompt/session metadata. `mempalace` captures by mining files/conversations and by MCP drawer writes, preserving verbatim text. `honcho` captures messages as the primary event stream, then derives observations. `verel` captures conversations and percepts but routes them through a trust gate before treating them as verified.

The important split is whether the captured item is itself memory or evidence for memory. Honcho, Verel, MemPalace, and RainBox are closer to evidence-aware designs, but with different answers: Honcho derives representations, Verel promotes trusted claims, MemPalace keeps raw drawers as the primary store, and RainBox stores compact claims with separate evidence rows. Mem0 and Supermemory's public surfaces are closer to "add memory" designs. Engram is in between: `mem_save` stores observations but may return conflict candidates requiring judgment.

### Extraction

`mem0` has the clearest open implementation of LLM extraction: retrieve nearby existing memories, ask the model for additive facts, parse JSON, dedupe, embed, insert, and link entities.

`langmem` delegates extraction to Trustcall and schemas. This is elegant if the application already knows what shape memory should have.

`honcho` formats timestamped session messages and derives representations/observations asynchronously.

`verel` extracts candidate memories but restricts promotion. It is deliberately suspicious of raw extracted claims.

`supermemory` exposes document/chunk/memory schemas, but the extraction engine behind hosted endpoints is not present in this checkout.

`mempalace` mostly avoids extraction for primary memory. It may build closets, entities, halls, and KG triples, but the authoritative memory remains verbatim drawer text.

`rainbox` does not center on automatic extraction in the inspected paths. Explicit user commands and assistant actions create/update claims; evidence rows record whether a claim was user-confirmed, model-inferred, imported, or observed.

### Consolidation

`honcho` and `verel` have the strongest visible consolidation stories. Honcho derives working representations from event streams. Verel clusters failures, induces candidate design rules and schemas, then requires promotion gates for verification.

`mem0` V3 is intentionally more append-oriented; consolidation is mostly dedupe and entity linking in the OSS path. `mempalace` consolidates operationally through dedup, closets, halls, tunnels, graph layers, and repair paths rather than by rewriting memories into summaries. `rainbox` consolidates through claim supersession, rejection, expiry, profile selection, and eval/feedback loops rather than through background summarization. `letta` separates core and archival memory but does not make consolidation the central visible mechanism in the inspected files. `langmem` provides reflection hooks rather than a fixed consolidation policy. `engram` keeps a pragmatic local model: update topic keys, count duplicates, surface conflicts.

### Retrieval

The repeated successful pattern is hybrid retrieval:

- semantic/vector search where embeddings exist;
- lexical/BM25/FTS for exact terms and identifiers;
- metadata filters for scope;
- reranking or rank fusion when quality matters.

`mem0` combines semantic, keyword, entity boost, and optional rerank. `honcho` blends semantic, recent, and most-derived observations. `engram` uses FTS5 and topic keys, favoring local reliability over embedding complexity. `mempalace` uses direct drawer vector search, BM25 rerank, metadata filters, closet boosts, neighbor expansion, and SQLite/FTS fallback. `rainbox` hard-filters by lifecycle/sensitivity/scope, then blends pgvector similarity, Postgres full-text rank, and subject/object entity boosts; both chat (`build_chat_memory_block`) and the assistant's `memory_query` action use this hybrid path. `verel` adds trust and confidence into ranking. `letta` distinguishes archival search from conversation search and core prompt memory. `langmem` and `supermemory` mostly delegate retrieval to backend services through a clean API.

### Context Injection

`letta` has the deepest runtime prompt integration: core memory blocks compile into the agent prompt, and updates trigger prompt rebuilds. `rainbox` injects an operator profile block, curated seed facts, and hybrid memory context into chat/assistant prompts, and records what was injected. `verel` has the safest visible recall renderer: recalled memory is token-budgeted and fenced as untrusted data. `mempalace` has a four-layer stack: identity, essential story, on-demand recall, and deep search. `supermemory` has a product-style profile/context endpoint that emits static/dynamic profile text and search results. `engram` has MCP context tools. `honcho` exposes working representations. `mem0` returns search results and leaves context assembly mostly to the application.

### Correction

This is where systems diverge sharply.

`verel` and `rainbox` now have the strongest visible correction semantics in this set. `verel` has explicit trust states, rejected tombstones, and verified/rejected effects on recall. `rainbox` has governed atomic correction (`correct_belief`): old claim superseded and tombstoned in one transaction, replacement keys derived from new text via `record_belief`, conflict-refused if the replacement would create a second active rival. Rejected values are tombstoned via `MemoryRejectedValue`, preventing silent re-entry via model writes; the `/memory` UI surfaces conflict candidates (4 resolution options) and tombstone hits. `engram` has conflict candidates and judgment tools. `mempalace` has storage correction tools: delete/update drawer, delete by source, dedup, repair, KG invalidation, and conservative fact-check primitives; it does not yet have Verel-style epistemic states for every memory. `letta` supports exact replace and patch-style core memory edits. `mem0` has explicit update/delete APIs, but the default additive path avoids rewriting existing memories. `honcho` has representation reconciliation concepts. `supermemory` schemas include versions and relations, but the producer logic is not visible. `langmem` exposes update/delete tools and leaves correctness to the application.

### Forgetting

Visible deletion varies from hard API deletion to lifecycle state:

- `mem0`: delete APIs and expiration metadata.
- `langmem`: delete tool operation.
- `honcho`: soft-delete style document handling.
- `engram`: deleted timestamps/sync mutation semantics.
- `mempalace`: delete drawer, delete by source, dedup, repair, backend delete; deletion must account for drawers, closets, KG, backups, sync, and remote backends.
- `rainbox`: reject claim (tombstones the value in `MemoryRejectedValue`), supersede claim (also tombstones), expire claim, prune embeddings; rejected/superseded evidence remains inspectable; tombstoned values block future model re-assertion (anti-laundering).
- `letta`: block/file/passage update paths, archival insert/search visible; deletion depends on manager APIs outside the key path.
- `supermemory`: forget API in MCP/client; semantic fallback delete is powerful but risky.
- `verel`: rejected tombstones, TTL/volatile/stale pruning, and protection for verified/rejected/pinned records.

Semantic forgetting is an antipattern unless there is explicit user review or exact ID targeting.

### Cross-Session and Cross-Agent Persistence

`honcho` has the richest multi-actor model: workspace, peer, session, collections, and derived representations. `supermemory` has org/user/space/container-tag schemas. `mem0` has simple `user_id`, `agent_id`, and `run_id` scopes. `rainbox` uses global/agent/room/project scopes plus sensitivity labels. `engram` uses project/session/scope/topic key, which is appropriate for coding agents. `mempalace` uses palace/wing/room/source/parent-drawer scopes and backend namespaces. `verel` uses a scope lattice. `letta` binds memory to agents and blocks. `langmem` uses namespace templates and inherits whatever sharing semantics the store implements.

## 4. Implementation Hotspots by Repo

### Memory Schema

- `mem0`: `mem0/mem0/configs/base.py`, payload construction in `mem0/mem0/memory/main.py`.
- `langmem`: store item shape is application-defined; see `langmem/src/langmem/knowledge/tools.py` and schema extraction in `langmem/src/langmem/knowledge/extraction.py`.
- `honcho`: `honcho/src/models.py`.
- `engram`: SQLite schema in `engram/internal/store/store.go`.
- `mempalace`: drawer metadata in `mempalace/mempalace/miner.py` and `mcp_server.py`; backend contract in `mempalace/mempalace/backends/base.py`; KG schema in `mempalace/mempalace/knowledge_graph.py`.
- `rainbox`: `MemoryClaim`, `MemoryEvidence`, `MemoryEmbedding`, `RetrievalEvent` in `rainbox/source/db/models.py`.
- `letta`: `letta/letta/schemas/memory.py`, `letta/letta/orm/block.py`, `letta/letta/orm/passage.py`.
- `supermemory`: `supermemory/packages/validation/schemas.ts`, `supermemory/packages/validation/api.ts`.
- `verel`: `verel/src/verel/memory/view.py`.

### Add/Write Path

- `mem0`: `Memory.add()` and `_add_to_vector_store()` in `mem0/mem0/memory/main.py`.
- `langmem`: `create_manage_memory_tool()` in `langmem/src/langmem/knowledge/tools.py`; extraction in `MemoryManager`.
- `honcho`: `honcho/src/crud/message.py`, `honcho/src/deriver/deriver.py`, `honcho/src/crud/representation.py`.
- `engram`: `AddObservation()` in `engram/internal/store/store.go`; MCP `handleSave()` in `engram/internal/mcp/mcp.go`.
- `mempalace`: `process_file()` and `mine()` in `mempalace/mempalace/miner.py`; `tool_add_drawer()` in `mempalace/mempalace/mcp_server.py`; collection access in `mempalace/mempalace/palace.py`.
- `rainbox`: explicit commands in `rainbox/source/memory/ops.py`; assistant actions in `rainbox/source/agents/assistant.py`; review UI actions in `rainbox/source/webapp/memory_api.py`; DB helpers in `rainbox/source/db/memory.py`.
- `letta`: `letta/letta/services/tool_executor/core_tool_executor.py`; `letta/letta/services/block_manager.py`; `letta/letta/services/passage_manager.py`.
- `supermemory`: `supermemory/packages/ai-sdk/src/tools.ts`, `supermemory/apps/mcp/src/server.ts`, `supermemory/apps/mcp/src/client.ts`.
- `verel`: `verel/src/verel/memory/local.py`, `verel/src/verel/memory/remember.py`.

### Search/Retrieve Path

- `mem0`: `Memory.search()` and `_search_vector_store()`; scoring in `mem0/mem0/utils/scoring.py`.
- `langmem`: `create_search_memory_tool()` delegates to `BaseStore.search/asearch`.
- `honcho`: `honcho/src/crud/representation.py`, `honcho/src/crud/document.py`, `honcho/src/dialectic/`.
- `engram`: `Search()` and context helpers in `engram/internal/store/store.go`; MCP search/context handlers.
- `mempalace`: `search_memories()`, `_hybrid_rank()`, `_bm25_only_via_sqlite()` in `mempalace/mempalace/searcher.py`.
- `rainbox`: `retrieve_memories_hybrid()`, `hard_filtered_claims()`, `build_chat_memory_block()` in `rainbox/source/memory/retrieval.py`; profile retrieval in `rainbox/source/user_profile/retrieval.py`.
- `letta`: `archival_memory_search()`, `conversation_search()`, `message_manager.search_messages_async`.
- `supermemory`: `client.search.execute`, `client.search.memories`, `/v4/profile` context helper.
- `verel`: `recall()` in `local.py`, `recall_budgeted()` in `recall.py`, rank logic in `view.py`.

### Context Assembly

- `mem0`: mostly application-owned after search.
- `langmem`: application-owned; tools return store/search results.
- `honcho`: working representation in `honcho/src/crud/representation.py`.
- `engram`: MCP context/session summary in `engram/internal/mcp/mcp.go`.
- `mempalace`: four-layer stack in `mempalace/mempalace/layers.py`; MCP search/status/list tools in `mempalace/mempalace/mcp_server.py`.
- `rainbox`: `rainbox/source/agents/chat_context.py`, `rainbox/source/memory/retrieval.py`, `rainbox/source/user_profile/retrieval.py`.
- `letta`: `Memory.compile()` in `letta/letta/schemas/memory.py`.
- `supermemory`: `supermemory/packages/tools/src/shared/context.ts`.
- `verel`: `verel/src/verel/memory/recall.py`.

### Background Workers

- `mem0`: no central open worker in the inspected OSS core; extraction happens in write path.
- `langmem`: `langmem/src/langmem/reflection.py`.
- `honcho`: `honcho/src/deriver/`, `honcho/src/reconciler/`, queue models.
- `engram`: sync queue in `engram/internal/sync/` and store mutation queue fields.
- `mempalace`: mining/convo/format miners, hallway/tunnel computation, daemon jobs, repair/sync/backups.
- `rainbox`: embedding sync/prune in `rainbox/source/memory/embeddings.py`; feedback/eval loop in `rainbox/source/db/feedback.py` and `rainbox/source/evals/`.
- `letta`: manager services and prompt rebuilds; not primarily worker-centric in inspected paths.
- `supermemory`: hosted processing not visible; graph UI and MCP/client visible.
- `verel`: consolidation, promotion, replication modules.

### MCP/API/SDK Surfaces

- `mem0`: Python SDK and service/API paths.
- `langmem`: LangChain/LangGraph tools.
- `honcho`: service endpoints and SDK-facing models.
- `engram`: `engram/internal/mcp/mcp.go`.
- `mempalace`: `mempalace/mempalace/mcp_server.py`, CLI modules, hooks under `mempalace/hooks/`, skills/commands.
- `rainbox`: web API/UI in `rainbox/source/webapp/memory_api.py` and `memory_views.py`; assistant capabilities in `rainbox/source/agents/assistant.py`.
- `letta`: tool definitions in `letta/letta/functions/function_sets/base.py`, runtime in core tool executor.
- `supermemory`: `supermemory/apps/mcp/src/server.ts`, `supermemory/packages/ai-sdk/src/tools.ts`.
- `verel`: `verel/src/verel/mcp_server.py`, hosted/replicated adapters.

### Evals/Tests

- `mem0`: tests are present but the report focused on core implementation.
- `langmem`: tests/examples around tools and extraction should be consulted before reuse.
- `honcho`: rich tests under `honcho/tests`.
- `engram`: Go package tests and MCP flows should be inspected for command behavior.
- `mempalace`: broad tests under `mempalace/tests`; benchmarks under `mempalace/benchmarks`.
- `rainbox`: memory/retrieval/assistant/UI tests under `rainbox/source/memory`, `rainbox/source/db`, `rainbox/source/agents`, and `rainbox/source/webapp`.
- `letta`: `letta/tests/test_memory.py`, manager tests, passage/message/block tests.
- `supermemory`: visible integration/e2e wrappers and memory graph tests; backend tests not present.
- `verel`: strong memory-focused tests under `verel/tests/test_memory*.py`, plus consolidation, promotion, lattice, replicated, hosted, MCP tests.

## 5. Design Patterns That Recur

### Tool-mediated memory writes

Repos: all nine, in different forms.

The agent or application explicitly calls a memory operation. This works because it gives the system a narrow interface for durable state changes. It fails when the model forgets to call the tool, calls it with low-quality facts, or treats tool descriptions as policy enforcement.

### Separate hot memory from archival memory

Repos: `letta`, `rainbox`, `honcho`, `supermemory`, `mempalace`, partly `mem0`.

Hot memory is small and prompt-ready. Archival/document memory is large and retrieved on demand. This works because prompt space is scarce and long-term stores are noisy. It fails when there is no promotion/demotion policy between the layers.

### Evidence first, derived memory second

Repos: strongest in `honcho`, `verel`, `mempalace`, and `rainbox`; partly in `engram`.

Raw messages, observations, files, drawers, or evidence rows are retained, and derived facts/representations/indexes are computed from them. This works because wrong memories can be audited and recomputed. It fails if the derived layer does not preserve source IDs, if raw stores become too noisy, if evidence excerpts are too thin, or if background derivation makes read consistency surprising.

### Hybrid retrieval

Repos: `mem0`, `honcho`, `engram`, `mempalace`, `rainbox`, `verel`, `supermemory` API settings, `letta` across separate search modes.

Vector search alone is not enough. Identifiers, names, exact phrases, dates, file paths, and project keys often need lexical search. Hybrid retrieval works because it handles both fuzzy semantic recall and exact lookup. MemPalace adds a useful variant: extracted/indexed "closets" boost drawer ranking but never gate direct evidence retrieval. Hybrid retrieval fails when rank fusion is opaque or not evaluated.

### Scope as a first-class key

Repos: all nine.

Good systems make memory boundaries explicit: user, agent, run, project, workspace, peer, session, space, palace, wing, room, source file, claim scope, sensitivity, scope lattice, namespace. This works because many memory bugs are scope bugs. It fails when scopes are just metadata filters with no migration, inheritance, or conflict policy.

### MCP as a universal adapter

Repos: `engram`, `mempalace`, `supermemory`, `verel`, and conceptually similar tool surfaces elsewhere.

MCP is useful because it lets different coding agents and desktop tools use the same memory backend. It fails if the MCP tool descriptions become the only guardrail against bad writes.

### Local SQLite for inspectable memory

Repos: `engram`, `mempalace`, `verel`; SQLite also supports history/messages in `mem0`.

SQLite works well for local agent memory: durable, fast, easy to inspect, transaction-friendly, and good enough with FTS5. MemPalace also shows the complementary local pattern: SQLite metadata/KG/FTS plus a local vector store. It fails if a product needs multi-tenant scale, remote sharing, or vector-heavy retrieval without extensions/adapters.

### Profiles and working representations

Repos: `honcho`, `supermemory`, `letta`.

A low-latency synthesized representation is often more useful than raw top-k memories. This works because agents need compact operating context. It fails when summaries drift, hide uncertainty, or cannot be traced back to evidence.

### Memory governance loop

Repos: strongest in `rainbox`; partly in `verel`.

Memory quality improves when memory use is observable and connected to review, feedback, and evals. RainBox's `RetrievalEvent`, `FeedbackEvent`, `/memory` review page, and eval loop show a practical product pattern. This fails if telemetry is mistaken for truth: a downvote is a review signal, not proof that a memory is false.

## 6. Antipatterns and Failure Modes

### Treating LLM-extracted facts as truth

Most systems extract with an LLM. Without trust state, provenance, and correction semantics, hallucinations become durable. `verel` addresses this directly; `honcho` preserves source events; `mem0` and `langmem` need application-level guardrails.

`mempalace` is the clearest counterexample in this workspace: it makes verbatim evidence the primary store and treats derived structures as indexes. That does not solve truth, but it avoids losing the original context during extraction.

### Vector-only memory

Vector search misses exact constraints and can retrieve plausible but wrong memories. Every serious design should include lexical search or structured filters. `engram` demonstrates the value of boring FTS. `mempalace` demonstrates vector plus BM25 plus metadata plus fallback paths. `mem0` and `honcho` show hybrid approaches.

### Weak correction semantics

Update/delete APIs are not enough. A system needs to model contradiction, supersession, source, timestamp, and rejected values. Otherwise a wrong fact can be reintroduced by later extraction. Verel's rejected tombstones are the clearest research-grade countermeasure; RainBox has adopted equivalent machinery in a product context: `MemoryRejectedValue` tombstones block future model re-assertion of rejected or superseded values, `correct_belief` is an atomic governed correction path, and write-time conflict detection is lattice-aware across the scope hierarchy.

### Semantic deletion

Deleting by "similar memory" is dangerous. It is useful as a discovery aid, but the actual forget operation should target exact IDs or require review. Supermemory's MCP client fallback semantic deletion is a risk pattern to treat carefully.

### Core memory as a junk drawer

Editable prompt memory is powerful and dangerous. Letta's core memory tools are useful, but any system with long-lived core blocks needs provenance, review, and compaction policy. Otherwise it accumulates stale identity and preference claims.

### Tool descriptions as policy

Several systems rely on tool docs telling the agent when to save memory. This is necessary but insufficient. The backend still needs dedupe, conflict detection, trust gates, and review.

### Telemetry mistaken for truth

RainBox explicitly avoids this: retrieval events and downvotes are signals for inspection/evals, not automatic confidence changes or deletion. This matters because "memory was used in a bad answer" does not prove the memory was false.

### Platform-only claims hidden behind OSS APIs

Mem0 and Supermemory both have product surfaces where advanced behavior may live outside the inspected source. For build decisions, separate what is visible in code from what is promised by hosted APIs.

### Throwing away raw evidence too early

Extraction-first systems can look elegant while deleting the only material needed to debug a wrong memory. MemPalace is the strongest evidence that raw text plus retrieval deserves to be the baseline before adding lossy summarization or fact extraction.

## 7. What Seems to Work

SQLite plus FTS works for local coding-agent memory. It gives inspectable state, transactional writes, simple backup/sync, and exact search. Engram and Verel are good references. MemPalace shows how to combine local SQLite-style operational machinery with a vector backend and fallback BM25/FTS paths.

Hybrid retrieval is the default serious choice. Pair semantic search with lexical matching and metadata filters. Add reranking only after basic retrieval metrics exist. MemPalace's "closets boost but never gate drawers" rule is a particularly reusable retrieval principle.

Scope must be part of the primary design, not a later filter. User/agent/project/session/workspace boundaries determine whether recall is useful or harmful.

Keep raw evidence. Messages, source IDs, documents, drawers, and provenance make correction possible. Honcho, Verel, and MemPalace benefit from this; systems that only store extracted facts lose auditability.

Separate truth from usefulness. Retrieval strength should not mean the memory is true. Verel's split between `epistemic_confidence` and `retrieval_strength` is one of the strongest ideas in the workspace.

Render recalled memory defensively. Verel's untrusted-memory fence is a practical prompt-injection mitigation. Context should be quoted as data, not instructions.

Use small, explicit mutation APIs. Letta's append/replace/patch operations are easier to reason about than free-form "update my memory" text.

Record embedder identity. MemPalace's explicit model/dimension checks are a useful operational guardrail: a vector index searched with the wrong embedding model can silently degrade.

Make memory use inspectable. RainBox's debug rows, retrieval events, and review UI are the best reference here. Users need to know which memories entered a prompt and need a way to correct or reject them.

## 8. What I Would Build

### Ship First

Build a local-first core even if a hosted version is planned later.

Data model:

- `event`: raw messages, tool calls, documents, user assertions, timestamps, actor IDs.
- `evidence_chunk`: verbatim text chunk with source path/session, line/span, authored/filed time, deterministic ID, embedding ID, and scope.
- `memory`: extracted or manually saved claim with `kind`, `subject`, `predicate`, `text`, `scope`, `status`, `confidence`, `retrieval_strength`, `source_event_ids`, `created_at`, `updated_at`.
- `memory_evidence`: append-only provenance rows, not a mutable field on `memory`.
- `memory_relation`: `supersedes`, `contradicts`, `supports`, `derived_from`, `same_as`.
- `rejected_value`: tombstone for values that should not be silently reintroduced.
- `embedding`: optional vector table or external vector ID.
- `retrieval_event`: append-only events for retrieved, used/injected, rejected, downvoted, considered.

Status should start simple:

- `candidate`
- `verified`
- `rejected`
- `stale`

Write path:

1. Store raw evidence first.
2. Chunk deterministically and record embedder identity.
3. Index raw evidence with lexical and vector paths.
4. Extract candidate facts with schema-constrained LLM output only after evidence is durable.
5. Search for same subject/predicate and near duplicates.
6. If same key plus same value, corroborate.
7. If same key plus different value, create a conflict or supersession.
8. Do not auto-promote to verified unless the source is trusted or corroborated.

Retrieval path:

1. Apply hard scope filters.
2. Run lexical search and vector search.
3. Retrieve raw evidence directly as the floor.
4. Let derived indexes/summaries/entities boost rank, not gate evidence.
5. Blend with recency, confidence, retrieval strength, and trust status.
6. Suppress rejected records from normal recall but use rejected tombstones during write conflict checks.
7. Return compact, source-linked results.

Context assembly:

- Token-budgeted.
- Verified first, then high-confidence candidates if needed.
- Group by subject or task.
- Fence as recalled data, not instructions.
- Include source or confidence markers when possible.
- Record which memories entered context.

Agent integration:

- MCP tools for `remember`, `recall`, `judge`, `forget`, and `context`.
- SDK methods with the same semantics.
- Tool calls should be small and boring; policy belongs in the backend.
- Review UI or API for activate/reject/correct/sensitivity/expiry.
- Confirm-tier write intents for high-impact assistant-proposed memory changes.

Testing:

- Extraction golden tests.
- Conflict/supersession tests.
- Retrieval recall/precision fixtures.
- Prompt-injection tests for recalled content.
- Deletion/privacy tests.
- Scope leakage tests.
- Telemetry and feedback-to-eval tests.
- Regression corpus of wrong memories that must not reappear.

### Add Later

- Background consolidation from failures into candidate rules.
- Promotion gates using held-out task suites.
- Entity graph linking.
- Closet-style source indexes and neighbor expansion.
- Hosted multi-tenant API.
- Cross-device sync.
- UI for memory review and conflict resolution.
- Retrieval telemetry dashboards and feedback/eval promotion.
- Temporal reasoning and decay.

Do not add background summarization before raw-evidence retrieval and correction semantics exist. Summaries are compressed belief; if the system cannot explain and repair a belief, summarization hides the problem.

## 9. Repo-by-Repo Verdicts

### `mem0`

- Best idea: pragmatic additive extraction plus hybrid retrieval/entity boost.
- Biggest risk: extracted facts are not strongly modeled as uncertain claims.
- Most reusable component: `Memory.add()` / `_add_to_vector_store()` pipeline.
- Maturity impression: practical SDK core, with some advanced features outside OSS.
- Study when: building a drop-in memory library.
- Do not copy when: you need rigorous trust/correction semantics.

### `langmem`

- Best idea: memory as LangGraph store tools with schema-driven extraction.
- Biggest risk: it is a primitive layer, not a full memory policy.
- Most reusable component: `create_manage_memory_tool()` and namespace templates.
- Maturity impression: clean and framework-native.
- Study when: already building on LangGraph.
- Do not copy when: you need a standalone memory service with built-in quality controls.

### `honcho`

- Best idea: event stream to derived working representation.
- Biggest risk: operational complexity and background consistency.
- Most reusable component: message ingestion plus deriver/representation flow.
- Maturity impression: serious service architecture with meaningful tests.
- Study when: modeling users/peers/sessions over time.
- Do not copy when: all you need is a local memory file.

### `engram`

- Best idea: local SQLite/FTS MCP memory with conflict-oriented writes.
- Biggest risk: lexical retrieval and agent-mediated judgment may hit limits.
- Most reusable component: `AddObservation()` and MCP `handleSave()`.
- Maturity impression: compact, inspectable, purpose-built for coding agents.
- Study when: building local developer-agent memory.
- Do not copy when: you need hosted multi-tenant vector retrieval.

### `mempalace`

- Best idea: verbatim drawers as the authoritative memory, with hybrid retrieval and extracted indexes as boosts.
- Biggest risk: raw stores get large/noisy and do not resolve contradictions by themselves.
- Most reusable component: `search_memories()` plus `_hybrid_rank()`, and the mining/write path around deterministic IDs.
- Maturity impression: operationally mature local system with broad tests, integrations, repair tooling, and benchmark artifacts.
- Study when: building local-first coding-agent memory or testing whether extraction is actually needed.
- Do not copy when: you need compact verified user facts as the primary memory surface.

### `rainbox`

- Best idea: claim/evidence memory tied to governed writes (single `record_belief` path, five-actor trust model, tombstones, conflict detection), review UI, retrieval telemetry, feedback, and eval gates.
- Biggest risk: active compact claims can steer behavior while losing nuance from original source context; no automatic candidate extraction means claims enter only through explicit writes.
- Most reusable component: `MemoryClaim`/`MemoryEvidence`/`MemoryRejectedValue`/`RetrievalEvent` model, `record_belief`/`correct_belief` governed write paths, `retrieve_memories_hybrid()`.
- Maturity impression: strong app-integrated memory subsystem with trust/correction machinery comparable to Verel's correctness properties, broad tests, and operator workflows.
- Study when: building an assistant product where memory must be inspectable, governable, and protected against model-write laundering.
- Do not copy when: you need a small embeddable library, raw transcript recall as the primary memory layer, or `epistemic_confidence`/`retrieval_strength` driving ranking (these columns are schema groundwork only; Tier-1 ranking still uses `confidence`).

### `letta`

- Best idea: core vs archival vs conversation memory inside the runtime.
- Biggest risk: agent-editable core memory without a strong truth model.
- Most reusable component: memory block compile/mutation and patch-style edits.
- Maturity impression: deep runtime integration with compatibility complexity.
- Study when: building an agent platform, not just a memory backend.
- Do not copy when: you want a small independent memory service.

### `supermemory`

- Best idea: product-grade API shape around documents, chunks, memory entries, spaces, profiles, SDKs, and MCP.
- Biggest risk: the hosted backend core is not visible here.
- Most reusable component: schemas and adapter surfaces.
- Maturity impression: polished integration surface; implementation evidence incomplete.
- Study when: designing public APIs and memory UX.
- Do not copy when: you need open implementation details for extraction/ranking.

### `verel`

- Best idea: explicit trust, confidence, retrieval strength, rejected tombstones, and defensive recall.
- Biggest risk: complexity.
- Most reusable component: `MemoryRecord`, `LocalMemory.write()`, and `recall_budgeted()`.
- Maturity impression: research-grade correctness focus with strong targeted tests.
- Study when: wrong memory is costly.
- Do not copy wholesale when: you need a fast MVP.

## 10. Practical Checklist for Your Own System

Schema and scoping:

- Define the memory unit before choosing vector storage.
- Store raw evidence separately from derived memory.
- Give raw evidence stable IDs and source/span metadata.
- Make scope mandatory: user, agent, project/session, and sharing boundary.
- Include provenance/source IDs on every derived memory.
- Store provenance/evidence as append-only rows when a claim can have multiple origins.
- Represent status/trust explicitly.

Write path:

- Store evidence first.
- Record embedder identity and index version.
- Extract structured candidates.
- Dedupe by exact hash and semantic similarity.
- Detect same subject/predicate conflicts.
- Preserve correction chains.
- Keep rejected tombstones.
- Use stale-write guards for review UI mutations.

Retrieval:

- Use lexical plus vector retrieval.
- Filter by scope before ranking.
- Let summaries/entities/indexes boost raw evidence, not hide it.
- Rank with relevance, recency, confidence, trust, and retrieval strength.
- Evaluate retrieval on realistic tasks.

Context assembly:

- Budget tokens.
- Prefer verified memories.
- Mark uncertainty.
- Fence recalled memory as data.
- Include enough source metadata for debugging.

Trust/provenance:

- Do not let model extraction imply truth.
- Separate "often retrieved" from "known true".
- Require attestation or corroboration for important claims.
- Track who said what and when.
- Treat feedback/downvotes as review signals, not automatic truth updates.

Agent UX:

- Provide small MCP/SDK tools.
- Make `remember`, `recall`, `judge`, and `forget` distinct.
- Return conflicts for review instead of silently overwriting.
- Avoid broad semantic deletion without ID confirmation.
- Expose "which memories did you use?" as a first-class audit command.

Testing/evals:

- Golden extraction cases.
- Contradiction and supersession cases.
- Scope leakage cases.
- Prompt-injection recall cases.
- Delete/forget compliance cases.
- Long-running compaction/summarization regression cases.

Operations:

- Keep local state inspectable during early development.
- Add background workers only after synchronous semantics are clear.
- Log memory mutations as audit events.
- Version schemas.
- Provide repair/reindex paths for vector-store corruption or embedding-model swaps.
- Keep retrieval events append-only; derive counters from events.

Privacy/deletion:

- Design deletion before shipping.
- Know whether delete means hide, tombstone, hard delete, or forget from embeddings.
- Propagate deletion to raw chunks, derived memories, summaries/indexes, graph facts, backups, sync, and remote backends.

## 11. Appendix

### Individual Reports

- `reports/repos/mem0.md`
- `reports/repos/langmem.md`
- `reports/repos/honcho.md`
- `reports/repos/engram.md`
- `reports/repos/mempalace.md`
- `reports/repos/rainbox.md`
- `reports/repos/letta.md`
- `reports/repos/supermemory.md`
- `reports/repos/verel.md`

### Repos Inspected

- `mem0`
- `langmem`
- `honcho`
- `engram`
- `mempalace`
- `rainbox`
- `letta`
- `supermemory`
- `verel`

### Commands Used

Representative local inspection commands:

- `find . -maxdepth ... -type d`
- `rg --files`
- `rg -n "memory|recall|remember|search|embedding|vector|MCP|Block|Passage|Representation|drawer|palace|wing|room|claim|evidence|retrieval_event"`
- `sed -n ...`
- `wc -l`

No internet sources were used for this report. The analysis is based on the checked-out code in this workspace.

### Known Limitations

- Supermemory's hosted backend implementation was not visible in this checkout; its report emphasizes schemas, clients, SDKs, MCP, and graph UI.
- Some mem0 advanced capabilities appear to be managed-platform-only in the inspected OSS code.
- This is an implementation-oriented static review, not a runtime benchmark.
- Retrieval quality and extraction quality were not independently re-measured; committed benchmark artifacts were inspected for MemPalace but not rerun.
- RainBox was reviewed as an application-integrated memory subsystem; unrelated assistant/product features were not exhaustively analyzed.
- The reports prioritize memory-management code paths over unrelated framework/application code.
