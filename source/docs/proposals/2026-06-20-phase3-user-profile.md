# Phase 3 user profile block — concrete PR spec (2026-06-20)

**Status: IMPLEMENTED & TESTED (2026-06-20)** — branch `phase3-user-profile`,
not yet merged to `main`. The plan below held; the **As-built notes** section
records the three intentional deviations. Implements the deferred half of
Phase 3 from [`2026-06-19-improvements-v2.md`](2026-06-19-improvements-v2.md).
PR 7 shipped hybrid memory *retrieval* (`query_memory` action); this work adds
the **one-shot user profile block**: a compact, always-injected prompt section
built from active memory, with source references — the "Profile A"
recommendation from Phase 3.

**What landed:** the `user_profile` package (`select_profile_facts` /
`format_profile_context` / `build_profile_block`), the shared
`memory.retrieval.hard_filtered_claims` (extracted so the profile reuses the
exact "filter before rank" path), and the `AssistantAgent` wiring that injects
the profile block before the skills block. 10 retrieval tests + 1 assistant
integration test, all green.

This is **read-only**. It injects existing active memory into the prompt; it
creates no new claims and infers nothing durable. Inferring profile facts is
Phase 3.5 (`profile_deriver`) and is explicitly out of scope here.

**What this work does NOT cover (still open):** the async profile deriver
(Phase 3.5), embedding the profile / vector selection, a profile-editing UI,
project-scoped profile facts, and migrating the chat agents onto the profile
block. See **Where Phase 3 goes next** at the bottom of this file.

## As-built notes (2026-06-20) — these supersede the prose where they differ

Implemented and merged behind this spec. Three intentional deviations from the
draft below:

- **Package is `user_profile/`, not `profile/`.** `profile` shadows the Python
  stdlib profiler, so the package (and its telemetry source) is `user_profile`.
  Telemetry source label is therefore **`user_profile.retrieval`** (the draft's
  `profile.retrieval` is superseded — keep consumers on `user_profile.retrieval`).
- **Project scope is excluded in the digest, not just "deferred."** Because the
  shared `hard_filtered_claims` lets `scope="project"` claims through (for hybrid
  retrieval), `select_profile_facts` explicitly drops them so a project claim
  can't leak into unrelated rooms. v1 visibility is global + this agent + this
  room. (Resolves open question 2.)
- **A `fact` needs a non-null `subject` to be profile material.** The draft's
  "subject referring to the operator" is implemented as the concrete, testable
  half (non-null subject); a true operator-identity check awaits an identity
  signal. Subject-less ambient facts are never injected.

## Goal and contracts

The profile block answers "who is the operator" the way the skills block answers
"how to do this task." It must satisfy the standing assistant contracts:

- **Filter before rank** — secret / expired / rejected / out-of-scope claims are
  removed before any selection. Reuse the same hard-filter path as hybrid
  retrieval; do not write a second filter.
- **Every influence is explainable** — every fact in the block has a persisted
  `memory_claim` row behind it, and each is recorded in `retrieval_event`
  telemetry (`considered` + `injected`), exactly like skills.
- **Context is budgeted** — the block lives under an explicit char cap with a
  declared drop order. It is injected first (before skills), so it is also the
  first thing trimmed if the prompt is over budget.
- **Candidates are inert** — only `status="active"` claims are eligible.

## Design: mirror the skills block exactly

The skills block is the template. The integration points are already proven:

| Skills (today) | Profile (this PR) |
|---|---|
| `skills/retrieval.py` `build_skill_block(query, …) -> (block, injected)` | `profile/retrieval.py` `build_profile_block(…) -> (block, facts)` |
| `skills.format_skill_context(skills)` | `profile.format_profile_context(facts)` |
| `MAX_SKILL_BLOCK_CHARS = 2000` | `MAX_PROFILE_BLOCK_CHARS = 1500` |
| `AssistantAgent._build_skill_block(...)` → `self._skill_block` | `AssistantAgent._build_profile_block(...)` → `self._profile_block` |
| injected first in `_build_user_prompt` | injected **before** the skill block |
| telemetry `source="skills.retrieval"`, `target_type="skill"` | `source="profile.retrieval"`, `target_type="memory_claim"` |

