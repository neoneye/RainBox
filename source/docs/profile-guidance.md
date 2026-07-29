# Profile guidance — formatting guide + knowledge calibration

The profile selected by `profile.current` drives three assistant prompt
blocks, all rendered from one per-turn context snapshot:

| Block | Authority | Source | Gated? |
|---|---|---|---|
| `<user_settings_json>` | context (by system-prompt rule; the tag carries no attributes) | profile fields as JSON (`user_profile/identity.py`; opaque enums like `number_format` carry a code-owned `.comment` entry) | no — always on when a profile is selected |
| `<formatting_guide>` | instructions | deterministic locale directives (`user_profile/formatting.py`) | **`assistant.formatting_guide`**, default off |
| `<knowledge_calibration>` | context | self-declared topic rows as JSONL (`user_profile/calibration.py`) | **`assistant.knowledge_calibration`**, default off |

The formatting guide compiles the locale fields — date format, first day of
week, time format + timezone (with the current UTC offset), measurement
system (metric / US customary / the UK hybrid), temperature (derived from
the measurement system when unset), number format, currency, language — into
code-owned directives with examples (free-text profile values pass a strict
prompt boundary or are omitted — they can never become instructions).

The language directive is rendered from the declared tag itself, so no
language is built in: a tag carrying a region or script subtag (`en-GB`,
`pt-BR`, `zh-Hans`) states that variant — spelling and vocabulary alike,
since a directive naming only spelling gets applied to orthography alone —
and a bare primary tag (`en`, `da`) has no variant to state. The variant is
always NAMED by its tag and never exemplified: contrastive example words in
a prompt get parroted into unrelated replies.
Knowledge calibration is the operator's per-topic declaration (level, stance,
depth, note), edited on `/profile` and injected under a shared 2 700-char
budget with an honest degrade-then-drop ladder. Explicit requests in the
current message always override both. Switching `profile.current` changes all
three blocks and posts a one-time context marker into each room; it preserves
history and is **not an audience boundary**.

The `reply` action carries one argument, `{"message": ...}`: the answer
text, written in the language of the operator's current message (the
profile's preferred language applies only on explicit request) and obeying
the constraints already established for the turn. Neither the constraints
nor the audit is a reply argument — the acceptance-criteria step
establishes the constraints before any work, and the audit is a separate
call after the message exists.

That call is the **reply audit**: a reviewer that did not write the message
reads it against the request (every sentence and sub-question answered),
`acceptance_criteria_json`, `user_settings_json`, the formatting guide
(separators, dates, units, currency, language and its variant) and the
turn's observations. It returns a typed
`{reason, problems[], verdict: send|revise}` — a verdict the code reads,
not prose it parses. A `revise` bounces the reply as a rejected step: the
message is not posted, the problems flow into the scratchpad, and the model
fixes the message. Bounces are capped (`MAX_AUDIT_REJECTIONS`, 2 per run)
so an auditor that never approves cannot fail the turn: past the cap the
reply ships anyway, and the run summariser typically flags the outcome
Unresolved.

The auditor is shown the turn's observations but not the decide loop's
reasoning. A message claiming a figure no step observed is exactly what an
audit should catch, so the evidence has to be there — while the reasoning
that produced the message is the rationalization a separate call exists to
escape. Its model resolves through the `reply_audit` binding on
`/agentmodel`, else the assistant's own group, and it fails open: an
unbound or unreachable auditor sends the message rather than losing the
turn's answer. Every verdict lands in its own `reply_audit` trace row with
the model, duration and prompts that produced it.

The two gated blocks ship dark: each switch is flipped only after its block
passes the live release gate below. Everything else on this page (the
`/profile` editor, calibration storage/API, the identity block) is active
regardless of the switches. One consumer bypasses the formatting switch on
purpose: the acceptance-criteria call (see `assistant-design.md` §Acceptance
criteria, which has no switch — it runs on every turn) always receives the
formatting guide rendered from its snapshot profile — the switch gates only
the decide-prompt injection, and the criteria step needs the guide's
derived defaults (units → temperature, separators) either way.

