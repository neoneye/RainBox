# Rainbox improvements v2 — phased roadmap with candidate analysis (2026-06-19)

**Status:** decision draft. Each phase states the *problem*, lays out *candidate solutions*
(including the weaker ones, on purpose), scores them across a fixed metric set, and picks the
strongest. The picks are marked **▶ RECOMMENDED**. Every phase ends with a **Codex: verify**
block listing the assumptions and alternatives Codex should check against the real codebase
before we commit.

**Background:** the comparison and reasoning that produced this roadmap live in
[`2026-06-19-improvements-v1-brainstorm.md`](2026-06-19-improvements-v1-brainstorm.md)
(rainbox vs Hermes Agent, the OpenClaw CIK security analysis, and the mem0 / supermemory /
honcho memory review). v2 is the actionable distillation; v1 is the *why*.

**Goal (unchanged):** rainbox becomes a **durable local personal assistant** whose
implementation the operator fully understands — not a clone of Hermes, OpenClaw, or any
hosted memory product.

---

## How candidates are scored

Every candidate is rated on the same six metrics. Higher is better except **Cost**.

| Metric | Meaning |
|---|---|
| **Value** | How much it advances the personal-assistant goal (multi-step, learns, remembers). |
| **Fit** | Fit with rainbox's architecture: durable Postgres queue, POSIX child processes, local-first. |
| **Legible** | How easily the operator can understand and inspect the implementation. |
| **Reuse** | How much it leverages code that already exists in rainbox. |
| **Safe** | Reversibility / low blast radius / low risk (higher = safer). |
| **Cost** | Implementation effort (S / M / L — *lower is better*). |

Ratings are H / M / L. The winner is not "highest on every axis" — it is the best balance for
*this* operator and goal, and the rationale says which metrics decided it.

---

## Phase order at a glance

| # | Phase | Core problem it solves | Cost |
|---|---|---|---|
| 1 | Assistant walking skeleton (ReAct loop + durable trace) | rainbox can't do multi-step work | M/L |
| 2 | Procedural skills MVP | nothing is learned/reused across tasks | S/M |
| 3 | Semantic memory + user profile | fact retrieval is weak; no user model | M/L |
| 3.5 | *(optional)* async profile deriver | profile goes stale; no inferred conclusions | M |
| 4 | Capability registry + approvals | assistant's power isn't bounded/inspectable | L |
| 5 | Controlled write actions | assistant can read but not act | M/family |
| 6 | Steerability + runtime visibility | long runs can't be stopped/redirected | M/L |

Ordering rationale (dependencies, not taste): the **loop** must exist before anything that
plugs into it; **skills** are useful even with crude retrieval and exercise the prompt
plumbing that Phase 3 then upgrades; **semantic memory** is what makes the assistant *feel*
useful, so it precedes the control plane; the **registry** must land before **write actions**
to avoid retrofitting permissions onto a mutating agent; **steerability** only matters once
runs are long enough to need stopping.

---

## Phase 1 — Assistant walking skeleton

### Problem
Rainbox agents are single-shot, schema-validated child processes with linear routing. There
is no agent that can *plan → act → observe → repeat* over several steps. That single gap is
what separates "agent runtime" from "personal assistant." Whatever shape it takes, it must
keep rainbox's two non-negotiables: **durability** (a crash mid-task is recoverable) and
**inspectability** (the operator can see what it did and why).

### Candidate solutions

**A. ReAct loop over a typed action enum** — the model emits structured output choosing one
of a small set of allowed actions per step; the loop dispatches it, appends the observation,
and re-prompts until a terminal `reply`. Each step is journaled.

**B. Tool-via-code** — the model writes a short Python script that calls rainbox APIs; the
system runs it in the `workspace_shell` sandbox. Fewer round-trips, concrete artifact.

**C. Use LlamaIndex `FunctionAgent` / AgentWorkflow directly** — let the framework own the
loop; rainbox just supplies tools and reads the final result.

