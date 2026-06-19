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

## Document status (2026-06-20)

This document is no longer just a brainstorm. It has three maturity levels:

| Area | Status | What that means |
|---|---|---|
| Roadmap direction | **Decided** | The phase order and major choices are stable enough to implement against. |
| PRs 1-4 | **Build-ready spec** | The first slice has concrete loop shape, fake-model seam, trace tables, helper names, prompt assembly, tests, and file placement. Start coding here. |
| PRs 5-10 and later phases | **Roadmap plus draft defaults** | The direction is decided, but several details remain draft/range/missing until their phase is next. |
| Concrete schemas in prose | **Illustrative unless promoted** | The assistant contracts are binding; field names and table sketches should be refined by the PR that implements them. |

The practical status is: **stop polishing before PR 1.** The first implementation
branch can start now. Further document work should be phase-local: when a phase
is next, promote its draft defaults into final spec text, resolve its open
questions, then implement.

Known rough edges in this document:

- It is intentionally dense. It now mixes roadmap, spec, decision log, draft
  schemas, and implementation notes in one file. That is useful before PR 1, but
  later phases may deserve smaller phase-specific spec files.
- Some later-phase gaps are listed as **range/missing** even though a draft
  answer exists below. That is deliberate: a draft answer is not binding until a
  phase owner promotes it.
- The first-slice trace schema is more concrete than the later schemas. Do not
  infer the same confidence level for `memory_embedding`, `assistant_control`,
  or `assistant_write_intent`; those are drafts.
- UI details are still thin. The doc says chat should render assistant traces,
  but it does not fully specify the Flask/Admin/chat rendering behavior.
- Eval runner behavior is only partly specified. PR 1 uses pytest/fake-model
  tests; the optional `eval_case` regression layer needs more spec before it is
  treated as a product surface.
- Prompt budgeting starts with character caps. That is good enough for PRs 1-4,
  but a tokenizer-aware budgeter is still future work.
- Schema evolution is documented around the current `init_db()` pattern, not a
  dedicated migration framework. If rainbox adopts Alembic or similar later,
  this section should be updated.
- Checked (2026-06-20): the v1 brainstorm has no stale embedding wording - its
  only LM Studio reference is the provider-list row, which is correct. Both v1
  and v2 follow the current Ollama `nomic-embed-text` path
  (`agents/query_kb_helpers.py`).

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
- Add write actions one family at a time, risk-tiered (log-and-undo for low-risk
  internal writes, dry-run/confirm for high-blast-radius ones), with trace and
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
| **Writes are family-scoped and risk-tiered** | No blanket mutation switch. Each write family owns its validator, trace shape, tests, and an approval tier - log-and-undo for low-risk internal writes, dry-run/confirm for high-blast-radius or outward-facing ones. | Phase 5 |
| **Stop is stateful** | A stopped run records why and where it stopped; it is not just a killed process with missing context. | Phase 6 |
| **Context is budgeted** | Everything injected into a prompt (skills, profile, memory, action catalog, conversation) lives under an explicit token budget with per-section caps. Local models have small windows (8k-32k) and degrade when stuffed ("lost in the middle"), so budgeting is enforced from Phase 1, not deferred to a compression phase. | Phase 1 |

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
| 1 | chat enqueue path, agent config/dispatch, structured output patterns, memory retrieval, QueryAgent handlers, workspace command policy | assistant loop, action enum, `assistant_run`/`assistant_step` tables + step writer, prompt token budget |
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

**Decision (revised):** build dedicated `assistant_run` / `assistant_step`
tables in PR 3 as the durable, queryable source of truth. Do not store the trace
as JSON inside `chat_message`.

