# Operator profiles and the working-context block

**Status: proposal.** Two coupled features:

1. **Operator profiles** — a named answer to *who is at the keyboard and what
   may they see*, built on the existing Q&A overlay + shield machinery, so
   one rainbox instance can serve the primary operator, a no-PII **demo**
   audience, and a **friends** audience (technical persona, family facts
   hidden) without three installs.
2. **The working-context block** — a budgeted, deterministic prompt section
   giving the assistant the situational answers it currently lacks: who am I
   helping, what are they working on, what changed recently, what is
   unresolved, what should I not repeat, what is pinned, what project is
   active.

They are one proposal because the block's first line *is* the profile, and
every other line must be filtered *through* the profile — a demo audience's
working context must not enumerate the family kanban board.

## Problem

Today the assistant literally cannot say who it serves. There is one seeded
human `chat_user`, the prompt contains no identity line, and "the operator"
is an implicit constant. Three concrete pains:

- **Identity.** Asked "who am I?", the assistant has to fish in Q&A/memory
  and may find nothing authoritative. Nothing in the prompt states whose
  preferences the profile block expresses.
- **Audiences.** The operator wants to hand the keyboard (or the screen) to
  other people soon: a public demo where **no PII may surface**, and geek
  friends who should get the technical persona but none of the family
  material. Today the only lever is manually toggling shield checkboxes on
  `/settings` before and after — error-prone exactly when the cost of a
  mistake is highest.
- **Situational blindness.** Each turn starts from the transcript plus
  whatever `query_memory` happens to fetch. The assistant re-asks things the
  operator answered yesterday, re-proposes ideas that were rejected, and
  doesn't know a write intent is still sitting unconfirmed — all knowable
  from tables it already owns.

## Part 1 — Operator profiles

### What a profile is

A profile is a **presentation and retrieval lens**, not an account. One
JSON file per profile in `<customize.dir>/operators/<profile>.json`
(hand-edited, like the rest of the overlay), plus a shipped
`data/operators/demo.json` so the no-PII profile exists on every install:

```json
{
  "id": "simon",
  "display_name": "Simon",
  "audience": "the primary operator; full personal context",
  "qa_audiences": ["private", "tech"],
  "unlocked_shields": ["ada", "ada.health", "maplest.cellar", "…"],
  "memory_ceiling": "private",
  "identity_qa_path": "human.simon.identity"
}
```

- `qa_audiences` — which overlay Q&A files this profile reads (see the file
  split below).
- `unlocked_shields` — the shield bundle unlocked while this profile is
  active. This reuses the per-subject shield aliases the person-schema
  proposal already defines; a profile is essentially a **named shield
  preset**. The effective unlocked set is
  `profile.unlocked_shields ∪ qa.unlocked_shields` (manual session unlocks
  still work; `/settings` shows which source unlocked each).
- `memory_ceiling` — the maximum claim sensitivity retrievable under this
  profile: `demo → public`, `friends → public`, `simon → private`
  (`secret` stays excluded everywhere, as today).
- `identity_qa_path` — the Q&A identity card that answers "who am I
  helping"; the working-context block quotes it.

The example profiles:

| id | qa_audiences | shields | memory_ceiling |
|---|---|---|---|
| `simon` | private + tech | full bundle | private |
| `friends` | tech | none (or a `tech.*` subset) | public |
| `demo` | *(none — base registry only)* | none | public |

### The active profile

One new setting, `operator.profile` (default `simon`-equivalent: the first
profile found, falling back to a built-in "unnamed operator" that behaves
exactly like today). Switchable on `/settings`. **Switching stamps
`mark_facts_invalidated()`** — the existing facts-invalidation machinery then
posts the one-time "re-check facts" notice in each room, which is precisely
the right semantics: the knowledge base just changed shape.

### Splitting `question_answer.jsonl`

The overlay grows audience files next to the existing one:

```
<customize.dir>/question_answer.jsonl            # audience "private" (existing file, unchanged)
<customize.dir>/question_answer.tech.jsonl       # audience "tech"
<customize.dir>/question_answer.<name>.jsonl     # any further audience
```

The loader tags each entry `_audience` from its filename (the base
`data/question_answer.jsonl` is audience `base`, always loaded — it is
publishable by contract). Merge rules are unchanged (later file wins by id;
same-file duplicate ids/paths still fail hard).

