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

## Codex

### Self-assessment after Claude's critique

Claude's critique is fair. Codex added useful substance, but it also overstepped the
document process by filling the "Combined refinement" section before a true joint pass. That
section should not pretend to be the reconciled view yet. The correction is to keep Codex's
roadmap as a **Codex proposed synthesis** and restore the combined section to an explicit
pending reconciliation.

The critique also catches a real prioritization error. Codex put the capability registry too
early in the phase list while the final recommendation put it later. For a single-operator
personal assistant, the cognition layer should come first: assistant loop, skills, semantic
memory. The capability control plane matters, but it should arrive as the assistant's power
expands, not as ceremony before the assistant feels useful.

### What Codex adds

**1. Durable assistant traces.**
Hermes' iterative loop is worth copying, but not as an opaque long-lived process. In
rainbox, an assistant run should leave a step trace:

- user request
- model plan
- chosen action
- normalized arguments
- observation/result
- final answer or blocked state

The first implementation can store this inside `journal.result` or a `debug-assistant` chat
row. Split it into `assistant_run` / `assistant_step` tables only when the JSON becomes hard
to query, link, or resume. That avoids building schema before the shape of real runs is
known.

**2. Reviewed procedural skills.**
Hermes' self-written skills are the best idea to steal, but rainbox should make skill
lifecycle inspectable. Human-authored skills can be active immediately. Model-proposed
skills should start as candidates with source run/journal metadata, then be activated,
edited, rejected, or superseded by the operator. A possible middle ground for later is
"auto-active only for read-only/dry-run skills after they pass a test," but the first version
should keep activation explicit.

**3. Capability control plane, later and lighter.**
A typed capability registry is still the best new idea Codex adds: it lets the assistant
prompt be generated from owned capabilities rather than a hand-written list of tools. But it
should start small and follow actual assistant needs. Register capabilities as they are
exposed to the assistant; do not build an enterprise policy framework before the assistant
has real daily use.

**4. Eval wiring before judging the loop.**
Rainbox already has eval and benchmark machinery. The next step is not a new eval platform,
but a tiny set of assistant-workflow cases: repo inspection, memory answer, cron dry-run,
kanban read/update, and "which memory/tool did you use?" That gives the assistant loop,
skills, and semantic memory something concrete to prove against.

### Open fork: ReAct loop vs tool-via-code first

Claude's roadmap assumes a ReAct-style assistant loop first. That is probably right for
assistant feel, but it is not the only rainbox-native path.

**ReAct loop first** means the model repeatedly chooses one typed action, observes the
result, and decides the next step. It is easier to constrain, easier to render as a chat
trace, and fits the existing FunctionAgent/structured-output paths. It may cost more model
round-trips and can feel verbose.

**Tool-via-code first** means the model writes a small inspectable script that calls
rainbox-owned functions, then the system runs that script in a constrained environment. This
is attractive for a developer-operator: fewer round-trips, concrete code to inspect, and
better composition for multi-step local tasks. It also creates a bigger execution-safety
problem earlier.

Codex's proposed resolution: start with a small ReAct loop because it is the safer walking
skeleton, but evaluate tool-via-code as the first major accelerator once the assistant has
read-only traces and a few workflow evals. Do not treat ReAct as a permanent architectural
commitment.

### Codex proposed synthesis

This is Codex's proposed ordering, not the final combined roadmap.

#### Phase 0 - wire a tiny assistant eval set

Use the existing eval/benchmark machinery to create cases for:

- answering from memory
- inspecting the repo and summarizing
- creating a cron/reminder dry-run
- reading or updating a kanban card
- explaining which memory/tool was used

**Done when:** at least three assistant workflows have repeatable baseline cases.

#### Phase 1 - assistant walking skeleton

Add `assistant` as a chat responder with a bounded loop and read-only actions:

- `reply`
- `ask_clarifying_question`
- `query_memory`
- `workspace_read_command`
- `kanban_read`

Persist a trace in the existing journal/debug surfaces. Keep max steps low, e.g. 4-6.

**Done when:** one user message can produce at least two model/tool iterations and the trace
is inspectable.

#### Phase 2 - procedural skills MVP

Load active markdown skills from `<customize.dir>/skills/` and retrieve them into the
assistant prompt. Start with human-authored skills so retrieval, formatting, and telemetry
can be validated before model-written skills enter the loop. Then add model-proposed
candidate skills.

**Done when:** a skill changes assistant behavior in a traceable way, and a proposed skill
can be reviewed before activation.

#### Phase 3 - semantic memory and user profile

Keep Postgres claim/evidence as the source of truth. Add pgvector retrieval for memory
claims, merge lexical and semantic results, and generate a compact profile view from
confirmed high-value claims. A dedicated memory review UI belongs here, because semantic
memory without review becomes hard to trust.

**Done when:** semantic memory improves an eval case without exposing forbidden/sensitive
memories, and the operator can inspect/correct the relevant memory.

#### Phase 4 - capability registry and approvals

Now formalize the capability surface exposed to the assistant. Start with the capabilities
already used by phases 1-3, then add flags for write/network/secret behavior,
confirmation, timeout/output caps, and dry-run support. `rainbox doctor` is new scope here,
not an existing command.

**Done when:** the assistant cannot call unregistered capabilities, and the operator can see
what the assistant is allowed to do.

#### Phase 5 - controlled write actions

Add write-capable actions one family at a time:

- create/update reminders and cron jobs
- update kanban cards
- propose document/file patches
- write memory or skill candidates
- use explicitly enabled MCP tools

**Done when:** the assistant can complete one real personal workflow end to end with the
write visible in the trace and either reversible, dry-run-first, or confirmed.

#### Phase 6 - steerability and runtime visibility

Add interrupt/redirect, `/stop`, context compression, long-model-call heartbeats, and a
runtime dashboard once assistant runs last long enough for those controls to matter.

**Done when:** an in-progress assistant run can be stopped or redirected without corrupting
the trace, and long calls no longer look like dead processes.

### Codex's first PR recommendation

The first PR should not be the capability registry. It should be a narrow assistant walking
skeleton with trace persistence and fake-model tests. The next two should be skills MVP and
semantic memory wiring. The control plane follows once the assistant has enough useful
capabilities to control.

---

## Combined refinement (Claude + Codex)

> **TODO:** Reconcile Claude's roadmap, Codex's proposed synthesis, and Claude's critique
> into a single final sequence. The current tentative ordering is:
>
> 1. assistant loop
> 2. procedural skills
> 3. semantic memory / user profile
> 4. capability registry and approvals
> 5. controlled write actions
> 6. steerability, compression, and runtime visibility
