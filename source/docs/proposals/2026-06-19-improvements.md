# Rainbox improvements — informed by Hermes Agent (2026-06-19)

**Context:** Comparison of rainbox against [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research's self-improving personal agent). Goal is **not** for rainbox to become
Hermes — the goal is a personal assistant whose implementation the operator fully
understands and owns. This document collects insights from multiple assistants so we can
refine on the combined view.

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
| 3 | Semantic memory and shared retrieval upgrade | Existing pgvector/embedding path for Q&A, memory claim/evidence schema, retrieval telemetry | Embeddings for memory claims and skills, merge lexical/vector results, compact user profile view | Semantic retrieval improves an eval case without forbidden/sensitive exposure; skills and facts share the retrieval path where practical | M, roughly 1 week |
| 4 | Formal capability registry and approvals | Phase 1 action enum, workspace-shell policy style, settings/admin patterns | Capability metadata, confirmation/dry-run flags, operator visibility, `rainbox doctor`, MCP policy hardening | Assistant can only call registered capabilities; operator can inspect what it is allowed to do | L, roughly 2-3 weeks if UI/doctor/MCP are included |
| 5 | Controlled write actions | Cron APIs, kanban APIs, patch/document agents, memory commands | One write family at a time with trace, dry-run or confirmation, and rollback/review path | Assistant completes one real personal workflow end to end with the write visible and reviewable | M per action family |
| 6 | Steerability and runtime visibility | Supervisor heartbeats, chat/SSE, journal, existing process watchdog | `/stop`, interrupt/redirect, context compression, long-call progress heartbeats, runtime dashboard | Active runs can be stopped or redirected without corrupting trace; long calls no longer look dead | M/L |

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
