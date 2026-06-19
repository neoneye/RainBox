# Rainbox improvements v2 - phased roadmap with candidate analysis (2026-06-19)

**Status:** decision roadmap. This file turns the v1 brainstorm into an
implementation sequence. Each phase states the problem, compares candidate
solutions, scores them with the same metric set, and names the recommended
choice. Weaker candidates are kept on purpose so the tradeoffs stay visible.

**Background:** the comparison and reasoning that produced this roadmap live in
[`2026-06-19-improvements-v1-brainstorm.md`](2026-06-19-improvements-v1-brainstorm.md):
rainbox vs Hermes Agent, the OpenClaw CIK security analysis, and the mem0 /
supermemory / honcho memory review. v2 is the actionable roadmap; v1 is the
evidence and debate log.

**Goal:** rainbox should become a durable local personal assistant whose
implementation the operator understands. It should borrow useful ideas from
Hermes, OpenClaw, mem0, supermemory, and honcho, but it should not become any of
those systems.

---

## Current implementation facts this roadmap must respect

These are the constraints verified against the current codebase and folded into
the phase choices:

- `workspace_shell` is not a Python/script sandbox. It runs validated argv with
  `shell=False`, and its allowlist deliberately excludes interpreters, mutation
  tools, and network tools. A future tool-via-code runner would be new scope,
  even if it reuses the same policy style.
- `journal.result` is a `Text` column and the base `Agent.run()` updates it only
  when `handle()` returns or raises. A kill-safe assistant trace therefore
  requires explicit per-step persistence from inside the assistant loop.
- `QueryAgent` already has a pgvector Q&A path plus read-only dynamic handlers
  such as git status. A useful first assistant should reuse that instead of
  trying to make `workspace_shell` do everything.
- Memory already has the right core shape: `MemoryClaim` includes scope,
  status, sensitivity, expiry, structured subject/predicate/object fields, and
  provenance via `MemoryEvidence`. Retrieval is the weak part: current fact
  retrieval is deterministic token overlap.
- `<customize.dir>` overlay loading exists for specific things like `mcp.json`
  and Q&A data, but there is no generic skills subtree loader yet.
- Chat SSE / Postgres `LISTEN/NOTIFY` currently pushes chat changes to browsers.
  It is useful for UI updates, but it is not by itself an interrupt channel into
  an already-running agent.

---

## How candidates are scored

Every candidate is rated on the same six metrics. Higher is better except
**Cost**.

| Metric | Meaning |
|---|---|
| **Value** | How much it advances the personal-assistant goal: multi-step work, learning, memory, useful action. |
| **Fit** | Fit with rainbox's architecture: durable Postgres queue, local child processes, Flask UI, local-first operation. |
| **Legible** | How easily the operator can inspect and understand the implementation. |
| **Reuse** | How much confirmed code can be reused now. If a loader/runner/control path is net-new, the score reflects that. |
| **Safe** | Reversibility, bounded blast radius, and resistance to CIK-style capability/knowledge poisoning. |
| **Cost** | Rough implementation effort. S = small focused PR or two; M = several PRs; L = multi-week control plane or broad surface. |

Ratings are H / M / L. The winner is not the candidate with the highest score
on every axis; it is the best balance for this operator and goal.

---

## Phase order at a glance

| # | Phase | Core problem it solves | Cost |
|---|---|---|---|
| 0 | Eval and acceptance spine | decisions need regression tests, not vibes | S/M |
| 1 | Assistant walking skeleton | rainbox cannot plan -> act -> observe -> repeat | M/L |
| 2 | Procedural skills MVP | rainbox cannot preserve reusable "how to" knowledge | S/M |
| 3 | Semantic memory + user profile | fact retrieval is weak; no compact user model | M/L |
| 3.5 | Optional async profile deriver | profile may go stale or miss inferred conclusions | M/L |
| 4 | Capability registry + approvals | assistant power must be bounded before writes | L |
| 5 | Controlled write actions | assistant can read and reason but cannot act | M per family |
| 6 | Steerability + runtime visibility | long runs need stop, redirect, progress, and recovery | M/L |