Key difference from `query_memory`: the profile block is **query-independent**.
`query_memory` is an action the model *chooses* and passes a query to. The
profile block is assembled once per turn in `handle()`, like the skill block,
and is always present. It surfaces stable self-model facts (preferences,
projects, constraints) regardless of whether the model thinks to ask.

## Selection policy

The profile block is not "top-k by similarity" — there is no query. It is a
small, stable digest of the operator's active self-model. Selection:

1. **Hard filters (reuse hybrid's filter path):** `status="active"`,
   `sensitivity != "secret"`, not expired, scope visible to this
   `agent_uuid`/`room_uuid`. This MUST be the same filter code that
   `retrieve_memories_hybrid` uses — extract it if it is currently inline so
   both callers share one implementation. (A second copy is how forbidden claims
   eventually leak.)
2. **Kind preference:** prioritise self-model `kind` values —
   `preference` and `project_decision` first, then `fact` with a non-null
   `subject` referring to the operator. `episode_summary` and `procedure` are
   excluded (procedures are the skills layer's job).
3. **Rank:** confidence desc, then `updated_at` desc (recency). No vector math —
   this is a digest, not a search.
4. **Budget:** add facts in rank order while the rendered block stays under
   `MAX_PROFILE_BLOCK_CHARS`; stop when the next fact would overflow. Same
   incremental-fit loop as `build_skill_block`.

Cap the candidate count too (e.g. `MAX_PROFILE_FACTS = 12`) so a large memory
store can't blow the char budget through many tiny facts before the cap bites.

## Rendered format

Follow `format_memory_context`'s provenance-tagged style so a bad answer is
traceable to a specific fact:

```
About the operator (active profile):
- [preference] Prefers concise replies; no preamble.
- [project_decision] rainbox: facts in Postgres, skills in files.
- [fact] Works in the Europe/Copenhagen timezone.
```

Empty selection → empty string (no header), so the prompt gains nothing when
there is no profile, identical to the skills block's empty behaviour.

## Files to change

**New: `profile/retrieval.py`** (mirror `skills/retrieval.py`)
- `RetrievedProfileFact` frozen dataclass: `uuid`, `kind`, `text`, `confidence`,
  `reason`.
- `select_profile_facts(*, agent_uuid, room_uuid, limit=MAX_PROFILE_FACTS) -> list[RetrievedProfileFact]`.
- `format_profile_context(facts) -> str`.
- `build_profile_block(*, agent_uuid, room_uuid, journal_id=None) -> tuple[str, list[RetrievedProfileFact]]`
  — selects, records `considered` telemetry, builds under the char budget,
  records `injected` telemetry. Best-effort: never raises into the caller.
- Constants `MAX_PROFILE_BLOCK_CHARS = 1500`, `MAX_PROFILE_FACTS = 12`.

**`memory/retrieval.py`** — extract the hard-filter query builder used by
`retrieve_memories_hybrid` into a shared helper (e.g. `active_claims_query(...)`)
so `profile/retrieval.py` reuses the *exact* filter. No behaviour change to
hybrid retrieval; just deduplicate the filter so there is one source of truth.

**`agents/assistant.py`**
- `__init__`: add `self._profile_block: str = ""` next to `self._skill_block`.
- `handle()`: after `self._skill_block = self._build_skill_block(...)`, add
  `self._profile_block = self._build_profile_block(journal_id, room_uuid)`.
- New `_build_profile_block(self, journal_id, room_uuid) -> str` mirroring
  `_build_skill_block` (best-effort, returns "" on failure). It takes no query.
- `_build_user_prompt`: inject `self._profile_block` **before**
  `self._skill_block`:
  ```python
  parts = []
  if self._profile_block:
      parts.append(self._profile_block)
  if self._skill_block:
      parts.append(self._skill_block)
  parts.append(transcript)
  ...
  ```

**`db/feedback.py`** — no change. `record_retrieval_event` already supports
`target_type="memory_claim"` and arbitrary `source`/`stage`. (Confirm the
`stage` CHECK already allows `considered`/`injected`; PR 6 widened it for
skills, so it should. If not, widen the CHECK in `init_db` in place, as PR 6/7
did.)

## Tests (deterministic, no live LLM)

New `profile/test_profile_retrieval.py` and additions to the assistant suite:

1. **Filters enforced:** a `secret` claim, an `expired` claim, a `candidate`
   claim, and an out-of-scope room claim are each absent from the block.
   (This is the Phase 3 gate — assert it directly.)
2. **Kind preference:** with mixed kinds, `preference`/`project_decision` win
   over a low-confidence `fact`; `episode_summary`/`procedure` never appear.
3. **Budget + cap:** with many active claims, the block stays under
   `MAX_PROFILE_BLOCK_CHARS` and includes facts in confidence/recency order;
   the dropped ones are recorded `considered` but not `injected`.
4. **Telemetry:** `considered` and `injected` rows are written with
   `source="user_profile.retrieval"` (as-built; the draft said
   `profile.retrieval`), `target_type="memory_claim"`.
5. **Empty profile:** no active claims → empty block → prompt unchanged.
6. **Prompt assembly (assistant):** with a scripted fake model, the profile
   block appears in the user prompt before the skill block. Reuse the
   fake-model seam (`scripted_decisions`) and an injectable fake embedder if any
   embedding path is touched (selection here is non-vector, so likely none).
7. **Explainability:** each injected fact's `uuid` is recoverable from the
   returned `facts` list so a UI/trace can link back to the claim row.

## Done when (Phase 3 profile gate)

- The assistant prompt carries a compact profile block built from active memory,
  injected before skills, under an explicit char budget.
- Secret / expired / out-of-scope / candidate claims are provably filtered
  before selection (test 1), sharing the hybrid filter path.
- Every injected fact has a `memory_claim` behind it and a `retrieval_event`
  telemetry trail (`considered` + `injected`).
- The block improves a real "what do you know about me / my projects" turn
  without becoming a hidden, unbounded prompt blob.

## Explicitly out of scope

- Inferring or deriving new profile facts (Phase 3.5 `profile_deriver`).
- Embedding the profile / vector selection (digest is non-vector by design).
- A profile-editing UI (operator edits the underlying memory claims as today).
- Migrating chat agents onto this block (assistant-only for now).
- Tokenizer-aware budgeting (still char caps; a later cross-cutting change).

## Open questions — resolved as-built

1. **Injection order vs. skills.** *Decided:* profile is injected **before**
   skills ("who you are" before "how to do the task"). One list order in
   `_build_user_prompt`; trivial to flip later if it proves wrong.
2. **`project` scope.** *Decided:* project-scoped claims are **excluded** from
   the profile (the turn carries no project key). v1 visibility is global + this
   agent + this room. Revisit only if/when a project key is threaded onto the
   turn — see next steps.

## Where Phase 3 goes next

The profile block and embedding freshness (roadmap item 2) are done. Remaining
Phase-3-adjacent work, roughly by value:

1. **Wire `sync_memory_embeddings` to a trigger.** The freshness functions exist
   and are wired into the write path, but nothing calls the periodic *reconcile*
   (backfill active + prune stale). Add a cron job or admin button. *(Belongs to
   roadmap "Operator surfaces"; small.)*
2. **Migrate the chat agents onto the profile block and hybrid retrieval.**
   Today the profile block and `retrieve_memories_hybrid` are assistant-only;
   the chat agents still use token-overlap `retrieve_memories` and have no
   profile. Unifying them is the biggest remaining recall win.
3. **Phase 3.5 async profile deriver.** Build only if the one-shot profile
   proves stale in practice (the original gate). Would propose `inferred_by_model`
   candidate claims that feed this same block once activated.
4. **Project-scoped profile facts.** Needs a project key on the assistant turn
   first; then drop the `scope == "project"` exclusion in `select_profile_facts`.
5. **Profile-editing affordance.** Operators currently shape the profile by
   editing the underlying memory claims; a dedicated review/edit surface is
   later scope.
6. **Tokenizer-aware budgeting.** The profile block (and every other section)
   still uses char caps; a shared token budgeter is a cross-cutting follow-up.
