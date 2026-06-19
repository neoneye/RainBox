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

## Executive decision summary

The roadmap has one recommended path:

- Build a rainbox-owned ReAct loop first, not tool-via-code and not an external
  framework-owned loop.
- Persist assistant steps during the loop, before and after actions. Final
  `journal.result` is a summary, not the trace.
- Treat the Phase 1 action enum as the primitive capability registry; Phase 4
  adds metadata and operator controls.
- Store skills as editable files plus small status/provenance metadata. Only
  active skills are injected.
- Upgrade memory with hybrid retrieval and pre-rank filters before adding
  heavier inference or rerank machinery.
- Add write actions one family at a time, with dry-run/confirm, trace, and
  registry metadata.
- Add stop/redirect through an agent-visible control path, not by pretending the
  existing browser SSE path can interrupt running agents.

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
- Memory already has the right core shape: `MemoryClaim` includes `scope`,
  `status`, `sensitivity`, `expires_at`, structured `subject`/`predicate`/`object`
  fields, and provenance via `MemoryEvidence`. (Verified column names, so the
  spec uses them verbatim: `status` is one of
  `candidate`/`active`/`superseded`/`rejected`/`expired`; `scope` is
  `global`/`agent`/`room`/`project`; `sensitivity` is `public`/`private`/`secret`.)
  Retrieval is the weak part: current fact retrieval is deterministic token
  overlap.
- `<customize.dir>` overlay loading exists for specific things like `mcp.json`
  and Q&A data, but there is no generic skills subtree loader yet.
- Chat SSE / Postgres `LISTEN/NOTIFY` currently pushes chat changes to browsers.
  It is useful for UI updates, but it is not by itself an interrupt channel into
  an already-running agent.

---

## Assistant contracts

These contracts are stronger than any individual implementation choice. If a
future phase violates one, the phase should be redesigned rather than patched
with prompt text.

| Contract | Meaning | First proven |
|---|---|---|
| **Authority is code-owned** | The model can request only capabilities exposed by code. Skills, memory, prompts, and MCP servers cannot expand the action set. | Phase 1 |
| **Trace before action** | Before a tool/action starts, the assistant commits the planned step and arguments. After it finishes, it commits the observation. | Phase 1 |
| **Candidates are inert** | Model-created skills, inferred facts, corrections, and plans do not affect future behavior until active/confirmed. | Phase 2 |
| **Filter before rank** | Secret, expired, rejected, or out-of-scope memories are removed before vector/full-text ranking. | Phase 3 |
| **Every influence is explainable** | An answer that used a memory, skill, tool, profile fact, or write approval can point to the persisted row/file/step that influenced it. | Phase 3 |
| **Writes are family-scoped** | No blanket mutation switch. Each write family owns its validator, dry-run/confirm path, trace shape, and tests. | Phase 5 |
| **Stop is stateful** | A stopped run records why and where it stopped; it is not just a killed process with missing context. | Phase 6 |

These are the "personal assistant but understandable" guardrails. The roadmap
can change phase boundaries, but these contracts should remain stable.

**These contracts are the single source of truth for invariants.** The phase
gates, per-phase "Done when" lists, decision ledger, and verification checklist
restate slices of them for convenience; if any of those drifts from a contract,
the contract wins. Likewise, every *concrete shape* later in this doc - the
`assistant_step` JSON, the skill frontmatter, the registry record, the approval
and run state machines - is an **illustrative starting point, not a binding
schema**. Field names and the `.v1` tags are sketches; expect the first PR to
refine them. A shape may change freely as long as it still satisfies the
contracts above.

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

## Phase gates

Each phase should leave behind one hard artifact, not just a prose decision.

