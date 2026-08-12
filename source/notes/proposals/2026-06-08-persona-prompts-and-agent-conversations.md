# Proposal: persona prompts and agent-to-agent conversations

**Status:** Phase 0 implemented — file-backed walking skeleton shipped with a
demo conversation (Egon ↔ Benny); production report-review work should use
task-specific functional personas, not the demo pair
**Date:** 2026-06-08
**Revision:** v15 — incorporates the PlanExe/report-review feedback from commit
`dfa71ef7`: separate the conversation runtime from a deliberation protocol,
demote Egon/Benny to demo personas, and define functional review roles,
issue-ledger records, consensus policies, and patch governance for real work. See
[Implementation status](#implementation-status-phase-0--shipped) for what is
real vs. still planned (builds on v14's Gemini termination/context notes)
**Scope:** File-backed persona/system-prompt organization first, with a clean
migration path to Postgres, and a practical model for letting two or more
Rainbox agents talk with each other — built on the inbox/journal queue and the
supervisor that already exist.

## Summary

Rainbox should treat **agent kind**, **persona**, **conversation template**, and
**conversation run** as four separate concepts.

- An **agent kind** is the runtime implementation and capability profile, e.g.
  `chat_unstructured`, `query_router`, `workspace_shell`, or `mcp`. It is defined
  in code (`agent_config.py`) and bound to a model group (`agent_model_binding`).
- A **persona** is the behavioral layer: name, description, tags, and system
  prompt. Today this is a hardcoded class constant; it should become data.
- A **conversation template** says which personas participate, which agent kind
  each persona uses, and how turns are scheduled.
- A **conversation run** is one live instance of a template: a transcript plus
  the *bounded* state that drives it — turn counter, budgets, stop flags.

The near-term implementation should keep persona/template data in files because
prompts are easiest to edit, diff, and review as text. The long-term
implementation stores the same concepts in Postgres with prompt revisions, while
preserving filesystem import/export.

The first useful target is deliberately small: two personas in one chatroom,
using a deterministic round-robin turn policy, a hard maximum turn count, and
explicit stop conditions. That gives agent-to-agent conversation without starting
with a free-form swarm.

**The key realization:** Rainbox already has almost everything this
needs. Agents are isolated child processes drained from a Postgres inbox, with a
journal, a routing pass, a SIGKILL watchdog, per-message chat persistence, SSE
streaming, and a cron scheduler. We are *not* building an orchestration runtime.
We are adding **one missing primitive — a bounded conversation turn scheduler —**
plus **persona-as-data** so an agent's prompt and identity come from a record
instead of a class constant. Everything else is composition of existing parts.

## Implementation status (Phase 0 — shipped)

Phase 0 is built and runs end-to-end on the live app: two demo personas (Egon,
Benny) hold a bounded, observable conversation driven by the manager,
started/stopped from a dedicated page. Egon/Benny are useful smoke-test fixtures
because they are memorable and easy to inspect; they should **not** be the
default personas for serious PlanExe/report-review work. The rest of this
document is the design of record; this section is the as-built delta.

**What shipped**

- **Persona-as-data** — `persona.py` (`Persona`, `resolve_persona_for_agent`,
  `load_conversation_template`, `list_conversation_templates`) backed by
  `agent_profiles/` (`personas.jsonl`, `prompts/egon.system.md`,
  `prompts/benny.system.md`, `conversations/egon-benny.json`).
- **Role/impl split** — `AgentConfigEntry.agent_kind`; `agent.py` dispatches on
  `config.get("agent_kind", config["name"])`; `persona_egon` / `persona_benny`
  (kind `chat_unstructured`) and the `conversation` manager role added.
- **Dynamic return-address routing** — `Agent.run()` copies the manager-authored
  `return_to_agent_uuid` into `result["_routing"]` on success *and* failure;
  `main.py` routes terminal rows via `db_queue.fetch_unrouted_terminal`, dynamic
  address first, static `next` success-only.
- **The manager** — `agent_conversation.py` (`ConversationManagerAgent`, pure
  `next_speaker` / `evaluate_stop`): tick claim, idempotent advance, human-
  interruption pause, stop checks, one in-flight turn per run.
- **Run state** — `ConversationRun` (`db_models.py`) + `db_conversation.py`
  (`create`/`get`/`claim_conversation_tick`/`advance_conversation_if_new`/
  `mark_conversation_turn_in_flight`/`finish`/`pause`/`request_conversation_stop`/
  `list_conversation_runs`/`find_human_message_after`).
- **Prompt resolution + provenance** — `agent_chat_unstructured.py` uses the
  persona system prompt (class constant fallback) and records
  `{persona_id, prompt_sha256}` on the journal result.
- **Operator UI** — a dedicated **`/conversations`** page
  (`webapp/conversation_views.py`) to start/stop/list runs, plus JSON endpoints
  (`webapp/conversation_api.py`). `/chat` is unchanged.
- **Tests** — `test_agent_conversation.py` (29) and `test_agent_heartbeat.py`
  (4): round-robin, stop conditions, `min_turns` gate, tick/advance CAS,
  full two-turn run, `max_turns` bound, failed-turn retry/fail, resume,
  reconcile, conversation context, operator stop, human-interruption pause, and
  heartbeat lifecycle.

**Deviations from the original plan (intentional)**

- **`min_turns` convergence floor (new).** Small local models emit `DONE` on turn
  0, so `evaluate_stop` now ignores stop phrases until `turn >= min_turns`
  (`egon-benny` uses `min_turns: 4`, `max_turns: 8`). This is the Phase 0 answer
  to premature termination; the heavier no-progress detector stays Phase 1+.
- **Full control page instead of a CLI/endpoint.** Phase 0 planned "a CLI or
  admin-only endpoint"; shipped a real `/conversations` page with
  start/stop/resume/reconcile and a live runs table.

**Hardened after first review (P1/P2 fixes)**

- **Failed turns no longer swallow errors.** The supervisor stamps journal
  `state` into the routed payload; the manager retries the same speaker up to
  `MAX_TURN_RETRIES` (`claim_failed_turn`), then marks the run `failed`.
- **Stop works for paused runs.** `stop_conversation` transitions a `paused` run
  straight to `stopped` (the manager skips non-running runs, so the flag alone
  never fired).
- **Resume + reconcile shipped.** `resume_conversation` (paused/stopped/failed →
  running — Stop is pause/play, not a hard terminal; clears stale active-turn,
  advances the human watermark; `finished` stays terminal) and
  `reconcile_conversation` (recovers a stale `active_turn` after a SIGKILLed
  speaker: retry once, then fail) with endpoints and buttons.
- **Conversation context builder.** Persona turns with a `run_uuid` now use
  `build_conversation_prompt` (runtime preamble: who/others/turn N of max + the
  DONE contract) over the last `CONVO_LAST_N` visible turns, excluding manager
  rows — not the generic chat context.
- **Base-agent heartbeat thread.** `Agent.run` now wraps `handle()` in
  `_handle_with_heartbeat`: a daemon thread emits a `heartbeat` status every
  `HEARTBEAT_INTERVAL` (20s) so a slow-but-healthy turn (a reasoning model
  thinking >60s) isn't SIGKILLed by the supervisor's silence watchdog. Socket
  writes are serialized (`_emit` + a lock); the supervisor resets its timer on
  any message and skips logging heartbeats. This removes the "fast model only"
  caveat — reasoning models are now safe in conversations.

**Deferred (still planned, not built)**

- Automatic/cron reconcile (today it is operator-triggered from the page) and a
  manager-maintained **summary** for older context (the builder windows but does
  not summarize).
- Structured yielding, the no-progress detector, and the Phase 1–3 items below.
- The report-review layer: explicit issues, evidence records, consensus
  decisions, and governed patches. The shipped manager controls conversation
  mechanics; it does not yet decide what a PlanExe review is trying to prove.

## From Conversation Runtime To Deliberation Protocol

The existing implementation answers the runtime questions: who speaks next, how
turns are bounded, how failures/retries/resume work, and how the transcript is
observed. That is necessary, but it is not sufficient for real report iteration.
For PlanExe-style reports, the important unit is not "Egon talks to Benny"; it is
an **issue under review**.

Keep these as two separate layers:

```text
Conversation runtime:
  who speaks next, queue/journal routing, bounds, stop/resume/reconcile,
  process isolation, transcript visibility

Deliberation protocol:
  which issue is being decided, which roles are required, what evidence is
  needed, what consensus means, what dissent remains, and what artifact/patch is
  produced
```

The conversation manager should stay boring and mechanical. A separate
deliberation protocol, expressed in templates and durable records, should define
what useful work the agents are doing.

### Should Egon And Benny Be Replaced?

For real work: **yes**. Keep Egon and Benny as demo personas and regression
fixtures for the runtime, but do not use them as the production default for
fact-checking, sourcing, or editing reports. They are intentionally playful; real
PlanExe/Rainbox work needs functional roles with evidence obligations and clear
authority boundaries.

Recommended split:

- **Demo personas:** `persona_egon`, `persona_benny`, `egon-benny.json`.
  Purpose: smoke tests, UI demos, local model behavior checks, prompt experiments.
- **Work personas:** role-named agents such as `claim_extractor`,
  `source_finder`, `evidence_assessor`, `domain_reviewer`, `skeptic`,
  `consistency_checker`, `patch_author`, and `consensus_chair`.
  Purpose: issue review, evidence assessment, consensus decisions, and governed
  patches.

In other words, Egon/Benny prove that the car starts. They are not the crew for
driving a serious report-review workflow.

### The Core Work Object: Review Issue

A PlanExe review run should gather agents around explicit issues, not around an
open-ended chat. A minimal issue record looks like:

```json
{
  "issue_id": "issue_123",
  "artifact_id": "report_456",
  "section_id": "risk_assessment",
  "issue_type": "claim_verification",
  "statement": "The plan assumes binding grid capacity can be secured within 36 months.",
  "required_roles": [
    "source_finder",
    "evidence_assessor",
    "domain_reviewer",
    "skeptic",
    "consensus_chair"
  ],
  "evidence_policy": {
    "minimum_sources": 1,
    "prefer_primary_sources": true,
    "allow_unsourced_verified": false,
    "require_retrieval_date": true
  },
  "consensus_policy": "qualified_consensus",
  "status": "under_review"
}
```

The chat transcript remains valuable for auditability, but the issue ledger is
the product. Report edits should consume decision records and accepted patches,
not scrape conversational text.

### Functional Personas For PlanExe Report Work

Start with a small library of work personas. Not every issue needs every role;
templates select the minimal required subset.

| Role | Responsibility | Should decide? |
| --- | --- | --- |
| `artifact_mapper` | Parse report structure, sections, dependencies, documents-to-find, WBS/timeline/risk surfaces | No; creates the map |
| `claim_extractor` | Turn prose into reviewable factual, numeric, timeline, regulatory, stakeholder, and assumption claims | No; creates issues |
| `source_finder` | Retrieve primary/official/technical sources and classify source type | No; gathers material |
| `evidence_assessor` | Judge whether sources support, partially support, contradict, or fail to support a claim | Yes, for evidence status |
| `domain_reviewer` | Apply domain-specific reasoning, e.g. grid/power, rail regulation, permitting, finance | Yes, for plausibility/substance |
| `skeptic` | Attack optimistic assumptions, weak evidence, hidden dependencies, timelines, and uncosted work | Yes, by filing or clearing objections |
| `stakeholder_proxy` | Simulate regulator, customer, municipality, operator, tenant, advocate, or other affected party | Sometimes; preserves stakeholder dissent |
| `consistency_checker` | Detect internal contradictions across summary, assumptions, scenarios, risks, WBS, timeline, and budget | Yes, for coherence |
| `patch_author` | Produce minimal report patches linked to issue IDs and evidence IDs | No alone; proposes edits |
| `consensus_chair` | Summarize positions, apply consensus policy, preserve dissent, and emit the final decision record | Yes, procedurally |

The conversation manager says "which runnable agent speaks next." The consensus
chair says "what was decided and why." Do not merge those roles.

### Consensus States And Policies

Consensus must not mean "the agents stopped arguing." It must mean the required
roles satisfied their obligations for a named issue.

Recommended decision states:

```text
verified
partially_verified
unsupported
contradicted
not_enough_evidence
needs_human_review
out_of_scope
```

Recommended policies:

- **Unanimous consensus:** legal, safety, financial, compliance-sensitive, or
  high-impact report changes. Every required role must agree or escalate.
- **Qualified consensus:** default for most substantive report improvements.
  Evidence Assessor, Domain Reviewer, and Skeptic must agree or record bounded
  dissent.
- **Majority consensus:** only for low-risk editorial issues such as wording,
  ordering, tone, or duplication. Never use majority voting for factual truth.
- **Chair decides after dissent:** allowed when work must proceed, but dissent is
  preserved in the decision record.
- **Human arbitration:** valid terminal state for evidence conflicts,
  high-impact uncertainty, circular debate, or proposed strategic-direction
  changes.

Every accepted decision should produce a structured record:

```json
{
  "issue_id": "issue_123",
  "decision": "partially_verified",
  "confidence": "medium",
  "consensus": {
    "policy": "qualified_consensus",
    "reached": true,
    "agreeing_roles": ["evidence_assessor", "domain_reviewer", "skeptic"],
    "dissent": []
  },
  "evidence": [
    {
      "source_id": "source_abc",
      "support": "partial",
      "notes": "Supports the regulatory direction but not the proposed timeline."
    }
  ],
  "recommended_action": "revise_claim",
  "patch_required": true
}
```

### Patch Governance

A factual decision and an edit decision are separate. "The claim is unsupported"
does not automatically imply how the report should change.

Suggested patch states:

```text
draft
needs_review
accepted
rejected
superseded
applied
```

Minimum patch approval for substantive edits:

```text
Patch Author proposes.
Domain Reviewer approves substance.
Consistency Checker approves local/global consistency.
Skeptic confirms no unresolved high-severity objection remains.
Consensus Chair accepts the decision record.
```

No patch should be applied merely because one agent proposed it.

### Task-Centric Conversation Templates

Production templates should be task-centric, not persona-centric. Example
template families:

```text
claim_fact_check
source_retrieval
assumption_review
risk_review
section_patch_review
full_report_review
```

Example: `claim_fact_check`

```json
{
  "id": "claim_fact_check",
  "goal": "Decide whether a specific report claim is supported, unsupported, contradicted, or requires human review.",
  "participants": [
    { "role": "source_finder", "required": true },
    { "role": "evidence_assessor", "required": true },
    { "role": "domain_reviewer", "required": true },
    { "role": "skeptic", "required": true },
    { "role": "consensus_chair", "required": true }
  ],
  "turn_policy": { "mode": "phase_script", "max_turns": 8 },
  "phases": [
    { "speaker": "source_finder", "task": "Find candidate sources and classify them." },
    { "speaker": "evidence_assessor", "task": "Assess whether the sources support the claim." },
    { "speaker": "domain_reviewer", "task": "Evaluate domain plausibility and missing context." },
    { "speaker": "skeptic", "task": "Challenge the strongest apparent conclusion." },
    { "speaker": "consensus_chair", "task": "Record decision, confidence, dissent, and recommended action." }
  ],
  "done_when": {
    "decision_states": [
      "verified",
      "partially_verified",
      "unsupported",
      "contradicted",
      "needs_human_review"
    ],
    "requires_evidence_ids": true
  }
}
```

This is stronger than round-robin for serious work. Round-robin remains good for
Phase 0 runtime testing; phase-scripted deliberation is the better default for
report improvement.

### PlanExe Boundary

PlanExe should remain the generator of the first structured plan/report. Rainbox
should be the artifact investigation and iteration system:

```text
PlanExe: generate the first structured plan/report.
Rainbox: map, verify, source, debate, patch, and evolve the report.
```

Rainbox should stay artifact-generic, but PlanExe reports are an ideal first
adapter because they are structured, sectioned, and rich in claims. A
`PlanExeReportAdapter` should parse sections, extract claims and assumptions,
identify documents-to-find, map WBS/timeline/risk surfaces, and create the first
review issue ledger.

## Decisions at a glance

The calls this proposal locks in, so a reader does not have to reverse-engineer
them from 1000 lines. Each is justified in its own section below.

| Decision | Choice | Why |
| --- | --- | --- |
| Persona storage | Files now (JSONL + Markdown), Postgres later | Prompts diff/review best as text |
| Prompt resolution | By `agent_uuid` at runtime, class constant as fallback | Pure superset; non-persona agents unchanged |
| Persona identity | Materialize as a `ChatUser`; v1 `agent_uuid == chat_user_uuid` | Distinct speaker bubbles need a distinct `sender_uuid` |
| Scheduler | Manager-as-agent, re-armed by dynamic return-address routing, including failed turns | Reuses inbox/journal/SIGKILL; personas stay usable outside managed runs |
| Turn driving | Explicit enqueue of the next speaker | Human-only trigger guard means nothing auto-advances |
| Speaker selection | Deterministic round-robin in v1 | LLM selection is harder to debug and loops |
| Concurrency | One in-flight turn per run (`active_turn` + compare-and-set advance) | Survives double-delivery / restarts |
| Termination | Phase 0: `min_turns` floor + `max_turns` + stop phrases + budgets + DB stop flag; later no-progress/evaluator | Bounded even if models never say DONE — and a premature DONE can't end it before `min_turns` |
| Recovery | Failed dynamic turns route to manager; stale processing uses reconcile tick | Exceptions wake the manager; SIGKILL/stale rows need reconciliation |
| Capability | Persona inherits only its `agent_kind`'s caps; v1 `chat_unstructured` only | No prompt-driven tool escalation |
| Dispatch | `agent_kind` splits role name from implementation class | Many persona roles share one Python class; later worker-pool keying by kind |
| Provenance (Phase 0) | In the journal result | `ChatMessage` has no metadata column today |
| DB safety | Additive migrations; iterate on `rainbox_claude` | Production is sacred |

## Architecture at a glance

```text
        operator (webapp)
            │ start run / set stop_requested
            ▼
   ┌─────────────────────┐      reads / writes      ┌────────────────────┐
   │  conversation_run   │◀────────────────────────▶│   manager agent    │
   │ turn, active_turn,  │                           │ (schedules turns,  │
   │ stop_requested,…    │                           │  no LLM of its own)│
   └─────────┬───────────┘                           └─────────┬──────────┘
             │ advance / finish                                │ enqueue(speaker)
             │                                                 ▼
   ┌─────────┴────────┐   routing pass: payload.return_to → mgr ┌───────────┐
   │   supervisor     │◀──────────────────────────────────▶│ inbox/journal│
   │ routing + watchdog│   spawn / SIGKILL                  │  (db_queue)  │
   └──────────────────┘                                     └──────┬───────┘
                                                                   │ take_item
                                                                   ▼
                                                       ┌────────────────────────┐
                                                       │     persona agents      │
                                                       │ chat_unstructured +     │
                                                       │ persona system prompt   │
                                                       └───────────┬─────────────┘
                                                                   │ post_chat_message
                                                                   ▼
                                              ChatMessage ──NOTIFY──▶ SSE /chat (live)
```

The only new box is `conversation_run` (state) and the manager (a normal agent).
Everything else already exists. See the [Glossary](#glossary) for the terms.

## How this maps onto Rainbox today

Before designing anything new, here is what already exists and where the new
concepts attach.

| Proposal concept | Already in Rainbox | Where |
| --- | --- | --- |
| Agent kind + capabilities + static routing | `agent_config` dict (`uuid`, `description`, `next`, capability flags) | `agent_config.py` (`AgentConfigEntry`) |
| Agent identity / speaker in a room | `ChatUser` (`name`, `user_type` ∈ {human, agent}); messages carry `sender_uuid` | `db_models.py` (`ChatUser`, `ChatMessage`) |
| "Run this agent" | `db.enqueue(agent_uuid, payload)` → `inbox` row | `db_queue.py:enqueue` |
| Agent does work | drain loop: `take_item` → `handle()` → `journal_update` | `agent.py` run loop; `db_queue.py` |
| Agent-to-agent handoff (linear) | supervisor routing pass: completed journal → `agent_config[role]["next"]` → `enqueue` | `main.py` routing pass |
| Isolation + liveness | one child process per role, SIGKILL after `HEARTBEAT_TIMEOUT=60s` of silence | `main.py` spawn/watchdog logic |
| Posting a turn / progress / diagnostics | `post_chat_message(room, sender, text, content_type, kind, streaming)`, `post_progress` | `db_chat.py` |
| Live UI updates | `NOTIFY` on post/update → SSE `/chat/stream` → browser upsert | `db_chat.py`, `webapp/chat_api.py` |
| Periodic / triggered runs | cron tick fires jobs (`message` / `command` / `backup`) | `main.py` cron pass, `db_cron.py` |
| Default model selection + fallback | `ModelGroupAgent` resolves a priority-ordered group; tries each in order | `agent.py` (`ModelGroupAgent`), `db_model_config.py` |

### Five hard truths that shape the design

These are the constraints the v1 draft did not account for. Each one is load
bearing.

1. **System prompts are class constants, not data.** `UNSTRUCTURED_CHAT_SYSTEM_PROMPT`
   (`agent_chat_unstructured.py`), `CHAT_SYSTEM_PROMPT` (`agent_chat_structured.py`),
   and `ROUTER_SYSTEM_PROMPT` (`router_agent.py`) are baked into the classes. A
   persona is, first and foremost, *a system prompt selected at runtime by the
   runnable agent identity*. **The linchpin change is making an agent resolve its
   system prompt from a persona record mapped to its `agent_uuid`, not from a
   constant.** Until that exists, "persona" is just metadata with nowhere to go.

2. **The supervisor runs one process per role name and routes each completion
   exactly once.** Running agents are keyed by role name (`name not in agents`),
   and the routing pass calls `mark_routed` on every completed journal row. This
   means: (a) two personas that share the agent kind `chat_unstructured` cannot
   both be live processes under the current keying, and (b) the static `next`
   pointer can model a *line* (dreamer → critic → verifier) but **cannot model a
   bounded loop** — there is no turn counter, no cycle, and no stop test. A
   conversation is a loop with state and a budget, so it needs a driver the
   routing pass does not provide.

   **Important refinement:** conversation participants must *not* statically set
   `agent_config["next"] = MANAGER_UUID`. That would lock the persona into group
   chat and break standalone use. Conversation turns need a **dynamic return
   address** in the queued payload, and the routing pass should prefer that over
   static `next`.

3. **Only human messages trigger responders.** `_maybe_trigger_chat_agents`
   returns early unless the sender's `user_type == "human"` (`chat_api.py`). This
   is the existing loop-prevention: an agent's reply never re-triggers anyone.
   For agent-to-agent this is a *gift and a gotcha*. Gift: no accidental
   runaway loops. Gotcha: nothing advances a multi-agent conversation unless
   something **explicitly enqueues the next speaker**. That "something" is the
   new primitive.

4. **The agent↔supervisor socket is effectively one-way after config**, and an
   agent emits no heartbeat *during* `handle()` (see `agent.py` KNOWN ISSUES #2).
   A single LLM turn slower than 60s risks a watchdog SIGKILL. So: operator
   "stop" cannot be a socket command — it must be a DB flag the scheduler reads.
   Slow conversation turns need a dedicated base-agent heartbeat during
   `handle()`, independent of token streaming or DB writes.

5. **Agent class dispatch is currently tied to role name.** In `agent.py`, the
   `agent_classes` lookup uses `config["name"]`. A new role called
   `persona_egon` would therefore run as a plain `ModelGroupAgent`, not as
   `UnstructuredChatAgent`, unless the dispatch is changed. Persona roles need
   a separate `agent_kind` field so many runnable roles can share one Python
   implementation.

### Identity model

Do not overload one UUID with every meaning in the design. Rainbox currently
uses the same UUID for a code-defined agent and its seeded `ChatUser`, but
personas introduce another identity. Treat these as separate concepts even when
v1 deliberately collapses some of them:

- `persona_id`: stable identity for the persona definition and prompt history.
- `agent_role`: code-side role name the supervisor can spawn, e.g.
  `persona_egon`.
- `agent_kind`: Python implementation to run, e.g. `chat_unstructured`.
- `agent_uuid`: runnable identity used by the inbox, journal, supervisor,
  `agent_config`, and model binding.
- `chat_user_uuid`: visible speaker identity used by `ChatMessage.sender_uuid`.

For the smallest v1, set `agent_uuid == chat_user_uuid` because that matches
today's seeded chat-user model, and map `agent_uuid -> persona_id` in the
persona loader. Keep `persona_id` separate so prompt revisions and future DB
records do not depend on runnable process identity.

## Goals

- Make custom system prompts first-class **data**, resolved per agent identity.
- Let two (then N) personas talk to each other in a chatroom, visibly, as
  distinct speakers in the transcript.
- Reuse the inbox/journal/supervisor/SIGKILL machinery instead of inventing a
  parallel runtime.
- Keep orchestration **deterministic, bounded, observable, and stoppable** from
  day one.
- Keep prompts and templates easy to version-control while they evolve quickly.
- Keep persona data out of `agent_config.py`, which describes implementation-level
  capabilities, not behavior.
- Leave a direct path to a Postgres-backed prompt library, prompt revisions, and
  a UI.

## Non-goals

- No autonomous agent spawning.
- No multiple write-capable agents mutating shared state in parallel.
- No complex graph runtime before the two-agent case works.
- No LLM-selected next-speaker routing in v1 (deterministic only).
- No tool/MCP access inside persona conversations in v1.
- No large escaped prompt strings inside JSONL.

## The core new primitive: a bounded conversation manager

A conversation is a **loop with state and a budget**. Rainbox has no construct
for that yet, so we add exactly one: a **conversation manager**. Crucially, the
manager is *itself an ordinary Rainbox agent kind* — it drains the inbox, runs in
an isolated child process, journals its work, and is SIGKILL-bounded like any
other agent. It just happens to do no LLM work of its own; it schedules turns.

This mirrors AutoGen's `GroupChatManager`, but implemented on Rainbox's queue
rather than an in-process event loop, so it inherits crash-recovery and isolation
for free.

### The turn loop

State for one conversation run lives in a `conversation_run` row (see data model)
keyed by `run_uuid`, with a bound chatroom and a snapshot of the participants
and turn policy. One "tick" of the manager:

1. **Drain a manager job** such as `{"run_uuid": "..."}` from the inbox via the
   normal `take_item` path. Queue payloads must contain JSON primitives, so UUIDs
   are serialized as strings.
2. **Load run state**: turn counter, participant list + turn order, policy
   (`round_robin`, `max_turns`, `stop_phrases`, budgets), and a `stop_requested`
   flag.
3. **Check stop conditions** *before* spending a turn:
   - `stop_requested` set by the operator (a DB flag — the only safe channel,
     per hard truth #4),
   - `turn >= max_turns`,
   - token / wall-clock budget exhausted,
   - last message contains a stop phrase (`DONE`, `NO_REPLY`),
   - optional later no-progress/evaluator policy tripped.
   If any holds: post a final `kind="message"` summary line, mark the run
   `finished`, and stop. Do **not** re-enqueue.
4. **Pick the next speaker** deterministically from the turn policy.
5. **Enqueue that persona's turn** with a JSON-safe payload:
   `{"run_uuid": "...", "turn": 3, "room_uuid": "...", "persona_id": "...",
   "expected_speaker_uuid": "..."}`. The speaker agent resolves its persona
   prompt by its runnable `agent_uuid`, verifies that the payload matches the
   expected persona/run, reads the transcript, and posts its reply as a normal
   chat message — exactly the existing chat-agent code path, minus the human
   trigger.
6. **Re-arm the manager.** When the speaker's journal row completes, the
   supervisor routing pass sees the dynamic return address from the original
   payload/result and enqueues the manager. The manager advances the run by
   reading `from_journal_id`, the original speaker payload, and the result.
   Static `next` remains available for existing linear agents, but conversation
   runs use `return_to_agent_uuid` so a persona can also be used in a normal
   one-on-one chatroom without calling the manager. The manager therefore
   handles two payload shapes: an initial/manual tick with only `run_uuid`, and a
   routed speaker-completion tick with `from_journal_id`, `input`, and `result`.
   This reuses the existing routing pass without pretending that static routing
   understands loops.

The conversation therefore advances as: `manager → speaker → (routing pass) →
manager → speaker → …` until a stop condition fires. One writer at a time, fully
serialized, every step a journal row, every turn a chat message.

### Why the manager is an agent, not supervisor code

Three implementation options were considered:

- **(A) Manager as an agent kind (recommended).** Keeps the supervisor dumb,
  reuses inbox/journal/SIGKILL/recovery, and is observable via the journal and a
  `debug-conversation` chat row. A manager bug kills one child process, not the
  supervisor.
- **(B) Put loop logic in the supervisor routing pass** (turn counters, stop
  tests). Rejected: it bloats the one thread that must never crash. The code
  already comments that "a cron bug must not take down the supervisor"; the same
  caution applies here. A small dynamic return-address check is acceptable; the
  manager still owns the loop state and stop decisions.
- **(C) Cron-driven "advance one turn."** Useful as a *manual stepping* mode for
  debugging (a cron `command`/`message`-style action that advances one tick), but
  too coarse (5s tick) and stateless to be the primary driver. Keep it as the
  operator's single-step button, not the engine.

Recommendation: **(A)**, with **(C)** available for manual single-stepping during
bring-up.

### Operator stop, budgets, and recovery

- **Stop** is a `stop_requested` boolean on `conversation_run`, set by a webapp
  button. The manager checks it at step 3. Because a turn already in flight runs
  in its own process, an immediate hard stop can also SIGKILL that child (the
  benchmark runner already does exactly this pattern with a stop flag + kill).
- **Budgets**: `max_turns` (required), optional wall-clock and token ceilings.
  Token-ish counts are already observable in some benchmark paths via
  reasoning/content instrumentation; for Phase 0, record wall-clock and character
  counts on the journal result, then add true token accounting only where the
  provider/model path exposes it reliably.
- **Recovery**: a normal exception becomes a `failed` journal row. With dynamic
  `_routing` preserved in failed results, the supervisor can route that failure
  back to the manager for retry/fail handling. A SIGKILL during `handle()` can
  still leave a stale `processing` journal row with no routed completion at all.
  Therefore Phase 0 also needs one explicit recovery path: a manual/admin
  "reconcile run" tick, or a small manager-side stale-turn check enqueued by
  cron/operator. It finds a turn stuck past a timeout, marks the run `failed` or
  retries once, and never relies on any routing pass firing after a killed child.
- **Idempotency**: the manager must be safe to run twice for the same completed
  speaker journal. Store `last_speaker_journal_id` plus the expected `turn` on
  `conversation_run` and update the row transactionally before enqueueing the
  next speaker. This prevents duplicate turns if the supervisor or manager
  restarts around the routing boundary.

### Slow turns vs the 60s watchdog — IMPLEMENTED

A reasoning model can stream for more than `HEARTBEAT_TIMEOUT=60s`, and agents did
not heartbeat during `handle()`. Conversation turns are exactly where this bit.
Liveness is deliberately **not** tied to streaming DB flushes (a model can think
for 90s without emitting a token). The durable fix lives in `agent.py`:
`Agent.run` wraps `handle()` in `_handle_with_heartbeat`, which starts a daemon
thread that emits a `heartbeat` status every `HEARTBEAT_INTERVAL` (20s, a class
attribute) over the existing supervisor socket, independent of model output, and
stops it in a `finally`. All sends go through `_emit` under a lock so the
heartbeat and main loop can't interleave on the socket; the supervisor resets its
silence timer on any message and skips logging heartbeats. Reasoning models are
therefore safe in persona conversations.

## Implementation sketch (code seams)

Concrete signatures so this is buildable, not merely describable. The names are
illustrative — match the real helpers when implementing — but the seams are real.

### 1. Split role from implementation (the dispatch change)

`AgentConfigEntry` gains an optional `agent_kind`; the spawn config carries it;
`agent.py` dispatches on it instead of the role name:

```python
# agent_config.py
class AgentConfigEntry(TypedDict):
    uuid: UUID
    description: str
    next: UUID | None
    agent_kind: NotRequired[str]   # NEW: which class to run; defaults to role name
    requires_function_calling: NotRequired[bool]
    requires_structured_output: NotRequired[bool]
    excludes_structured_output: NotRequired[bool]

# agent.py main(): today it is  agent_classes.get(config["name"], ModelGroupAgent)
kind = config.get("agent_kind", config["name"])
agent_cls = agent_classes.get(kind, ModelGroupAgent)
```

Every existing role keeps working: its role name already equals its
implementation key, so `get("agent_kind", name)` is a no-op for them. `main.py`'s
`spawn()` must include `agent_kind` in the JSON config line it sends the child.

### 2. Persona resolver and record

```python
@dataclass(frozen=True)
class Persona:
    persona_id: UUID
    slug: str
    name: str
    system_prompt: str          # already-read prompt body
    prompt_sha256: str          # provenance stamp recorded on each turn/journal
    agent_kind: str
    agent_uuid: UUID            # runnable identity (inbox/journal/spawn)
    chat_user_uuid: UUID        # visible speaker (v1: == agent_uuid)

def resolve_persona_for_agent(agent_uuid: UUID) -> Persona | None:
    """File-backed now (parse personas.jsonl, read the prompt file), DB-backed
    later. Cached per process. Returns None for non-persona agents."""
```

### 3. The chat agent change (one line)

```python
# in UnstructuredChatAgent (and siblings), at prompt-build time
persona = resolve_persona_for_agent(self.agent_uuid)
system_prompt = persona.system_prompt if persona else UNSTRUCTURED_CHAT_SYSTEM_PROMPT
```

### 4. Payload shapes and dynamic return address

All payloads use JSON primitives because the queue serializes payloads as JSON.
The `return_to_agent_uuid` field is a dynamic return address: the routing pass
uses it instead of static `agent_config["next"]` for this one completion.

```jsonc
// initial / manual tick → manager
{"run_uuid": "...", "kind": "tick", "expected_tick_count": 0}
// speaker turn → persona agent
{"run_uuid": "...", "turn": 3, "room_uuid": "...", "persona_id": "...", "expected_speaker_uuid": "...", "return_to_agent_uuid": "..."}
// routed speaker-completion → manager (built by the supervisor routing pass)
{"from": "persona_egon", "from_journal_id": 412, "input": {/* speaker turn */}, "result": {/* agent result */}}
```

Routing pass rule:

```python
dynamic_next = ((journal_row["result"] or {}).get("_routing") or {}).get("return_to_agent_uuid")
static_next = agent_config[src_role]["next"] if src_role else None
next_uuid = UUID(dynamic_next) if dynamic_next else (
    static_next if journal_row["state"] == "completed" else None
)
```

The base `Agent.run()` loop should copy a recognized routing key from payload to
`result["_routing"]` after `handle()` returns **and inside the exception handler
that journals `failed`**. Do not trust model-authored text or arbitrary agent
results to choose routing targets. The payload was created by the manager; the
model output was not. The supervisor may route failed rows only when a dynamic
`_routing.return_to_agent_uuid` is present; static `next` remains success-only.

### 5. The manager turn (pseudocode)

```python
class ConversationManagerAgent(Agent):
    def handle(self, journal_id, payload):
        run_uuid = _run_uuid_from(payload)          # "run_uuid", else payload["input"]["run_uuid"]
        run = db.get_conversation_run(run_uuid)
        if run is None or run.status != "running":
            return {"ok": True, "skipped": "run not active"}

        # Manual/start/resume ticks are idempotent too. A double-clicked resume
        # should not enqueue two speakers for the same turn.
        if "from_journal_id" not in payload:
            expected = payload.get("expected_tick_count")
            if expected is None or not db.claim_conversation_tick(run_uuid, expected):
                return {"ok": True, "skipped": "stale manual tick", "expected_tick_count": expected}

        # Idempotency: a routed completion advances the run at most once.
        src = payload.get("from_journal_id")
        completed_turn = _completed_turn_from(payload)  # None for initial/manual tick
        if src is not None and not db.advance_conversation_if_new(run_uuid, src, completed_turn):
            return {"ok": True, "skipped": "already advanced", "journal": src}

        interruption = db.find_human_message_after(run.room_uuid, run.last_human_message_id)
        if interruption is not None:
            db.pause_conversation(run_uuid, reason="human_interruption", last_human_message_id=interruption.id)
            db.post_chat_message(run.room_uuid, MANAGER_UUID, "Conversation paused: human message received.", kind="debug-conversation")
            return {"ok": True, "paused": "human_interruption"}

        stop = evaluate_stop(run, db.list_room_messages(run.room_uuid))
        if stop.should_stop:
            db.finish_conversation(run_uuid, status=stop.status, reason=stop.reason)
            db.post_chat_message(run.room_uuid, MANAGER_UUID, stop.summary, kind="message")
            return {"ok": True, "stopped": stop.reason}

        if run.active_turn is not None:
            return {"ok": True, "skipped": "speaker already in flight", "turn": run.active_turn}

        speaker = next_speaker(run.participants, run.turn)     # pure function
        db.mark_conversation_turn_in_flight(run_uuid, run.turn, speaker.agent_uuid)
        db.post_chat_message(
            run.room_uuid, MANAGER_UUID,
            json.dumps({"next": speaker.slug, "turn": run.turn, "budget_left": stop.budget_left}),
            content_type="json", kind="debug-conversation",
        )
        db.enqueue(speaker.agent_uuid, {
            "run_uuid": str(run_uuid), "turn": run.turn, "room_uuid": str(run.room_uuid),
            "persona_id": str(speaker.persona_id), "expected_speaker_uuid": str(speaker.agent_uuid),
            "return_to_agent_uuid": str(MANAGER_UUID),
        })
        return {"ok": True, "enqueued": speaker.slug, "turn": run.turn}

def next_speaker(participants, turn):              # trivially unit-testable
    ordered = sorted(participants, key=lambda p: p.turn_order)
    return ordered[turn % len(ordered)]
```

`advance_conversation_if_new(run_uuid, src_journal, completed_turn)` is one
transactional compare-and-set — the entire concurrency story (see invariants).
It must reject duplicates and stale older completions:

```sql
UPDATE conversation_run
   SET last_speaker_journal_id = :j,
       turn = turn + 1,
       active_turn = NULL,
       active_speaker_uuid = NULL,
       active_turn_enqueued_at = NULL,
       updated_at = now()
 WHERE id = :run
   AND status = 'running'
   AND turn = :completed_turn
   AND active_turn = :completed_turn
   AND (
     last_speaker_journal_id IS NULL OR last_speaker_journal_id < :j
   );
-- returns whether a row changed
```

Using `< :j` matters: `<> :j` would allow an older completion to advance the run
after a newer one had already been processed.

Do not store `active_journal_id` at enqueue time: Rainbox creates the journal row
later, inside `take_item()`, not when `db.enqueue()` inserts the inbox row.
Instead, set the active logical turn immediately before enqueueing
(`active_turn`, `active_speaker_uuid`, `active_turn_enqueued_at`) and let
reconciliation find matching inbox/journal rows by `{run_uuid, turn, speaker}`
if recovery is needed. If enqueue fails after marking the turn active, the same
reconcile path can clear/retry it; this is why Phase 0 must be idempotent across
helper commit boundaries.

Manual ticks (`start`, `resume`, and single-step) need the same protection as
routed completions. Use a monotonic `tick_count` CAS:

```sql
UPDATE conversation_run
   SET tick_count = tick_count + 1,
       updated_at = now()
 WHERE id = :run
   AND status = 'running'
   AND tick_count = :expected_tick_count;
-- returns whether this manual tick owns the scheduling decision
```

The start/resume endpoint reads the current `tick_count`, includes it as
`expected_tick_count` in the manager payload, and treats duplicate button clicks
as harmless stale ticks.

For resume from `paused` or `failed`, the endpoint first performs a small
operator-owned transition (`status='running'`, clear stale active-turn fields as
needed, update `last_human_message_id` for a human-interruption resume), then
enqueues the manager tick with the pre-read `expected_tick_count`. If two resume
requests race, both may enqueue, but only one manager invocation can claim the
tick.

### Files touched (Phase 0)

| File | Change |
| --- | --- |
| `agent_config.py` | add `agent_kind` to `AgentConfigEntry`; add `persona_egon` / `persona_benny` roles with `next = None`; add the `conversation` manager role |
| `agent.py` | dispatch on `config.get("agent_kind", config["name"])`; copy safe dynamic routing keys from payload to `result["_routing"]` on success and failure |
| `main.py` | include `agent_kind` in the spawned config line; route completed/failed rows with `result["_routing"]["return_to_agent_uuid"]`, and use static `next` only for completed rows |
| `db_queue.py` | expose unrouted terminal rows with `state` and `result`, including failed rows that carry dynamic `_routing` |
| `agent_chat_unstructured.py` | system prompt = persona or the existing constant |
| `persona.py` *(new)* | `Persona` dataclass + `resolve_persona_for_agent` + JSONL loader/validator |
| `agent_conversation.py` *(new)* | `ConversationManagerAgent` (the turn loop) |
| `db_*.py` | `conversation_run` table + `get/advance/claim_tick/finish/reconcile` helpers; seed persona and manager `ChatUser`s (reuse `seed_chat_defaults`) |
| `webapp/...` | admin endpoints: start a run, set `stop_requested` |
| `agent_profiles/` *(new)* | `personas.jsonl`, `prompts/*.system.md`, `conversations/*.json` |

## Worked example: a two-turn run, event by event

Egon (`persona_egon`) and Benny (`persona_benny`) in room R, `max_turns=6`,
round-robin, manager role `conversation` (`MANAGER_UUID`). Persona roles have
`next = None`; they return to the manager only when the manager put
`return_to_agent_uuid=MANAGER_UUID` in that specific turn payload.

```text
t0  operator starts run → conversation_run{run, room=R, status=running, turn=0}
                          last_human_message_id=max human message id in R
                        → enqueue(MANAGER_UUID, {run_uuid:run, kind:tick, expected_tick_count:0})

t1  supervisor spawns `conversation`; manager.handle:
      claim_conversation_tick(run, expected_tick_count=0) → True (tick_count 0→1)
      stop? no (turn 0 < 6); next_speaker(0) = Egon
      mark active_turn=0, active_speaker_uuid=EGON_UUID
      post debug-conversation {next:egon, turn:0}
      enqueue(EGON_UUID, {run_uuid:run, turn:0, room:R, persona_id:egon, expected:EGON_UUID, return_to_agent_uuid:MANAGER_UUID})
    journal#1 (manager) completed. routing pass: conversation.next is None → nothing.

t2  supervisor spawns `persona_egon` (runs UnstructuredChatAgent via agent_kind):
      resolve_persona_for_agent(EGON_UUID) → Egon prompt
      posts ChatMessage(sender=EGON_UUID, kind=message, "…plan…")
    journal#2 (persona_egon, payload=turn0) completed.
    routing pass: result._routing.return_to_agent_uuid = MANAGER_UUID →
      enqueue(MANAGER_UUID, {from:persona_egon, from_journal_id:2, input:{turn0…}, result:{…}})
      mark_routed(2)

t3  manager.handle (routed tick):
      advance_conversation_if_new(run, 2, completed_turn=0) → True
        (turn 0→1, clears active_turn)
      stop? last msg has no stop phrase, 1 < 6 → no; next_speaker(1) = Benny
      mark active_turn=1, active_speaker_uuid=BENNY_UUID
      enqueue(BENNY_UUID, {turn:1, …})
    journal#3 completed (next None).

t4  persona_benny posts ChatMessage(sender=BENNY_UUID, "…counter… DONE")
    routing pass → enqueue(MANAGER_UUID, {from:persona_benny, from_journal_id:4, …}); mark_routed(4)

t5  manager.handle (routed tick):
      advance_conversation_if_new(run, 4, completed_turn=1) → True
        (turn 1→2, clears active_turn)
      stop? last msg contains "DONE" → yes
      finish_conversation(run, status=finished, reason=stop_phrase); post summary. Loop ends.
```

The persona's static `next` stays `None`; only the manager's payload says "return
to the manager after this particular turn." The stateful part — which run, which
turn, whether to stop — lives in `conversation_run` and the routed payload.
`mark_routed` keeps its once-semantics; `advance_conversation_if_new` keeps the
manager idempotent even if it is ticked twice for journal 2 or 4.

## Persona as data (filesystem first)

### Filesystem layout

```text
agent_profiles/
  personas.jsonl
  prompts/
    egon.system.md
    benny.system.md
    critic.system.md
  conversations/
    egon-benny.json
  README.md
```

Metadata stays machine-readable; prompt bodies stay pleasant to edit and diff.

### Persona metadata

`agent_profiles/personas.jsonl`, one record per line:

```jsonl
{"id":"9b55d40d-c7d5-47f8-a4c4-53c43e8c1234","slug":"egon","name":"Egon","description":"Planner persona inspired by Egon Olsen.","system_prompt_path":"prompts/egon.system.md","agent_kind":"chat_unstructured","agent_role":"persona_egon","agent_uuid":"c9e2669f-2d7d-4e7d-827e-e6c7eaf3c2fb","tags":["fun","planner"],"enabled":true}
{"id":"65f61f0e-908e-4e4c-9404-6b5d4fd94671","slug":"benny","name":"Benny","description":"Practical sidekick that reacts to plans and asks for concrete next steps.","system_prompt_path":"prompts/benny.system.md","agent_kind":"chat_unstructured","agent_role":"persona_benny","agent_uuid":"20bcb996-771c-4d87-86e3-28421c0a866b","tags":["fun","operator"],"enabled":true}
```

Use valid JSON, ASCII quotes, and Python-friendly keys (`system_prompt_path`, not
`system-prompt`). Recommended fields: `id` (stable UUID for the DB migration),
`slug` (filenames/URLs), `name` (display), `description` (selection/routing),
`system_prompt_path` (relative to `agent_profiles/`), `agent_kind` (which runtime
to drive this persona), `agent_role` (the supervisor role name), `agent_uuid`
(the runnable identity), `tags`, `enabled`. Optional later:
`chat_user_uuid` (if it diverges from `agent_uuid`), `model_group_uuid`
(override the default binding), `prompt_version` or `prompt_sha256`
(checksum for diagnostics), `created_at`/`updated_at`.

### How a persona actually reaches the model (the linchpin)

A persona is inert until an agent loads its prompt by identity. The minimal,
reversible change:

1. Add a resolver, `resolve_persona_for_agent(agent_uuid) -> Persona | None`,
   backed by the file loader now and a DB query later. On miss, return `None`.
2. In each chat agent, replace the class-constant system prompt with
   `persona.system_prompt if persona else <existing constant>`. The constant
   becomes the default, so non-persona agents behave exactly as today — a pure
   superset change.
3. The persona's runnable `agent_uuid` materializes as a `ChatUser`
   (`user_type="agent"`) so it appears as a distinct speaker. Messages already
   carry `sender_uuid` -> rendered sender name; without a distinct `ChatUser`,
   two personas would be indistinguishable in the transcript. (This decisively
   answers the v1 open question: materialize personas as chat users.)
4. The loader must validate that `agent_role` and `agent_uuid` agree with the
   generated or static `agent_config` entry. A persona whose runnable identity
   cannot be spawned should fail at startup, not at first turn.

This is the smallest change that turns "hardcoded assistant" into
"data-driven persona" without changing the queue or the chat UI. It does require
one runtime refactor: agent class dispatch must use `agent_kind` instead of only
the role name.

### Prompt bodies

Markdown, outside JSONL:

```md
You are Egon.

You are inspired by Egon Olsen. You always have a plan.
You speak confidently, but you must still be useful, concrete, and concise.

When talking with another agent:
- State the plan.
- Ask for objections.
- Revise the plan when the objection is valid.
- When the plan is agreed or no progress is being made, end your message with DONE.
```

Multiline prompts stay readable, diffs stay useful, prompts can carry headings
and checklists without escaping, and each file later imports cleanly into a
`persona_prompt_revision` row. Note the explicit `DONE` stop phrase — termination
is part of the prompt *and* enforced by the manager (never prompt-only).

### Conversation templates

`agent_profiles/conversations/egon-benny.json`:

```json
{
  "id": "egon-benny",
  "name": "Egon and Benny",
  "participants": [
    { "persona_slug": "egon",  "agent_kind": "chat_unstructured", "turn_order": 1 },
    { "persona_slug": "benny", "agent_kind": "chat_unstructured", "turn_order": 2 }
  ],
  "turn_policy": {
    "mode": "round_robin",
    "min_turns": 4,
    "max_turns": 8,
    "stop_phrases": ["DONE", "NO_REPLY"],
    "max_wall_clock_seconds": 600
  }
}
```

The template composes personas with agent kinds and a turn policy; it never
duplicates prompt text. `min_turns` is the convergence floor: the manager ignores
stop phrases until that many turns have happened, so a small model that blurts
`DONE` on turn 0 still produces a real exchange (this is what the shipped
`egon-benny` template uses).

## Long-term database model

The file layout maps directly to Postgres, **reusing the existing chat tables**.
New tables are persona/prompt/conversation-shaped only; the transcript stays in
`ChatMessage`, identities in `ChatUser`.

```text
persona
  id uuid primary key
  slug text unique not null
  name text not null
  description text not null default ''
  agent_kind text not null            -- which runtime drives this persona
  agent_role text null                -- v1 supervisor role name, e.g. persona_egon
  agent_uuid uuid null                -- runnable agent identity used by inbox/journal
  chat_user_uuid uuid null            -- visible speaker (v1 may equal agent_uuid)
  model_group_uuid uuid null          -- optional binding override
  tags jsonb not null default '[]'
  enabled boolean not null default true
  created_at timestamptz not null
  updated_at timestamptz not null

persona_prompt_revision
  id uuid primary key
  persona_id uuid not null references persona(id)
  body text not null
  source_path text null
  version integer not null
  created_at timestamptz not null
  created_by text null

conversation_template
  id uuid primary key
  slug text unique not null
  name text not null
  turn_policy jsonb not null
  enabled boolean not null default true
  created_at timestamptz not null
  updated_at timestamptz not null

conversation_template_participant
  id uuid primary key
  template_id uuid not null references conversation_template(id)
  persona_id uuid not null references persona(id)
  agent_kind text not null
  turn_order integer not null
  config jsonb not null default '{}'

conversation_run                      -- the live, bounded state the manager reads
  id uuid primary key
  template_id uuid null references conversation_template(id)
  room_uuid uuid not null             -- existing Chatroom
  status text not null                -- running | paused | finished | failed | stopped
  turn integer not null default 0
  tick_count integer not null default 0 -- idempotency guard for manual ticks
  participants jsonb not null         -- snapshot of persona/order/agent uuids
  turn_policy jsonb not null          -- snapshot so template edits do not affect runs
  last_speaker_journal_id integer null -- idempotency guard for routed speaker turns
  active_turn integer null            -- currently expected logical turn, if any
  active_speaker_uuid uuid null       -- currently expected speaker, if any
  active_turn_enqueued_at timestamptz null
  last_human_message_id integer null  -- interruption watermark
  retry_count integer not null default 0
  stop_requested boolean not null default false
  budget jsonb not null default '{}'  -- max_turns, token/wall-clock ceilings, spent
  created_at timestamptz not null
  updated_at timestamptz not null
```

Per-turn lineage reuses the **journal** (one row per turn) first. Today
`ChatMessage` has no metadata column, so Phase 0 should record
`{run_uuid, turn, persona_id, prompt_sha256}` in the speaker journal result and,
if needed for UI, emit a `debug-conversation` JSON row beside the visible
message. Do not pretend message metadata exists until a migration adds it. A
later additive migration can add a SQL column named `chat_message.metadata`
mapped in SQLAlchemy as `ChatMessage.metadata_` (matching the existing
`FeedbackEvent.metadata_` / `RetrievalEvent.metadata_` pattern), or add a
`conversation_turn` table if message-level provenance becomes worth querying
directly. Prompt revisions matter because a prompt change can radically alter
behavior; recording the revision or SHA per turn makes failures reproducible.
`conversation_run` is the only genuinely new runtime state — everything else is
description or transcript.

## Migration and data safety

- **Honor the prod/claude split.** Per `CLAUDE.md`, `rainbox_production` is sacred
  and `rainbox_claude` is the sandbox; `conftest.py` already pins tests to
  `rainbox_claude`. All schema iteration and the fake-LLM integration tests run
  against claude; only the final, reviewed migration touches production, and never
  with a destructive statement.
- **Additive migrations only.** The new tables (`persona`,
  `persona_prompt_revision`, `conversation_template`,
  `conversation_template_participant`, `conversation_run`) are pure additions; no
  existing table changes in Phase 0–2. The Phase 3 `ChatUser` display columns
  (`display_name`, `avatar`, `color`) are nullable adds.
- **`conversation_run` is the only hot, mutable table.** Everything else is
  append-only or descriptive. The critical state transition should be
  transactional. Existing helpers such as `post_chat_message()` and `enqueue()`
  commit internally today, so either add no-commit variants for the manager path
  or keep the Phase 0 design idempotent across those commit boundaries. Do not
  claim all tick side effects are atomic unless the helpers are changed.
- **Prompt revisions are append-only.** Mirror the existing immutable-arguments
  discipline on model configs: never mutate a `persona_prompt_revision.body`; a
  prompt edit inserts a new revision, and messages reference the revision id/sha
  that produced them.
- **Seed personas like chat defaults.** Reuse the idempotent `seed_chat_defaults`
  pattern to materialize each persona's `agent_uuid` and the manager's
  `MANAGER_UUID` as `ChatUser`s at startup; re-running is a no-op.
- **File is source of truth until Phase 3.** The loader reads `agent_profiles/`;
  the DB import is one-way (file → DB) until the UI becomes the editor, after
  which export keeps prompts reviewable in git.

## Rainbox-specific design decisions and gotchas

These are sharp, non-obvious consequences of the current code. Get them wrong and
the feature misbehaves in confusing ways.

- **Participants must not also be auto-responders.** If a persona's underlying
  agent uuid is in `CHAT_RESPONDER_UUIDS`, then a human posting in the room would
  trigger it via the human guard *and* the manager would drive it — double
  activation, interleaved replies. Conversation participants should be **distinct
  agent identities** (their own `agent_config` entries / uuids) that are *not* in
  `CHAT_RESPONDER_UUIDS`. The manager is the sole driver inside a run.
- **One process per role name.** Two personas on the same agent kind cannot both
  be live under the current spawn keying (`name not in agents`). Options: give
  each persona its own `agent_config` role+uuid (simplest, fits the existing
  model), or rework spawn keying to key by `agent_uuid` instead of role name
  (more invasive). v1 should take the former.
- **Role name and implementation kind must split.** A persona role named
  `persona_egon` should still run the `chat_unstructured` class. Add
  `agent_kind` to `AgentConfigEntry`, send it in the spawn config, and make
  `agent.py` choose the class with `config.get("agent_kind", config["name"])`.
  Existing agents keep working because their role name already equals their
  implementation key.
- **Stop is a DB flag, not a socket message.** The agent socket is one-way after
  config; `stop_requested` on `conversation_run` is the supported channel, with an
  optional SIGKILL of the in-flight turn for a hard stop.
- **Manager ticks add visible latency.** One visible persona message requires a
  manager tick, a speaker turn, routing back to the manager, and then another
  manager tick. The durable queue/journal path is worth it, but the UI must make
  the mechanical gap feel alive: the manager should post `debug-conversation`
  rows early enough for `/chat` or `/conversations` to show "manager routing" /
  "Egon is next" rather than looking idle.
- **Human interruption pauses Phase 0 runs.** Persona UUIDs are excluded from
  auto-response, but humans can still type into the room while a turn is in
  flight. On the manager's next tick, before enqueueing another speaker, compare
  the room's newest human `ChatMessage.id` with `last_human_message_id` and with
  the active turn window. If a human message arrived during the run since the
  last manager decision, set `status='paused'`, post a summary/debug row, and do
  not enqueue the next speaker. A resume endpoint can continue after the human
  message is now part of the transcript. Later templates may choose an
  `interruption_policy='address_next'`, but Phase 0 should pause.
  Accepted Phase 0 UX caveat: the in-flight speaker can still finish and post a
  stale response because there is no cancellation channel into `handle()`. The
  guarantee is that the manager pauses **before the following speaker**. On
  resume, the interrupted human message must be pinned into the context builder
  even if it would otherwise fall outside `last_turns_k`.
- **Future immediate interruption needs process-aware cancellation.** Rainbox has
  the ingredients for a hard interruption (DB flag + SIGKILL precedent), but the
  manager/webapp must know the active speaker's running process and must journal
  the outcome as `interrupted` or `failed` without leaving a stale reply. Until
  that exists, "pause before the following speaker" is the correct guarantee.
- **Slow turns no longer need SIGKILL for liveness.** The base-agent heartbeat
  sends `heartbeat` status during `handle()`, so the 60s supervisor watchdog no
  longer kills healthy reasoning turns just because they have not emitted text.
- **Dynamic return-address routing.** Do not point participant roles'
  `agent_config["next"]` at the manager. The manager includes
  `return_to_agent_uuid` in the speaker payload, the base agent preserves that
  key in the journal result, and the supervisor routing pass prefers it over
  static `next`. This keeps personas usable outside managed runs while preserving
  `mark_routed` once-semantics.
- **Retry CAS must stay turn-first.** `last_speaker_journal_id < src_journal_id`
  is only safe because the CAS also checks the logical `turn` and `active_turn`.
  Keep tests where a failed turn is retried with a higher journal id for the
  *same* logical turn, then succeeds, so the monotonic journal guard never
  becomes a substitute for turn matching.
- **Conversation memory can cross-contaminate tasks.** Distinct persona UUIDs
  isolate Egon from Benny, but they also let Egon remember across unrelated rooms
  and runs. That is useful for stable personal traits and preferences, but toxic
  for task crews if database-migration facts bleed into a CSS conversation.
  Conversation-run retrieval should either filter by `room_uuid`/`run_uuid` or
  heavily weight same-room memories before enabling cross-room persona memory.
- **Static `agent_config` vs generated persona roles.** For Phase 0, explicit
  static entries are acceptable because there are only two personas. For a
  larger persona library, generate runtime entries from `personas.jsonl` during
  startup before the supervisor builds `uuid_to_role`. Do not hand-edit dozens
  of persona roles into `agent_config.py`.
- **Persona process scaling.** One role per persona is a walking-skeleton
  compromise. Before supporting large persona libraries, add a worker-pool path:
  either key running workers by `agent_kind` and let a `chat_unstructured` worker
  drain persona-tagged jobs, or add queue fields that let one implementation
  process service many persona identities. Until then, cap the enabled persona
  count and document that simultaneous persona runs can spawn one child process
  per active persona.

## Correctness invariants

State them explicitly; the tests assert them.

1. **At most one turn in flight per run.** The manager enqueues exactly one
   speaker per tick, stores `active_turn` / `active_speaker_uuid` /
   `active_turn_enqueued_at`, and normally re-arms on a routed completion. With
   one `conversation` process per role plus the compare-and-set advance, two
   manager jobs for the same completion cannot both move the run forward.
2. **Each speaker completion advances the run at most once.** Guaranteed by
   `advance_conversation_if_new(run, from_journal_id, completed_turn)` — a
   single conditional `UPDATE` that checks status, expected turn, and monotonic
   journal id.
3. **A paused/finished/stopped/failed run never enqueues.** The manager returns
   early unless `status == running`; resume is an explicit operator action.
4. **Every turn is attributable.** One journal row per turn; one `ChatMessage`
   for the visible reply; provenance in Phase 0 lives in the journal result and
   optional `debug-conversation` JSON row, not in `ChatMessage` metadata.
5. **No participant auto-responds.** Persona agent uuids are excluded from
   `CHAT_RESPONDER_UUIDS`; the manager is the only driver inside a run.
6. **Dynamic failures wake the manager.** Any job with manager-created
   `return_to_agent_uuid` has `_routing` copied into the journal result on
   success or failure, and the supervisor routes failed rows only through this
   dynamic path. Stale `processing` rows after SIGKILL remain the reconcile
   path's job.
7. **Boundedness.** `turn < max_turns` and the budget checks run before every
   enqueue, so the loop terminates even if every model ignores the stop phrase.

## Run state machine

```text
              start                       stop condition / DONE
   (none) ───────────▶ running ───────────────────────────────▶ finished
                          │  ▲
        human interrupt   │  │ routed completion (advance turn)
                          ▼  │
                       paused ── resume ───────────────────────┘
                          │
        operator stop /   │
        hard SIGKILL      ▼
                       stopped            failed (turn errored twice)
```

- **running → finished:** a stop condition fires (max_turns, stop phrase,
  evaluator/no-progress in later phases, or budget).
- **running → paused:** a human posts during a managed run and Phase 0's
  interruption policy pauses before the next speaker.
- **paused → running:** operator resumes; the resume endpoint clears active
  turn state as needed and enqueues a manager tick.
- **running → stopped:** `stop_requested` observed at a tick, or an operator
  hard-stop SIGKILLs the in-flight turn and marks the run.
- **running → failed:** a speaker turn errors and the single retry also fails;
  the manager records the error and stops. Normal failed journals with dynamic
  `_routing` wake the manager; stale `processing` rows after SIGKILL need an
  explicit reconcile tick.
- **failed → running:** explicit resume may retry from the last expected speaker
  after clearing stale active-turn state and incrementing/inspecting
  `retry_count`; do not auto-resume failed runs.
- **Turn-level outcomes** map to journal states: `completed` (posted a message),
  `failed` (raised), or stale `processing` after SIGKILL. Stale processing does
  not automatically wake the manager; cron/operator reconcile must enqueue a
  manager tick for recovery.

## Observability and telemetry

- **`debug-conversation` chat row per tick**, reusing the collapsible debug-bubble
  rendering: `{next_speaker, turn, budget_left, stop_checks:{max_turns,
  stop_phrase, budget, stop_requested}}`. One glance shows *why* the manager did
  what it did. Add `no_progress` to this structure only when the optional Phase
  1 detector exists.
- **Per-turn metrics on the journal result:** at minimum wall-clock, reply
  length, persona id, prompt SHA, and the model UUID/name actually used. If the
  chat agent is later wrapped in `llm.capture_reasoning()`, also record
  `reasoning_chars` / `content_chars`; those counters exist in other paths but
  are not currently captured by `UnstructuredChatAgent`.
- **Run summary message** at the end: turns taken, stop reason, total tokens/time,
  participants — the human-readable record beside the machine lineage.
- **Debugging path:** transcript (what was said) → `debug-conversation` rows (why
  each speaker) → journal lineage via `from_journal_id` (the queue path) → model
  logs.

## Capability and safety boundary

- **A persona inherits the capability profile of its `agent_kind`, nothing more.**
  Selecting a persona must not grant tools. v1 conversations run only
  `chat_unstructured` (no tools, no structured output, no shell).
- **Reject dangerous kinds at load time.** The validator refuses a conversation
  whose participant `agent_kind` is `workspace_shell`, `mcp`, or `tool_demo` until
  an explicit, reviewed Phase ≥2 opt-in exists.
- **No prompt-driven escalation.** Capabilities come from the agent kind (code),
  not the prompt (data), so a persona prompt cannot talk its way into tool access.
- **Room scoping.** A run reads and writes only its bound `room_uuid`; the manager
  never posts into other rooms.

## Writing persona prompts that converge

The manager guarantees *termination* (it will stop at `max_turns`), but only the
prompts produce *convergence* — a conversation that ends because it is done, not
because it ran out of budget. On small local models (LM Studio / Ollama, the
common Rainbox case) the dominant failure is not crashing; it is two agents
agreeing politely forever, restating the task, or drifting off-topic. Treat
persona prompts as part of the control system.

**Runtime-injected context (not in the persona file).** The manager prepends a
short, generated preamble to every turn so the prompt files stay DRY and the
persona body stays portable:

```text
You are {name}. You are in a working conversation with: {other participants and
their one-line roles}. Goal: {conversation goal}. This is turn {turn} of at most
{max_turns}. Make concrete progress or concede. If the goal is met or no new
progress is possible, reply with exactly DONE on its own line.
```

**Persona body guidance, tuned for small models** (consistent with Rainbox's
local-LLM prompt conventions — direct imperatives, neutral minimal examples,
simplest possible output):

- Use plain-text output for `chat_unstructured`; do not demand JSON from a small
  model in a chat loop.
- One instruction per line, imperative voice. Avoid long few-shot blocks; they
  cost context and small models imitate their surface form.
- Give the persona a *job in the conversation*, not just a vibe: "propose a
  concrete next step", "find the strongest objection", "decide".
- Make disagreement explicit and bounded: "If you agree, say so once and add the
  single most useful next step — do not restate the other agent."
- Put the termination rule **last and literal**: the exact stop token (`DONE`),
  on its own line, with the condition that triggers it.

## Deciding when a conversation is done (design options)

**Motivation (observed in use).** Phase 0 puts the termination contract in the
persona prompt ("end with `DONE` when the goal is met"). With a *reasoning* model
this backfires badly: a large fraction of the model's thinking is spent
deliberating whether this turn qualifies for `DONE` — meta-reasoning about the
rules instead of the task. Self-policing also makes each persona carry knowledge
that isn't its job. The principle the proposal already states applies:
**termination should be decided by an outsider (the manager or a referee), and
enforced — not prompted into each participant.** This section catalogs the ways
to do that so we can choose deliberately (and so others, e.g. Codex, can append
token-efficiency proposals below).

This is a design catalog, not yet a decision. Whatever we pick, the first step is
to **stop asking the personas to decide** — drop `DONE`/stop-phrase self-policing
from the persona prompts and the runtime preamble.

### The options

| # | Who decides "done" | Extra LLM cost | Reliability | Rainbox fit |
| --- | --- | --- | --- | --- |
| A1 | Participant via stop phrase in prose (`DONE`) — *current* | none (but inflates reasoning) | low; meta-reasoning, format variance | as-built; to be removed |
| A2 | Participant via structured `action: continue/yield/done` | none extra | medium; still self-judged | needs a structured-output model (conflicts with current must-not-have personas) |
| B1 | Manager: `min_turns`/`max_turns` | none | hard bound, not semantic | shipped |
| B2 | Manager: budgets (wall-clock / tokens / tool-calls) | none | hard bound, not semantic | wall-clock shipped; token/tool TODO |
| B3 | Manager: model-free no-progress detector | none | catches loops/repeats, not "solved" | deferred; cheap |
| B4 | Manager: structural exit-criteria match (template declares a marker/artifact, e.g. `FINAL PLAN:`) | none | good if the artifact is well-defined | template + manager regex; cheaper than DONE-reasoning |
| C1 | Referee agent (LLM judge) every turn | 1 small call/turn | high (semantic) | new agent kind w/ own model group; matches "outsider agent" |
| C2 | Referee every N turns / only after `min_turns` | ~1 call / N turns | high, cheaper than C1 | same, with a cadence knob |
| C3 | Referee against an explicit rubric/goal from the template | 1 call/check | highest semantic precision | template carries `done_when`; judge scores it |
| D | Manager-as-summarizer-judge: one structured call every N turns returns {summary, done, reason} | ~1 call / N turns | high; also compresses context | manager becomes a `ModelGroupAgent`; amortizes cost with the deferred summary feature |
| E | Human-in-the-loop: operator ends it | none | perfect intent, needs a human | Stop button exists; could add "mark done" |
| F | Hybrid: B (hard floor/cap) + C/D (semantic "resolved") | C/D's cost | most robust | recommended shape once a judge exists |
| G | Manager: implicit consensus detector | none or ~1 tiny call | medium; good for agree/accept patterns | useful for 2-agent propose/critique loops |
| H | System/tool-style `submit_final_artifact` | none extra | high when structured tools are available | Phase 1+ structured/tool-capable agent kind |
| I | Escalation: participant yields to human | none | high intent signal, requires operator | pauses the run cleanly for tie-breaking |

### Detail and trade-offs

- **A — participant self-termination.** A1 (current) is the thing to retire: it
  taxes reasoning and is unreliable across phrasings (`Done.`, `**DONE**`, "I am
  done"). A2 (structured yield) is strictly better than A1 but still asks the
  model to self-judge, and Phase 0 personas run `chat_unstructured` (a
  must-not-have-structured group), so A2 needs a different agent kind/model. Keep
  A2 only as a fallback for models where an outsider judge is too slow.
- **B — deterministic manager.** Free and already partly built. B1/B2 are *bounds*
  (stop eventually), not *completion* (stop because solved). B3 (no-progress) and
  B4 (structural exit marker) are the cheapest ways to approximate "done" without
  an LLM. B4 is attractive: instead of "say DONE when finished," the contract
  becomes "when there's an agreed plan, the last line is `FINAL PLAN: …`" — a
  cheap, unambiguous artifact the manager matches, and a useful output by itself.
- **C — referee agent (the "outsider").** A dedicated agent the manager consults:
  given the recent transcript (+ optional goal), it returns `{done: bool,
  reason}`. Personas never see any termination rule, so their full budget goes to
  the task. Make it cheap: a tiny/fast model, reasoning off, structured boolean
  output, short transcript window, and only invoked **after `min_turns`** and
  **every N turns** (C2). This is the faithful "outsider takes the DONE role."
- **D — summarizer-judge.** Fold completion detection into the same periodic call
  that maintains the bounded-context summary: one structured call returns
  `{summary, done, reason}`. This pays for the judge and the deferred
  context-summary feature at once, and keeps a single place that "reads the whole
  room."
- **E — human.** Already have Stop; a "mark done" is trivial. Useful as the
  ultimate override, not the default for autonomous runs.
- **F — hybrid (likely answer).** Deterministic floor/cap always on (so a run is
  always bounded and loop-safe), with a referee or summarizer-judge providing the
  semantic "we actually solved it" signal in between. Degrades gracefully: if the
  judge is unavailable, the run still terminates on bounds.
- **G — implicit consensus.** The manager can detect simple acceptance patterns
  such as "I agree", "accepted", "that works", or a reply that only acknowledges
  the previous proposal. Start with this as an observability signal or
  `finish_if_consensus=true` template option; do not make it global because some
  domains require explicit final artifacts, not mere agreement.
- **H — final-artifact submission.** For structured/tool-capable participants,
  termination can be a system action, not prose: `submit_final_artifact({kind,
  body})`. This is stronger than `FINAL PLAN:` regex matching because it records
  a typed artifact and gives the manager an unambiguous stop event. Keep it
  Phase 1+ because Phase 0 personas deliberately run `chat_unstructured`.
- **I — yield to human.** Add a low-friction escape hatch for stalemates:
  `YIELD:` or a structured `action="yield"` pauses the run with
  `reason="yield_to_human"` and posts the unresolved question. This is different
  from `failed`: the agents are not broken; they are asking for arbitration.

### Token-efficiency proposals (open — to be expanded, incl. by Codex)

Seeds for "spend the tokens smarter"; expand/append here:

- **Remove meta-rules from the per-turn prompt.** The biggest immediate save:
  personas should never reason about `DONE`/format. Keep their prompt to the task
  + their role; move termination out (this section's whole point).
- **Cheap judge.** Referee on a tiny fast model, reasoning disabled, structured
  boolean output, fed only the last K turns + the goal — not the full transcript.
- **Check less often.** Only evaluate completion after `min_turns`, and then every
  N turns, not every turn.
- **Amortize.** Combine summarize + judge + (optionally) next-speaker into one
  structured call (option D), so periodic "whole-room" reasoning is paid once.
- **Cap per-turn reasoning.** Where the provider supports it, bound each persona
  turn's reasoning effort/length so a single turn can't burn the budget.
- **Prefer a structured artifact over deliberation (B4).** Asking for `FINAL
  PLAN:` is cheaper to produce and to detect than asking the model to judge
  whether it's allowed to stop.
- **Sandwich context.** For small models, keep the initial task/proposal, drop
  the middle, and keep the last 2 visible turns. This preserves the original
  goal and the current objection while cutting repetitive middle turns that
  encourage drift.
- **Bottom-of-prompt role reinforcement.** End the runtime prompt with one terse
  reminder such as `(Reminder: you are Egon. Address Benny's last point.)`. This
  reduces speaker mimicry when models see several alternating transcript lines.
- **Disagree-and-commit budget.** Near the cap (`turn >= max_turns - 1`), the
  manager should stop inviting new branches. The final speaker must accept the
  current best plan, produce the artifact, or yield to the human.
- **Fast-fail circular arguments.** If the same speaker's current turn and prior
  turn overlap above ~90% by token or `difflib` similarity, pause with
  `reason="circular_argument"` instead of spending the rest of the budget on
  restatements.

> **For Codex / reviewers:** add further token-spend strategies and any new
> termination mechanisms below this line, with rough cost/benefit, so we can
> compare before implementing.

### Reviewer notes (Codex + Gemini)

The direction is right: remove the `DONE` decision from persona prompts. The
important next move is to avoid replacing one vague control rule with another
vague control rule. Before adding a referee model, make the conversation
template say what success means in machine-readable terms:

```json
{
  "goal": "Produce a short agreed plan for ...",
  "done_when": {
    "artifact": "final_plan",
    "required_sections": ["decision", "next_step", "open_risks"]
  },
  "termination_policy": {
    "mode": "phase_script|artifact_marker|referee|summarizer_judge",
    "check_after_turn": 2,
    "check_every_turns": 2,
    "fallback_max_turns": 8
  }
}
```

That gives any later judge a stable rubric, and it also enables cheaper
non-judge policies.

**Recommended order:**

1. **Measure the current pain first.** Record per-turn `reasoning_chars`,
   `content_chars`, wall-clock, stop reason, and prompt SHA for persona turns.
   Without this, "token efficiency" is anecdotal.
2. **Try a phase-scripted template before an LLM judge.** For many two-persona
   runs, the manager can drive a fixed sequence: Egon proposes, Benny critiques,
   Egon revises, Benny accepts or lists remaining risks, then stop. This removes
   the need for any participant to decide "done" and costs zero extra model
   calls.
3. **Try a structural artifact contract.** Instead of asking for `DONE`, ask the
   final phase to produce `FINAL PLAN:` or a small sectioned artifact. The manager
   can detect the artifact cheaply. This still asks a participant to produce a
   final artifact, but not to spend long reasoning about whether a control token
   is permitted.
4. **Add yield and consensus as cheap intermediate signals.** A participant
   should be able to yield to a human when stuck, and the manager can cheaply
   detect simple acceptance-only replies. Treat both as policy-controlled
   signals, not universal defaults.
5. **Only then add a referee.** Use a tiny/fast structured-output model, invoke
   it only after `min_turns` and every N turns, and feed it the goal,
   `done_when`, and last K visible turns. Treat referee failure as "continue
   until hard bounds", not as a run failure.
6. **Combine judge + summary if both are needed.** If Phase 1 adds a
   manager-maintained summary, prefer one periodic structured call returning
   `{summary, done, reason}` over separate summarizer and judge calls.

**Extra token-spend tactics:**

- Split prompts into **work instructions** and **control policy**. Personas get
  only work instructions; the manager/referee owns control policy.
- Keep the referee transcript smaller than the persona transcript. The judge
  only needs the goal, current artifact candidate, and recent disagreement.
- Prefer deterministic stop reasons (`phase_complete`, `artifact_present`,
  `max_turns`, `operator_stop`) before semantic ones. They are cheaper to
  debug.
- Use no-progress detection as a pause/diagnostic signal first, not as proof
  that the goal was solved.
- Maintain a small A/B eval: current `DONE`, phase-scripted, artifact marker,
  and referee-every-2-turns. Compare convergence, token spend, and bad stops.

Net recommendation: for the next implementation slice, add `goal`/`done_when`
to conversation templates and implement a **phase-scripted** or
**artifact-marker** template. A referee model should be the second optimization,
not the first.

## Context window policy for local models

Phase 0 must not send an unbounded transcript. Small local models degrade quickly
as the transcript grows, and local inference servers can fail hard when context
limits are exceeded. The first implementation should use a fixed context builder:

```text
system prompt
runtime preamble (goal, participants, turn N/max)
conversation summary / current decision state (short, manager-maintained)
last K visible turns, default K=4, or sandwich context: initial task/proposal +
last 2 visible turns
pinned human interruption, if resuming from paused
current instruction: answer the latest turn, produce the required artifact, or
yield according to the template policy
bottom reminder: "You are {name}. Address {other}'s last point."
```

The summary can start as a deterministic compact line derived from the template
goal plus any manager stop/reason fields; it does not have to be a model-written
summary in Phase 0. A model-written or structured summary belongs in Phase 1 once
the skeleton is stable. The important rule is mechanical: **never build persona
prompts from the full transcript without a cap**.

Use **sandwich context** when the middle of the transcript is mostly repetition:
preserve the original task/proposal, drop the middle, and keep the last 2 visible
turns. This is often better for small local models than a plain last-K window
because it keeps the goal anchored while still showing the current disagreement.
Keep strict speaker labels in the rendered transcript and repeat the active
persona's role at the bottom of the prompt to reduce mimicry.

Record context-shaping parameters (`last_turns_k`, summary text/SHA, prompt SHA)
in the journal result so a bad turn can be reproduced.

If a run was paused because of a human interruption, the interrupted message is
not optional context: include it explicitly on resume, even when the capped
`last_turns_k` window would otherwise drop it. This prevents the resumed speaker
from ignoring the human's correction.

**No-progress detection is not a Phase 0 safety boundary.** In Phase 0, rely on
the `min_turns` floor, `DONE`, `max_turns`, budgets, operator stop, and
pause/resume. The shipped skeleton adds `min_turns` precisely because small
models say `DONE` too early; a brittle string-similarity detector should not be
the thing that makes the walking skeleton correct.

For Phase 1, a conservative model-free detector can be used as an observability
signal or pause reason. Do not use embeddings in a hot loop on local hardware.
Compare the last two turns by the *same* author over a window of
`no_progress_window`:

- normalize (lowercase, collapse whitespace, strip the stop token), then
- trip if normalized texts are identical, or token-set Jaccard >= ~0.9, or
  `difflib.SequenceMatcher(None, a, b).ratio() >= ~0.92`.

On trip, Phase 1 should pause or finish with reason `no_progress` only if the
template opted into it. Thresholds are policy fields so a template can loosen
them. For Phase 2, a cheap evaluator model can replace or augment string math
when semantic loop detection matters more than determinism.

**Output contract.** Every conversation template should declare what "done" looks
like (an agreed plan, a decision, a short artifact). Phase 2's structured handoff
summaries formalize this; even in Phase 0, the goal line above gives the models a
target so they stop when it is met.

## Alternatives considered (decision log)

Why the obvious shortcuts were rejected. Recording these prevents re-litigating
them in review.

| Alternative | Rejected because |
| --- | --- |
| Put system prompts in `agent_config.py` | Mixes behavior (data) with capability (code); not diffable as prose; defeats hard truth #1 |
| LLM-selected next speaker in v1 | Harder to debug, easy to loop; deterministic round-robin first, revisit later |
| Threads in one process (no child per persona) | Loses the SIGKILL isolation and crash-recovery the supervisor already gives; one hung model would wedge the others |
| Adopt LangGraph / AutoGen / CrewAI as a dependency | A whole foreign control plane for the one thing Rainbox lacks (a bounded loop); Rainbox already has queue + journal + isolation |
| Extend the supervisor routing pass to understand loops (option B) | Bloats the one thread that must never crash; the code itself warns against this |
| Static `next = MANAGER_UUID` on persona roles | Couples the persona permanently to group chat and breaks standalone assistant use; dynamic `return_to_agent_uuid` is per-job |
| One shared persona process serving all personas | Collides with one-process-per-role keying; conflates identities and prompt caches |
| Store the transcript outside `ChatMessage` | Throws away SSE, the chat UI, and feedback that come for free; reuse the chat tables |
| Add bespoke `ChatMessage` metadata now | No metadata column exists today; keep provenance in the journal result until an additive migration earns it |
| Cron as the turn engine | 5s granularity and stateless; keep cron only as the manual single-step button |
| Mutate prompt bodies in place | Destroys reproducibility; revisions are append-only, like model-config arguments |

## What other systems do

A short survey, each entry tied to a Rainbox decision.

- **Two-agent chat (AutoGen).** The simplest form: A sends, B replies, repeat
  until termination. The right place to start — easy to inspect and bound.
  *Lesson:* two agents need explicit roles and stop rules or they agree politely
  forever. → drives our hard `max_turns` + stop phrases first; no-progress
  detection is optional later.
- **Group chat with a manager (AutoGen `GroupChatManager`).** A manager receives,
  broadcasts, and selects the next speaker (round-robin, random, manual, or
  LLM-auto). *Lesson:* a manager is useful, but speaker selection should be
  deterministic first. → our manager-as-agent, round-robin in v1.
- **Handoffs (OpenAI Agents SDK; LangGraph).** Delegation modeled as a tool/state
  transition (`active_agent`, `current_step`). *Lesson:* better than open group
  chat when the task has clear stages. → our Phase 2 `handoff` turn policy.
- **Flow + crew (CrewAI).** Deterministic outer orchestration; agents collaborate
  inside bounded steps. *Lesson:* production systems want a deterministic outer
  workflow. → the manager is that deterministic shell; agents are bounded steps.
- **Orchestrator-worker (Anthropic research system).** A lead plans and spawns
  read-mostly subagents in parallel that return compressed findings. *Lesson:*
  parallel subagents shine for independent, read-only branches — explicitly a
  *non-goal* for v1's shared-context conversation, but a model for a future
  parallel-research mode.
- **Generator-reviewer (Cognition).** Keep writes single-threaded; extra agents
  add intelligence, not concurrent edits. *Lesson:* a clean-context reviewer
  catches what the generator missed. → our "one writer at a time" + Phase 2
  reviewer.

## What works in practice

- **Clear role separation** — personas describe behavior; the template assigns
  task responsibility.
- **Bounded turn policies** — `max_turns`, stop phrases, budgets, human
  interruption pause, and operator stop, all enforced by the manager.
- **Deterministic orchestration first** — round-robin before any LLM-selected
  routing.
- **One writer at a time** — extra agents critique/research/summarize; no
  concurrent writes. (Aligns with Rainbox's single-writer journal model.)
- **Clean-context review** — reviewers see the artifact/diff/summary, not the
  full generator context.
- **Structured handoff artifacts** — pass summaries, decisions, assumptions, open
  questions, file references; not just raw chat history.
- **Short participant introductions** — each agent is told who else is present
  and who owns what.
- **Prompt provenance recorded per turn** — reproducible behavior; Phase 0 stores
  it in journal results, and a later message-metadata/turn table migration can
  make it directly queryable from chat rows.
- **Tracing and transcripts** — agent-to-agent systems are undebuggable without a
  visible log and decision metadata; Rainbox already has chat + journal + the
  `debug-*` message kinds to lean on.
- **Small eval sets early** — 10–20 scenarios catch loops, over-delegation, and
  role confusion.

## Next-level extensions

These are not required for the Phase 0 walking skeleton, but they are good
directions once the basic loop is reliable.

### Structured yielding

Phase 0 uses `chat_unstructured` and literal stop phrases (`DONE`, `NO_REPLY`)
because it is the smallest compatible path for local plain-text models. Phase 1
should add a structured persona mode for model groups that support structured
output:

```json
{
  "visible_reply": "I agree with the plan.",
  "internal_reasoning": "Benny accepted the revised next step, so the goal is met.",
  "action": "continue|yield|done"
}
```

The manager can then transition on `action` instead of parsing stop phrases.
This reuses Rainbox's existing structured-agent capabilities and removes the
brittleness of variants like `Done.`, `**DONE**`, or "I am done". Keep the
visible reply and action in the same structured response; small models tend to do
better when they can produce a short reasoning field before choosing the enum.
If `action == "yield"`, the manager should pause with `reason="yield_to_human"`
and expose the unresolved question to the operator.

### Isolated persona memory

Rainbox memory retrieval is already scoped by `agent_uuid`. Because personas have
distinct runnable UUIDs, each persona naturally gets separate long-term memory:
Egon can remember facts learned across rooms and runs without leaking them into
Benny's memory. This is a feature, not an accident. Document it clearly when
personas become user-visible, and make import/export preserve persona UUIDs so
memory continuity is intentional. For task-oriented conversations, however,
default retrieval should prefer same `room_uuid` / `run_uuid` memories or disable
cross-room memory entirely unless the template opts in; persona continuity should
not become topic contamination.

### Manager summarization

Phase 0 uses a deterministic compact summary/current-state line to keep local
contexts bounded. In Phase 1, the manager can optionally become a
`ModelGroupAgent` for summarization only: every N turns, or before dropping old
turns from the context window, it makes a fast structured summarization call and
updates the run summary. The manager still should not select speakers with an
LLM in v1; this is context compression, not control-plane delegation.

### Fork-from-turn debugging

The inbox/journal model gives Rainbox a natural path to time-travel debugging.
In Phase 2, add a "Fork conversation here" operation that copies the
`conversation_run`, resets the turn/active state to a selected journal/message
boundary, and enqueues a manual tick with a fresh `expected_tick_count`. This
lets an operator try a different prompt or resume strategy without mutating the
original transcript.

## What does not work well

- **Unstructured swarms** — look great in demos, hard to control, rarely coherent.
- **Prompt-only hierarchy** — "you are the manager" is not enforcement; the
  runtime must decide who speaks, who calls tools, who writes.
- **Everybody sees everything** — full transcripts drown agents; use summaries
  and artifacts as context grows.
- **Everybody writes** — parallel writers fragment style and decisions.
- **No budget** — without turn/token/tool/wall-clock limits, agents over-invest.
- **No output contract** — discussion without a required final artifact or status.
- **No recovery model** — long runs need checkpoints; restarting from scratch is
  expensive and non-deterministic. (Rainbox's journal gives this cheaply.)
- **Hidden tool access** — participants must not silently gain tool/MCP
  capabilities because a persona was selected.

## Recommended Rainbox path

### Phase 0: walking skeleton (smallest shippable slice)

> **Status: ✅ implemented and working** (see
> [Implementation status](#implementation-status-phase-0--shipped)). The plan
> below is kept as the record of intended order. As-built deltas: a `min_turns`
> convergence floor was added; operator controls shipped as a full
> `/conversations` page (start/stop/resume/reconcile) rather than just an
> endpoint; failed-turn retry, resume (incl. stopped runs), operator-triggered
> reconcile, and the base-agent heartbeat are all in. Only **automatic/cron**
> reconcile and a manager-maintained context **summary** remain deferred.

Goal: two personas exchange a few turns, fully bounded, fully visible, no new
infra beyond the linchpin and the manager.

Implement Phase 0 in this order. Each step should land with focused tests before
moving to the next; do not start Phase 1 items until this list is green.

1. **Routing foundation.** Add `agent_kind` to config/spawn dispatch, preserve a
   manager-created `return_to_agent_uuid` in `Agent.run()` on success and
   failure, and teach the supervisor to route terminal rows dynamically while
   keeping static `next` success-only.
2. **Persona identity and prompts.** Add the file-backed persona loader, prompt
   resolver, static Egon/Benny persona roles, `ChatUser` seeding, and the
   `chat_unstructured` prompt override with the existing class constant as
   fallback.
3. **Run state and CAS helpers.** Add the additive `conversation_run` table plus
   helpers for `claim_tick`, `advance_conversation_if_new`, active-turn marking,
   finish/stop/pause/resume, and stale-turn reconciliation.
4. **Manager walking skeleton.** Implement the `conversation` manager agent with
   round-robin speaker choice, Phase 0 stop checks only (`DONE`/`NO_REPLY`,
   `max_turns`, budgets, `stop_requested`), bounded context, and one in-flight
   turn per run.
5. **Operator controls.** Add the admin-only start/stop/resume/reconcile entry
   points and `debug-conversation` rows so the loop can be observed and recovered
   without touching the supervisor by hand.
6. **Verification and demo.** Add fake-LLM integration coverage for the
   acceptance cases, then run the local Egon/Benny demo on a fast model. Add the
   base-agent heartbeat before testing slow reasoning models.

Phase 0 scope checklist:

- Add optional `agent_kind` to `AgentConfigEntry` and make `agent.py` dispatch
  on `config.get("agent_kind", config["name"])`.
- Add `resolve_persona_for_agent(agent_uuid)` and wire it into
  `chat_unstructured` with the existing constant as fallback.
- Add `agent_profiles/personas.jsonl` + two `prompts/*.system.md`.
- Add two persona agent identities (`persona_egon`, `persona_benny`) with their
  own `agent_config` roles/uuids, not in `CHAT_RESPONDER_UUIDS`, and materialize
  their `agent_uuid`s as `ChatUser`s.
- Add a `conversation` manager agent kind implementing the turn loop, driven by a
  `conversation_run` row; speaker payloads include `return_to_agent_uuid`, and
  the routed payload carries JSON-safe values: UUIDs as strings, `turn` as a
  number.
- Drive it with `max_turns=6`, round-robin, no tools, and a bounded context
  window: system prompt + runtime preamble + last 4 visible turns + a compact
  manager-maintained summary/goal line for older context. Do not send the full
  transcript unbounded, even in Phase 0.
- Add a CLI or admin-only endpoint to enqueue the first manager job for a run.
  Manual single-step can come immediately after, but it should not block the
  walking skeleton.
- Add a resume endpoint for `paused` and `failed` runs. It clears stale
  active-turn fields as appropriate, updates the human-message watermark, reads
  `tick_count`, and enqueues a manager tick with `expected_tick_count`. Failed
  runs are resumed only by explicit operator action.
- **Acceptance:** starting a run produces alternating Egon/Benny messages as
  distinct speakers; it stops at `max_turns` or a `DONE`; an operator stop ends it
  within one turn; every turn is a journal row; repeating a routed manager job
  does not duplicate a turn; a stale `processing` speaker turn can be reconciled
  to retry-once or failed without waiting forever; a human message during a run
  pauses before the next speaker.

### Phase 1: file-backed personas, task templates, and issue-ledger review

- Loader that validates JSONL, resolves prompt paths, rejects duplicate `id` /
  `slug`, and surfaces clear errors.
- Conversation templates in `agent_profiles/conversations/*.json`.
- Keep `egon-benny` as the demo template, but add task-centric templates such as
  `claim_fact_check`, `assumption_review`, `risk_review`, and
  `section_patch_review`. These should use functional roles, not playful demo
  personas.
- Add the first durable issue-ledger records: review issue, agent position,
  evidence reference, consensus decision, and proposed patch. Chat remains the
  visible transcript; the ledger becomes the artifact that report editors and
  humans trust.
- Add a `PlanExeReportAdapter` that maps sections, extracts candidate claims and
  assumptions, identifies documents-to-find, and seeds review issues from a
  PlanExe report.
- Background driver (manager re-armed via the routing pass) replacing manual
  stepping as the default; keep manual step for debugging.
- A `debug-conversation` chat row per tick (next speaker, turn, budget remaining)
  reusing the existing collapsible debug-bubble rendering.
- Optional conservative no-progress detection can be introduced here, but it is
  explicitly a Phase 1 feature. It should not be implemented, tested, or relied
  on in Phase 0. `DONE`, `max_turns`, budgets, stop, and pause/resume are the
  Phase 0 boundary.
- **Acceptance:** a task-centric template starts a run end-to-end with no manual
  stepping; it produces a decision record for a named issue; evidence-required
  decisions cannot be marked verified without evidence IDs; stop phrases,
  silence, and `max_turns` all terminate cleanly; no double activation when a
  human also posts in the room.

### Phase 2: controlled handoffs and artifacts

- A `handoff` or `phase_script` turn policy for staged workflows (mapper →
  extractor → source finder → assessor → reviewer → skeptic → chair → patch
  author).
- Structured handoff summaries between participants.
- One agent creates an artifact or patch, another reviews it; writes stay
  single-threaded.
- Patch governance: draft → needs_review → accepted/rejected/superseded →
  applied. A patch is never applied only because one agent proposed it.
- Record prompt path/checksum (then prompt revision) on each generated turn; keep
  Phase 0 provenance in journal results unless/until a message metadata or
  `conversation_turn` table is added.
- **Acceptance:** a staged run hands off with summaries; the reviewer sees a
  summary/diff, not full context; provenance is available for every turn; an
  accepted patch has required role approvals and preserves any dissent.

### Phase 3: Postgres and UI

- Import personas and prompt bodies into `persona` / `persona_prompt_revision`.
- A prompt editor that writes a **new revision** instead of mutating in place.
- Keep file import/export so prompts stay reviewable in git.
- Create/run conversation templates from the UI; per-persona display fields
  (`display_name`, `avatar`, `color`) added to `ChatUser` for the transcript.
- If chat-row-level provenance is needed, add a nullable SQL
  `chat_message.metadata` JSONB column mapped as `ChatMessage.metadata_`, or a
  separate `conversation_turn` table in this phase, not in Phase 0.
- **Acceptance:** a non-developer can create personas, start a conversation, and
  watch it run with attributable, revision-tagged messages.

## Testing and evaluation

The loop must be testable **without any model**, in line with Rainbox's test
culture (`conftest.py` pins `rainbox_claude`).

### A deterministic fake LLM

Inject a scripted responder so a persona turn returns canned text. This makes the
scheduler unit-testable and the eval deterministic:

```python
class ScriptedLLM:
    def __init__(self, lines): self._lines = iter(lines)
    def reply(self, *_): return next(self._lines, "DONE")
```

Wire it through the model-group seam (or an env-gated hook in `prepare_llm`) so an
integration test runs a full conversation with, e.g., Egon scripted to say
`"plan A"` then `"agreed DONE"`, and asserts the run ends at turn 2.

### Unit tests (no DB, no model)

- `next_speaker` round-robin over 2 and 3 participants, including wrap-around.
- `evaluate_stop`: each Phase 0 condition in isolation (max_turns, each stop
  phrase, budget exhausted, stop_requested). Add no-progress near-duplicate tests
  only with the optional Phase 1 detector.
- Loader validation: duplicate `id`/`slug`, missing prompt file,
  `agent_role`/`agent_uuid` mismatch with `agent_config`, dangerous `agent_kind`
  rejected.
- Payload parsing: initial tick and routed completion both yield the right
  `run_uuid`.

### Integration tests (claude DB + fake LLM)

- **Happy path:** two personas scripted to converge → alternating speakers, ends
  on `DONE`, exactly N visible persona messages, expected manager/speaker journal
  rows, and `status=finished`. Account for optional `debug-conversation` rows
  separately.
- **Boundedness:** personas scripted to *never* say DONE → ends at `max_turns`,
  not more.
- **Idempotency:** deliver the same routed manager job twice → `turn` advances
  once, no duplicate speaker turn.
- **Manual tick idempotency:** double-click start/resume so two manual ticks are
  enqueued with the same `expected_tick_count` → only one claims the tick and
  only one speaker is enqueued.
- **Operator stop:** set `stop_requested` mid-run → next tick ends as `stopped`.
- **Human interruption:** insert a human message while a speaker turn is in
  flight → routed manager tick marks run `paused`, does not enqueue the next
  speaker; resume continues with the human message in context.
- **Failure/retry:** a turn raises once → retried once → stops `failed` if it
  raises again.
- **Failed dynamic routing:** a persona turn raises before posting a reply →
  `Agent.run()` journals `failed` with `_routing.return_to_agent_uuid`, and the
  supervisor routes that failure to the manager.
- **Resume failed:** manually resume a failed run after a transient failure →
  active-turn fields clear and the manager retries the expected speaker rather
  than restarting from turn 0.
- **Stale processing recovery:** simulate a speaker journal stuck in
  `processing` past timeout → reconcile tick retries once or marks the run
  `failed`.
- **No double activation / interruption:** a human posts in the room mid-run →
  no extra responder fires (participants excluded from `CHAT_RESPONDER_UUIDS`);
  the manager pauses before the next speaker.
- **Teardown:** each test removes its rows (the shared-DB cleanup rule — any test
  that creates rows must tear them down).

### Behavioral eval set (10–20 scenarios, scored)

Even with real models, keep a small scored set: planner/critic converges within K
turns; two stubborn personas hit the cap cleanly; a persona that emits `NO_REPLY`
yields gracefully; a long reasoning turn does not trip the 60s watchdog; a prompt
edit changes behavior and is attributable by revision. Track pass/fail per
release — this is the early-warning system for loops, over-talk, and role
confusion.

## Rollout, operations, and local demo

- **Kill switch / feature gate.** Personas and the manager are inert unless a
  `conversation_run` exists, so the feature is off by default. Gate the start
  endpoint, dynamic persona-role registration, and persona/manager chat-user
  seeding behind one flag (env var or a settings row). Turning it off prevents
  new runs; in-flight runs end via `stop_requested`.
- **No normal-chat behavior change when off.** Prompt resolution falls back to
  the class constants, no persona roles are registered/seeded, and existing
  `CHAT_RESPONDER_UUIDS` stays unchanged. If Phase 0 uses static persona roles
  instead of dynamic registration, they must still be excluded from
  `CHAT_RESPONDER_UUIDS`, but they may appear in admin/model-binding screens;
  that is acceptable for the walking skeleton, not for a polished off state.
- **Local demo (walking skeleton).** With the single `main.py` running
  (supervisor + webserver): ensure the two persona roles are bound to a model
  group on `/agent_models`; create room R containing `persona_egon` and
  `persona_benny`; hit the admin "start run" endpoint for the `egon-benny`
  template; watch the turns stream live in `/chat` (SSE); click "stop" to set
  `stop_requested`; type a human interruption and verify the run pauses; click
  "resume" to continue. Expected: alternating Egon/Benny messages, ending on
  `DONE` or at `max_turns`.
- **Metrics to watch.** Run-status counts (running/finished/stopped/failed),
  turns per run, wall-clock per turn, SIGKILL count, stop-reason distribution,
  and token spend per run. A spike in `failed`, `paused`, or stop-reason
  `max_turns` (rather than `DONE`, or `no_progress` once that policy exists)
  means prompts or interruption handling need attention.
- **Backout.** Remove the persona roles from `agent_config` (or flip the flag).
  The additive tables can stay — never drop them on production. Because nothing
  else changed, backout is a config revert, not a migration.

### As-built operating notes (learned in use)

Hard-won specifics from actually running Phase 0; these caused real confusion and
are not obvious from the design above.

- **Starting a run is explicit and lives on `/conversations`, not `/chat`.**
  Adding `persona_egon`/`persona_benny` to a room does nothing on its own (the
  human-only trigger guard never wakes them). Start/stop/resume/reconcile from the
  `/conversations` page (or the `/conversation/api/...` endpoints). `/chat` only
  shows the resulting transcript.
- **Restart matrix — what a code/data change requires.** Agents run as *fresh
  child processes per turn*, but the webserver + supervisor are one long-lived
  `main.py` process:
  - **Live immediately (no restart):** persona prompt bodies
    (`agent_profiles/prompts/*.md`) and the manager's per-tick logic
    (`agent_conversation.evaluate_stop`, scheduling) — each turn/tick is a new
    subprocess that re-reads them.
  - **Needs a `main.py` restart:** anything the webapp or supervisor execute —
    the `/conversation/api` endpoints and their `db_conversation` helpers
    (`create`/`resume`/`stop`/`reconcile`), `agent_config` role/UUID changes, the
    routing pass, and the page templates. Conversation *templates* are read by the
    webapp at run-creation, so edits apply to **new** runs after a restart;
    existing runs keep their snapshotted `turn_policy`. (This is why the
    wall-clock-anchor fix needed a restart even though `evaluate_stop` runs in the
    subprocess — the anchor is *set* on the webapp side.)
- **Stop is pause/play; only `finished` is terminal.** `stopped`, `paused`, and
  `failed` runs are resumable; `finished` (DONE / `max_turns` / `wall_clock`) is
  not — start a fresh run instead. The `/conversations` page reflects this.
- **Budgets must measure *active* time, not wall time since creation.**
  `max_wall_clock_seconds` is measured from a resettable anchor in the run's
  `budget` that `resume` refreshes; otherwise a run resumed hours/days later
  finishes `wall_clock` on its first tick. Apply the same "active-time" rule to
  any future token/turn budgets.
- **Reusing a room carries stale transcript.** A new run in a room that already
  hosted runs sees the prior messages as history (the context builder excludes
  manager rows, but earlier personas' `DONE` lines remain). For a clean demo use
  a fresh room; a later improvement is scoping the persona transcript to the
  current run.
- **Small fast models emit `DONE` immediately** — the reason `min_turns` exists.
  Phase 0 was validated end-to-end with a fast "structured output: must not have"
  group: Egon↔Benny ran the full `max_turns=8`, Stop/Resume/interruption all
  behaved.

## Effort and critical path

Rough sizing to plan order, not to bill hours. Risk is implementation risk.

| Phase | Size | Risk | Depends on |
| --- | --- | --- | --- |
| 0 — walking skeleton | M | Medium (the new concurrency + reconcile) | dispatch split, persona resolver, `conversation_run` |
| 1 — file-backed + background driver | S–M | Low | Phase 0 |
| 2 — handoffs + artifacts | M | Medium (capability opt-in) | Phase 1 |
| 3 — Postgres + UI | M–L | Low–Medium | Phases 1–2 |

**Critical path:** `agent_kind` dispatch split + dynamic `return_to_agent_uuid`
routing → `resolve_persona_for_agent` + persona `ChatUser` identities →
`conversation_run` + CAS helpers → manager turn loop + bounded context builder →
operator controls (start/stop/resume/reconcile) → fake-LLM integration tests and
local Egon/Benny demo. A dedicated base-agent heartbeat thread is a prerequisite
before running slow reasoning models; the walking skeleton can use a fast model
first.

## Success metrics (feature level)

Beyond per-phase acceptance, the feature is worth keeping if:

- A two-persona run terminates within budget in ≥99% of eval scenarios.
- Zero duplicate turns and zero double-activations across the integration suite.
- A prompt edit changes behavior and is attributable to a revision/SHA.
- An operator stop ends a run within one turn.
- No supervisor crash is ever attributable to a conversation (manager isolation
  holds — failures stay inside one child process).
- Stop-reason distribution is dominated by `DONE` in Phase 0, and by
  `DONE`/reviewed evaluator outcomes in later phases, not `max_turns` (i.e.
  conversations converge; they do not just time out).

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Slow reasoning turn SIGKILLed at 60s | dedicated heartbeat thread during `handle()`; bound per-turn time |
| Double activation (participant also auto-responds) | participants get distinct uuids, excluded from `CHAT_RESPONDER_UUIDS` |
| Two personas, same agent kind, one process slot | distinct `agent_config` roles per persona in v1 |
| Runaway / never-terminating chat | Phase 0: manager-enforced `max_turns` + stop phrases + budgets; Phase 1+: optional no-progress/evaluator policy |
| Operator can't stop a wedged run | `stop_requested` DB flag checked each tick + optional SIGKILL of in-flight turn |
| Manager bug takes down scheduling | manager is an isolated agent, not supervisor code; one crashed child, recoverable via journal |
| Non-reproducible behavior after prompt edits | prompt SHA/revision recorded per turn; Phase 0 in journal results |

## Open design decisions

- Should `agent_profiles/` be committed sample data, operator-local, or both?
  Likely committed examples plus local overrides gitignored.
- **(Resolved)** Materialize personas as `ChatUser`s — distinct sender bubbles
  require a distinct `sender_uuid`.
- **(Resolved)** Drive turns with a manager-as-agent re-armed by the routing
  pass; keep cron single-step for debugging.
- How much transcript should each agent receive? Phase 0 uses a capped context
  builder: runtime preamble + manager summary/current state + last 4 visible
  turns by default. Tune `last_turns_k` by eval, never send unbounded history.
- Should personas reuse one agent kind via uuid-keyed spawning, or get distinct
  roles? v1: distinct roles for two personas; before scaling beyond a small
  sample, add an `agent_kind` worker-pool/queue path so one implementation can
  service many persona identities.
- Where do per-turn token/wall-clock counts live — journal result,
  `conversation_run.budget`, or a future `conversation_turn` table? Lean toward
  journal result summed into run budget for Phase 0 because `ChatMessage` has no
  metadata column today.

## Glossary

- **Agent kind** — the Python implementation + capability profile, e.g.
  `chat_unstructured`. Code.
- **Agent role** — a spawnable name in `agent_config`, e.g. `persona_egon`. Many
  roles can share one agent kind.
- **`agent_uuid`** — the runnable identity used by the inbox, journal, supervisor,
  and model binding.
- **`chat_user_uuid`** — the visible speaker identity on `ChatMessage.sender_uuid`
  (v1: equal to `agent_uuid`).
- **Persona** — behavior as data: name, description, tags, and a system prompt
  resolved by `agent_uuid`. Has a stable `persona_id`.
- **Conversation template** — which personas participate, their agent kinds, turn
  order, and turn policy. No prompt text.
- **Conversation run** — one live instance of a template: the bound chatroom plus
  the bounded state (`turn`, `active_turn`, budgets, `stop_requested`).
- **Manager** — the scheduler agent that drives a run; does no LLM work itself.
- **Tick** — one invocation of the manager (initial, manual, or routed).
- **Turn** — one speaker's contribution; one speaker journal row and one visible
  chat message, with optional thinking/debug rows.
- **Speaker** — the persona agent chosen to take a turn.
- **Reconcile tick** — a manager tick that recovers a stale `processing` turn
  (SIGKILled child) since failed/killed turns are not routed.
- **Stop phrase** — a literal token (`DONE`, `NO_REPLY`) that ends a run.
- **No-progress** — optional Phase 1+ detection that the last same-author turns
  are near-duplicates or semantically looping; can pause/finish the run with
  reason `no_progress` when a template opts in.

## References

### External

- AutoGen: Multi-agent Conversation Framework:
  https://autogenhub.github.io/autogen/docs/Use-Cases/agent_chat/
- AutoGen: Conversation Patterns:
  https://autogenhub.github.io/autogen/docs/tutorial/conversation-patterns/
- OpenAI Agents SDK: Handoffs:
  https://openai.github.io/openai-agents-python/handoffs/
- LangGraph / LangChain: Handoffs:
  https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs
- CrewAI Introduction:
  https://docs.crewai.com/en/introduction
- Anthropic Engineering: How we built our multi-agent research system:
  https://www.anthropic.com/engineering/multi-agent-research-system
- Cognition: Multi-Agents: What's Actually Working:
  https://cognition.ai/blog/multi-agents-working

### Internal (the primitives this builds on)

- Supervisor loop, routing pass, spawn keying, watchdog: `main.py`
- Inbox/journal queue: `db_queue.py` (`enqueue`, `take_item`, `journal_update`,
  `fetch_unrouted_completed`, `mark_routed`, `agent_uuids_with_work`)
- Agent base + drain loop + model-group fallback + KNOWN ISSUES: `agent.py`
- Agent kinds, capabilities, `next` routing: `agent_config.py`
- Chat schema (`ChatUser`, `Chatroom`, `ChatMessage`): `db_models.py`
- Chat posting / streaming / NOTIFY: `db_chat.py`
- Human-only trigger guard + responder set + SSE: `webapp/chat_api.py`
- Hardcoded system prompts to replace with persona data:
  `agent_chat_unstructured.py`, `agent_chat_structured.py`, `router_agent.py`
- Cron scheduler (single-step option): `db_cron.py`, `main.py` cron pass
- Stop-flag + SIGKILL precedent for in-flight work: `benchmark_runner.py`

## How wild could this get? (feasible moonshots)

The [Next-level extensions](#next-level-extensions) above are the incremental next
steps. This section is deliberately more ambitious — what becomes possible once
the bounded loop is solid and boring. The discipline stays the same throughout:
every idea here is **composition of primitives Rainbox already has**, and every
one still inherits the v1 guardrails (bounded turns, one writer at a time,
capability gating, process isolation, full observability). Wild does not mean
unsafe.

### Standing conversations: the system holds meetings about itself

Add one cron `action_type` — `start_conversation` — beside the existing
`message` / `command` / `backup` (`db_cron.py`). Now Rainbox can run scheduled,
bounded agent meetings while you sleep. A nightly **ops council**: a watcher
persona pulls real state from the dynamic query handlers that already exist
(`get_system_health`, `get_git_status`, `get_todo_list`,
`get_outdated_dependencies` in `query_handlers.py`), a planner persona drafts the
day's plan, and the run posts a digest to a room. *New bit:* one cron action type
that starts a run. Everything else exists.

### Self-improving personas: a prompt-smith on the eval loop

Rainbox already has an evaluation loop — `db_eval.py` (baselines / cases / runs),
`eval_runner.py`, and `eval_optimizer.py` — and prompt revisions are append-only.
Close the loop: a prompt-smith proposes a new `persona_prompt_revision`, runs it
against the behavioral eval set with the model-free `ScriptedLLM` harness, and
**auto-promotes the revision only if it beats the current baseline**. The cast
tunes itself, every change attributable to a revision/SHA and reversible. *New
bit:* an optimizer target that scores persona revisions; the scoring and storage
already exist.

### Voice personas: listen to the agents argue

The repo already ships `kokoro_service` (TTS, with `voices.py`) and
`whisper_service` (STT). Give each persona a distinct kokoro voice and render a
run as an **audio drama** you can listen to; accept spoken input via whisper so a
human can join the room by voice. *New bit:* a thin render step that pipes each
posted turn to TTS (and optionally STT for input). Both engines are already
in-tree.

### Tournaments and a (persona × model) leaderboard

Reuse the benchmark machinery (`benchmark_runner.py`, the `/benchmark` grid) to
run bracketed persona-vs-persona debates judged by a clean-context judge persona,
across model groups. The output is a leaderboard of **which model best plays which
persona** — the same scorecard UI, pointed at conversations instead of single
prompts. *New bit:* a debate-scoring harness; the grid, scoring, and model-group
plumbing already exist.

### Router-as-moderator: dynamic speakers, still bounded

v1 defers LLM-selected speakers for debuggability. The wilder version is already
sitting in the tree: let the existing `RouterAgent` (`RouterResponse`
`{subject, action, reply}`) choose the next speaker, logged as a `debug-router`
row so every choice is inspectable, still inside `max_turns` and the budgets.
*New bit:* swap `next_speaker` for a router call behind a policy flag.

### Crews that actually change the repo — safely

Take Phase 2 handoffs to their conclusion: a planner (no tools) hands to an
implementer running `workspace_shell` confined inside a **git worktree**, then to
a reviewer with clean context that reads only the diff — single-writer
throughout. The capability boundary and the shell-safe confined runner already
exist; the worktree keeps writes isolated and reversible. *New bit:* a
worktree-scoped implementer kind plus the Phase 2 capability opt-in.

### A research swarm (the v1 non-goal, finally earned)

Once the single loop is reliable, realize the orchestrator-worker pattern: a lead
persona spawns several **parallel, bounded, read-only** research runs (each its
own `conversation_run` in its own child process), workers post compressed findings
to a shared room, and the lead synthesizes. Parallel isolation is exactly what the
supervisor already provides. *New bit:* a fan-out lead plus a synthesis tick; the
isolation and per-run bounding come free.

### The system that documents itself

EgonBot already answers "what is rainbox" from `memory/question_answer.jsonl`. A
periodic **docs council** could review the recent git log and code and propose new
Q&A entries — which are just JSONL lines plus a KB rebuild — so the assistant's
self-knowledge tracks the codebase instead of drifting. *New bit:* a persona that
emits proposed registry entries for human approval; the registry and rebuild path
already exist.

---

The through-line: none of these need a new runtime. Each needs one small, named
seam on top of the bounded conversation loop — one cron action, one optimizer
target, one render step, one router swap, one worktree-scoped kind. That is the
whole bet of this proposal: **get the boring loop right, and the wild stuff is
mostly wiring.**