Ordering rationale:

- The eval spine starts first, but trace-specific cases co-develop with Phase 1
  because the assistant trace does not exist yet.
- The loop must exist before skills, memory, registry, or writes have somewhere
  to plug in.
- Skills can start with lexical retrieval, then ride the Phase 3 semantic
  upgrade. Building two retrievers would waste effort.
- The primitive Phase 1 action enum is the seed of the Phase 4 registry. The
  formal registry lands before write actions so permissions are not retrofitted
  onto a mutating assistant.
- Steerability matters most once runs are long, write-capable, and worth
  interrupting.

---

## Existing leverage inventory

| Phase | Reuse now | Net-new work |
|---|---|---|
| 0 | existing pytest style, fake model patterns, retrieval telemetry, feedback/eval hooks | assistant-specific fake-model fixtures and acceptance cases |
| 1 | chat enqueue path, agent config/dispatch, structured output patterns, memory retrieval, QueryAgent handlers, workspace command policy | assistant loop, action enum, explicit step trace writer |
| 2 | `<customize.dir>` overlay pattern, markdown/operator workflow, retrieval telemetry pattern | skills loader, status metadata, candidate review flow |
| 3 | Q&A pgvector/embedding path, MemoryClaim schema, RetrievalEvent telemetry | memory embeddings, merged ranking, profile prompt block |
| 3.5 | normal agent process model, `inferred_by_model` evidence kind | schedule/drip mechanism, derivation prompts, dedupe/conflict policy |
| 4 | Phase 1 action enum, workspace_shell policy style, kanban authority metadata | formal registry metadata, generated prompt/tool catalog, minimal doctor |
| 5 | cron/kanban/memory/document/MCP code surfaces | per-family dry-run/confirm adapters and rollback/review UX |
| 6 | heartbeat, journal, process watchdog, chat UI/SSE for display | agent-visible stop/control channel and progress-aware runtime state |

---

## Phase 0 - Eval and acceptance spine

### Problem

The roadmap makes subjective claims: retrieval is better, traces are inspectable,
write actions are safe, skills are not injected before activation. Without
focused evals and fake-model tests, those claims will drift as prompts and
tools change.

Phase 0 should not pretend to test features that do not exist yet. Some cases
can be written cold against existing agents; trace-specific cases must be
created alongside the assistant skeleton.

### Candidate solutions

**A. Manual checklist only** - rely on the operator to try representative
prompts after each change.

**B. Focused fake-model and retrieval evals** - deterministic tests for loop
control, action dispatch, step caps, forbidden-memory filtering, skill
activation, and trace shape. Co-develop assistant-trace tests with Phase 1.

**C. Full benchmark platform now** - large golden transcript set, grading UI,
and broad regression dashboard before the assistant exists.

**D. Defer evals until after Phase 3** - build behavior first and test later.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| A. manual checklist | L | H | H | H | L | S |
| **B. focused fake-model/retrieval evals** | H | H | H | H | H | S/M |
| C. full benchmark platform | M | M | M | M | H | L |
| D. defer evals | L | H | M | H | L | S |

### Recommendation: B. Focused fake-model and retrieval evals

B gives the assistant work a safety rail without front-loading a large eval
product. It also matches how Phase 1 should be built: fake model outputs drive
the loop deterministically, so failures are control-flow failures rather than
model-quality arguments.

Phase 0 deliverables:

- A small acceptance-case file for existing behavior: memory answer, query/Q&A,
  forbidden secret memory not injected, project-status handler.
- Fake-model fixtures for assistant step sequences. These can start as test
  helpers before the full assistant exists.
- A written rule: every new action family in Phase 5 must add at least one
  dry-run/confirm/trace test.

Done when:

- Existing memory/query behavior has runnable regression tests.
- Phase 1 has a place to add deterministic loop tests before the first real LLM
  call is wired.

---

## Phase 1 - Assistant walking skeleton

### Problem

Rainbox agents are durable child processes, but they are mostly single-turn
workers or fixed pipelines. There is no assistant that can plan, choose a
bounded action, observe the result, and continue for several steps. That loop is
the smallest real personal-assistant primitive.

The loop must preserve rainbox's two core properties:

- **Durability:** a crash or kill should leave a useful trace of the last
  committed step.
- **Inspectability:** the operator can see what the assistant tried, why, which
  action ran, and what observation came back.

### Candidate solutions

**A. ReAct loop over a typed action enum** - rainbox owns the loop. Each step
chooses one action from a small enum, dispatches it, persists the observation,
and repeats until a terminal reply or step cap.

**B. Tool-via-code with a new generated-script runner** - the model writes a
short script that calls rainbox APIs. This produces inspectable artifacts and
can be powerful for a developer-operator, but it requires a new sandboxed runner;
`workspace_shell` cannot run Python.

**C. Let LlamaIndex `FunctionAgent` / AgentWorkflow own the loop** - rainbox
supplies tools and receives a final answer.

**D. Adopt an external agent framework** - LangGraph, CrewAI, or similar as a
spawned child process.

**E. Extend the existing linear pipeline** - add more fixed stages to the
dreamer -> critic -> verifier style routing.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| **A. ReAct + typed enum + explicit trace** | H | H | H | H | H | M/L |
| B. tool-via-code with new runner | H | M | M | L/M | M/L | L |
| C. framework owns loop | M | L | L | H | M | S/M |
| D. external framework | H | L | L | L | M | L |
| E. hardcoded pipeline | L | H | H | H | H | S |

### Recommendation: A. ReAct loop over a typed action enum

A wins on Fit, Legible, and Safe while still creating real assistant behavior.
The important detail is that rainbox owns the loop. It may reuse structured
output helpers or FunctionAgent patterns, but only if the loop can be driven one
step at a time with per-step persistence. If a framework hides the loop, it
loses the main reason to build this inside rainbox.

The Phase 1 enum is also the primitive capability registry. Phase 4 should
formalize this enum with metadata; it should not introduce a separate permission
system from scratch.

Why the others lose:

- **B** is a good future accelerator, especially for a developer-operator, but
  it is not cheap reuse. It needs a new generated-code sandbox/API boundary.
- **C** starts quickly but fights step-level durability and crash recovery if
  the framework owns the async loop.
- **D** conflicts with the goal of understanding and owning the implementation.
- **E** does not create model-directed multi-step work.

### Phase 1 action set

The minimum useful read-only enum should be:

- `reply` - terminal answer to the user.
- `ask_clarifying_question` - terminal request for missing input.
- `query_memory` - current memory retrieval path.
- `query_qa` - reuse QueryAgent's exact/semantic Q&A and dynamic handlers,
  including project status handlers such as git status.
- `workspace_read_command` - reuse the workspace command policy for safe file
  inspection commands only. Do not expect it to run git, Python, shell syntax,
  mutation, or network commands.
- `kanban_read` - read board/card state without writing events.

This set is intentionally small, but it should be useful on day one. `query_qa`
prevents the assistant from feeling weaker than the existing QueryAgent.

### Phase 1 trace storage

Use Postgres from the beginning, but do not add assistant-specific tables yet.

Recommended v1 trace shape:

- Persist a `debug-assistant` chat row for each step transition:
  `planned`, `running`, `observed`, `failed`, or `final`.
- Store a final JSON string summary in `journal.result` when the agent exits.
- Commit the step trace before the action starts and after the observation is
  available. If the process is killed mid-action, the operator should at least
  see which step/action was in progress.

Do not rely on `journal.result` alone for mid-run durability. It is a text
column and the base agent only writes it when `handle()` exits.