| Phase | Hard artifact | Gate before moving on |
|---|---|---|
| 0 | deterministic eval/fake-model harness | a regression fails if trace shape, forbidden-memory filtering, or action dispatch breaks |
| 1 | assistant role with read-only loop | a killed run leaves the last committed step visible |
| 2 | skills loader and candidate lifecycle | an unactivated model-written skill cannot enter the assistant prompt |
| 3 | hybrid memory/profile context builder | secret/expired/out-of-scope claims are filtered before ranking and tested |
| 3.5 | optional deriver agent | every inferred claim has evidence and can be rejected |
| 4 | formal capability registry | disabling a capability removes it from both prompt and dispatch |
| 5 | first write family | dry-run/confirm and trace are enforced by code, not prompt discipline |
| 6 | control channel and runtime view | `/stop` leaves a clean stopped trace, not just a dead child process |

If a phase cannot meet its gate cheaply, split it. Do not continue by weakening
the contract.

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

### Minimum eval catalog

| Case | Purpose | Can be written before Phase 1? |
|---|---|---|
| memory exact answer | protects current `remember` / `recall` behavior | yes |
| forbidden secret memory | proves sensitivity filtering is enforced before prompt injection | yes |
| query project status | proves QueryAgent handler reuse remains available | yes |
| two-step assistant trace | proves model -> action -> observation -> reply loop shape | no, co-develop with Phase 1 |
| step cap | proves infinite loops stop deterministically | no, co-develop with Phase 1 |
| failed action trace | proves errors are persisted and visible | no, co-develop with Phase 1 |
| unactivated skill | proves candidate skills are inert | no, Phase 2 |
| hybrid retrieval regression | proves semantic retrieval improves recall without leaking forbidden claims | no, Phase 3 |
| write dry-run/confirm | proves a write family cannot mutate silently | no, Phase 5 |

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

The `debug-assistant` JSON payload should be intentionally stable:

```json
{
  "schema": "assistant_step.v1",
  "journal_id": 123,
  "step_index": 2,
  "status": "running",
  "action": "query_qa",
  "args": {"query": "git status"},
  "model": {"group_uuid": "...", "model_uuid": "..."},
  "observation_preview": null,
  "error": null
}
```

Rules:

- `planned` or `running` is committed before dispatch.
- `observed`, `failed`, or `final` is committed after dispatch or final answer.
- Arguments are redacted before persistence if a future action can carry
  secrets.
- The final journal summary links back to step indexes rather than duplicating
  every observation.

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

Preferred file shape:

```markdown
---
id: summarize-pr-review
status: active
created_by: human
source_journal_id:
source_step_id:
supersedes:
retrieval_tags: [github, review, pull-request]
---

# Summarize a PR review

Use when the operator asks for a review summary. First list blocking findings,
then open questions, then a short change summary.
```

Use frontmatter unless queryability becomes painful. If the operator needs a
review dashboard with filtering/sorting across many skills, add a DB index row
later; do not start there.

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

Retrieval pipeline contract:

1. Build a query from the latest user request plus a small amount of room/task
   context.
2. Apply hard filters: active status, allowed scope, non-expired, allowed
   sensitivity.
3. Score candidates with vector similarity, full-text/lexical match, and
   structured subject/object boosts.
4. Merge scores into a small candidate list with reasons attached.
5. Apply budget caps before prompt injection.
6. Record retrieved and injected claims in telemetry.
7. Render the prompt block with provenance tags, not just raw facts.

The output should be auditable enough that a bad answer can be traced to either
retrieval, ranking, prompt injection, or model reasoning.

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

Registry record shape:

```json
{
  "name": "query_qa",
  "family": "query",
  "description": "Answer from the Q&A registry and read-only dynamic handlers.",
  "read": true,
  "write": false,
  "network": false,
  "secrets": false,
  "confirm_required": false,
  "dry_run": false,
  "timeout_seconds": 10,
  "output_cap_chars": 6000,
  "enabled": true,
  "prompt_exposed": true
}
```

Keep two concepts separate: `family` is the **grouping** a capability belongs to
(`query`, `memory`, `kanban`, `cron`, `workspace`, `document`, `mcp`) and is the
same word Phase 5 uses for write-family rollout; the `read`/`write`/`network`/
`secrets` booleans are the **permission flags**. Do not encode permission in the
family value (no `family: "read"`); a read-only query tool and a kanban writer
can share neither field by accident.