## Where things live

| Piece | File |
|---|---|
| Formatting renderer + prompt-boundary validation | `user_profile/formatting.py` |
| Calibration renderer + guidance budget | `user_profile/calibration.py` |
| Per-turn context snapshot | `user_profile/context.py` |
| Calibration storage/validator/API | `db/profile_calibration.py`, `webapp/profile_api.py` |
| Row-lock mutation helper (cross-subtree safety) | `db/profile.py` `profile_mutate_data` |
| Switch + pointer settings | `db/settings.py` (`assistant.formatting_guide`, `assistant.knowledge_calibration`, `profile.current`, internal `profile.current_changed_at`) |
| Assistant injection + context marker | `agents/assistant.py` |
| Live eval runner (prompt variants, seeded case inventory) | `evals/profile_guidance.py` |
| Executable release gate | `evals/profile_gate.py` |

## Verifying that things work

Ordered from cheap to expensive; the first three need no LLM at all.

### 1. Automated tests (no model, sandbox DB)

```bash
cd source
venv/bin/python -m pytest user_profile/ evals/ \
    db/test_profile_calibration.py db/test_set_current_profile.py \
    db/test_profile_tree.py \
    agents/test_assistant_formatting_guide.py \
    agents/test_assistant_context_marker.py \
    webapp/test_profile_api.py webapp/test_profile_views.py -q
```

All of these must pass (~220 tests; `conftest.py` forces the sandbox
`rainbox_claude` database). They cover the renderers (golden Germany/India
bodies, DST offsets, currency minor-unit exceptions, the truncation ladder),
the validator limits, merge/concurrency safety incl. the delete/switch lock,
the marker semantics, prompt assembly order, and every gate rule.

### 2. Browser check — the calibration editor

Start the app, open `/profile`:

- Open the **US** template → the *Knowledge calibration* fieldset shows the
  two shipped fixture rows (Python, JavaScript), read-only, under the
  Topic / Level / Stance / Depth column headers.
- **Duplicate** it → in the copy, add a topic row (its Level defaults to
  `intermediate`; the Stance and Depth pickers read `Unspecified`, Level's
  empty state reads `Choose…`), pick a stance *before* typing a topic
  (status must read `Not saved — a row needs a topic`), type the topic
  (→ `Saving…` → `Saved ✓`), reorder with ↑/↓ (row stamps must NOT change),
  enter a duplicate topic (a precise red validation message, no retry
  loop), remove a row.
- The *Locale & formats* preview line shows the selected number format's
  sample.

### 3. Prompt inspection — see the blocks in a real turn

This is the direct proof the assistant actually carries the blocks:

1. On `/settings`: set `profile.current` to a profile (e.g. your duplicated
   copy), and set `assistant.formatting_guide` and
   `assistant.knowledge_calibration` to `true` (temporarily, if you are just
   verifying — see section 6 for the gated rollout).
2. In a chat room with the assistant, ask anything ("how far is 100 km?").
3. Open `/assistant`, select the newest run, and inspect any step's **user
   prompt**. It must contain, in order: `<user_settings_json>`,
   `<formatting_guide authority="instructions">` with the profile's
   directives, `<knowledge_calibration authority="context">` with the JSONL
   rows (when the profile has calibration topics).
4. In the same run, the final `reply` must be preceded by a `reply_audit`
   row carrying its own model, duration and prompts. Open it: the
   observation shows the verdict and any problems. A `revise` verdict must
   appear as a rejected reply step followed by a corrected one. With no
   model bound to `reply_audit`, the row records `no_model_group` and the
   message is sent — the audit fails open by design.