Pre-agreed trigger for `assistant_run` / `assistant_step` tables:

- The operator needs to query/filter traces by action or status.
- Resume-from-step becomes real, not just inspect-last-step.
- Trace rows need foreign keys to action outputs, files, or approval records.

Until then, `debug-assistant` plus final `journal.result` is simpler.

### First PR scope

Build:

- Add an `assistant` role/UUID and dispatch class.
- Add a bounded loop with max 4-6 steps.
- Add the read-only action enum above.
- Add explicit per-step trace persistence.
- Use fake model outputs for deterministic tests before any live LLM behavior.

Done when:

- One user message causes at least two model/action iterations.
- The full step trace is visible in chat or Flask-Admin.
- Killing the process mid-run leaves the last committed step visible, even if
  the current observation is missing.
- Tests cover step cap, dispatch, terminal reply, failed action, and trace
  shape.

Out of scope:

- Write actions.
- MCP tools.
- Skills.
- Generated-code execution.
- Formal registry UI.

---

## Phase 2 - Procedural skills MVP

### Problem

Rainbox has declarative memory: facts, preferences, decisions, and provenance.
It does not yet have procedural memory: "when solving this kind of task, follow
these steps." Hermes-style systems get value from reusable skills, but silent
self-modification is a knowledge-poisoning risk. A model must not be able to
inject unreviewed behavior into its own future prompts.

### Candidate solutions - storage

**A. Markdown files in a skills directory** - portable, inspectable, diffable.

**B. `skill` table in Postgres** - queryable and consistent with memory
provenance, but less pleasant for a developer to edit.

**C. Markdown files plus thin metadata** - markdown content with frontmatter or
sidecar JSON for status, provenance, supersession, and source journal id.

### Candidate solutions - lifecycle

**A. Human-authored active; model-proposed candidate -> operator activates,
edits, rejects, or supersedes.**

**B. Auto-activate all model-written skills.**

**C. Auto-activate only sandboxed/dry-run skills after tests pass.**

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| Storage A. files only | M | H | H | M | M | S |
| Storage B. table | M | H | M | H | M | M |
| **Storage C. files + thin metadata** | H | H | H | M | H | S/M |
| **Lifecycle A. candidate -> active** | H | H | H | H | H | S |
| Lifecycle B. auto-active all | H | M | L | M | L | S |
| Lifecycle C. auto-active after tests | H | M | M | M | M | M |

### Recommendation: files plus thin metadata, candidate -> active lifecycle

Use markdown for the skill body and frontmatter or sidecar JSON for metadata:

- `status`: `candidate`, `active`, `superseded`, `rejected`
- `source_journal_id` / `source_step_id`
- `created_by`: `human` or `assistant`
- `supersedes`
- optional smoke-test command or eval case

Only `active` skills are eligible for prompt injection. Model-written skills
start as `candidate`, and activation is an operator decision.

This is a small new loader, not free reuse. The existing `<customize.dir>`
overlay pattern justifies the file location, but the skills subtree and metadata
reader still need to be built.

### Retrieval note

Start with lexical/token-overlap skill retrieval. Upgrade facts and skills
together in Phase 3. Do not build a second semantic retriever just for skills.

Done when:

- Active skills from the base directory and `<customize.dir>/skills/` can be
  loaded.
- Candidate skills are visible but never injected into the assistant prompt.
- A fake-model test proves an unactivated model-written skill cannot influence a
  later answer.
- Retrieval telemetry records which active skills were considered/injected.

---

## Phase 3 - Semantic memory and user profile

### Problem

The memory schema is stronger than the retrieval path. Facts are scoped,
versioned, provenance-backed, and filterable by sensitivity and expiry, but
retrieval is token-overlap only. The assistant also lacks a compact user model:
stable preferences, current projects, personal constraints, and recent context.