The registry is not just UI metadata. Dispatch must reject disabled or unknown
capabilities even if the model emits a valid-looking action name.

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

### Approval state machine

- `proposed` - assistant generated a write intent and dry-run/preview.
- `confirmed` - operator approved this exact intent.
- `executing` - dispatcher is running the write.
- `completed` - write finished and trace links to the result.
- `failed` - write failed with error and no hidden retry.
- `rejected` - operator rejected or edited the proposal.

Do not let the assistant mutate the payload after confirmation. A changed
payload is a new proposal.

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

### Run state machine

- `running` - normal loop execution.
- `stopping` - operator requested stop; loop should finish the current safe
  boundary and stop before the next action.
- `stopped` - clean terminal state with reason and last completed step.
- `failed` - terminal error, with trace and exception summary.
- `killed` - watchdog or operator killed the process; trace may show only the
  last committed `running` step.

`killed` is allowed as an emergency outcome, but the product path should prefer
`stopped`.

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

## Implementation PR stack

The roadmap should begin with a deliberately narrow stack. Each PR should leave
the system shippable; none should require write actions or MCP.

| PR | Scope | Must include | Must not include |
|---|---|---|---|
| 1 | Eval harness | fake-model fixture, existing memory/query regression cases, helper to assert trace events | assistant runtime |
| 2 | Assistant role and loop | `assistant` config/dispatch, bounded loop, `reply`, `ask_clarifying_question`, fake-model tests | live LLM dependency |
| 3 | Step trace persistence | `debug-assistant` schema, before/after action commits, final `journal.result` summary | new assistant tables |
| 4 | Read-only actions | `query_memory`, `query_qa`, `workspace_read_command`, `kanban_read`, dispatch tests | writes, generated code, MCP |
| 5 | Chat/UI visibility | render assistant debug rows clearly enough to inspect plan/action/observation | polished dashboard |
| 6 | Skills MVP | loader, active/candidate lifecycle, frontmatter metadata, lexical retrieval | semantic skill retriever |
| 7 | Hybrid memory retrieval | memory embeddings, hard filters, full-text/entity boosts, telemetry/evals | LLM rerank |
| 8 | Minimal registry | formal metadata over existing action enum, disabled-capability enforcement | approval UI complexity |
| 9 | First write family | memory/skill candidates or kanban events with dry-run/confirm | blanket write enable |
| 10 | Runtime controls | `/stop` control row, step-boundary stop, progress-aware heartbeat | supervisor rewrite |

Recommended first slice:

1. PR 1 proves the test harness can fail for broken existing behavior.
2. PR 2 adds an assistant that can only talk and ask clarifying questions.
3. PR 3 makes the trace durable before any nontrivial tool dispatch exists.
4. PR 4 adds the useful read-only tools.

This order is intentionally conservative: do not add more assistant power until
the trace exists and the tests can prove it.

---

## PR 1-4 concrete spec

This section pins the choices needed to write the first slice without another
planning pass. These are implementation decisions for PRs 1-4; later
phases may revise them only by preserving the assistant contracts above.

### Step-decision schema

Use one structured model for the first loop. Keep action-specific typing inside
the dispatcher for now; do not build a union of per-action Pydantic models until
the action surface grows.

```python
class AssistantActionName(str, Enum):
    REPLY = "reply"
    ASK_CLARIFYING_QUESTION = "ask_clarifying_question"
    QUERY_MEMORY = "query_memory"
    QUERY_QA = "query_qa"
    WORKSPACE_READ_COMMAND = "workspace_read_command"
    KANBAN_READ = "kanban_read"


class AssistantStepDecision(BaseModel):
    reason: str = Field(
        description="Brief operator-facing rationale for this step, not hidden chain-of-thought."
    )
    action: AssistantActionName
    args: dict[str, Any] = Field(default_factory=dict)
```

Validation rule: the loop rejects an action before dispatch if required args are
missing, unknown args are risky, or the action is not in the enum. The first
validator can be a small explicit `match action:` block:

- `reply`: requires `message`.
- `ask_clarifying_question`: requires `question`.
- `query_memory`: requires `query`.
- `query_qa`: requires `query`.
- `workspace_read_command`: requires `command`.
- `kanban_read`: accepts optional `board_uuid`, `task_uuid`, or empty args for
  the current board summary.

Use `reason`, not `thought`, so the model produces a concise audit note rather
than private chain-of-thought. The trace should show this rationale.

### Action interface

Represent actions as ordinary Python callables behind a tiny protocol. The
dispatcher owns validation, timeout, output caps, and trace boundaries; actions
just perform one bounded read and return an observation.

```python
@dataclass(frozen=True)
class AssistantActionContext:
    journal_id: int
    room_uuid: UUID
    agent_uuid: UUID
    step_index: int


@dataclass(frozen=True)
class AssistantObservation:
    ok: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)


AssistantAction = Callable[
    [AssistantActionContext, dict[str, Any]],
    AssistantObservation,
]
```

Dispatcher rules for PR 4:

- Validate args before calling the action.
- Catch exceptions and turn them into `AssistantObservation(ok=False, text=...)`
  after writing a failed trace event.
- Apply the output cap after the action returns. Store a truncated
  `observation_preview` in trace and keep any larger structured data out of the
  prompt unless the action explicitly allows it.
- Do not let actions post final assistant replies directly. Terminal reply
  actions return text to the loop, and the loop posts the final chat message.

### Fake-model seam

The assistant should be a specialized `ModelGroupAgent`, not a
`StructuredLLMAgent`, because it needs multiple structured calls inside one
`handle()`.

The only live-model seam is:

```python
class AssistantAgent(ModelGroupAgent):
    def _decide_next_step(
        self,
        *,
        transcript: str,
        scratchpad: list[dict[str, Any]],
        step_index: int,
    ) -> AssistantStepDecision:
        ...
```

Production `_decide_next_step()` performs the structured-output model call,
using the same model-group fallback style as `StructuredLLMAgent._structured_call`.

Verified-against-code note: `_structured_call` currently lives on
`StructuredLLMAgent` (`agents/base.py:250`), so `AssistantAgent(ModelGroupAgent)`
cannot inherit it. PR 2 should extract the structured-call-with-group-fallback
logic down to `ModelGroupAgent` (or a small shared mixin) and have both
`StructuredLLMAgent.handle()` and `AssistantAgent._decide_next_step()` call it -
not copy-paste it. This refactor is part of PR 2, not a later cleanup.

Tests monkeypatch this method with a scripted provider:

```python
def scripted_decisions(*decisions: AssistantStepDecision):
    queue = list(decisions)

    def fake_decide_next_step(**_kwargs):
        assert queue, "assistant requested more decisions than expected"
        return queue.pop(0)

    return fake_decide_next_step
```

This seam is the linchpin for PR 1 and PR 2: deterministic tests should exercise
the loop, step cap, validation, dispatch, and trace shape without LM Studio,
network, or a live model.

### Agent placement, binding, and enablement

For PRs 1-4:

- Add `assistant` to `agents/config.py` with `requires_structured_output=True`
  and no function-calling requirement.
- Add `AssistantAgent` to `agents/__main__.py` dispatch like the other
  specialized agents.
- Add the assistant UUID to `CHAT_RESPONDER_UUIDS` **in `webapp/chat_api.py`**
  (not `agents/config.py` - that tuple lives in the web layer). Verified
  mechanism: `_maybe_trigger_chat_agents()` enqueues a responder only if it is
  both in `CHAT_RESPONDER_UUIDS` *and* a member of the room, and only when a
  *human* posts. That human-only guard already prevents self-retrigger, so the
  assistant posting its own trace rows and final reply will not loop. Do not make
  it a global singleton agent.
- Leave the default model binding empty. Tests use the fake-model seam; live use
  requires the operator to bind the assistant to a structured-output model group
  through the existing agent-model binding UI.

This keeps enablement local and inspectable: adding the assistant to a room is
the opt-in switch for that room, and model binding stays in the existing
operator-controlled model configuration path.

