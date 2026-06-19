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

### Self-assessment

Claude's diagnosis is stronger than Codex's first pass. The important product gap is not
another admin page or another hardening layer; it is that rainbox does not yet have a
first-class assistant that can stay with a task, use tools over several steps, learn a
procedure, and reuse that procedure later. Claude's "infrastructure ahead, cognition
behind" framing is the right center of gravity.

Codex's contribution is still useful, but it belongs as the *operating model around* that
assistant loop: authority boundaries, capability inventory, approvals, runtime visibility,
MCP policy, eval gates, and memory review. Those are what keep a Hermes-inspired assistant
compatible with rainbox's actual goal: a personal assistant whose behavior is inspectable
and whose implementation remains legible to its operator.

### What Codex adds

**1. The assistant loop must be durable, not just iterative.**
Do not import Hermes' shape literally as one long-running opaque process. In rainbox, the
assistant should be an iterative agent whose *steps* are persisted:

- user request
- model plan
- selected tool/capability
- normalized arguments
- approval decision when required
- observation/result
- final answer or blocked state

The existing `journal` table can carry the first version, but the design should quickly
grow a step-level ledger (`assistant_run` / `assistant_step` or equivalent) if the JSON
inside one journal row becomes hard to inspect. The invariant is simple: if the process dies,
the operator can see exactly which step ran last and what it observed.

**2. Skills should be proposed first, active second.**
Hermes' self-written skills are the best idea to steal, but rainbox should not silently let
a model install new behavior into its future prompt. Treat generated skills like memory
candidates:

- write the skill as a candidate in `<customize.dir>/skills/`
- store metadata: source run/journal id, task summary, tool calls used, model, created_at
- require human activation before retrieval injects it into the assistant prompt
- keep rejected and superseded skills for audit
- add a "run this skill in dry-run mode" test hook before activation

This preserves the useful learning loop without giving the model unreviewed authority over
future behavior.

**3. Capability boundaries are the missing control plane.**
The assistant loop should not see "all Python functions" or "all MCP tools." It should see a
small catalog of typed capabilities. Each capability declares:

- read/write/network/secret behavior
- whether it can run unattended
- whether it requires confirmation
- timeout and output limits
- redaction rules
- eval coverage or a manual-test requirement

That catalog can drive the prompt, UI badges, `rainbox doctor`, cron validation, and tests.
It also gives the operator a concise answer to "what can my assistant actually do?"

**4. Personal usefulness should come from narrow owned workflows.**
Do not chase Hermes' 20-platform gateway breadth. Rainbox should get good at a small number
of personal workflows:

- answer questions about the local machine and projects
- remember and retrieve operator preferences
- inspect repos safely
- manage reminders/cron jobs
- update kanban/task state
- draft or propose file/document edits
- talk through Telegram or one other chosen gateway

Each workflow should be backed by tests and audit rows. Breadth comes later only when a real
daily workflow asks for it.

**5. Safety work should enable autonomy, not postpone it.**
Capability registry, runtime dashboard, long-call heartbeats, MCP policy, and memory review
are not the main product. They are the scaffolding that makes a real assistant loop safe
enough to run unattended for bounded tasks. Build them alongside the assistant, not as a
separate prelude that delays the useful part indefinitely.

---

## Combined refinement (Claude + Codex)

### North star

Rainbox should become a **durable local personal assistant**:

- it can do multi-step work, not just answer one prompt
- it learns reusable procedures, but only in inspectable form
- it remembers facts with provenance and correction paths
- it acts through typed, bounded capabilities
- every non-trivial action leaves a ledger row the operator can inspect

That is the useful overlap with Hermes. The non-overlap is just as important: rainbox should
not become a broad hosted gateway, a framework dependency pile, or a black-box autonomous
process.

### Architecture choice

Build one new first-class `assistant` agent, but make it rainbox-native:

```text
chat message
-> assistant_run
-> assistant_step(plan)
-> assistant_step(tool_call / observation)*
-> assistant_step(final / blocked)
-> chat reply
```

The loop can start with a bounded max-step count and a small toolset. The first useful
version does not need parallel subagents, tool-via-code, or autonomous skill writing. It
only needs to demonstrate: "I can plan, inspect, use one or two owned tools, observe, and
answer with a trace."

### Prioritized roadmap

#### Phase 0 - sharpen the baseline

Before adding the loop, make a tiny evaluation set for the workflows that should improve:

- "answer from memory"
- "inspect repo and summarize"
- "create a reminder/cron dry-run"
- "update a kanban card"
- "explain which memory/tool was used"

This prevents the assistant loop from being judged by vibes. It also gives later skills and
memory retrieval changes something to prove against.

**Done when:** there are active eval cases or benchmark scripts for at least three real
assistant workflows, and a baseline run is recorded.

#### Phase 1 - iterative assistant walking skeleton

Add `assistant` as a chat responder with a bounded ReAct-style loop:

- model proposes the next step as structured output
- allowed actions are initially small: `reply`, `ask_clarifying_question`, `query_memory`,
  `workspace_read_command`, maybe `kanban_read`
- tool observations are appended to the step transcript
- final answer links to a trace/debug row
- max steps defaults low, e.g. 4-6