The mem0/supermemory lesson is not "buy a memory service." It is: use hybrid
retrieval, filter before ranking, track source/provenance, and make memory use
auditable.

### Candidate solutions - retrieval

**A. Pure vector retrieval** - embed memory claims and retrieve by cosine
similarity.

**B. Multi-signal retrieval** - pre-rank filters, then vector similarity plus
lexical/full-text signal plus structured subject/object/entity match, merged
into one score.

**C. LLM rerank** - retrieve a candidate set, then ask a model to rerank or
filter.

**D. Keep token-overlap** - do nothing.

### Candidate solutions - user profile

**A. One-shot profile summarizer** - build a compact prompt block from active
memory claims and recent context.

**B. Async `profile_deriver` agent** - background process that derives and
refreshes inferred claims. See Phase 3.5.

**C. Adopt Honcho dialectic API** - add an external service for persona/profile
reasoning.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| Retrieval A. pure vector | M | H | M | H | M | S/M |
| **Retrieval B. multi-signal + pre-rank filters** | H | H | M | H | H | M/L |
| Retrieval C. LLM rerank | H | M | L | M | M | M |
| Retrieval D. token-overlap baseline | L | H | H | H | M | none |
| **Profile A. one-shot summarizer** | H | H | H | H | H | S/M |
| Profile B. async deriver | H | H | M | M | H | M/L |
| Profile C. Honcho service | M | L | L | L | M | L |

### Recommendation: multi-signal retrieval, one-shot profile summarizer

Retrieval B is the right first semantic upgrade:

- Apply `status`, `scope`, `sensitivity`, and `expires_at` filters before any
  ranking. Forbidden claims must never enter the candidate set.
- Reuse the existing Q&A embedding/pgvector path rather than adding a second
  embedding stack.
- Add memory embeddings for active claims. This can be a new table or column;
  choose the smallest schema that keeps rebuilds and provenance clear.
- Prefer Postgres full-text search (`tsvector`) for the lexical signal before
  adding a BM25 dependency. Add BM25 only if evals prove native full text is not
  enough.
- Use structured `subject`, `predicate`, and `object` fields for exact/entity
  boosts.
- Record retrieval telemetry for retrieved and injected claims.

Profile A is the right first user-model step. It should produce a compact block
from already-active memories and recent context, with source references. It
should not invent durable inferred facts unless the operator confirms them or
Phase 3.5 is explicitly built.

Why the others lose:

- **Pure vector** increases recall but loses exact-term precision and can hide
  why something matched.
- **LLM rerank** can be useful later, but it adds latency and reduces
  determinism before the cheaper signals are exhausted.
- **Honcho service** is the wrong dependency for a local personal assistant;
  rainbox already has the database/worker shape needed to copy the useful
  pattern locally.

### What these systems still get wrong (design pressure)

A cross-model review (mem0 / supermemory / honcho) confirms none of them fully
solves a few problems. These are not reasons to avoid the ideas; they are reasons
rainbox's existing choices are the right ones, and each maps to a Phase 3
acceptance test:

- **Extraction can hallucinate or capture noise.** Mitigation rainbox already
  has: provenance via `MemoryEvidence`, plus candidate-before-active so a derived
  or extracted fact is reviewable, not silently trusted.
- **Retrieval can return stale or contradictory context** under heavy or long
  histories. Mitigation: pre-rank expiry/scope/sensitivity filters, plus
  detect-and-surface contradictions instead of silent merges.
- **Governance and right-to-be-forgotten usually need manual work** in these
  tools. This is a rainbox strength, not a gap: `forget`/`correct`, sensitivity,
  scope, and expiry are first-class in the schema - keep them enforced in
  retrieval, not just at write time.
- **Deep causal/temporal "what-if" reasoning is out of scope** for all three. Do
  not expect the profile or retrieval layer to do it.