5. Switch `profile.current` to another profile → the room's next turn is
   preceded by a visible one-time notice ("the active profile switched to
   …"); the marker itself must NOT appear inside the model's prompt.
6. Set both switches back to unset — the next run's prompt must carry the
   identity block only.

If a block is missing when expected, expand the step's collapsed **log**
(above the model request) first — it records the active profile (with a
`/profile` deep link) and both switch states for that exact turn. Then
check: is the switch on; is `profile.current` set (unset = no blocks at
all); does the profile have the relevant fields/topics; and the supervisor
log — a renderer failure logs a warning and empties only its own block,
never the turn.

### 4. Live evals — the Phase 0/3 measurement (needs your bound model)

```bash
cd source
venv/bin/python -m evals.profile_guidance --seed-cases
```

This creates/updates the code-owned candidate cases (idempotent and
versioned: re-running after a definition fix updates cases in place;
operator-edited cases are never touched). Review them in Flask-Admin
(`/admin` → EvalCase, names start with `pg `) and flip the ones you accept
to `active`. Then run the four gate variants — three repetitions per case at
production sampling, so expect model traffic:

```bash
venv/bin/python -m evals.profile_guidance --variant baseline
venv/bin/python -m evals.profile_guidance --variant formatting_only
venv/bin/python -m evals.profile_guidance --variant calibration_only
venv/bin/python -m evals.profile_guidance --variant combined
```

Each prints its EvalRun uuid and summary. Exit code 2 means invalid case
definitions (broken counterfactual pair) — fix before proceeding. The runner
never touches settings or chat rooms; the profile is a per-call override.

A fifth variant, `classifier`, runs both blocks **and** makes the live
response-language call before the decide prompt, so its Markdown reaches the
reply exactly as in production:

```bash
venv/bin/python -m evals.profile_guidance --variant classifier
```

It is not part of the release gate — it is how the `language` family
separates the two metrics the classifier design keeps apart: whether the
reply language is *decided* correctly, and whether the decision is
*delivered*. Compare it against `combined` over the same cases; the delta is
the classifier's effect, with the guide held constant. Nothing persists from
the classifier call in an eval run (the agent has no run row, so its
checkpoint and step rows are skipped).

### 5. The release gate

```bash
venv/bin/python -m evals.profile_gate \
    --baseline <uuid> --formatting <uuid> \
    --calibration <uuid> --combined <uuid>
```

The gate validates the evidence before trusting any number (finished live
runs of the right variants, the currently bound model group and membership,
exactly three repetitions, the complete current seed inventory, identical
per-case manifests) and applies the fixed contract: hard-zero exact-source,
2-of-3 with the 90% override rate, no regressions, +0.15 locale / +0.10
calibration margins. Exit codes: **0** every requested decision passed,
**1** a decision failed, **2** the evidence is invalid (never read 2 as a
fail). The verdict persists as a `profile-gate` EvalRun and ends with:

```text
allowed enablement: {'formatting_alone': …, 'calibration_alone': …, 'both': …}
```

### 6. Enable (and roll back)

Flip only what the gate allowed, on `/settings`:
`assistant.formatting_guide` and/or `assistant.knowledge_calibration` →
`true`. Rollback is the same switch back to unset — the blocks vanish from
the next turn; nothing else depends on them.

## Known limitations

- Deterministic scoring cannot detect *paraphrased* system-prompt leakage in
  the injection case (the canary catches literal compliance); there is no
  LLM judge by design.
- The currency minor-unit sets cover prompt examples, not ISO 4217; unknown
  currencies default to two decimals (a documented v1 decision).
- The calibration editor's state machine is covered by marker tests and
  manual browser verification; there is no automated browser suite.
- Chat agents (`agents/chat_context.py`) do not carry the blocks yet —
  main-assistant-first is deliberate (Phase 4 of the proposal adds a shared
  assembler after a positive Phase 3 result).

## See also

- `docs/proposals/2026-07-21-formatting-guide-and-knowledge-survey.md` — the
  full design, precedence contract, and release-gate rationale.
- `profile-design.md`, `assistant-design.md`, `settings-design.md`,
  `eval-loop.md` — the subsystem docs this feature touches.