Why this reverses the earlier "chat rows first" pin: the doc's own deferral
trigger was "the operator needs to query/filter traces by action or status" -
and that need arrives on *day one* of debugging the ReAct loop ("how often does
`query_qa` fail?", "which step caps out?"). A JSON-in-`chat_message.text`
approach answers those only via full scans and JSON extraction, and it conflates
two different domain entities (a human message vs a machine step trace), making
the context builder filter trace rows out of every prompt. The migration is
small enough to pay now.

Minimal schema (illustrative, not binding):

- `assistant_run`: `id`, `uuid`, `journal_id`, `room_uuid`, `agent_uuid`,
  `status` (`running`/`stopped`/`failed`/`killed`/`finished`), `step_limit`,
  `started_at`, `finished_at`, `final_summary`, `metadata` (JSONB).
- `assistant_step`: `id`, `run_id` (FK), `step_index`, `phase`
  (`planned`/`running`/`observed`/`failed`/`final`/`control`), `action`,
  `reason`, `args` (JSONB), `observation_preview`, `error`,
  `model_group_uuid`, `model_uuid`, `created_at`. Append-only: one row per
  transition, so a killed mid-action run still shows the last committed
  `running` row.

Rules:

- `planned`/`running` is committed before dispatch; `observed`/`failed`/`final`
  after. `journal.result` holds only a short final summary (it is a `Text`
  column the base agent writes solely on `handle()` exit, so it cannot be the
  mid-run trace - the tables are).
- `args` is JSONB so the operator can index/filter by action and argument shape
  without full scans.
- Arguments are redacted before persistence once any action sets `secrets=true`.

**Chat stays the inline view, not the store.** The chat UI still renders the
trace inline (operators want to see the agent's steps in the conversation), but
it renders *from* the tables - either by reading them or via a thin
`kind="debug-assistant"` pointer row that carries only `run_id`/`step_index`,
not the whole payload. Keep `kind="debug-assistant"` (never `"progress"`): the UI
folds non-`message` kinds away, and `post_chat_message` reaps the sender's
`progress` rows on a terminal reply, which must not destroy the trace pointers.

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

Scope/sizing honesty: the **M/L** estimate is the *minimal* build - pgvector +
Postgres `tsvector` + a `subject`/`object` exact-match boost, combined with a
simple weighted merge. A *full* hybrid engine (reciprocal-rank fusion, entity
graphs, BM25, learned weights) is a much larger project and is explicitly **not**
Phase 3; add those pieces one at a time only if an eval shows the minimal blend
misses recall. Do not start Phase 3 by building the maximal search stack.

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

**The registry stays code-level, not a database/UI subsystem.** For a solo
operator this is a Python object - a list/dict of `Capability` dataclasses (or a
decorator that registers each action callable into the same enum from Phase 1) -
read at startup, used to generate the prompt catalog and gate dispatch. Do *not*
build a DB-backed registry table, an admin CRUD UI, roles, or per-channel ACLs;
that is enterprise governance with no payoff for one user. Enabled/disabled state
can be a config value or a settings row; the catalog itself lives in code where
it is diffable and obviously correct. The JSON above is just the shape of one
such dataclass, not a table schema.

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

### Approval by risk tier (not blanket confirm)

This is a single-operator, local-first tool, largely air-gapped from money and
the public internet. Forcing a manual "approve" on every kanban move or reminder
is alert fatigue - it turns the assistant into a chat-driven GUI. Since
**trace-before-action already guarantees auditability**, default approval is set
by *blast radius*, not applied uniformly:

- **Log-and-undo tier (default for low-risk internal writes):** kanban work
  events, reminders/cron, memory/skill candidate creation. These execute
  immediately, write a trace, and expose an undo/revert. No pre-confirmation.
  They are reversible local state, and the trace is the audit trail.
- **Confirm tier (default for high-blast-radius or outward-facing writes):**
  file/document patches, MCP tool calls, activating a skill or confirming a
  memory that *steers future behavior*, and anything `network=true` or
  `secrets=true`. These require the dry-run/confirm path below.

The tier is a per-capability default in the registry (Phase 4), and the operator
can move a capability between tiers. "Unattended eligibility" is then just:
log-and-undo capabilities are unattended by default; confirm-tier ones are not.

Each write family must define: its tier, the trace shape, failure behavior, and
a rollback/undo (log-and-undo tier) or dry-run/confirm text (confirm tier).

### Approval state machine (confirm tier only)

- `proposed` - assistant generated a write intent and dry-run/preview.
- `confirmed` - operator approved this exact intent.
- `executing` - dispatcher is running the write.
- `completed` - write finished and trace links to the result.
- `failed` - write failed with error and no hidden retry.
- `rejected` - operator rejected or edited the proposal.

Do not let the assistant mutate the payload after confirmation. A changed
payload is a new proposal. Log-and-undo writes skip `proposed`/`confirmed` and
go straight to `executing` -> `completed`/`failed`, with `undone` as an extra
terminal state.

Done when:

- The assistant can perform at least one log-and-undo family and one confirm-tier
  family end to end.
- A confirm-tier write is never executed without an approved proposal; a
  log-and-undo write always lands a reversible trace.
- The registry enforces the same tier the prompt describes.
- Downvotes, failed confirmations, or undos can become eval cases.

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
- **Budget the prompt from day one.** The assistant assembles its prompt from a
  fixed token budget with per-section caps (system/instructions, action catalog,
  memory, skills, profile, conversation). Phase 1 starts with a trivial budget
  (instructions + action catalog + recent turns); every later phase that adds an
  injected section must declare its cap and a drop/trim order, not just append.
  This is a hard requirement because rainbox targets local models with small
  context windows, where over-stuffing both overflows the window and degrades
  reasoning.
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
| 3 | Step trace persistence | `assistant_run`/`assistant_step` tables + migration, before/after action commits, thin chat pointer row, final `journal.result` summary | resume-from-step, approval FKs, dashboards |
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

Why structured output, not native function-calling, for the decision step: this
`AssistantStepDecision` is emitted via the provider's **grammar-constrained
structured-output mode** (`as_structured_llm`), not freeform string parsing - on
local models served by LM Studio/Jan/Ollama, constrained decoding is at least as
reliable as function-calling and more uniformly supported. rainbox also has its
own evidence: its kanban benchmark (2026-06-11) found *markdown context +
structured output beats JSON and function calling on reliability and speed* for
local models. Function-calling stays available as a per-capability/per-model
option once the registry exists (Phase 4), for models where it benchmarks better -
but it is not the default, and rainbox still owns the loop either way.

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

PR 3 writes to the `assistant_step` table (the source of truth), and posts a thin
`debug-assistant` chat row only so the trace renders inline. The helper belongs
in `db` (it touches the new tables alongside the existing chat helper).

```python
def append_assistant_step(
    *,
    run_id: int,
    step_index: int,
    phase: Literal["planned", "running", "observed", "failed", "final"],
    action: str | None,
    reason: str | None = None,
    args: dict[str, Any] | None = None,
    observation_preview: str | None = None,
    error: str | None = None,
    model_group_uuid: UUID | None = None,
) -> AssistantStep:
    """Append one step-transition row, then post a thin debug-assistant chat
    pointer row (run_id + step_index, not the full payload) for inline display."""
    ...
```

Implementation rules:

- The row is committed to `assistant_step` first; the chat pointer is posted in
  the same transaction via
  `db.post_chat_message(..., content_type="json", kind="debug-assistant")`
  carrying only `{run_id, step_index}`.
- `(run_id, step_index)` groups the events of one step; rows are append-only
  (`planned`, `running`, `observed`, `failed` are separate rows), so there is no
  uniqueness constraint on the pair. Ordering is `assistant_step.id` /
  `created_at`.
- Keep the pointer's `kind="debug-assistant"`, **never `"progress"`**:
  `post_chat_message` reaps the sender's `progress` rows on a terminal reply, and
  the UI already folds non-`message` kinds away - so the pointer stays
  inspectable and is never auto-destroyed when the final answer posts.
- Redaction v1: PRs 1-4 have no secret-carrying actions, so persist `args`
  verbatim. When a later capability sets `secrets=true`, redaction is mandatory
  before this helper is called.
- `journal.result` stores only the short final summary; the tables are the trace.

PR 3 acceptance tests should assert:

- a `running` row lands in `assistant_step` before the action returns;
- a successful action appends `observed`; a failed action appends `failed`;
- final reply appends `final`, then posts the user-visible assistant message;
- the trace is queryable by `action`/`phase` without scanning chat history;
- `journal.result` summarizes final state but is not the trace source.

### PR 3 table sketch

The implementation may adjust names, but the first migration should be close to
this shape. Keep it small; approval/control/resume columns come later.

```text
assistant_run
- id
- uuid
- journal_id                     indexed, unique enough for v1 lookup
- room_uuid
- agent_uuid
- status                         running | finished | stopped | failed | killed
- step_limit                     int, default 6
- started_at
- finished_at
- final_summary
- metadata JSONB                 empty by default; model/run diagnostics only
indexes: journal_id, room_uuid + started_at

assistant_step
- id
- uuid
- run_id                         FK assistant_run.id, indexed
- step_index                     int
- phase                          planned | running | observed | failed | final | control
- action                         nullable text
- reason                         nullable text
- args JSONB                     empty object by default
- observation_preview            nullable text
- error                          nullable text
- model_group_uuid               nullable UUID
- model_uuid                     nullable UUID
- created_at
indexes: run_id + step_index + id, action + phase, created_at
```

Notes:

- `phase="control"` is reserved for Phase 6 so the later stop/redirect feature
  does not need a constraint migration just to record a control event.
- `model_uuid` is nullable because fake-model tests and pre-model validation
  steps have no real model.
- Do not add a uniqueness constraint on `(run_id, step_index)`: every step is
  append-only and normally has multiple rows.
- These are new tables, so `db.create_all()` in `init_db()` creates them on both
  fresh and existing DBs and never touches existing rows. No guarded `ALTER` is
  needed in PR 3 (see *Verified runtime bindings*).

### Prompt assembly contract

Prompt assembly is part of the assistant's behavior, not incidental string
concatenation. For PRs 1-4, keep it deliberately small:

1. System instructions: role, contracts, and the rule that `reason` is an
   operator-facing rationale, not hidden chain-of-thought.
2. Action catalog: only the currently enabled read-only enum entries.
3. Recent conversation: chat messages where `kind == "message"` only. Exclude
   `debug-*`, `progress`, and `thinking` rows from the model prompt.
4. Scratchpad: compact summaries of prior assistant steps in this run:
   `step_index`, `action`, `ok`, and a capped observation preview.

PR 1-4 caps:

- max steps per run: 6
- max recent chat messages: 30
- max observation preview per step in the prompt: 1200 characters
- max total scratchpad characters: 5000
- max workspace command output passed back to the model: 4000 characters

These are simple character caps, not a final token-budget system. They are good
enough for the first slice and keep small local models from being drowned by
trace text. Phase 3 can replace this with a tokenizer-aware budget builder.

### Loop skeleton

The first loop should look like this structurally:

```text
handle(journal_id, payload):
  room_uuid = require_room_uuid(payload)   # payload is a dict: payload["room_uuid"]
  run = start_assistant_run(journal_id, room_uuid, self.agent_uuid, step_limit=6)
  transcript = format_history(                       # chat.transcript.format_history
    [m for m in db.list_room_messages(room_uuid) if m.kind == "message"])
  scratchpad = []

  for step_index in range(run.step_limit):
    decision = _decide_next_step(transcript=transcript,
                                 scratchpad=scratchpad,
                                 step_index=step_index)
    append_assistant_step(run, step_index, "planned", decision)

    validation_error = validate_decision(decision)
    if validation_error:
      append_assistant_step(run, step_index, "failed", validation_error)
      scratchpad.append(compact_validation_failure(decision, validation_error))
      continue

    if decision.action in terminal_actions:
      append_assistant_step(run, step_index, "final", decision)
      post_final_chat_message(decision.args)
      finish_run("finished")
      return {"ok": True, "assistant_run_id": run.id, "status": "finished"}

    append_assistant_step(run, step_index, "running", decision)
    observation = dispatch_action(decision.action, decision.args)

    if observation.ok:
      append_assistant_step(run, step_index, "observed", observation)
    else:
      append_assistant_step(run, step_index, "failed", observation)

    scratchpad.append(compact_step(decision, observation))

  post_step_limit_message()
  finish_run("stopped", final_summary="step limit reached")
```

Important boundaries:

- The final assistant chat message is posted by the loop, not by actions.
- Nonterminal action failures do not crash the process by default; they become
  observations the model can react to until the step cap is reached.
- Validation failures are traceable failed steps. If the model repeatedly emits
  invalid decisions, the step cap stops the loop.
- Unhandled Python exceptions still let `Agent.run()` mark the journal failed;
  the assistant should also try to mark the `assistant_run` failed before
  re-raising when possible.

### Verified runtime bindings

These tie the skeleton to the real code so PR 1-4 can be written directly:

- **Payload is a dict.** `handle(self, journal_id, payload: dict)` - read
  `payload.get("room_uuid")` and raise on missing, the same guard
  `chat_structured.py` uses (`_room_uuid`). The trigger enqueues
  `{"room_uuid", "message_uuid"}`; like the existing responders, the assistant
  reads current room history and does not anchor to `message_uuid`.
- **Transcript reuse.** Build it from `db.list_room_messages(room_uuid)` filtered
  to `kind == "message"`, rendered with `chat.transcript.format_history` - no new
  transcript code.
- **Run-failure handling is explicit.** Wrap the loop body so any exception sets
  `assistant_run.status = "failed"` (and a final `assistant_step` `failed` row),
  then re-raise. `Agent.run()` then marks the *journal* failed
  (`base.py` catch -> `journal_update(..., "failed")`); without the wrapper the
  run row is left stuck in `running`. The existing `_handle_with_heartbeat`
  wrapper already keeps slow multi-step runs from being killed by the watchdog.
- **Helper set in `db/assistant.py`** (re-exported from `db/__init__.py`):
  `start_assistant_run(journal_id, room_uuid, agent_uuid, step_limit) -> run`,
  `append_assistant_step(...)` (defined above), and
  `finish_run(run, status, final_summary=None)`. The loop calls exactly these.
- **Migration is trivial for new tables.** `assistant_run`/`assistant_step` are
  brand-new tables, so `db.create_all()` in `init_db()` creates them on both
  fresh and existing DBs (it only skips *existing* tables; it never wipes them).
  No `_add_column_if_missing` is needed in PR 3 - that guarded-`ALTER` pattern is
  only for columns added to these tables in a *later* PR. The idempotency test
  ("`init_db()` twice preserves a sentinel row") therefore passes by construction.
- **Final reply posting.** The loop posts the terminal message via
  `db.post_chat_message(room_uuid, self.agent_uuid, text, content_type, kind="message")`.
  Because that is a direct DB call (not the HTTP post endpoint), it does not run
  `_maybe_trigger_chat_agents`, so the assistant never re-triggers itself.

### PR 1-4 test matrix

| PR | Test | Assertion |
|---|---|---|
| 1 | scripted decision provider helper | helper returns scripted decisions in order and raises when over-consumed |
| 1 | existing query/memory smoke cases | current behavior is protected before assistant work starts |
| 2 | reply-only assistant | one scripted `reply` posts one final message and exits |
| 2 | clarifying-question assistant | `ask_clarifying_question` is terminal and posts a question |
| 2 | scripted decision provider exhausts in loop | test fails clearly when the loop asks for an unexpected extra model decision |
| 2 | step cap | repeated nonterminal decisions stop at the configured limit |
| 2 | invalid action args | invalid/missing args produce a traceable failed step, not a silent crash |
| 3 | trace-before-action | `running` is committed before a fake slow action returns |
| 3 | failed action trace | action exception becomes a `failed` assistant step |
| 3 | final summary | `journal.result` contains summary/id pointers, not the full trace |
| 3 | init idempotency | `init_db()` twice preserves a sentinel `assistant_run` row |
| 4 | `query_qa` action | reuses QueryAgent/query handler path for project-status style questions |
| 4 | `workspace_read_command` action | allowed read command returns output; forbidden command returns blocked observation |
| 4 | `query_memory` action | current memory retrieval path returns auditable observation |
| 4 | `kanban_read` action | reads board/card state without appending kanban events |

### File placement for PR 1-4

Keep the first branch boring and local to the existing module layout:

| Area | File(s) |
|---|---|
| assistant agent | `source/agents/assistant.py` |
| agent config | `source/agents/config.py`, `source/agents/__main__.py` |
| chat responder trigger | `source/webapp/chat_api.py` (`CHAT_RESPONDER_UUIDS`) |
| structured-call extraction | `source/agents/base.py` |
| assistant trace models | `source/db/models.py` |
| assistant trace helpers | `source/db/assistant.py`, re-exported from `source/db/__init__.py` |
| assistant tests | `source/agents/test_assistant.py` |
| trace/db tests | `source/db/test_assistant_trace.py` |
| eval helper tests | colocate with the helper they exercise; do not require live LLM |

Avoid adding a new package or framework for the assistant. If a helper is only
used by `AssistantAgent`, keep it in `agents/assistant.py` until a second caller
exists.

---

## Decision ledger

These decisions are considered settled for v2 unless implementation facts
invalidate them:

| Decision | Chosen | Revisit only if |
|---|---|---|
| First assistant primitive | rainbox-owned ReAct loop | a framework can prove step-at-a-time durable tracing with less code |
| Generated code | defer; new runner required | Phase 1 loop is useful and read-only generated scripts have a clear sandbox |
| Trace storage | dedicated `assistant_run`/`assistant_step` tables (source of truth) + thin chat pointer for inline view | a single-table or chat-only shape demonstrably suffices in practice |
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

- **Trace write helper** *(decided for PR 1-4)* - `db.append_assistant_step(...)`
  appends to the `assistant_run`/`assistant_step` tables (the source of truth)
  and posts a thin `debug-assistant` chat pointer row for inline display.
  `(run_id, step_index)` is the logical step grouping key, not a uniqueness
  constraint.
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

- **Registry source of truth** *(decided)* - a code-level Python object (list of
  `Capability` dataclasses / decorator-registered callables), not a DB table or
  CRUD UI. The prompt catalog is generated from it; enabled/disabled is config.
- **Control channel** *(range)* - the "control row" needs a concrete home: a new
  table vs a chat row `kind`, plus the poll cadence the loop uses between steps.
- **Approval persistence** *(missing)* - where the `proposed -> confirmed -> ...`
  state lives (table + columns) and how a confirmed intent is bound immutably to
  the exact payload.

### Cross-cutting

- **Migrations** *(missing)* - every new column/table needs a migration; none
  are written. The `assistant_run`/`assistant_step` tables are the first one,
  due in PR 3.
- **Open questions that are still genuinely undecided** *(missing)* - collect them
  in one place so a spec author knows exactly what to resolve first.

### Minimum needed to start PR 1-4

The conservative first-slice blockers are now pinned in **PR 1-4 concrete spec**:

1. Step-decision schema.
2. Action interface signature.
3. Fake-model seam.
4. Trace tables + `append_assistant_step(...)` helper + `(run_id, step_index)` key.
5. Assistant placement and structured-output binding requirement.
6. Room-member chat responder enablement.

With those decisions pinned, PRs 1-4 (eval harness -> talk-only assistant -> durable
trace -> read-only actions) can be written straight through. Remaining gaps are
phase-local and can stay roadmap until their phase is next.

---

## Draft answers for remaining spec gaps

This section is a draft, not yet a settled contract. It gives the next spec
author concrete defaults for the areas above that still say **range** or
**missing**. Promote a draft answer to the main phase text only after checking it
against the implementation at that point.

### Draft: acceptance-case format

Use the existing `eval_case` / `eval_run` / `eval_result` tables. Do not add a
separate eval-case file format for the assistant until the DB shape becomes
painful. A hand-authored eval case should be stored as:

```json
{
  "name": "assistant two-step query then reply",
  "case_type": "tool_output",
  "split": "regression",
  "status": "active",
  "input": {
    "agent_role": "assistant",
    "room_history": [
      {"sender_type": "human", "text": "what is the current git status?"}
    ],
    "scripted_decisions": [
      {"reason": "Need project status.", "action": "query_qa", "args": {"query": "git status"}},
      {"reason": "Have enough information.", "action": "reply", "args": {"message": "Working tree ..."}}
    ]
  },
  "expected": {
    "trace_phases": ["running", "observed", "final"],
    "actions": ["query_qa", "reply"],
    "must_include": ["Working tree"],
    "must_not_include": ["Traceback"]
  },
  "rubric": {
    "assertions": [
      "trace_contains_actions_in_order",
      "final_reply_matches_expected",
      "no_failed_step"
    ]
  }
}
```

Current caveat: the DB check constraint only allows `chat_reply`,
`memory_retrieval`, `query_answer`, and `tool_output` (verified:
`eval_case_case_type_check`). For PRs 1-4, reuse `tool_output` for assistant-loop
control-flow tests. If assistant evals become a first-class surface, migrate the
constraint to add `assistant_loop`, `skill_injection`, and `write_action`.

Two verified mechanics worth pinning here:

- **A `case_type` value is a CHECK constraint, not a column** - it cannot use
  `_add_column_if_missing`. Changing it means DROP + ADD the constraint,
  idempotently. `init_db()` already has the precedent: guard with
  `_constraint_def("eval_case_case_type_check") is not None` before dropping,
  then re-add with the widened `CHECK (...)`.
- **PR-1 loop tests are pytest, not `eval_case` rows.** The deterministic
  fake-model loop tests (the seam below) live in-repo as pytest and need no DB.
  The `eval_case`/`eval_run`/`eval_result` tables are the *optional* hand-authored
  regression layer driven by the eval runner. Do not block PR 1 on writing
  `eval_case` rows; the JSON shape above is for that later regression layer.

### Draft: memory embedding storage and ranking

Pick a separate table, not a vector column on `memory_claim`.

Rationale:

- `MemoryClaim` stays readable in Flask-Admin and normal SQLAlchemy queries.
- Multiple embedding models or text hashes can coexist during rebuilds.
- Failed or partial embedding rebuilds do not corrupt the memory source row.
- The existing Q&A vector table is owned by `PGVectorStore`; memory can use a
  simpler rainbox-owned table with explicit provenance.

Draft table:

```text
memory_embedding
- id
- uuid
- memory_uuid
- model_name
- embed_dim
- text_hash
- embedding vector(768)
- created_at
- updated_at
unique(memory_uuid, model_name, text_hash)
index: hnsw on embedding vector_cosine_ops when pgvector supports it; otherwise no index until row count justifies ivfflat
```

Draft model/index choices:

- embedding model: reuse the Q&A path, `nomic-embed-text`
- runtime: Ollama-compatible OpenAI embeddings endpoint already used by
  `agents/query_kb_helpers.py`
- dimension: 768
- distance: cosine
- vector candidate count: 40
- full-text candidate count: 40
- final injected memories: 6
- memory prompt budget: 1200 tokens or the smaller per-model cap declared by the
  prompt-budget builder

Draft merge formula for the minimal Phase 3 build:

```text
hard_filter = status=active AND scope allowed AND sensitivity allowed AND
              (expires_at IS NULL OR expires_at > now)

score =
  0.55 * vector_similarity_0_to_1 +
  0.30 * full_text_score_0_to_1 +
  0.15 * entity_boost

entity_boost =
  1.0 if subject/object exact-match a query entity
  0.5 if subject/object token-overlap the query
  0.0 otherwise
```

Keep confidence and scope as tie-breakers after the score, not hidden score
multipliers in v1. If evals show the weighted formula is brittle, try reciprocal
rank fusion later; do not start there.

Two gaps the table alone leaves open:

- **Population/sync.** Define when a `memory_embedding` row is written: (1) a
  one-shot backfill over active claims when the feature ships, and (2) on every
  transition that makes a claim `active` (new claim, confirm, correct). A claim
  with no embedding row yet falls back to lexical-only retrieval - never an
  error, just lower recall until the backfill catches it. Embeddings for claims
  that leave `active` can be pruned lazily (the hard filter already excludes
  them from results, so this is housekeeping, not correctness).
- **Similarity normalization.** pgvector's cosine operator (`<=>`) returns
  *distance* in `[0,2]`, not a `[0,1]` similarity. Pin the conversion explicitly
  - `vector_similarity_0_to_1 = 1 - (cosine_distance / 2)` - so the merge weights
  above behave and nobody accidentally ranks by raw distance (which inverts the
  order).

### Draft: skills metadata and dedup

Use markdown frontmatter for v1. Do not use sidecar JSON unless frontmatter
causes tooling pain.

Required fields:

```yaml
id: summarize-pr-review
status: active
created_by: human
source_journal_id:
source_step_id:
supersedes:
retrieval_tags: [github, review, pull-request]
updated_at: "2026-06-20T00:00:00Z"
```

Draft load order:

1. Load base skills from the repo skills directory.
2. Load overlay skills from `<customize.dir>/skills/`.
3. Normalize `id` as a lowercase slug; reject ids with path separators.
4. Overlay wins over base for the same id.
5. A `rejected` overlay skill with the same id suppresses the base skill.
6. `candidate` skills are visible in review lists but never injected.
7. `supersedes` hides the predecessor only when the successor is `active`.
8. Cycles in `supersedes` make all involved skills invalid until fixed.
9. Duplicate ids inside the same directory are an error, not last-write-wins.

Retrieval v1: lexical match against title, `retrieval_tags`, headings, and first
paragraph. Semantic skill retrieval waits until the Phase 3 memory retrieval
upgrade is available.

### Draft: control channel

Use a new DB table rather than a chat row. Chat rows are user/operator
conversation artifacts; control rows are runtime state.

Draft table:

```text
assistant_control
- id
- uuid
- run_id
- command: stop | redirect
- payload JSONB
- state: pending | applied | ignored
- requested_by_uuid
- created_at
- applied_at
- note
```

Draft behavior:

- `/stop` inserts `command=stop`, `state=pending`.
- A redirect inserts `command=redirect` with the new instruction in `payload`.
- The assistant checks for pending controls before each model call and before
  each action dispatch.
- PR 10 does not promise mid-LLM interruption. If a model call is already
  blocked, the supervisor kill path remains the emergency fallback.
- Applying a control writes an `assistant_step` row with phase `control`.

### Draft: approval persistence

Use a dedicated `assistant_write_intent` table for confirm-tier writes. Do not
store approvals only in chat messages.

Draft table:

```text
assistant_write_intent
- id
- uuid
- run_id
- step_id
- capability_name
- payload_hash
- payload JSONB
- preview_text
- state: proposed | confirmed | executing | completed | failed | rejected | undone
- created_at
- confirmed_at
- executed_at
- completed_at
- confirmed_by_uuid
- result JSONB
```

Rules:

- The hash is computed over canonical JSON for `capability_name + payload`.
- Confirming approves exactly that hash. Any edited payload creates a new
  intent.
- Confirm-tier dispatch refuses to execute unless state is `confirmed` and the
  current payload hash matches.
- Log-and-undo writes may skip this table in v1 if their trace includes enough
  data to undo; use this table only when preview/confirmation is required.

### Draft: migrations and schema changes

Rainbox currently uses SQLAlchemy models plus idempotent startup migrations in
`db.init_db()`, not a separate Alembic workflow. For assistant schema PRs, use
that pattern unless the project adopts a migration tool first.

Each schema PR should include:

- SQLAlchemy model definitions.
- `db.create_all()` coverage for fresh DBs.
- guarded `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` only when modifying an
  existing table.
- constraint migrations when enum/check values change.
- idempotency tests that call `init_db()` twice against an existing DB with
  sentinel rows.
- facade exports from `db/__init__.py` for new helper functions.

The first schema PR is PR 3: `assistant_run` and `assistant_step`, plus
`db.append_assistant_step(...)`.

### Draft: open-question register

Add an `## Open questions` subsection to this document whenever a phase reaches
implementation and still has a **range** or **missing** item. Use this shape:

```markdown
| ID | Phase/PR | Question | Current default | Decision owner | Blocks |
|---|---|---|---|---|---|
| OQ-001 | PR 7 | HNSW vs no vector index for memory_embedding? | no index until row count requires it | operator | memory retrieval PR |
```

Open questions should be temporary. If a question survives a phase PR, either
move it to a later phase explicitly or make the conservative default the
decision.

---

## Further document improvements

Do not improve this file by continuing broad critique loops. Improve it only
when a phase is about to be implemented or when implementation proves a claim
wrong.

Recommended next document edits:

1. **Before PR 1:** no more roadmap edits required. If anything changes, keep it
   limited to test names or helper names discovered while writing PR 1.
2. **After PRs 1-4 land:** replace the PR 1-4 concrete spec with links to the
   actual files/tests, and mark any deviations from this proposal in the
   decision ledger.
3. **Before PR 6 (skills):** promote the draft skills metadata/dedup rules to a
   final spec, or explicitly choose a different loader shape.
4. **Before PR 7 (semantic memory):** promote or revise the `memory_embedding`
   table, embedding sync policy, merge formula, and prompt caps.
5. **Before PR 8 (registry):** pin the enabled/disabled config location and the
   exact `Capability` dataclass shape.
6. **Before PR 9 (writes):** finalize the write-intent/approval schema and the
   undo data required for log-and-undo writes.
7. **Before PR 10 (steerability):** finalize the control-channel table, poll
   cadence, and stopped/failed/killed UI behavior.
8. **When stale source references are found:** update v1 or add a short errata
   note rather than letting v2 inherit obsolete assumptions.

Good cleanup once implementation begins:

- Split phase-specific build specs out of this file if it becomes hard to scan.
- Replace draft table sketches with links to SQLAlchemy models once implemented.
- Keep the **Assistant contracts** section stable; most other sections may
  evolve as code lands.
- Maintain an explicit open-question table instead of scattering unresolved
  questions across prose.

The document should become less speculative over time, not longer by default.
Every new paragraph should either unlock the next PR, record a verified
implementation fact, or retire an obsolete assumption.

---

## Sources

See [`2026-06-19-improvements-v1-brainstorm.md`](2026-06-19-improvements-v1-brainstorm.md)
for the broader comparison and citations.