Net: the shared limitations argue for rainbox's provenance-first,
human-in-the-loop, filter-before-rank design - which Phase 3 already encodes. Add
one eval per bullet where it is testable: no unactivated extracted fact
influences an answer; expired or out-of-scope claims never surface; a flagged
contradiction is surfaced rather than silently resolved.

### Contradiction handling

Split detection from mutation:

- Phase 3 may detect and surface conflicts, such as "lives in NYC" vs "moved to
  SF." This is read-only and useful.
- Auto-supersede is a write action and belongs in Phase 5 with dry-run/confirm.

Done when:

- Eval cases show improved recall over token-overlap.
- Eval cases prove secret/expired/out-of-scope claims do not surface.
- The assistant can explain which memory/profile facts were injected.
- The profile block improves a real task without becoming a hidden prompt blob.

---

## Phase 3.5 - Optional async profile deriver

### Problem

A one-shot profile may go stale and may miss useful inferred conclusions about
the operator. Honcho's useful idea here is not the external API; it is the
background deriver pattern: periodically read recent interactions and derive
candidate profile facts.

This phase is optional. Do it only after Phase 3 proves that the one-shot
profile is insufficient.

### Candidate solutions

**A. `profile_deriver` rainbox agent** - a normal agent periodically reads
recent chat/journal rows and proposes `inferred_by_model` claims or refreshed
profile summaries.

**B. Inline derivation inside every assistant turn** - simpler control flow, but
adds latency and cost to every interaction.

**C. Adopt Honcho as a service** - external profile/memory service.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| **A. `profile_deriver` agent** | H | H | M | M | H | M/L |
| B. inline derivation | M | M | M | H | M | S/M |
| C. Honcho service | H | L | L | L | M | L |

### Recommendation: defer by default; if needed, build A

A fits rainbox best because it is just another local agent writing into the
existing memory/evidence model. But it is not "almost free." It needs a schedule
or drip mechanism, prompts, source attribution, dedupe, conflict handling,
operator review, and evals.

Use `inferred_by_model` evidence for derived conclusions, and keep them as
candidate or low-confidence facts unless confirmed.

Done when:

- The deriver can run without slowing normal assistant turns.
- Every inferred claim links back to chat/journal evidence.
- The operator can reject or supersede bad inferred claims.
- Evals show the deriver helps enough to justify the extra moving part.

---

## Phase 4 - Capability registry and approvals

### Problem

Before the assistant can write, call MCP tools, or execute generated code, its
power must be inspectable and bounded. A prompt, skill, or MCP server must not
be able to widen the allowed action set. This is the OpenClaw CIK "capability"
lesson applied locally.

### Candidate solutions

**A. Grow the Phase 1 enum into a formal registry** - each capability declares
metadata: read/write/network/secrets behavior, confirmation requirement, dry-run
support, timeout, output cap, argument validator, enabled state, and prompt
description.

**B. Full enterprise policy framework** - roles, scopes, ACLs, per-channel
policy, large UI.

**C. No registry** - rely on code review, existing sandboxes, and the operator's
trust.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| **A. enum -> formal registry** | H | H | H | H | H | L |
| B. enterprise framework | M | M | L | L | H | L |
| C. no registry | L | M | M | H | L | S |

### Recommendation: A. Enum -> formal registry

The registry begins in Phase 1 as the primitive action enum. Phase 4 formalizes
it with metadata and operator visibility.

Useful existing patterns:

- `workspace_shell` has explicit policy/validator style.
- `AgentConfigEntry` already has kanban authority and verified/unverified
  concepts.
- MCP config loading already centralizes tool discovery enough to gate it.

Registry fields should be boring and explicit:

- name and family
- read/write/network/secrets flags
- confirm-required default
- dry-run support
- timeout and output cap
- validator/dispatcher function
- whether it is exposed to the assistant prompt
- docs/prompt description

`rainbox doctor` belongs here only in minimal form: parse configs, list enabled
capabilities, show missing model/embedding/MCP prerequisites, and report stale
or invalid skill metadata. A polished subsystem-health UI is later scope.