**D. Adopt an external agent framework** (LangGraph / CrewAI) as a spawned child process.

**E. Extend the existing linear pipeline** (the dreamer→critic→verifier `next: UUID` pattern)
with more hardcoded stages — no real model-driven loop.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| **A. ReAct + typed enum** ▶ | H | H | H | H | H | M |
| B. Tool-via-code | H | M | M | M | M | M/L |
| C. FunctionAgent owns loop | M | L | L | H | M | S |
| D. External framework | H | L | L | L | M | L |
| E. Hardcoded pipeline | L | H | H | H | H | S |

### ▶ Recommendation: **A (ReAct loop over a typed action enum)**
Wins on **Fit + Legible + Safe** without sacrificing Value. Each step is a journaled
plan/action/args/observation row, so durability and the audit trail come for free and the
trace renders naturally in chat/Flask-Admin. The typed enum is also the **seed of the Phase 4
capability registry** — building it now means Phase 4 *formalizes* rather than introduces.
- **B** is genuinely attractive for a developer-operator and should be the *next* accelerator
  (it produces inspectable code), but it makes the sandbox/API boundary the very first thing
  to get right — too much irreversible design risk for the walking skeleton. Revisit once
  read-only traces and workflow evals exist; the first hybrid is a ReAct action that runs a
  read-only generated script.
- **C** loses per-step durability: the framework owns an opaque async loop, so crash-recovery
  and step-level tracing fight the abstraction. Cheap to start, expensive to observe.
- **D** is against the project's whole ethos (own the impl, no framework dependency) and adds
  the most cost for the least legibility.
- **E** isn't actually multi-step reasoning; it's the status quo with more stages.

### Sub-decision: where does the trace live?
- **A. Reuse `journal.result` JSON + `debug-assistant` chat rows** ▶ — zero new schema.
- B. New `assistant_run` / `assistant_step` tables now — premature; build when the JSON
  becomes hard to query/link/resume (pre-agreed trigger, watch in Phase 2–3).
- C. External log file — breaks the "everything in Postgres, recoverable" invariant.

### First-PR scope (the only commitment needed to start)
- **Build:** `assistant` chat responder, bounded ReAct loop (max ~4–6 steps), read-only
  action set: `reply`, `ask_clarifying_question`, `query_memory`, `workspace_read_command`,
  `kanban_read`; each step persisted.
- **Reuse:** chat enqueue path, structured-output/`FunctionAgent` patterns, `workspace_shell`
  sandbox, existing memory retrieval.
- **Test:** drive with **fake model outputs** (deterministic, no live LLM) to cover control
  flow, dispatch, step-cap, and trace shape.
- **Done when:** one message → ≥2 model/tool iterations; full trace inspectable; killing the
  process mid-run leaves a journal state showing exactly which step ran last.
- **Out of scope:** any write action, MCP, skills, semantic retrieval, formal registry.

### Codex: verify
- Confirm `FunctionAgent`/structured-output can be driven step-at-a-time so each step
  journals, *or* confirm a hand-rolled loop is cheaper than fighting the framework.
- Confirm the chat responder enqueue path can host a new `assistant` role without supervisor
  changes; check `agents/config.py` for the role/UUID/`next` wiring.
- Confirm `journal.result` JSON can carry the step trace and is queryable enough for v1.
- Challenge: is the read-only action set the right *minimum*, or is one missing
  (e.g. `git_status`, `query` Q&A) that makes the skeleton demonstrably useful sooner?
- Sanity-check the M cost estimate against the real enqueue/streaming plumbing.

---

## Phase 2 — Procedural skills MVP

### Problem
Rainbox has **declarative** memory (facts) but no **procedural** memory. It cannot capture
"how I solved this" and reuse it. This is the single biggest capability gap vs Hermes — and
the thing that makes an assistant "grow" — but it must not let a model silently install
behavior into its own future prompts.