**Embed once, filter per profile.** Rather than repopulating pgvector on
every profile switch, `_audience` joins `shield` in the embedded metadata,
and the retrieval filter becomes: audience ∈ active profile's set AND
(shield empty OR shield ∈ effective unlocked). Same two-layer enforcement as
shields today: a pgvector metadata filter plus the in-memory backstop
(`_entry_locked` grows an audience check). A profile switch is then
instant — no re-embedding — and repopulate is only needed when files change,
exactly as now.

Authoring stays compatible with the person-schema proposal: cards and
stories keep their shields; the split is *coarse routing by audience*, the
shields remain *fine-grained consent per subject/topic*. A family fact in
the private file with shield `ada.health` is invisible to `friends` twice
over.

### What profiles deliberately do NOT do

Stated plainly so nobody trusts them beyond their design:

- **Not a security boundary.** The settings API is unauthenticated
  (security review Findings 1/8a): any local caller can flip
  `operator.profile` back with one POST. Until the Phase 1 auth boundary
  lands, a profile protects against *accidents in front of a trusted
  audience*, not against a curious one. For an untrusted audience the right
  tool is a **separate database** (`DATABASE_URL=…/rainbox_demo` with a
  clean overlay) — cheap, absolute, and available today. Profile switching
  should join the "high-sensitivity, audited" settings list alongside
  `qa.unlocked_shields` in the mitigation plan.
- **Not retroactive.** Chat history, journals, kanban text, and the git page
  show whatever they contain. The demo recipe is therefore: demo profile
  **plus** fresh demo rooms (or the separate DB). The proposal adds a
  `/settings` warning line when the active profile's ceiling is `public`
  but the current rooms contain history.
- **Not multi-user.** There is still exactly one human `chat_user`; profiles
  choose what that human's assistant may see and say. Real accounts (one
  `chat_user` + auth identity per person, per-account rooms) belong to the
  security work's Phase 2 (real request identity) and would slot in as
  profile-per-account then. Nothing here blocks that; a profile record is
  exactly the per-account policy object that work will need.

## Part 2 — The working-context block

One deterministic block assembled per assistant turn, absorbing today's
user-profile block as its first sections, placed where that block sits now
(before skills, before the transcript). No LLM calls on the hot path; every
line comes from an indexed query and carries its uuid/link so every
influence stays explainable. Telemetry mirrors the profile block
(`considered`/`injected`, `source="working_context"`).

The seven questions, their sources, and their line shapes:

| # | Question | Source (all existing unless marked NEW) | Line shape |
|---|---|---|---|
| 1 | **Who am I helping?** | the active profile + its `identity_qa_path` card | `Operator: Simon — the primary operator; full personal context.` For demo: `Operator: demo audience — no personal data is available; do not speculate about the person.` |
| 2 | **What are they working on?** | active project (#7); kanban tasks in non-first/non-last columns assigned to or recently touched by the operator's rooms; current git branch (existing `get_git_branch` handler) | `Working on: project <slug>; 2 tasks in progress on board <name> (…uuids); branch main.` |
| 3 | **What changed recently?** | last N `assistant_run.summary` digests (the summarizer already produces these off-path); memory claims created/corrected in the last 7 days | `Recently: reminder scheduled for Tue (run …); remembered 2 facts, corrected 1 (uuids).` |
| 4 | **What is unresolved?** | write intents in `proposed`; memory conflict candidates (`conflicts_with_uuid` set); kanban Review-column cards; last cron run errors | `Unresolved: 1 edit_file proposal awaiting your confirmation (…); 1 memory conflict to resolve (…).` |
| 5 | **What should I not repeat?** | tombstones with `hit_count > 0` (top few `claim_text` snippets); write intents `rejected` in the last 14 days; rejected skills | `Do not re-suggest: "carol prefers tea" (rejected, re-asserted 3×); the backup-rename proposal (rejected Jul 3).` |
| 6 | **What memories are pinned?** | NEW: `pinned_at` on `memory_claim` + a pin/unpin lifecycle action on `/memory` | pinned active claims render first in the profile section, bypassing *ranking* but never the hard filters — a pinned private claim still vanishes under a `public` ceiling |
| 7 | **What project context is active?** | NEW: `project.active` setting (slug) + NEW `project_key` column on `memory_claim`, which finally gives the dormant `scope="project"` its key and lets `hard_filtered_claims` admit matching project claims | heads line 2; also unlocks project-scoped memory everywhere |

Design rules:

- **Hard budgets.** Whole block ≤ ~2500 chars; per-section caps; a section
  renders only when non-empty. The block competes with the transcript for a
  small model's context — the budget is the feature.
- **Profile-filtered end to end.** Every section draws through the active
  profile's gates: Q&A lines via audience+shield filters, memory lines via
  the ceiling, and under the `demo` profile sections 2–6 are suppressed
  entirely (they enumerate the operator's real life).
- **Deterministic and cheap.** A handful of indexed selects; the expensive
  synthesis (run digests) is already produced off the critical path by
  `assistant_run_summarizer`.
- **An off-switch.** `assistant.working_context` (bool setting, default on)
  — the block is a hypothesis about model behavior, and small models may get
  *worse* with more preamble. Which leads to:
- **Measure it.** Before enabling by default, add eval cases: the
  don't-repeat section should stop a scripted re-proposal; the unresolved
  section should make the assistant mention the pending intent when asked
  "anything waiting on me?"; and existing chat-reply cases must not regress
  (the gate already enforces this).

## Phasing

1. **Identity + profiles over existing machinery.** Profile records, the
   `operator.profile` setting (+ facts-invalidation stamp on switch), shield
   bundles, the audience file split with embed-once/filter-per-profile, the
   memory ceiling in `hard_filtered_claims`, and working-context section 1
   (who am I helping). This alone fixes "the assistant doesn't know who the
   operator is" and makes the demo/friends switch one action.
   *Acceptance:* under `demo`, no overlay Q&A entry and no non-public claim
   reaches any prompt (test the pgvector filter AND the backstop); switching
   profiles posts the re-check-facts notice; `/settings` shows the active
   profile and per-shield provenance.
2. **Working-context sections from existing tables** (2–5: working on,
   changed recently, unresolved, don't repeat) behind the off-switch, with
   telemetry and the eval cases above.
   *Acceptance:* block stays under budget with real data; demo profile
   suppresses sections 2–6; evals green.
3. **Pins and projects** (6–7): the `pinned_at` and `project_key` columns
   (one idempotent migration), `/memory` pin action, `project.active`
   setting, project-scope retrieval.
   *Acceptance:* a pinned claim always appears while active and passing
   filters; project claims retrievable only while their project is active.
4. **Accounts (deferred).** Fold profiles into the security work: auth
   (Phase 1) makes the active profile trustworthy; real request identity
   (Phase 2) maps authenticated users to profiles. Explicitly out of scope
   here.

## Alternatives considered

- **One registry with only shields, no file split** — workable (a profile =
  shield preset alone), but the operator explicitly wants separate files,
  and they are better *authoring* units: the tech file can be shared with a
  friend as a file, and a whole audience can be added/removed without
  touching entry-level shields. Shields alone also can't express "demo sees
  nothing from the overlay" without shielding every entry.
- **Per-profile customize dirs** (swap `customize.dir`) — rejected: switching
  would require a repopulate each time, skills would swap too (usually
  unwanted), and diffing three near-identical dirs is worse than one dir
  with audience files.
- **Filtering chat history per profile** — rejected as false safety; history
  is a transcript, not a knowledge base. Separate rooms or a separate DB are
  honest; a filter that *usually* hides PII is worse than a rule everyone
  can reason about.
- **An LLM-written "session summary" instead of the deterministic block** —
  rejected for the hot path: unbounded cost, unexplainable influence, and
  the summarizer digests already give the useful synthesis off-path.

## Open risk

The working-context block is the largest standing addition to the assistant
prompt since skills. If evals show small models losing the thread, the
fallback order is: shrink budgets → drop sections 2–3 (the informational
ones) → keep only 1, 4, 5 (identity, unresolved, don't-repeat), which are
the three with direct behavioral payoff.

## See also

- `2026-07-04-qa-overlay-person-schema.md` — the cards/stories/shield
  conventions profiles build on (per-subject shield aliases are the profile
  bundles' vocabulary).
- `2026-07-07-qa-overlay-first-person-voice.md` — the voice convention;
  unaffected, but the identity card quoted by section 1 follows it.
- `2026-06-25-security-review-mitigations.md` — Findings 1/8a (why profiles
  are not a boundary yet) and the phases accounts would ride on.
- `docs/assistant-design.md` — where the block slots into prompt assembly.
- `docs/qa-system.md`, `docs/memory-architecture.md` — the retrieval layers
  the filters extend.
