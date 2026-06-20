# Rainbox improvements v3 — actionable roadmap from current code (2026-06-20)

**Status:** live, actionable backlog. This file supersedes the "Where we are
going next" section of the now-frozen
[`2026-06-19-improvements-v2.md`](2026-06-19-improvements-v2.md). v2 remains the
**constitution** — the phase model, the assistant contracts, the scored
candidate decisions, and the rejected alternatives. v3 is the **executable plan**
grounded in the code on `main` today. When a step here is picked up it gets its
own spec + plan pair under `docs/superpowers/` (the pattern the kanban-move
family used); this file is the ordered index and the per-step acceptance bar.

---

## 1. Where the code actually is (the substrate new work plugs into)

Everything below is on `main` and is the real interface each step extends. File
paths are the current source of truth; the v2 prose schemas are historical.

**Assistant loop & capability registry** — `agents/assistant.py`
- `AssistantAgent.handle()` is the bounded ReAct loop (plan → act → observe,
  step cap, per-step `assistant_run`/`assistant_step` trace).
- `AssistantActionName` (enum) + `CAPABILITIES: dict[AssistantActionName,
  Capability]` is the code-owned registry. `Capability` carries
  `read/write/network/secrets`, `tier` (`log_and_undo`/`confirm`/None),
  `dry_run`, `timeout_seconds`, `output_cap_chars`, `enabled`, `prompt_exposed`,
  and `adapter` (currently always `None`). The prompt catalog *and* dispatch are
  generated from this one object.
- Actions live as module-level `_action_*` functions. Current surface (locked by
  `agents/test_assistant_fakes.py`): `reply`, `ask_clarifying_question`,
  `query_memory`, `query_qa`, `workspace_read_command`, `kanban_read`,
  `remember`, `activate_memory`, `kanban_move`.
- `capability_report()` (`agents/assistant.py:462`) returns the enabled-capability
  inventory — the seed for `rainbox doctor`.

**Write families & the undo ledger** — `agents/assistant_writes.py`, `db/assistant.py`, `db/models.py`
- Loop write branch: `tier=="confirm"` → `_propose_write` (records a `proposed`
  `assistant_write_intent`, never executes inline); else dispatch immediately and,
  for `tier=="log_and_undo"` with `observation.ok`, `_record_log_and_undo` writes
  a `completed` intent carrying `result["undo"]` (the inverse op).
- `AssistantWriteIntent` states: `proposed → confirmed → executing →
  completed|failed`, plus `rejected` and `undone`.
