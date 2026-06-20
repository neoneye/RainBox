# Rainbox improvements v3 — actionable roadmap from current code (2026-06-20)

**Status:** live, actionable backlog. This file supersedes the "Where we are
going next" section of the now-frozen
[`2026-06-19-improvements-v2.md`](2026-06-19-improvements-v2.md). v2 remains the
**constitution** — the phase model, the assistant contracts, the scored
candidate decisions, and the rejected alternatives. v3 is the **executable plan**
grounded in the code on `main` today. When a step here is picked up it gets its
own spec + plan pair under `docs/superpowers/` (the pattern the kanban-move
family used); this file is the ordered index and the per-step acceptance bar.

> v3 is the **single live tracker**. Every Phase 5/6 write surface v2 committed to
> has a card here — losing one to "we'll remember it" is exactly the drift this
> file exists to prevent. The §4 cards trace back to v2's Phase 5 rollout list
> (memory/skill candidates, kanban, cron/reminders, file/document patches, MCP)
> and Phase 6 dashboard.

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
- **Implemented write families:** memory `remember` (log-and-undo candidate) +
  `activate_memory` (confirm); `kanban_move` (log-and-undo). The *skill* side of
  v2's "memory and skill candidates" is **not** built (see S3).

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
  skills block (assistant only). No contradiction detection/surfacing yet (v2
  Phase 3 wanted read-only contradiction surfacing — see S8).

**Skills** — `skills/`
- File-backed (base + `<customize.dir>/skills/` overlay), candidate→active
  lifecycle, lexical retrieval, `retrieval_event` telemetry. The lifecycle exists
  but the assistant cannot yet *create* a candidate skill or *activate* one as a
  write (S3).

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
  `/redirect`; progress-aware heartbeat; process watchdog (the blunt kill path).
  Endpoints exist; **no runtime dashboard UI.**

**Scheduler / admin (substrate for reminders, the sync trigger, and dashboards)**
- `db/cron.py`: `CronJob`/`CronRun`, `cron_tick(now)` fires due jobs,
  `cron_compute_next_run`, `cron_save_tree`. A periodic job is the natural home
  for both `sync_memory_embeddings` and reminders.
- Admin views exist (`webapp/` cron/chat/kanban admin) — precedent for a doctor
  page and a runtime dashboard.

**Half-built / unused seams to be aware of**
- `Capability.adapter` exists but is unused (no external systems wired) — S9/S10.
- `Capability.dry_run` exists but no capability sets it yet (the confirm-tier
  preview today is just the proposal text) — first real users are S4/S5.
- `capability_report()` exists but there is no `rainbox doctor` CLI — S6.

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
Size (S/M/L) · Depends on.** Write families cluster in S2–S5 and S10; the
trailing `(v2 …)` notes anchor each to the frozen commitment it carries.

### S1 — Embedding-sync trigger  ·  ✅ DONE (merged `25feec0`)  ·  Size S
- **Shipped:** a first-class in-process `memory_sync` cron action resolved like
  `backup`, plus a seeded ENABLED daily System job; `(embedded, pruned)` posts to
  the cron event log. Spec:
  [`../superpowers/specs/2026-06-20-s1-embedding-sync-cron-design.md`](../superpowers/specs/2026-06-20-s1-embedding-sync-cron-design.md).
- **Goal:** Actually run `sync_memory_embeddings` (backfill active + prune stale)
  on a schedule, closing the one caveat left by the embedding-freshness work.
- **Touches:** `db/cron.py` (a built-in periodic job), or an admin button in a
  webapp view; calls `memory.embeddings.sync_memory_embeddings`.
- **Trigger shape (recommended, settle in the spec):** add a **first-class
  in-process cron action type** — e.g. `action_type="memory_sync"` — resolved
  in-process at the `cron_tick` outcome step exactly like the existing `backup`
  action (`db/cron.py` ~503-521), and seed/configure one maintenance job. The
  cron action types today are `message` / `command` / `backup`; `sync` is an
  in-process maintenance task, a sibling of `backup`. **Do not** run it through
  the `command` / workspace-shell action.