Done when:

- Disabling a capability removes it from both the prompt and dispatch path.
- MCP tools cannot bypass the registry.
- Write-capable actions cannot be added without metadata and tests.
- The operator can inspect the currently enabled assistant powers.

---

## Phase 5 - Controlled write actions

### Problem

A read-only assistant is a better search and reasoning interface, but it cannot
complete workflows. To become a useful personal assistant, rainbox needs
controlled writes: reminders, kanban changes, memory/skill candidates, file or
document patches, and selected MCP tools.

The challenge is to add power without making a single blanket "the assistant
can mutate everything" switch.

### Candidate solutions

**A. One write family at a time** - every family gets trace, dry-run or confirm,
rollback/review where possible, and registry metadata.

**B. Blanket write enable after the registry exists** - all registered writes
become available at once.

**C. Always-confirm everything forever** - safe, but high-friction and not
really assistant-like.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| **A. per-family rollout with dry-run/confirm** | H | H | H | H | H | M per family |
| B. blanket write enable | H | M | M | M | L | S |
| C. always-confirm forever | M | H | H | H | H | M |

### Recommendation: A. Per-family rollout

Recommended order:

1. **Memory and skill candidates** - high personal-assistant value, low blast
   radius if they remain candidates until confirmation. Note: writing *inert
   candidates* is allowed earlier (Phase 2 stores candidate skills, Phase 3.5
   proposes inferred claims) precisely because they cannot affect behavior until
   activated. Phase 5 is where the assistant gains the *activation/confirmation*
   write path, not the first place a candidate row is created.
2. **Kanban work events** - existing typed APIs and review semantics make this
   a good bounded write family.
3. **Cron/reminders** - strong personal value, but require careful dry-run,
   confirmation, and visible audit trail.
4. **File/document patch proposals** - start with proposed patches, not silent
   file writes.
5. **MCP tools** - last, one server/tool at a time, because the surface is
   externally supplied and easy to over-grant.

Each write family must define:

- dry-run output or confirmation text
- exact persisted trace shape
- failure behavior
- rollback or review path where possible
- unattended eligibility default, which should start as false

Done when:

- The assistant can perform at least one useful write family end to end.
- The operator sees the planned write before it runs unless that capability was
  explicitly approved for unattended use.
- The registry enforces the same policy the prompt describes.
- Downvotes or failed confirmations can become eval cases.

---

## Phase 6 - Steerability and runtime visibility

### Problem

Once runs are multi-step and write-capable, the operator needs to stop,
redirect, inspect, or retry in-flight work. A slow model call should look
different from a dead process. A long run should not corrupt its trace if
stopped.

### Candidate solutions

**A. Incremental controls** - `/stop`, interrupt/redirect between steps,
progress-aware heartbeats, runtime dashboard, kill/retry controls.

**B. Supervisor rewrite / preemptive scheduler** - redesign process control
around in-flight preemption.

### Scoring

| Candidate | Value | Fit | Legible | Reuse | Safe | Cost |
|---|---|---|---|---|---|---|
| **A. incremental controls** | H | H | H | M | H | M/L |
| B. supervisor rewrite | M | M | L | L | M | L |

### Recommendation: A. Incremental controls

Do not describe existing SSE as the interrupt mechanism. Current SSE /
`LISTEN/NOTIFY` is a browser update path. The assistant needs an agent-visible
control path:

- `/stop` writes a control row or flag for the active assistant run.
- The assistant checks for control messages between steps and before starting
  long actions.
- For long model/tool calls, the supervisor can still kill the child as a blunt
  fallback, but the normal path should stop at step boundaries and persist a
  clean trace state.
- A redirect can be represented as a new user message/control row that the loop
  consumes before the next step.
- SSE remains useful for displaying progress/control changes in the UI.

Progress-aware heartbeat should include enough state to distinguish:

- waiting on model
- running action
- persisting observation
- stopped by operator
- hung/no heartbeat

Done when:

- A run can be stopped without losing the trace.
- A new instruction can redirect the next step without corrupting prior steps.
- The dashboard shows PID, journal id, current step, current action/model, last
  heartbeat age, and stop/kill/retry controls.
- The watchdog no longer treats all long calls as identical silence.

---

## Cross-cutting decisions

- **Model proposes, code enforces.** Prompts, skills, and MCP tools can suggest
  actions, but only registered code paths can execute.
- **Primitive registry early, formal registry later.** Phase 1's enum is the
  first capability boundary. Phase 4 adds metadata, UI, and MCP/write gating.
- **Facts in Postgres, skills in files.** Declarative memory stays in the
  provenance-first schema. Procedural knowledge should be diffable and editable
  as files.
- **Every answer that used tools or memory needs a trace.** The operator should
  be able to answer: what did it do, with which capability, using which memory
  or skill, and why?
- **Candidate before active.** Model-written skills, inferred profile facts, and
  memory corrections start as candidates unless the operator explicitly confirms
  them or a later policy allows a narrow unattended path.
- **Bound everything.** Step count, timeout, output length, retrieved memories,
  injected skills, and enabled capabilities all need caps.
- **Evals gate risky changes.** Retrieval, skills, registry policy, and write
  families must add regression tests before becoming default behavior.

---

## What this roadmap deliberately rejects

- Cloning Hermes' many platform integrations or execution backends.
- Depending on hosted memory products such as mem0, supermemory, or honcho.
- Letting MCP tools or model-written skills silently expand authority.
- Treating `workspace_shell` as a generic code sandbox.
- Adding a large governance UI before there is assistant behavior worth
  governing.
- Making a framework own the core assistant loop in a way rainbox cannot inspect
  or resume.

---

## First implementation slice

The first code slice should be Phase 0 plus the smallest useful Phase 1:

1. Add deterministic fake-model tests for loop control and trace persistence.
2. Add the `assistant` role and bounded loop.
3. Add read-only actions: `reply`, `ask_clarifying_question`, `query_memory`,
   `query_qa`, `workspace_read_command`, and `kanban_read`.
4. Persist each step as `debug-assistant` before/after action execution.
5. Use `journal.result` only as the final summary, not as the only trace store.
6. Prove a killed run leaves the current committed step visible.

This slice gives rainbox a real assistant skeleton without write risk, without a
new generated-code sandbox, and without pretending the later control plane is
already built.

---

## Verification checklist

Use this checklist while implementing. Each item should become confirmed,
changed, or deleted as code lands.

1. **Loop ownership:** the assistant loop is owned by rainbox; any framework use
   is step-at-a-time and traceable.
2. **Trace persistence:** per-step state is committed during `handle()`, not only
   returned through `journal.result`.
3. **Action set:** `query_qa` reuses QueryAgent behavior so Phase 1 can answer
   project-status and Q&A questions.
4. **Workspace command limits:** `workspace_read_command` remains a validated
   argv reader, not a Python/git/shell runner.
5. **Skills:** `<customize.dir>/skills/` is implemented as a new loader using
   the existing overlay pattern.
6. **Retrieval:** memory embeddings reuse the existing Q&A embedding stack, and
   forbidden claims are filtered before ranking.
7. **Profile:** Phase 3 starts with a one-shot profile block; Phase 3.5 stays
   optional until staleness/inference gaps are observed.
8. **Registry:** Phase 1's enum can grow metadata without stringly-typed drift.
9. **Writes:** each write family has dry-run/confirm, trace, tests, and registry
   metadata before it is exposed.
10. **Steerability:** interrupts use an agent-visible control path; SSE remains
    the browser update mechanism.

---

## Sources

See [`2026-06-19-improvements-v1-brainstorm.md`](2026-06-19-improvements-v1-brainstorm.md)
for the broader comparison and citations.