### Candidate solutions — storage
**A. Markdown files in `<customize.dir>/skills/`** (agentskills.io-compatible) — mirrors the
existing customize-overlay pattern; portable; diff-able. ▶
**B. `skill` table in Postgres** reusing the claim/evidence provenance model.
**C. Hybrid:** markdown content in files + a small DB/sidecar row for status & provenance.

### Candidate solutions — activation lifecycle
**A. Human-authored skills active immediately; model-proposed start as `candidate` →
operator activates/edits/rejects/supersedes.** ▶
**B. Auto-activate all model-written skills** (Hermes-style).
**C. Auto-activate only read-only/dry-run skills that pass a test.**

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| Storage A. files ▶ | H | H | H | H | H | S |
| Storage B. table | M | H | M | H | M | M |
| Storage C. hybrid (files + status) ▶ | H | H | H | H | H | S/M |
| Lifecycle A. candidate→active ▶ | H | H | H | H | H | S |
| Lifecycle B. auto-active all | H | M | L | M | L | S |
| Lifecycle C. auto-active dry-run-only | H | M | M | M | M | M |

### ▶ Recommendation: **files for content (A), thin status metadata (C), candidate→active lifecycle (A)**
Files win on **Legible + Reuse + Portability + Cost**; a small status field (`candidate` /
`active` / `superseded` / `rejected`) plus source run/journal id gives provenance without a
heavy schema. The candidate→active lifecycle is the **knowledge-poisoning mitigation** from
v1's CIK analysis: never inject an unactivated skill.
- **Lifecycle B** hands a model unreviewed authority over future behavior — exactly what the
  CIK "knowledge" dimension warns against. Rejected for v1.
- **Lifecycle C** is the right *eventual* middle ground (auto-active sandboxed/dry-run skills
  after a passing test) but needs the test harness first — defer.
- **Storage B** (pure table) loses portability and diffability for no real gain over the
  hybrid.

### Retrieval note
Skill retrieval starts **lexical/token-overlap** here and rides the Phase 3 semantic upgrade
later — do **not** build a separate skill retriever.

### Codex: verify
- Confirm `<customize.dir>` overlay loading exists and can host a `skills/` subtree the same
  way it hosts `question_answer.jsonl` / `mcp.json`.
- Confirm there's an existing retrieval-telemetry pattern (like memory/query) skills can emit.
- Challenge: should status metadata be a sidecar `.json`/frontmatter in the file (simplest)
  or a DB row (queryable)? Pick by how the operator will review candidates.
- Verify agentskills.io format compatibility is actually worth honoring (portability payoff
  vs format constraints).

---

## Phase 3 — Semantic memory + user profile

### Problem
The memory **schema** is good (provenance-first `memory_claim` + `memory_evidence`,
lifecycle, confirm/correct). The **retrieval** is the weak link: deterministic token-overlap
for facts, with pgvector used only for Q&A. There is also no operator **user model** injected
into prompts. The mem0 / supermemory / honcho review (v1) confirmed rainbox already made the
hard schema bets correctly — the gap is retrieval quality and the profile.

### Candidate solutions — retrieval
**A. Pure pgvector semantic** — embed claims, cosine similarity.
**B. Multi-signal: vector + lexical/BM25 + entity/`subject` match, merged into one score
(mem0 recipe), with expiry/scope/sensitivity filters applied *before* ranking.** ▶
**C. LLM re-rank** over a candidate set.
**D. Keep token-overlap** (do nothing) — the baseline.

### Candidate solutions — user profile
**A. One-shot summarizer** producing a compact static-facts + dynamic-context profile claim
set, injected into the assistant prompt. ▶ (upgradeable to 3.5)
**B. Async `profile_deriver` agent** (Honcho-style) — *see Phase 3.5*.
**C. Full Honcho dialectic API** (NL queries, reasoning levels).

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| Retrieval A. pure vector | M | H | M | H | M | S |
| Retrieval B. multi-signal + filters ▶ | H | H | M | H | H | M |
| Retrieval C. LLM re-rank | H | M | L | M | M | M |
| Retrieval D. token-overlap (baseline) | L | H | H | H | M | — |
| Profile A. one-shot summarizer ▶ | H | H | H | H | H | S/M |
| Profile B. async deriver | H | H | M | H | H | M |
| Profile C. Honcho dialectic | M | L | L | L | M | L |