### Trace write helper

Use append-only chat debug rows for PR 3. The helper belongs in the assistant
module first; move it into `db` only if another subsystem needs it.

```python
def post_assistant_trace(
    *,
    room_uuid: UUID,
    agent_uuid: UUID,
    journal_id: int,
    step_index: int,
    status: Literal["planned", "running", "observed", "failed", "final"],
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    observation_preview: str | None = None,
    error: str | None = None,
) -> ChatMessage:
    ...
```

Implementation rule:

- It calls `db.post_chat_message(..., content_type="json", kind="debug-assistant")`.
- The JSON payload includes `schema`, `journal_id`, `step_index`, `status`,
  `action`, `reason`, `args`, `observation_preview`, and `error`.
- `(journal_id, step_index)` is the logical grouping key for all events in a
  step. It is not a database uniqueness constraint because `planned`,
  `running`, `observed`, and `failed` are separate append-only events.
- Ordering comes from `chat_message.id` / `created_at` (verified: `id` is the
  autoincrement primary key the chat already uses as its ordering cursor).
- Load-bearing detail: keep `kind="debug-assistant"`, **never `"progress"`**.
  `post_chat_message` deletes the sender's own `kind="progress"` rows in the room
  whenever it posts a terminal reply (`kind="message"`/`"notice"`). Trace events
  must survive the final reply, so they must not use the auto-reaped `progress`
  kind. The UI already folds away non-`message` kinds, so `debug-assistant` rows
  stay inspectable without cluttering the chat.
- Redaction v1: PRs 1-4 have no secret-carrying actions, so persist args
  verbatim for those actions. When a later capability sets `secrets=true`,
  redaction becomes mandatory before this helper is called.

PR 3 acceptance tests should assert:

- a `running` event is committed before the action returns;
- a successful action writes `observed`;
- a failed action writes `failed`;
- final reply writes `final` and then the user-visible assistant message;
- `journal.result` summarizes final state but is not the only trace source.

---

## Decision ledger

These decisions are considered settled for v2 unless implementation facts
invalidate them:

| Decision | Chosen | Revisit only if |
|---|---|---|
| First assistant primitive | rainbox-owned ReAct loop | a framework can prove step-at-a-time durable tracing with less code |
| Generated code | defer; new runner required | Phase 1 loop is useful and read-only generated scripts have a clear sandbox |
| Trace storage | `debug-assistant` rows plus final `journal.result` | query/filter/resume needs exceed chat-row JSON |
| First action set | read-only enum including `query_qa` | a fake-model eval shows a smaller set is still useful |
| Skill storage | markdown plus frontmatter/sidecar metadata | operator review requires DB filtering at scale |
| Skill activation | candidate before active | tests and policy exist for narrow auto-activation |
| Memory retrieval | hybrid vector/full-text/entity with hard pre-filters | native full-text cannot meet recall evals |
| Profile deriver | defer by default | one-shot profile demonstrably goes stale or misses important inferred context |
| Registry timing | primitive in Phase 1, formal in Phase 4 | write actions need to move earlier, which they should not |
| Interrupts | agent-visible control path | supervisor gets redesigned for unrelated reasons |

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

## What is still missing to make the full roadmap actionable

PRs 1-4 now have a concrete starting spec above. The rest of this document is
still a decided *roadmap*, not yet a buildable spec for every phase. The
difference is that a spec leaves no open decision between "read it" and "write
the code." The items below are the gaps that still force a developer to make a
judgment call mid-implementation. Each must be pinned to a single concrete
answer before its phase can be coded straight through.

Status: **decided** = answer is fixed in this doc; **range** = doc gives options
but not a choice; **missing** = not addressed.

### Loop and dispatch (pinned for PR 2 and PR 4)

- **Step-decision schema** *(decided for PR 1-4)* - use
  `AssistantStepDecision(reason, action, args)` with `AssistantActionName`.
- **Action interface** *(decided for PR 1-4)* - use
  `AssistantActionContext`, `AssistantObservation`, and action callables; the
  dispatcher owns validation, timeout, output caps, and trace boundaries.