- `execute_write_intent` (confirm path; refuses non-`confirm`-tier capabilities),
  `reject_write_intent`, `undo_write_intent` (generic: replays
  `result["undo"]`'s capability+payload, then flips the intent `completed →
  undone`). `db.create_write_intent(..., state=, result=)` lands log-and-undo
  rows atomically `completed`.
- Endpoints: `POST /chat/api/assistant/write-intents/<uuid>/{confirm,reject,undo}`
  (`webapp/chat_api.py`).

**Memory** — `memory/`, `db/`
- `retrieve_memories_hybrid` (vector + full-text + entity, hard-filtered) backs
  the assistant's `query_memory`. The shared filter is
  `memory.retrieval.hard_filtered_claims` (active, non-secret, non-expired,
  in-scope; project scope excluded). Legacy token-overlap `retrieve_memories`
  still serves the chat agents.
- Embedding freshness: `refresh_claim_embedding` (write-path hook),
  `prune_stale_embeddings` (lazy sweep), `sync_memory_embeddings` (reconcile).
  **No production trigger calls `sync` yet.**
- `user_profile.build_profile_block` injects an active-memory digest before the
  skills block (assistant only).

**Skills** — `skills/`
- File-backed (base + `<customize.dir>/skills/` overlay), candidate→active
  lifecycle, lexical retrieval, `retrieval_event` telemetry.

**Kanban** — `db/kanban.py`, `tools/kanban_dispatcher.py`
- Worker authority model `observe`/`work`/`shape` (`kanban_dispatcher`) governs
  *autonomous worker agents*; the assistant's `kanban_move` is code-owned and
  bypasses it (safety = reversibility + trace), documented in
  `docs/kanban-design.md`.
- Write fns available for the next families: `kanban_append_event`
  (comment/note), `kanban_complete_task`, `kanban_create_board` /
  `kanban_save_board` (task creation), `kanban_get_task`, `kanban_move_task`.

**Runtime controls & trace** — `agents/assistant.py`, `agents/base.py`, `webapp/chat_api.py`
- `assistant_control` channel; step-boundary `/stop` (clean stopped trace) and
  `/redirect`; progress-aware heartbeat. Endpoints exist; **no runtime dashboard UI.**

**Scheduler / admin (substrate for reminders, the sync trigger, and dashboards)**
- `db/cron.py`: `CronJob`/`CronRun`, `cron_tick(now)` fires due jobs,
  `cron_compute_next_run`, `cron_save_tree`. A periodic job is the natural home
  for both `sync_memory_embeddings` and reminders.
- Admin views exist (`webapp/` cron/chat/kanban admin) — precedent for a doctor
  page and a runtime dashboard.

**Half-built / unused seams to be aware of**
- `Capability.adapter` exists but is unused (no external systems wired).
- `Capability.dry_run` exists but no capability sets it yet (the confirm-tier
  preview today is just the proposal text).
- `capability_report()` exists but there is no `rainbox doctor` CLI.

---

## 2. Contracts that still bind (full statements in v2)

Every step must satisfy these; v2's "Assistant contracts" table is the source of
truth. In one line each: **authority is code-owned**; **trace before action**;
**candidates are inert**; **filter before rank**; **every influence is
explainable**; **writes are family-scoped and risk-tiered** (log-and-undo for
low-risk reversible internal writes, dry-run/confirm for high-blast-radius or
outward-facing ones); **stop is stateful**; **context is budgeted**.

---

## 3. How to execute a step

1. Pick the next step from §4 (respect `Depends on`).
2. Brainstorm only the *open decisions* the card names, then write a spec +
   plan under `docs/superpowers/specs/` and `…/plans/` (one feature each).
3. Implement TDD, model-free (the suite must stay runnable without a live LLM).
4. Each write family adds at least one tier/trace/undo-or-confirm test.
5. Review, then merge; flip the card to ✅ here.

---

## 4. The actionable backlog (ordered by value ÷ cost)

Each card: **Goal · Touches · Decisions to resolve in its spec · Done when ·
Size (S/M/L) · Depends on.**

### S1 — Embedding-sync trigger  ·  Size S  ·  Depends on: none
- **Goal:** Actually run `sync_memory_embeddings` (backfill active + prune stale)
  on a schedule, closing the one caveat left by the embedding-freshness work.
- **Touches:** `db/cron.py` (a built-in periodic job), or an admin button in a
  webapp view; calls `memory.embeddings.sync_memory_embeddings`.
- **Decisions:** cron job vs admin button vs both; cadence; whether to log the
  `(embedded, pruned)` counts to an existing telemetry/run table.
- **Done when:** a scheduled tick embeds newly-active claims and prunes stale
  embeddings with no manual call; a test drives the job end to end with a fake
  embedder.

### S2 — More kanban write families  ·  Size M (per family)  ·  Depends on: none (extends kanban_move)
- **Goal:** Extend the assistant's kanban writes beyond `move`, reusing the
  log-and-undo ledger and the code-owned-capability authority stance.
- **Touches:** `agents/assistant.py` (new capabilities + `_action_*`),
  `db/kanban.py` (`kanban_append_event`, `kanban_complete_task`, task creation
  via `kanban_save_board`), the surface-lock test, `docs/kanban-design.md`.
- **Sub-cards (pick order in the spec):**
  - **comment/note** (`append_event` kind=comment): low blast radius; append-only,
    so "undo" can only post a retraction — likely **confirm-tier** or
    log-and-undo with an explicit "undo = retraction note" caveat.
  - **complete / mark done**: reversible (re-open by moving back), good
    **log-and-undo**; mind the verified→Done vs unverified→Review routing.
  - **create task**: higher blast radius; undo = delete; decide log-and-undo vs
    confirm.
- **Decisions:** per-sub-family tier; how non-reversible ops (comment) express
  "undo"; whether to add a `None`-undo guard in `_record_log_and_undo` now that a
  second log-and-undo family exists (deferred follow-up from kanban-move review).
- **Done when:** at least one new kanban write works end to end with its tier +
  trace + (undo or confirm); surface-lock test updated; model-free tests cover it.

### S3 — Reminders / scheduling write family  ·  Size M  ·  Depends on: S1 (familiarity with cron) recommended
- **Goal:** The assistant can set a reminder ("remind me Friday to …") that fires
  a chat message — strong personal-assistant value.
- **Touches:** new capability + action in `agents/assistant.py`; `db/cron.py`
  (`cron_save_tree` / job creation); `webapp` for visibility.
- **Decisions:** tier (scheduling a future action has real blast radius →
  likely **confirm-tier with a dry-run preview** of the schedule, the first user
  of `Capability.dry_run`); how a reminder renders/fires (a cron job that posts a
  chat message); edit/cancel semantics.
- **Done when:** a confirmed reminder creates a CronJob that fires a chat message
  at the computed time; an unconfirmed one never schedules; tests use
  `cron_tick`/fake clock (no real waiting).

### S4 — `rainbox doctor` CLI  ·  Size S/M  ·  Depends on: none
- **Goal:** Promote `capability_report()` into an operator-facing health check.
- **Touches:** a CLI entry (mirror existing `agents/__main__` style) + `webapp`
  doctor view; reads capability registry, model-group config, embedder
  reachability, MCP config, skill metadata.
- **Decisions:** CLI-only vs CLI + admin page; which prerequisites to probe.
- **Done when:** `rainbox doctor` lists enabled capabilities and flags missing
  model/embedding/MCP prerequisites and stale/invalid skill metadata.

### S5 — Runtime dashboard  ·  Size M  ·  Depends on: none (endpoints exist)
- **Goal:** See and steer in-flight assistant runs.
- **Touches:** a `webapp` view over `assistant_run`/heartbeat; buttons wired to
  the existing `/stop`, `/redirect`, and write-intent `confirm/reject/undo`
  endpoints.
- **Decisions:** live-update mechanism (reuse chat SSE/LISTEN-NOTIFY) vs poll.
- **Done when:** the dashboard shows PID, journal id, current step/activity,
  heartbeat age, and working stop/redirect/undo controls.

### S6 — External-system adapter boundary (MCP read-only first)  ·  Size M/L  ·  Depends on: none
- **Goal:** Activate `Capability.adapter`: route a non-null `adapter` capability
  through a narrow adapter contract instead of a direct rainbox call. MCP is the
  first (read-only) adapter; git a natural second.
- **Touches:** `agents/assistant.py` dispatch (adapter routing), the MCP config
  loader, a small adapter surface (`status/list/read/search/summarize`).
- **Decisions:** the adapter interface shape; which MCP server/tool first; how
  the registry gates per-adapter capabilities.
- **Done when:** one read-only MCP tool is callable as a registry capability with
  `adapter="mcp:…"`, gated and traced like any other; no bespoke controller.

### S7 — Unify chat agents with the assistant's memory stack  ·  Size M  ·  Depends on: none
- **Goal:** Biggest remaining recall win: move the chat agents off token-overlap
  `retrieve_memories` onto `retrieve_memories_hybrid`, and give them the profile
  block.
- **Touches:** `memory/retrieval.py` (`build_chat_memory_block`), the chat agents,
  `user_profile`.
- **Decisions:** keep `retrieve_memories` for anything, or remove it; profile
  block for chat agents y/n.
- **Done when:** chat agents retrieve via hybrid + carry the profile block; recall
  eval shows no regression and ideally a gain; secret/expired filtering still holds.

### S8 — Phase 3.5 async profile deriver  ·  Size M/L  ·  Depends on: S7-ish  ·  Optional
- **Goal:** Background agent proposing `inferred_by_model` candidate profile
  facts — build **only if** the one-shot profile proves stale in practice.
- **Done when:** every inferred claim links to chat/journal evidence, is
  candidate/rejectable, and the deriver runs without slowing assistant turns.

### S9 — Smaller follow-ups (grab-bag)  ·  Size S each
- `kanban_read` `task_uuid` support (currently rejected by validation).
- Tokenizer-aware prompt budgeter to replace the character caps
  (`MAX_*_CHARS` in `agents/assistant.py`, `skills/`, `user_profile/`).
- Promote the optional `eval_case` regression layer to a first-class surface.
- Project-scoped profile facts (needs a project key threaded onto the turn first).
- Superseded-move undo awareness + a `None`-undo guard in `_record_log_and_undo`
  (deferred follow-ups noted in the kanban-move design doc).

---

## 5. Recommended sequence

1. **S1 (sync trigger)** — tiny, finishes already-built freshness work, and warms
   up the cron subsystem reused by S3.
2. **S2 (more kanban writes)** — highest momentum; the pattern is fresh and the
   DB functions exist.
3. **S3 (reminders)** — high personal-assistant value; first real
   `dry_run`/confirm-tier user.
4. **S4 (doctor)** and **S5 (dashboard)** — operator-facing polish; independent,
   can interleave or run in parallel with the above.
5. **S7 (chat-agent unification)** — biggest recall win; do before investing more
   in retrieval breadth.
6. **S6 (MCP adapter)** — opens the external-system direction once the internal
   write/registry surface is mature.
7. **S8 (deriver)** and **S9 (follow-ups)** — optional / opportunistic.

Rationale: finish cheap high-value loose ends (S1), ride momentum on the just-built
write machinery (S2–S3), then operator polish (S4–S5) and the recall win (S7),
before taking on the larger external-system surface (S6). S8/S9 are pulled in only
when an eval or a real annoyance justifies them.