### ▶ Recommendation: **retrieval B (multi-signal + pre-rank filters), profile A (one-shot summarizer)**
Retrieval B wins on **Value + Safe**: vector adds recall, lexical keeps exact-term precision,
entity/`subject` match exploits the schema rainbox already has, and the pre-rank
expiry/scope/sensitivity filter is the **sensitive-memory-leak mitigation** (forbidden claims
never reach ranking regardless of similarity). It reuses the existing Q&A pgvector path.
- **A (pure vector)** is cheaper but loses exact-term precision and the existing token-overlap
  strength — a regression on some queries.
- **C (LLM re-rank)** is a good *optional add-on* once B exists, but adds latency/cost and
  reduces determinism (legibility); not the first move.
- **D** is the thing we're fixing.
- Profile **A** is the cheapest path to a real user model; **C** is overkill for one operator
  (rejected). **B** is the natural upgrade — sequenced as 3.5.

### Contradiction handling (split along the existing decision boundary)
- **Detect** in Phase 3: surface conflicts ("'moved to SF' vs 'lives in NYC'") to the
  operator. Cheap, read-only.
- **Auto-supersede** only in Phase 5 as a write action with dry-run/confirm.

### Codex: verify
- Confirm the Q&A pgvector + embeddings path (LM Studio `nomic-embed-text`) can be reused for
  `memory_claim` embeddings without a second embedding stack.
- Confirm `scope` / `sensitivity` / `expiry` columns exist on `memory_claim` and can be
  filtered pre-ranking.
- Challenge: is BM25 worth the dependency, or does Postgres full-text search (`tsvector`)
  cover the lexical signal natively? Prefer the native option if recall is comparable.
- Verify entity match is feasible from existing `subject`/`predicate`/`object` fields.
- Confirm there's an eval case (Phase 0/1) that can prove a retrieval improvement *and* a
  forbidden-claim-never-surfaces assertion.

---

## Phase 3.5 — *(optional)* async profile deriver

### Problem
A one-shot profile (Phase 3) goes stale and only captures explicit facts, not **inferred
conclusions** about the operator. Honcho solves this with a background "deriver" — and
Honcho's stack (FastAPI + Postgres + pgvector + background worker) is **architecturally a
near-twin of rainbox** (Flask + Postgres + pgvector + supervisor + agents).

### Candidate solutions
**A. `profile_deriver` agent** — a normal rainbox agent that periodically reads recent
chat/journal rows and emits/refreshes `inferred_by_model` claims + the compact profile. ▶
**B. Inline derivation** inside the assistant loop each turn — simpler but slows every turn.
**C. Adopt Honcho itself** as a service.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| A. `profile_deriver` agent ▶ | H | H | H | H | H | M |
| B. inline derivation | M | M | M | H | M | S |
| C. adopt Honcho service | H | L | L | L | M | L |

### ▶ Recommendation: **A (`profile_deriver` agent)**
It is *just another rainbox agent* writing into the existing schema — no new infra, no new
datastore, no dialectic endpoint. This is the one genuinely new architectural idea worth
importing, and it costs almost nothing because rainbox already is Honcho's architecture.
- **B** taxes every assistant turn for work that belongs in the background.
- **C** imports a whole service and threat surface to replicate a pattern rainbox can express
  natively — rejected.

### Codex: verify
- Confirm the supervisor can schedule a periodic agent (reuse cron, or a low-priority inbox
  drip) without a new scheduling mechanism.
- Confirm `inferred_by_model` evidence kind is the right home for derived conclusions.
- Challenge: is 3.5 worth doing at all for a single operator, or does the Phase 3 one-shot
  summarizer suffice indefinitely? Default to *defer until proven needed*.