- **Decisions:** new `memory_sync` action type vs a seeded system job (+ optional
  admin button); cadence; where to record the `(embedded, pruned)` counts so they
  are visible (a `CronRun` outcome / existing telemetry).
- **Done when:** a scheduled tick embeds newly-active claims and prunes stale
  embeddings with no manual call; a test drives the job end to end with a fake
  embedder.

### S2 — More kanban write families  ·  ✅ DONE  ·  Size M (per family)  ·  (v2 Phase 5 #2)
The assistant's kanban write family now covers **move / complete / comment /
create**, all log-and-undo on the shared write-intent ledger. Shipped in two
batches:
- **Batch 1 (merged `46230eb`):** `kanban_complete` (mark done; undo re-opens via
  `kanban_move`) + `kanban_comment` (append comment; undo posts `↩ retracted: …`);
  plus the `_record_log_and_undo` None-undo warning.
  [spec](../superpowers/specs/2026-06-20-s2-kanban-complete-comment-design.md)
- **Batch 2 (merged `4d5b905`):** `kanban_create` (undo deletes the task) via an
  internal, non-prompt-exposed `kanban_delete_task` inverse; enforces the "model
  may request only prompt-exposed capabilities" contract with a `_validate_decision`
  guard. New `kanban_create_task`/`kanban_delete_task` DB primitives.
  [spec](../superpowers/specs/2026-06-20-s2-kanban-create-design.md)
- **Goal:** Extend the assistant's kanban writes beyond `move`, reusing the
  log-and-undo ledger and the code-owned-capability authority stance.
- **Touches:** `agents/assistant.py` (new capabilities + `_action_*`),
  `db/kanban.py` (`kanban_append_event`, `kanban_complete_task`, task creation
  via `kanban_save_board`), the surface-lock test, `docs/kanban-design.md`.
- **Sub-cards (pick order in the spec):** comment/note (append-only → confirm-tier
  or log-and-undo with "undo = retraction note"); complete/mark-done (reversible →
  log-and-undo; mind verified→Done vs unverified→Review); create task
  (higher blast radius; undo = delete).
- **Decisions:** per-sub-family tier; how non-reversible ops express "undo";
  add a `None`-undo guard to `_record_log_and_undo` now there is a second
  log-and-undo family (deferred follow-up from kanban-move review).
- **Done when:** ≥1 new kanban write works end to end with its tier + trace +
  (undo or confirm); surface-lock test updated; model-free tests cover it.

### S3 — Skill-candidate write family  ·  ✅ DONE (merged `28e382d`)  ·  Size M  ·  (v2 Phase 5 #1, skill side)
- **Shipped:** `propose_skill` (log-and-undo) writes an inert candidate skill file
  to the `<customize.dir>/skills/` overlay (`created_by=assistant`, provenance);
  undo deletes it via an internal `skill_delete` capability (not model-invocable).
  `activate_skill` (confirm-tier) flips a genuine candidate to active. New loader
  writers `write_candidate_skill`/`set_skill_status`/`delete_skill_file`. The
  **candidates-are-inert contract is tested directly** (an assistant-written skill
  is not injected until activated). Closes the last Phase 5 write family — the
  assistant now has memory, skill, kanban (move/complete/comment/create),
  reminder, and file-edit writes.
  [spec](../superpowers/specs/2026-06-20-s3-skill-candidates-design.md)
- **Follow-ups:** editing/superseding an existing active skill; a skill-review UI.

### S4 — Reminders / scheduling write family  ·  ✅ DONE (merged `a6098ea`)  ·  Size M  ·  (v2 Phase 5 #3)
- **Shipped:** `set_reminder` — confirm-tier write that schedules a one-shot cron
  `message` job at an ISO-8601 time. First `Capability.dry_run` user: the
  assistant proposes, `_propose_write` runs the action in dry-run to preview the
  resolved fire instant (no mutation; bad datetimes fail at propose time), the
  operator confirms to schedule. One-shot mechanism: `cron_create_one_shot_message`
  (empty `cron_expr` + preset `next_run_at`); `cron_tick` retires it after firing.
  The **dry-run preview protocol is reusable** (S5 reuses it).
  [spec](../superpowers/specs/2026-06-20-s4-reminders-design.md)