- **Assistant agent placement** *(decided for PR 1-4)* - implement
  `AssistantAgent` as a specialized `ModelGroupAgent`, not a
  `StructuredLLMAgent`, because it needs multiple structured calls inside one
  `handle()`.
- **Model binding** *(decided for PR 1-4)* - the assistant declares
  `requires_structured_output=True`, requires no function calling, and has no
  default live binding. Tests use the fake-model seam; live use requires the
  operator to bind a structured-output model group.
- **Enablement** *(decided for PR 1-4)* - the assistant is a room-member chat
  responder via `CHAT_RESPONDER_UUIDS`, not a global singleton.

### Eval harness (fake-model seam pinned for PR 1)

- **Fake-model seam** *(decided for PR 1-4)* - monkeypatch
  `AssistantAgent._decide_next_step(...)` with a scripted sequence of
  `AssistantStepDecision` objects.
- **Acceptance-case format** *(range)* - each eval in the catalog needs a
  concrete given/when/then and assertion, not just a name.

### Trace and persistence (pinned for PR 3)

- **Trace write helper** *(decided for PR 1-4)* - use an assistant-local
  `post_assistant_trace(...)` helper that writes append-only `debug-assistant`
  chat rows. `(journal_id, step_index)` is the logical step grouping key, not a
  uniqueness constraint.
- **Redaction rule** *(decided for PR 1-4; range for later)* - PRs 1-4 expose no
  secret-carrying actions, so args persist verbatim. Later capabilities with
  `secrets=true` must redact before calling the trace helper.

### Memory and retrieval (blocks PR 7)

- **Embedding storage** *(range)* - decide now: a `vector` column on
  `memory_claim` vs a separate `memory_embedding` table; the doc says "smallest
  schema" but a spec must choose one.
- **Embedding model + index** *(missing)* - which embedding model (presumably the
  Q&A `nomic-embed-text` path), the vector dimension, the pgvector index type
  (hnsw vs ivfflat), and the distance operator.
- **Merge formula** *(missing)* - "merge into one score" needs an actual strategy
  (weighted sum vs reciprocal-rank-fusion), the weights or RRF constant, the
  candidate `k`, and the prompt-injection budget caps as numbers.

### Skills (blocks PR 6)

- **Metadata home** *(range)* - frontmatter vs sidecar JSON is left open; pick one
  for v1 (the doc leans frontmatter - make it the decision).
- **Skill id / dedup rule** *(missing)* - how `id` collisions and `supersedes`
  chains resolve at load time.

### Registry and control (blocks PR 8 and PR 10)

- **Registry source of truth** *(range)* - is the registry a Python list of
  dataclasses, a DB table, or a config file? The prompt catalog is generated from
  whichever this is.
- **Control channel** *(range)* - the "control row" needs a concrete home: a new
  table vs a chat row `kind`, plus the poll cadence the loop uses between steps.
- **Approval persistence** *(missing)* - where the `proposed -> confirmed -> ...`
  state lives (table + columns) and how a confirmed intent is bound immutably to
  the exact payload.

### Cross-cutting

- **Migrations** *(missing)* - every new column/table above needs a migration;
  none are written.
- **Open questions that are still genuinely undecided** *(missing)* - collect them
  in one place so a spec author knows exactly what to resolve first.

### Minimum needed to start PR 1-4

The conservative first-slice blockers are now pinned in **PR 1-4 concrete spec**:

1. Step-decision schema.
2. Action interface signature.
3. Fake-model seam.
4. Trace write helper + `(journal_id, step_index)` logical key.
5. Assistant placement and structured-output binding requirement.
6. Room-member chat responder enablement.

With those decisions pinned, PRs 1-4 (eval harness -> talk-only assistant -> durable
trace -> read-only actions) can be written straight through. Remaining gaps are
phase-local and can stay roadmap until their phase is next.

---

## Sources

See [`2026-06-19-improvements-v1-brainstorm.md`](2026-06-19-improvements-v1-brainstorm.md)
for the broader comparison and citations.