---

## Phase 4 — Capability registry + approvals

### Problem
Once the assistant gains power (and especially before write actions), it must not see "all
functions" or "all MCP tools." It needs a small, typed, inspectable catalog of what it can
do — and retrofitting permissions *after* write actions exist is the painful ordering.

### Candidate solutions
**A. Grow the Phase 1 action enum into a formal registry** — each capability declares
read/write/network/secret behavior, confirm-required, timeout/output caps, dry-run support;
the prompt is generated from enabled capabilities. ▶
**B. Full enterprise policy framework up front** (roles, scopes, per-channel ACLs).
**C. No registry** — rely on code review and the sandbox.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| A. enum → formal registry ▶ | H | H | H | H | H | L |
| B. enterprise framework | M | M | L | L | H | L |
| C. no registry | L | M | M | H | L | S |

### ▶ Recommendation: **A (enum → formal registry)**
Because the Phase 1 enum already exists, this is a *formalization*, not a from-scratch build —
it adds metadata and operator visibility around the dispatch gate that's already there. This
is the **capability-poisoning mitigation** (CIK): a prompt/skill/MCP tool cannot widen the
allowed-action set.
- **B** is the governance-first trap v1 explicitly rejected for a single operator — premature
  ceremony.
- **C** is fine *until* write actions exist, then it's unsafe; the whole point of Phase 4
  before Phase 5 is to avoid that.
- `rainbox doctor` (subsystem health) is **new scope here**, not an existing command.

### Codex: verify
- Confirm the Phase 1 enum is structured so metadata can hang off it (not stringly-typed).
- Confirm `workspace_shell`'s existing policy style is the right template for capability flags.
- Challenge: how much of `rainbox doctor` is genuinely Phase 4 vs deferrable? Keep it minimal.
- Verify MCP tool registration can be gated through the same registry rather than bypassing it.

---

## Phase 5 — Controlled write actions

### Problem
A read-only assistant is a smarter search box. To complete real personal workflows it must
*act*: create reminders/cron, update kanban, propose file/document patches, write memory or
skill candidates, run explicitly-enabled MCP tools.

### Candidate solutions — rollout
**A. One write-family at a time**, each with trace + (dry-run or confirm) + rollback/review.
▶
**B. Blanket write enable** once the registry exists.
**C. Always-confirm everything, forever.**

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| A. per-family + dry-run/confirm ▶ | H | H | H | H | H | M/family |
| B. blanket enable | H | M | M | M | L | S |
| C. always-confirm forever | M | H | H | H | H | M |

### ▶ Recommendation: **A (per-family rollout with dry-run/confirm + trace)**
Wins on **Safe** without giving up Value. Cron and kanban families are cheapest (rainbox
already has typed APIs); file/document edits and MCP are riskier and go last. Capabilities
default to *confirm* until the operator deliberately marks one safe-for-unattended.
- **B** trades the whole Phase 4 safety investment for speed — rejected.
- **C** is safe but so high-friction the assistant stops feeling autonomous; confirmation
  should be a per-capability default the operator can relax, not a permanent law.

### Codex: verify
- Confirm cron/kanban/memory APIs already expose typed write paths the assistant can call.
- Confirm a dry-run mode is feasible per family (cron already has dry-run/debug).
- Challenge: which write family delivers the most personal value first? Sequence by that, not
  by implementation ease alone.

---

## Phase 6 — Steerability + runtime visibility

### Problem
Once runs are multi-step and write-capable, the operator needs to *stop* or *redirect* an
in-flight run, compress long rooms, and tell a slow model call apart from a dead process.

### Candidate solutions
**A. Incremental: interrupt-via-new-chat-message + `/stop` + long-call heartbeats + a runtime
dashboard (PID, current step, model, heartbeat age, kill/retry).** ▶
**B. Full preemptive scheduler / supervisor rewrite.**

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| A. incremental controls ▶ | H | H | H | H | H | M/L |
| B. preemptive scheduler | M | M | L | L | M | L |