- **Follow-ups (own cards):** natural/relative time ("Friday 9am" — needs "now"
  injected into the prompt + a parser; absolute ISO only today); edit/cancel of a
  pending reminder; recurring reminders.

### S5 — File/document patch proposals  ·  ✅ DONE (merged `8416fe7`)  ·  Size M/L  ·  (v2 Phase 5 #4)
- **Shipped:** `edit_file` — confirm-tier write; the model supplies a path + full
  new content, `_propose_write` (the S4 dry-run protocol) shows a **unified diff**,
  the operator confirms to apply. Path safety reuses
  `tools.workspace_policy.resolve_workspace_path` (workspace-confined; rejects
  traversal/`~`/NUL/symlink-escape/sensitive names) — re-validated again at execute
  time against the hash-checked payload. 100KB cap; new-file create + no-op
  refusal; `output_cap_chars=12000` so the diff isn't truncated.
  [spec](../superpowers/specs/2026-06-20-s5-file-edit-design.md)
- **Security review (Opus):** containment empirically verified for traversal,
  absolute, `~`, NUL, and final-component + parent-dir symlink escapes; dry-run
  writes nothing; confirm-tier never writes inline.
- **Follow-ups (own cards / S12):** line-range patch fragments (token-cheaper than
  full content); automated one-click revert (needs an undo path for confirm-tier);
  **harden the sensitive-name denylist for writes** (it was sized for reads —
  exact/case-sensitive, e.g. `.git/config` and `.ENV` aren't covered).

### S6 — `rainbox doctor` CLI  ·  Size S/M  ·  Depends on: none  ·  (v2 Phase 4)
- **Goal:** Promote `capability_report()` into an operator-facing health check.
- **Touches:** a CLI entry (mirror `agents/__main__` style) + optional `webapp`
  doctor view; reads capability registry, model-group config, embedder
  reachability, MCP config, skill metadata.
- **Decisions:** CLI-only vs CLI + admin page; which prerequisites to probe.
- **Done when:** `rainbox doctor` lists enabled capabilities and flags missing
  model/embedding/MCP prerequisites and stale/invalid skill metadata.

### S7 — Runtime dashboard  ·  Size M  ·  Depends on: none (endpoints exist)  ·  (v2 Phase 6)
- **Goal:** See and steer in-flight assistant runs.
- **Touches:** a `webapp` view over `assistant_run`/heartbeat; buttons wired to
  `/stop`, `/redirect`, the write-intent `confirm/reject/undo` endpoints, and a
  **kill** (watchdog) and **retry** (re-enqueue) path.
- **Decisions:** live-update mechanism (reuse chat SSE/LISTEN-NOTIFY) vs poll;
  whether `retry` re-runs from scratch or resumes (resume is out of scope unless
  cheap).
- **Done when (full v2 bar):** the dashboard shows PID, journal id, current step,
  **current action/model**, last heartbeat age, and **stop / kill / retry**
  controls plus redirect and per-intent undo. *(If kill or retry proves large,
  ship stop/redirect/undo + visibility first and drop the remainder into S12 with
  a note — do not silently narrow this bar.)*

### S8 — Unify chat agents with the assistant's memory stack  ·  Size M  ·  Depends on: none  ·  (v2 Phase 3)
- **Goal:** Biggest remaining recall win: move the chat agents off token-overlap
  `retrieve_memories` onto `retrieve_memories_hybrid`, and give them the profile
  block.
- **Touches:** `memory/retrieval.py` (`build_chat_memory_block`), the chat agents,
  `user_profile`.
- **Decisions:** keep `retrieve_memories` for anything, or remove it; profile
  block for chat agents y/n; **contradiction surfacing** — v2 Phase 3 wants
  retrieval to *detect and surface* conflicts read-only (e.g. "lives in NYC" vs
  "moved to SF"), with auto-supersede deferred to a Phase 5 write. Decide here
  whether to add read-only contradiction surfacing as part of the retrieval
  rework or split it to S12.
- **Done when:** chat agents retrieve via hybrid + carry the profile block; recall
  eval shows no regression and ideally a gain; secret/expired filtering still
  holds; the contradiction-surfacing decision is recorded (built or deferred).

### S9 — External-system adapter boundary: MCP read-only  ·  Size M/L  ·  Depends on: none  ·  (v2 Phase 4 adapter boundary)
- **Goal:** Activate `Capability.adapter`: route a non-null `adapter` capability
  through a narrow adapter contract instead of a direct rainbox call. MCP is the
  first (read-only) adapter; git a natural second.
- **Touches:** `agents/assistant.py` dispatch (adapter routing), the MCP config
  loader, a small read-only adapter surface (`status/list/read/search/summarize`).
- **Decisions:** the adapter interface shape; which MCP server/tool first; how
  the registry gates per-adapter capabilities.
- **Done when:** one read-only MCP tool is callable as a registry capability with
  `adapter="mcp:…"`, gated and traced like any other; no bespoke controller.

### S10 — MCP write-capable adapter / selected tool calls  ·  Size M/L  ·  Depends on: S9  ·  (v2 Phase 5 #5, "MCP last")
- **Goal:** The last write family: allow *selected* MCP tools to mutate, one
  server/tool at a time, because the surface is externally supplied and easy to
  over-grant.
- **Touches:** the adapter surface (add `propose`/`dry_run`/`execute_approved`),
  registry per-tool gating, the write-intent path.
- **Decisions:** **confirm-tier by default** (external + likely `network=true`);
  per-tool allowlist; dry-run support per tool; how approval binds to the exact
  tool payload (reuse `payload_hash`).
- **Done when:** one write-capable MCP tool runs only via an approved, hash-bound
  intent; disabling it removes it from prompt + dispatch; nothing else in that
  server is callable; traced like any write.

### S11 — Phase 3.5 async profile deriver  ·  Size M/L  ·  Depends on: S8-ish  ·  Optional  ·  (v2 Phase 3.5)
- **Goal:** Background agent proposing `inferred_by_model` candidate profile
  facts — build **only if** the one-shot profile proves stale in practice.
- **Done when:** every inferred claim links to chat/journal evidence, is
  candidate/rejectable, and the deriver runs without slowing assistant turns.

### S12 — Smaller follow-ups (grab-bag)  ·  Size S each
- `kanban_read` `task_uuid` support (currently rejected by validation).
- Tokenizer-aware prompt budgeter to replace the character caps
  (`MAX_*_CHARS` in `agents/assistant.py`, `skills/`, `user_profile/`).
- Promote the optional `eval_case` regression layer to a first-class surface.
- Project-scoped profile facts (needs a project key threaded onto the turn first).
- Superseded-move undo awareness + the `None`-undo guard in `_record_log_and_undo`
  (deferred follow-ups noted in the kanban-move design doc).
- Read-only contradiction surfacing, if deferred from S8.
- Dashboard `kill`/`retry` or model visibility, if deferred from S7.

---

## 5. Recommended sequence

1. **S1 (sync trigger)** — tiny, finishes already-built freshness work, and warms
   up the cron subsystem reused by S4.
2. **S2 (more kanban writes)** then **S3 (skill candidates)** — highest momentum;
   both ride the just-built write/candidate machinery, DB + loader functions
   already exist.
3. **S4 (reminders)** then **S5 (file/document patches)** — the two confirm-tier,
   `dry_run`-preview families; reminders first (lower blast radius, reuses cron),
   patches second (writable-path policy + diff apply is the bigger lift).
4. **S6 (doctor)** and **S7 (dashboard)** — operator-facing polish; independent,
   can interleave or run in parallel with the write families.
5. **S8 (chat-agent unification)** — biggest recall win; do before investing more
   in retrieval breadth, and settle the contradiction-surfacing decision here.
6. **S9 (MCP read-only)** then **S10 (MCP write, last)** — opens the
   external-system direction once the internal write/registry surface is mature;
   write-capable MCP is deliberately the *last* write family.
7. **S11 (deriver)** and **S12 (follow-ups)** — optional / opportunistic.

Rationale: finish cheap high-value loose ends (S1), ride momentum on the
write/candidate machinery (S2–S5), add operator polish (S6–S7) and the recall
win (S8), then take on the external-system surface read-before-write (S9→S10).
S11/S12 are pulled in only when an eval or a real annoyance justifies them.
