# Rainbox improvements — informed by Hermes Agent (2026-06-19)

**Context:** Comparison of rainbox against [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research's self-improving personal agent), with security design pressure drawn from the
[OpenClaw CIK safety analysis](https://arxiv.org/abs/2604.04759). Goal is **not** for rainbox
to become either of them — the goal is a personal assistant whose implementation the operator
fully understands and owns. This document collects insights from multiple assistants so we
can refine on the combined view.

---

## Claude

### TL;DR

Rainbox and Hermes aim at the same target — a self-hosted persistent personal assistant —
but made opposite bets. **Hermes is a single long-lived conversational agent loop** that
learns by writing skills and lives behind a multi-platform gateway. **Rainbox is a durable
multi-agent supervisor** built on Postgres + POSIX, where each agent is a single-shot,
schema-validated child process.

The honest summary: **rainbox's infrastructure is ahead; its cognition layer is behind.**
Hermes feels like an assistant because it *loops and learns*. Rainbox feels like a
well-engineered agent runtime. The highest-value work is at the cognition layer, not the
infrastructure layer.

### What Hermes is

Nous Research's open-source self-improving agent (Python core, TS frontends). Distinctive
pieces:

- **Self-improving skills** — when it solves a hard task it writes a markdown *skill file*
  and reuses it later (procedural memory, `agentskills.io` standard).
- **Multi-level memory** — `MEMORY.md`/`USER.md` files, FTS5 cross-session search + LLM
  summarization, Honcho-based user modeling.
- **One conversational agent loop** with tools, interrupt-and-redirect mid-task,
  `/compress` for context compression.
- **Unified gateway** to 20+ platforms (Telegram, Discord, Slack, Signal, email…).
- **6 execution backends** (local, Docker, SSH, Singularity, Modal, Daytona) + MCP +
  multi-provider LLM (Nous Portal, OpenRouter, OpenAI, Anthropic, …).
- **Tool-via-RPC** — the agent writes Python that calls tools directly, collapsing
  multi-step pipelines into one turn.

### Side-by-side

| Dimension | Rainbox | Hermes | Verdict |
|---|---|---|---|
| **Concurrency model** | Durable Postgres queue + POSIX child processes, SIGKILL watchdog, full journal | In-process loop + gateway process | **Rainbox wins** — crash-recoverable, observable |
| **Agent shape** | Many single-shot, schema-validated agents; linear routing | One iterative ReAct loop with 60+ tools | **Hermes wins** — true multi-step "do it for me" |
| **Memory (facts)** | Provenance-first claims+evidence, lifecycle, confirm/correct | MEMORY.md + user model | **Rainbox's model is better designed**, retrieval is weaker |
| **Memory retrieval** | Deterministic token-overlap (facts); pgvector (Q&A only) | FTS5 + LLM summarization | **Hermes wins** on recall quality |
| **Procedural memory (skills)** | None | Self-written, self-improving skill files | **Hermes wins** — biggest gap |
| **Observability** | Flask-Admin, diagnostic rows, eval loop, benchmarks | Logs | **Rainbox wins clearly** |
| **Gateways** | Telegram bridge | 20+ | Hermes broader; Telegram likely enough |
| **Provider flexibility** | LM Studio / Jan / Ollama (local-first) | 8+ incl. cloud | Tie — different goals |
| **Steerability** | None mid-task (single-shot) | Interrupt-and-redirect, `/compress` | Hermes wins |

### Proposed roadmap

> **Superseded by the [Combined recommendation](#combined-recommendation) below.** This
> tiered list is kept as the original analysis; the Combined section is the single
> authoritative plan (it folds these tiers and Codex's refinements into one sequence with
> sizing). Read this for the *why*, build from the Combined roadmap.

Filter: makes rainbox a better **personal assistant**, fits its **durable / Postgres /
local-first** architecture, and stays **simple enough to fully understand**. Deliberately
*not* chasing Hermes' breadth (20 gateways, 6 backends).

#### Tier 1 — highest leverage

**1. A first-class iterative "assistant" agent (real ReAct loop).**
The gap that most separates "agent runtime" from "personal assistant." The pieces already
exist — `FunctionAgent` (LlamaIndex), `workspace_shell`, `query`, memory commands,
cron/kanban APIs. Promote one agent that loops: plan → call tool → observe → repeat, with
existing tools as its toolset. Preserve durability: each *turn* still journals to Postgres,
so a crash mid-loop is recoverable (something Hermes cannot do).
*Fits because:* it's one agent built from primitives already owned, not a framework.

**2. Procedural skills (self-improving).** *The single best idea to steal.*
Declarative memory (facts) already exists; add procedural memory: markdown skill files the
assistant writes after solving something, retrieved into the prompt when relevant. Store in
`<customize.dir>/skills/` (mirrors the existing customize-overlay pattern) or a `skill`
table reusing the claim+evidence provenance model. Be `agentskills.io`-compatible for
portability.
*Fits because:* it's just files + existing retrieval, fully inspectable. This is what makes
Hermes "grow."

**3. Upgrade memory retrieval to semantic + add a user model.**
The fact *schema* is already better than Hermes'; token-overlap retrieval is the weak link.
pgvector already runs for Q&A — embed memory claims the same way. Add a lightweight
`USER.md`-equivalent (a curated subject-profile claim set) injected into every assistant
prompt. Skip Honcho's dialectic complexity; a summarized profile is enough.

#### Tier 2 — strong, after Tier 1

**4. Tool-via-code execution.** Let the assistant write a short Python script that calls
rainbox's own functions and runs in the sandboxed workspace, instead of many LLM
round-trips. Strongly aligned with "developer who wants to know what's going on"; the
`workspace_shell` sandbox is already most of the security story.

**5. Interrupt-and-redirect + context compression.** Once the looping agent exists, add a
way to inject a new message mid-loop (SSE + LISTEN/NOTIFY already present) and a `/compress`
summarizer for long rooms.

**6. Conditional routing / DAG in the supervisor.** Already on the roadmap (Path B);
prerequisite to replace the placeholder dreamer/critic/verifier with a real feedback loop.

#### Tier 3 — only if actually wanted

- **A second gateway** (Signal or email) — only if Telegram isn't covering mobile use.
- **Docker sandbox** for `workspace_shell` — a security upgrade, not a feature.
- **Retry/backoff for `failed` journal rows** — small robustness fix (known gap).

### What to deliberately NOT copy

- **6 execution backends / serverless hibernation** — irrelevant for local-first personal
  use; pure complexity.
- **20-platform gateway** — maintenance burden with no payoff for one user.
- **Hermes' file-based fact memory (`MEMORY.md`)** — the Postgres claim/evidence model is
  genuinely better; don't regress to flat files. Use files only for *skills*, where
  portability matters.

### Recommendation

Do **#1 (assistant loop)** and **#2 (skills)** first — together they convert rainbox from
"agent runtime" into "assistant that does multi-step work and gets better over time,"
exactly the stated goal, without losing durability or operator understanding. Start with the
assistant loop; skills plug into it naturally.

### Sources

- [Hermes Agent GitHub](https://github.com/NousResearch/hermes-agent)
- [Hermes Agent docs](https://hermes-agent.nousresearch.com/docs/)
- [Analytics Vidhya guide](https://www.analyticsvidhya.com/blog/2026/05/hermes-agent-guide/)
- [Medium overview](https://medium.com/@tentenco/hermes-agent-desktop-app-everything-you-need-to-know-about-nous-researchs-self-improving-ai-agent-3cb59bd31e5f)

---

## Codex refinements

Codex's useful additions are implementation constraints that keep the Hermes-inspired
pieces rainbox-native:

- **Durable traces.** Assistant runs should record the user request, model plan, chosen
  action, normalized arguments, observation/result, and final/blocked state. Start in
  `journal.result` or a `debug-assistant` chat row; split into `assistant_run` /
  `assistant_step` tables only when the trace becomes hard to query, link, or resume.
- **Primitive action registry early.** Phase 1 needs a code-enforced allowed-action enum
  (`reply`, `query_memory`, `workspace_read_command`, etc.). That is the seed of the
  capability registry. The full control plane - UI badges, write/network/secret metadata,
  approvals, MCP policy, and `rainbox doctor` - can wait until the assistant has useful
  capabilities to control.
- **Reviewed procedural skills.** Human-authored skills can be active immediately.
  Model-proposed skills should start as candidates with source run/journal metadata, then be
  activated, edited, rejected, or superseded by the operator.
- **Shared retrieval path.** Skills can start with lexical retrieval, then move onto the
  same semantic retrieval work as factual memory. Do not build separate skill and memory
  retrievers unless their behavior truly diverges.
- **Decision by cost and reuse.** Each phase should state what it reuses, what is net-new,
  and rough size. For a solo developer, this matters as much as conceptual priority.

---

## Combined recommendation

### North star

Rainbox becomes a **durable local personal assistant**: it does multi-step work (not just
one-shot replies), learns reusable procedures in an inspectable form, remembers facts with
provenance, acts through bounded actions, and leaves an inspectable trace for every
non-trivial action. The non-negotiable property — the thing that separates this from
Hermes — is that the operator can always answer *"what did it just do, with what, and
why?"* from persisted state.

**Definition of done (v1):** a single chat message can trigger a bounded multi-step run
that reads memory, inspects the repo, and answers with a trace; at least one human-authored
skill demonstrably changes behavior; and a crashed run can be diagnosed from the journal.
Everything past that (write actions, registry, steerability) is hardening and reach, not the
core thesis.

### Decisions

- **First cognition primitive:** ReAct loop first. It is the cheaper walking skeleton, fits
  chat, produces a natural step trace, and reuses existing structured-output / FunctionAgent
  patterns. Revisit tool-via-code as an accelerator once read-only traces and workflow evals
  exist.
- **Capability registry timing:** primitive allowed-action enum in Phase 1; formal
  capability registry in Phase 4.
- **Skill retrieval:** lexical first, semantic upgrade shared with factual memory in Phase
  3.
- **Review UI:** follow-on, not a gate. Flask-Admin plus chat commands are enough until
  semantic retrieval proves it needs a purpose-built review surface.
- **First PR:** assistant walking skeleton. It gives the most personal-assistant value for
  the least irreversible design risk.

### Roadmap

| Phase | Goal | Reuse | Net-new | Done when | Size |
|---|---|---|---|---|---|
| 0/1 | Assistant walking skeleton with a tiny eval baseline | Chat responder enqueue path, journal/debug rows, structured-output agent patterns, workspace shell, memory retrieval/commands, existing eval/benchmark harness | New `assistant` role, bounded loop, primitive action enum, trace format, fake-model tests, initial read-only actions | One user message can drive at least two model/tool iterations; trace is inspectable; at least the cold eval cases are baselined | M/L, roughly 1-2 weeks |
| 2 | Procedural skills MVP | `customize.dir` overlay pattern, markdown files, retrieval telemetry patterns | Skill loader, lexical skill retrieval, prompt formatting, optional candidate metadata | A human-authored skill changes assistant behavior in a traceable way; model-proposed skills can be stored for review | S/M, roughly 2-4 days |
| 3 | Semantic memory and shared retrieval upgrade | Existing pgvector/embedding path for Q&A, memory claim/evidence schema, retrieval telemetry, chat users/rooms | Embeddings for memory claims and skills, merge lexical/vector/entity/temporal signals, compact static/dynamic user profile, lightweight session context (concrete recipe: see [Memory systems](#memory-systems--what-to-borrow-mem0--supermemory--honcho)) | Semantic retrieval improves an eval case without forbidden/sensitive exposure; skills and facts share the retrieval path where practical | M/L, roughly 1-2 weeks |
| 4 | Formal capability registry and approvals | Phase 1 action enum, workspace-shell policy style, settings/admin patterns | Capability metadata, confirmation/dry-run flags, operator visibility, `rainbox doctor`, MCP policy hardening | Assistant can only call registered capabilities; operator can inspect what it is allowed to do | L, roughly 2-3 weeks if UI/doctor/MCP are included |
| 5 | Controlled write actions | Cron APIs, kanban APIs, patch/document agents, memory commands | One write family at a time with trace, dry-run or confirmation, and rollback/review path | Assistant completes one real personal workflow end to end with the write visible and reviewable | M per action family |
| 6 | Steerability and runtime visibility | Supervisor heartbeats, chat/SSE, journal, existing process watchdog | `/stop`, interrupt/redirect, context compression, long-call progress heartbeats, runtime dashboard | Active runs can be stopped or redirected without corrupting trace; long calls no longer look dead | M/L |

### Why this order

The sequence is driven by dependencies and by risk-per-phase, not by conceptual neatness:

- **Loop before everything** because skills, semantic memory, registry, and write actions
  all need something that *uses* them; building any of them first means building against a
  consumer that doesn't exist.
- **Skills before semantic memory** because skills are valuable even with crude lexical
  retrieval, and they exercise the prompt-injection/telemetry plumbing that the semantic
  upgrade then improves — so Phase 2 de-risks Phase 3 rather than the reverse.
- **Semantic memory before the formal registry** because retrieval quality is what makes the
  assistant *feel* useful, and the registry is a control plane — it should arrive when there
  is real power to control, not before.
- **Registry before write actions** because retrofitting permissions onto an assistant that
  can already mutate state is the painful ordering. (The Phase 1 action enum is the seed, so
  this is a formalization, not a from-scratch build.)
- **Steerability last** because interrupt/redirect and compression only matter once runs are
  long enough to need stopping — which they aren't until write actions and multi-step depth
  exist.

Read the column dependencies as: every phase reuses the *Reuse* column and ships only the
*Net-new* column. If a phase's Net-new list grows past what one PR can hold, split it — do
not let a phase silently absorb the next one's scope.

### Memory-system influences

Supermemory, Mem0, and Honcho are useful references for Phase 3, but should not become
dependencies yet. Rainbox already has the core thing worth protecting: a Postgres
claim/evidence memory model with provenance, lifecycle state, sensitivity, telemetry, and an
eval loop. The missing work is retrieval quality, profile synthesis, and session semantics.

**Supermemory-style context stack.**
Steal the shape, not the service: automatic candidate extraction, static/dynamic user
profiles, hybrid "personal memory + project docs" retrieval, contradiction/update handling,
expiry/forgetting, and memory benchmarks. Rainbox already has explicit evidence and expiry;
the Phase 3 upgrade should add a `profile.static` / `profile.dynamic` equivalent and a
single assistant context query that can return both remembered facts and relevant project
knowledge.

**Mem0-style retrieval signals.**
Mem0's useful lesson is that memory retrieval should not be just vector search. Add entity
linking and temporal ranking as additional signals alongside lexical and vector similarity:
who/what the memory is about, whether it supersedes another fact, how recent it is, and
whether it matches the current room/project scope. Keep deterministic filters
(status/expiry/sensitivity/scope) before any ranking.

**Honcho-style peer/session model.**
Honcho's strongest fit is its explicit model of peers and sessions, not its storage backend.
Rainbox already has `chat_user`, chatrooms, membership, and persona work; Phase 3 should
define the minimum assistant-session vocabulary: operator profile, agent profile, room or
project session, and prompt-ready session summary. Defer Honcho-style cross-peer modelling
("what agent X believes about user Y") until there are multiple real peers that need it.

**Benchmarks.**
Use LongMemEval/LoCoMo/ConvoMem/MemoryBench as inspiration, not as a required imported test
suite. Phase 0/1 should add a few tiny local cases now: temporal update ("I moved from A to
B"), contradiction/supersession, project-scoped recall, sensitive-memory exclusion, and
skill-vs-fact retrieval. Expand only after the assistant loop exists.

**Explicit non-goal.**
Do not replace rainbox memory with Supermemory/Mem0/Honcho wholesale. Hosted APIs and broad
connectors would weaken the "I know the implementation" goal. A future optional MCP/client
adapter is fine for experiments, but the durable source of truth should remain rainbox's own
tables and files.

### First PR scope (the only thing that needs to be decided to start)

Everything downstream is sequenced; the only commitment needed now is PR 1.

- **Build:** a new `assistant` chat responder running a bounded ReAct loop (max ~4–6 steps)
  over the read-only action enum below, persisting each step to the existing journal/debug
  surfaces.
- **Reuse:** the chat enqueue path, `FunctionAgent`/structured-output patterns, the
  `workspace_shell` sandbox, and existing memory retrieval.
- **Test:** drive the loop with fake model outputs (deterministic, no live LLM) so the
  control flow, action dispatch, step-cap, and trace format are covered without provider
  flakiness.
- **Done when:** one user message produces ≥2 model/tool iterations; the full trace
  (plan → action → args → observation → final) is inspectable from chat or Flask-Admin; and
  killing the process mid-run leaves a journal state that shows exactly which step ran last.
- **Explicitly out of scope for PR 1:** any write action, MCP, skills, semantic retrieval,
  and the formal registry. Resist scope creep here — the value of PR 1 is proving the loop
  and trace are sound.

### Risks and mitigations

- **The loop rambles / burns steps without converging.** Mitigation: hard step-cap, a
  required `reply` or `ask_clarifying_question` terminal action, and an eval case that fails
  if a known-simple task takes more than N steps.
- **Trace-in-`journal.result` becomes unqueryable.** This is the pre-agreed trigger to split
  into `assistant_run`/`assistant_step` tables — watch for it in Phase 2–3, don't pre-build.
- **Knowledge poisoning (the CIK dimension that actually applies here).** Memory or skill
  content gets inserted once and silently reused later — the OpenClaw analysis found
  poisoning any single capability/identity/knowledge dimension lifts attack success from
  ~25% to 64–74%. For a local single-operator box, *knowledge* is the live dimension because
  memory + model-proposed skills are exactly what Phases 2–3 build (capability and identity
  matter only once a second, externally-reachable channel exists — see the Security watchlist).
  Mitigations: model-proposed skills use a candidate→active lifecycle with operator
  activation and never inject unactivated; an adversarial eval case asserts that a poisoned
  memory/skill cannot expand the allowed-action set; provenance is required on every claim so
  a poisoned fact is traceable to its source.
- **Semantic retrieval surfaces sensitive/forbidden memory.** Mitigation: scope/sensitivity/
  expiry filters run *before* ranking, and an eval case asserts forbidden claims never
  appear regardless of similarity score.
- **Registry retrofit pain.** Mitigation: the Phase 1 action enum *is* the registry seed, so
  Phase 4 extends rather than introduces it — keep the enum the single dispatch gate from
  day one.

### Security watchlist (CIK)

The OpenClaw safety analysis ([*Your Agent, Their Asset*](https://arxiv.org/abs/2604.04759))
frames persistent-agent risk along three dimensions — **C**apability, **I**dentity,
**K**nowledge — and shows poisoning any one of them is enough to dominate an agent. That
study targets OpenClaw (full local system access, reachable from messaging apps, wired to
Gmail/Stripe/filesystem), a far larger attack surface than a local single-operator rainbox.
So this is *design pressure*, not roadmap — and it is deliberately right-sized to rainbox's
actual threat model rather than imported wholesale.

**Applies now — Knowledge.** This is the only CIK dimension live for a local single-operator
box, because memory + model-proposed skills are exactly what Phases 2–3 build. It is handled
as a first-class item in **Risks and mitigations** above (candidate→active skill lifecycle,
an adversarial eval that a poisoned memory/skill cannot expand the action set, required
provenance). Keep it there; do not let it become a parallel governance track.

**Contingent on a second, externally-reachable gateway — Capability & Identity.** These only
become real if rainbox ever adds a channel beyond the existing allowlisted Telegram bridge,
which the [Non-goals](#non-goals) currently reject. Recorded here so the design doesn't
foreclose them, *not* scheduled:

- *Identity:* normalize sender identity, trust level, and room/session mapping per channel
  before its messages can trigger the assistant; generalize the Telegram allowlist pattern.
- *Session model:* make explicit what state belongs to one room / sender / project / run /
  workspace — matters once multiple channels reach the same assistant.
- *Capability:* an adversarial test that a prompt, skill, or MCP tool cannot widen the
  allowed-action set (the Phase 1 action enum is the enforcement point).

**Already covered elsewhere — do not duplicate.** Budgets/step-caps live in
**Risks and mitigations**; `/stop`, interrupt, compression, and the runtime dashboard are
**Phase 6**; `rainbox doctor` and its subsystem checks are **Phase 4**; the forensic-grade
trace (who triggered it, which memories/skills/capabilities were in play, what ran, what was
observed) is the **North star** "what did it just do, with what, and why?" contract plus the
**Durable traces** refinement. These are referenced, not restated.

**The one PR-1 obligation:** avoid choices that make the contingent items expensive later —
specifically, keep action dispatch gated through the enum, keep the trace shape rich enough
to carry a future `source`/`identity`/`session`, and don't hard-code "the only sender is the
local operator" into the dispatch path. That costs nothing now and preserves the option.

### Phase 0/1 detail

Phase 0 is not a separate long prelude. Some eval cases can be written before the assistant
exists, such as memory answers or repo inspection using existing agents/tools. Trace-specific
cases, such as "which memory/tool did you use?", should be co-developed with the assistant
skeleton because the trace format does not exist yet.

Initial action set:

- `reply`
- `ask_clarifying_question`
- `query_memory`
- `workspace_read_command`
- `kanban_read`

Keep max steps low, e.g. 4-6. Do not start with broad MCP, file writes, or cron mutation.

### Tool-via-code follow-up

Tool-via-code remains attractive for this project because the operator is a developer and
inspectable scripts may be easier to trust than many small hidden tool calls. Defer it only
because the first sandbox/API boundary is easier to validate once the ReAct loop has traces,
evals, and a primitive capability registry. The first useful hybrid is a ReAct action that
generates and runs a read-only script against a narrow rainbox API surface.

### Non-goals

- Do not clone Hermes' gateway breadth.
- Do not add many execution backends before there is one good local loop.
- Do not let MCP tools silently expand assistant authority.
- Do not let model-written skills affect future behavior without a visible lifecycle.
- Do not build a large governance UI before the assistant has enough useful behavior to
  govern.

---

## Memory systems — what to borrow (mem0 / supermemory / honcho)

Three dedicated memory projects were reviewed to pressure-test rainbox's memory plan. The
reassuring headline: **rainbox already made the hard, opinionated decisions correctly.** What
these projects mainly contribute is a concrete *recipe* for the Phase 3 retrieval upgrade,
plus one optional later phase. None of them is a dependency to adopt — they are local-first
designs whose good ideas port cleanly because rainbox's stack (Postgres + pgvector + a
background worker) is nearly identical to theirs.

### One line each

- **[mem0](https://github.com/mem0ai/mem0)** — an extract-facts memory layer. As of April
  2026 it pivoted to **single-pass ADD-only accumulation** ("memories accumulate; nothing is
  overwritten"), with **multi-signal retrieval** (semantic + BM25 + entity, scored in
  parallel) and temporal ranking. Agent-generated facts are first-class.
- **[supermemory](https://github.com/supermemoryai/supermemory)** — a "memory ≠ RAG" engine:
  fact memory *and* document retrieval in one query, with automatic expiry, contradiction
  resolution, and an auto-maintained user profile injected into the prompt. Self-hostable as
  one binary with local embeddings.
- **[honcho](https://github.com/plastic-labs/honcho)** — a *theory-of-mind* / user-modeling
  system: extracts **inferred conclusions** about peers (not just facts), built
  asynchronously by a background "deriver," queryable via `context()` / `representation()` /
  `search()` / a natural-language `chat()` (dialectic) endpoint. Runs on **FastAPI +
  Postgres + pgvector + a background worker** — architecturally a near-twin of rainbox.

### Three things that validate rainbox's existing bets

- **mem0's ADD-only pivot independently validates rainbox's provenance-first model.** Rainbox
  already never destroys: `memory_claim` + `memory_evidence`, with `correct` superseding
  rather than overwriting. The most-benchmarked fact-memory layer converged on the same
  "accumulate, don't overwrite" principle rainbox already shipped.
- **supermemory's "Memory ≠ RAG" split is the split rainbox already has** — `memory_claim`
  (facts about the operator) vs the Q&A pgvector store (document/knowledge retrieval). Keep
  them separate; don't merge them into one index.
- **Agent-generated facts as first-class** (mem0) is already rainbox's `memory_evidence`
  kinds (`observed`/`inferred_by_model`/`confirmed_by_user`/`imported`).

So the cognition-layer gap is *not* the memory schema — it is retrieval quality and the
absence of a user profile, both already in Phase 3.

### Idea-by-idea map

| Idea (source) | Rainbox today | Borrow? | Where |
|---|---|---|---|
| Memory ≠ RAG: separate fact memory from doc retrieval (supermemory) | **Already has it** (`memory_claim` vs Q&A pgvector) | Keep the split | — |
| Accumulate, never overwrite; supersede via evidence (mem0) | **Already has it** (claim + evidence + `correct`) | Keep | — |
| Agent/inferred facts first-class (mem0) | **Already has it** (evidence kinds) | Keep | — |
| Multi-signal retrieval: vector + BM25/lexical + entity, merged score (mem0) | Token-overlap only; pgvector for Q&A | **Yes — the concrete recipe** | **Phase 3** |
| Pre-rank expiry/scope/sensitivity filtering (supermemory auto-forget) | `expiry`/`scope`/`sensitivity` fields exist, not enforced in retrieval | **Yes — cheap, high value** | **Phase 3** (already in Risks: filters before ranking) |
| Auto contradiction detection ("moved to SF" ⊃ "NYC") (supermemory) | Manual `correct` only | **Yes, lightweight** — *detect & flag*, operator confirms | **Phase 3** detect → **Phase 5** auto-supersede (write action) |
| Auto-maintained user profile injected into prompt (supermemory/honcho) | None | **Yes** — this is the roadmap's "user model" | **Phase 3** (compact static/dynamic profile) |
| Inferred *conclusions* about the user, built by an async deriver (honcho) | `inferred_by_model` evidence kind exists; no deriver | **Pattern yes, scope later** — a deriver is just another rainbox agent | **New optional Phase 3.5/7** |
| NL dialectic query of the user model w/ reasoning levels (honcho) | None | **Skip for v1** — overkill for one operator | Much later / maybe never |
| Entity graph / linking across memories (mem0) | `subject`/`predicate`/`object` fields exist but aren't linked/traversed | **Later** — S/P/O is the seed; entity *match* (not full graph) is enough for Phase 3 | Phase 3 (match) / later (graph) |
| Multi-modal ingestion + SaaS connectors: Drive, Gmail, Notion (supermemory) | — | **No** — gateway-breadth equivalent | Non-goal |
| Managed cloud / external memory API (all three) | — | **No** — local-first | Non-goal |

### The one genuinely new idea: an async user-model deriver (Honcho-style)

Honcho's architecture is the interesting find because it is *rainbox's architecture*:
Postgres + pgvector + a background worker that asynchronously turns raw messages into
derived representations. In rainbox terms, a **`profile_deriver` agent** would periodically
read recent chat/journal rows and emit/refresh `inferred_by_model` claims and a compact
operator profile — exactly the existing agent pattern, writing into the existing schema. No
new infrastructure, no dialectic endpoint, no new datastore.

This refines the earlier "skip Honcho's dialectic complexity" note: skip the *dialectic
query API*, but the *async-derivation pattern* is worth adopting because it costs almost
nothing on rainbox's stack. Sequence it as an **optional Phase 3.5** (after semantic
retrieval exists to consume the profile) or fold the first version into Phase 3's
"compact user profile" as a one-shot summarizer, upgrading to a periodic deriver later.

### Net effect on the roadmap

No phase reordering. Three concrete changes, all inside Phase 3 except one optional add:

1. **Phase 3 retrieval** becomes mem0's recipe explicitly: vector + lexical + entity-match,
   merged, with expiry/scope/sensitivity filters applied *before* ranking.
2. **Phase 3 user profile** gains supermemory's "static facts + dynamic recent context"
   shape, injected into the assistant prompt.
3. **Contradiction handling** splits cleanly along the existing decision boundary: *detect*
   in Phase 3 (surface conflicts to the operator), *auto-supersede* only as a Phase 5 write
   action with the usual dry-run/confirm.
4. *(Optional)* **`profile_deriver` agent** (Honcho-style async derivation) as Phase 3.5.