### ▶ Recommendation: **A (incremental controls)**
Reuses SSE + `LISTEN/NOTIFY` for interrupt, the journal for trace integrity, and the existing
process watchdog for heartbeats. Sequenced **last** because it only matters once runs are long
enough to need stopping.
- **B** rebuilds the supervisor for a problem incremental controls already solve — rejected.

### Codex: verify
- Confirm a new chat message can be injected into an in-flight run via `LISTEN/NOTIFY`
  without corrupting the step trace.
- Confirm the 60s heartbeat watchdog can be made progress-aware (distinguish slow-but-alive
  from hung) rather than a blunt timer.
- Challenge: is `/compress` an LLM summarizer or a cheaper truncation+pin strategy? Pick by
  cost vs fidelity.

---

## Cross-cutting decisions (apply to every phase)

- **Model proposes, code enforces.** A model may propose actions and skills; the action enum /
  registry decides what actually runs, and the operator activates skills.
- **Facts in Postgres, skills in files.** Don't regress fact memory to flat `MEMORY.md`; do
  use files for skills (portability/inspectability).
- **Every tool/memory-using answer has an inspectable trace.** The North-star contract:
  "what did it just do, with what, and why?" answerable from persisted state.
- **Evals gate changes** to retrieval, skills, and the assistant prompt — no judging by vibes.
- **Bound everything** by step count, timeout, and capability policy.

## What this roadmap deliberately rejects
- Cloning Hermes' 20-platform gateway or 6 execution backends.
- A managed/cloud memory API or external memory service (mem0/supermemory/honcho as
  dependencies) — local-first, own the impl.
- Letting MCP tools or model-written skills silently expand authority.
- Front-loading an enterprise governance UI before there's behavior worth governing.

---

## Consolidated Codex verification checklist

Codex should validate these against the actual codebase and challenge any pick whose
assumptions don't hold. Flag each as **confirmed / wrong / needs-change**.

1. **Loop shape (Phase 1):** can `FunctionAgent`/structured-output be driven one step at a
   time with per-step journaling, or is a hand-rolled loop cheaper? Is candidate **A** still
   best, or does **B (tool-via-code)** deserve to be first given the developer-operator?
2. **Trace storage (Phase 1):** is `journal.result` JSON sufficient for v1, and what's the
   concrete trigger to split into `assistant_run`/`assistant_step`?
3. **Action set (Phase 1):** is the read-only enum the right minimum for a *useful* skeleton?
4. **Skills (Phase 2):** does `<customize.dir>` overlay support a `skills/` subtree? Sidecar
   metadata vs DB row — which fits the operator's review flow?
5. **Retrieval (Phase 3):** reuse the Q&A embedding path for claims? `tsvector` vs BM25 for
   the lexical signal? Are `scope`/`sensitivity`/`expiry` filterable pre-rank?
6. **Profile (Phase 3 / 3.5):** is the one-shot summarizer enough, or is the async deriver
   worth it for one operator?
7. **Registry (Phase 4):** is the Phase 1 enum structured to carry metadata? How minimal can
   `rainbox doctor` be?
8. **Write actions (Phase 5):** which families already have typed + dry-run APIs? Which family
   is highest personal value first?
9. **Steerability (Phase 6):** can `LISTEN/NOTIFY` inject mid-run safely? Can the watchdog be
   made progress-aware?
10. **Sizing:** are the S/M/L costs realistic against the real plumbing, or optimistic?

For each, Codex should either confirm the **▶ RECOMMENDED** candidate, or propose a stronger
one with the metric(s) that justify the change.

---

## Sources
See [`2026-06-19-improvements-v1-brainstorm.md`](2026-06-19-improvements-v1-brainstorm.md)
for the full comparison and citations (Hermes Agent, the OpenClaw CIK paper, and the
mem0 / supermemory / honcho reviews).