Do not start with broad MCP or write actions. The goal is to make rainbox feel like an
assistant while keeping the first version easy to reason about.

**Done when:** a single user message can trigger at least two model/tool iterations, the
trace is inspectable from chat/admin, and a crashed run can be diagnosed from persisted
state.

#### Phase 2 - capability registry and approval policy

Introduce a code-side capability registry before the assistant gains more power. Register
workspace shell commands, memory operations, cron actions, kanban operations, backup, MCP
servers/tools, and any gateway actions.

Each capability should declare:

- id and display name
- owner module
- read/write/network/secret flags
- default enabled state
- requires confirmation
- timeout/output caps
- dry-run support
- eval/manual-test coverage

The assistant prompt should be generated from enabled capabilities, not hand-written lists.

**Done when:** the assistant cannot call an unregistered capability; `/settings` or an admin
view can show the active capability surface; `rainbox doctor` can report unsafe or
misconfigured capabilities.

#### Phase 3 - procedural skills as reviewed artifacts

Add Hermes-style skills, but with rainbox's provenance model:

- skill files live in `<customize.dir>/skills/`
- metadata tracks status: `candidate`, `active`, `superseded`, `rejected`
- the assistant may propose a skill after a successful run
- the operator activates, edits, or rejects it
- active skills are retrieved by lexical/semantic match and injected into the assistant
  prompt
- skill use emits telemetry like memory retrieval

Start with human-authored skills before allowing model-proposed candidates. That validates
the retrieval and prompt format without letting the model write future behavior too early.

**Done when:** a hand-authored skill changes assistant behavior in a traceable way, and a
model-proposed skill can be reviewed before activation.

#### Phase 4 - semantic memory and user profile

Keep the existing claim/evidence schema. Upgrade retrieval:

- embed active memory claims using pgvector
- merge lexical and vector candidates
- keep scope, sensitivity, expiry, and provenance filters first
- add a compact `USER` profile view generated from confirmed high-value claims
- add a memory review UI for candidates, active claims, evidence, correction, and
  sensitivity

Avoid a flat `MEMORY.md` fact store. Files are appropriate for procedural skills; Postgres
is better for sourced personal facts.

**Done when:** semantic retrieval improves at least one eval case without increasing
forbidden/sensitive memory exposure, and the operator can inspect or correct the memory from
a purpose-built UI.

#### Phase 5 - controlled write actions

After the assistant can read, plan, and use skills, add write-capable actions one family at
a time:

- create/update reminders and cron jobs
- update kanban cards
- propose document/file patches
- write memory candidates
- run MCP tools that have explicit policy

Every write action should support either dry-run, confirmation, or both. Cron and gateway
actions should default to confirmation until the operator deliberately marks a capability as
safe for unattended use.

**Done when:** the assistant can complete one real personal workflow end to end, with the
write action visible in the trace and reversible or reviewable by the operator.

#### Phase 6 - steerability and compression

Once runs last long enough to matter, add:

- interrupt-and-redirect from a new chat message
- `/stop` for active assistant runs
- `/compress` or automatic context summaries for long rooms
- long-running model heartbeats
- runtime dashboard with PID, current step, model/provider, heartbeat age, kill/retry

This is when rainbox starts matching the lived feel of Hermes without losing its own
supervisor model.

**Done when:** an in-progress assistant run can be stopped or redirected without corrupting
the ledger, and long model calls no longer look like dead processes.

### First three PRs

**PR 1: `assistant` walking skeleton.**
One bounded loop, read-only capabilities, persisted trace, chat integration, and tests with
fake model outputs.

**PR 2: capability registry.**
Register current built-in capabilities, expose them in a simple admin/settings view, and
make the assistant consume only registered capabilities.

**PR 3: reviewed skills MVP.**
Load active markdown skills from `<customize.dir>/skills/`, retrieve them for assistant
prompts, and add candidate metadata/status even if the first skills are human-authored.

### Design invariants

- A model may propose actions; code enforces allowed actions.
- A model may propose skills; the operator activates them.
- Memory facts stay in Postgres with evidence, not flat files.
- Procedural skills may be files because inspectability and portability matter there.
- All write/network/secret capabilities are explicit and visible.
- The assistant loop is bounded by step count, timeout, and capability policy.
- Every assistant answer that used tools or memory has an inspectable trace.
- Evals gate changes to memory retrieval, skills, and assistant prompts.

### What this deliberately rejects

- cloning Hermes' gateway breadth
- adding many execution backends before there is one good local loop
- letting MCP tools silently expand the assistant's authority
- self-modifying prompts or skills without review
- optimizing for impressive demos over repeatable personal workflows

### Final recommendation

Adopt Claude's ordering, but with Codex's guardrails:

1. Build the iterative assistant loop.
2. Add reviewed procedural skills.
3. Improve semantic memory and user profile retrieval.
4. Add capability registry, approvals, and runtime visibility as the control plane.
5. Expand write actions only after traces, evals, and review flows exist.

That sequence turns rainbox from an agent runtime into a personal assistant without giving
up the core advantage it already has: the operator can see how it works.
