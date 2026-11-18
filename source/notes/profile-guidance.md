# Profile guidance — formatting guide + knowledge calibration

The profile selected by `profile.current` drives two assistant prompt
blocks, both rendered from one per-turn context snapshot:

| Block | Authority | Source | Gated? |
|---|---|---|---|
| `<user_settings_yaml>` | context (by system-prompt rule; the tag carries no attributes); ranked in `source_priority` where its comments' defaults belong | profile fields as YAML (`user_profile/identity.py`), with the formatting guide as `#` comments on the fields it derives from (`user_profile/formatting.py`) | no — always on when a profile is selected |
| `<user_expertise_yaml>` | context (by system-prompt rule; the tag carries no attributes) | self-declared topic rows as a YAML list (`user_profile/user_calibration.py`) | no — always on when a profile has topics |

The formatting guide compiles the locale fields — date format, first day of
week, time format, timezone (with the current UTC offset), measurement
system (metric / US customary / the UK hybrid), temperature (derived from
the measurement system when unset, in which case it rides the units
comment), number format, currency — into code-owned comments with examples
(free-text profile values pass a strict prompt boundary or are omitted —
they can never become instructions). Each comment sits on the line of its
own field, after the value, and never restates it: the key is the topic,
the value the setting, the comment the example or the rule, one short
clause. A field whose stored value is an opaque enum shows the guide's
display form instead (`temperature: Celsius (°C)` for the stored
`celsius`, also when derived from the units). The guide adds nothing else
to the block, no header and no trailing line:

```yaml
full_name: Karl Weierstraß
given_name: Karl
address_as: you # address the user as "you", never by name
units: metric # Prefer km and kg; keep a source value when precision matters and add the conversion
temperature: Celsius (°C)
timezone: Europe/Berlin # Currently UTC+02:00
date_format: DD.MM.YYYY # Example 31.12.2026
time_format: 24h # Example 23:59
number_format: 1.234.567,89 # Use DOT as thousands separator and COMMA as decimal separator.
currency: EUR # Example 1.234,56 EUR; convert only with a supplied or freshly retrieved rate
```

An enum field's registry gloss renders the same way (`address_as`,
`mention_as`, `number_format`). Comments are invisible to a YAML parser, so the block still
round-trips exactly through `yaml.safe_load` (`user_profile/export.py`
relies on that).
The system prompt says what the comments are (the profile's formatting
defaults, overridden by the current request and exact source notation), so
the block does not repeat it.

Language is not a formatting comment. The reply language is decided by the
response-language classifier and delivered as `reply_language_markdown`
(see `assistant-design.md`); the declared language rows reach the
classifier through its own `user_settings_languages_json` block.
Knowledge calibration is the operator's per-topic declaration (level, stance,
depth, note), edited on `/profile` and injected under a shared 2 700-char
budget with an honest degrade-then-drop ladder (the guide's comments are
admitted first, calibration takes the remainder). Explicit requests in the
current message always override both. Switching `profile.current` changes
both blocks and posts a one-time context marker into each room; it preserves
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
`acceptance_criteria_markdown`, `user_settings_yaml` with its formatting
comments (separators, dates, units, currency, language and its variant) and
the turn's observations. It returns a typed
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
escape. Its model resolves through the `assistant.reply_audit`
slot on `/agentmodel`, else `assistant.default`, and it fails open: an
unbound or unreachable auditor sends the message rather than losing the
turn's answer. Every verdict lands in its own `reply_audit` trace row with
the model, duration and prompts that produced it.

Neither block has a production switch: a selected profile puts both in
every call of the turn — the acceptance-criteria call included — from one
rendering per turn. The live release gate below measures whether they earn
their tokens; it does not gate shipping.

## Where things live

| Piece | File |
|---|---|
| Formatting renderer + prompt-boundary validation | `user_profile/formatting.py` |
| Calibration renderer + guidance budget | `user_profile/user_calibration.py` |
| Per-turn context snapshot | `user_profile/context.py` |
| Calibration storage/validator/API | `db/profile_calibration.py`, `webapp/profile_api.py` |
| Row-lock mutation helper (cross-subtree safety) | `db/profile.py` `profile_mutate_data` |
| Pointer settings | `db/settings.py` (`profile.current`, internal `profile.current_changed_at`) |
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

1. On `/config`: set `profile.current` to a profile (e.g. your duplicated
   copy).
2. In a chat room with the assistant, ask anything ("how far is 100 km?").
3. Open `/assistant`, select the newest run, and inspect any step's **user
   prompt**. It must contain, in order: `<user_settings_yaml>` carrying a
   `# …` comment on each locale field (and `temperature:` in its display
   form), then `<user_expertise_yaml>` with the YAML rows (when the profile
   has calibration topics).
4. In the same run, the final `reply` must be preceded by a `reply_audit`
   row carrying its own model, duration and prompts. Open it: the
   observation shows the verdict and any problems. A `revise` verdict must
   appear as a rejected reply step followed by a corrected one. With no
   model bound to `assistant.reply_audit` or `assistant.default`, the row
   records `no_model_group` and the
   message is sent — the audit fails open by design.
5. Switch `profile.current` to another profile → the room's next turn is
   preceded by a visible one-time notice ("the active profile switched to
   …"); the marker itself must NOT appear inside the model's prompt.
6. Set both switches back to unset — the next run's prompt must carry the
   identity fields only (no locale comments; the `number_format` comment
   stays).

If a block or the comments are missing when expected, expand the step's collapsed **log**
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

### 6. What the gate decides

There is no production switch: both blocks render on every turn of a
selected profile. The gate's verdict is advice on whether the blocks earn
their tokens, and the way to act on a failed one is to fix the block (or
the profile) and re-run, not to flip it off.

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

- `notes/proposals/2026-07-21-formatting-guide-and-knowledge-survey.md` — the
  full design, precedence contract, and release-gate rationale.
- `profile-design.md`, `assistant-design.md`, `settings-design.md`,
  `eval-loop.md` — the subsystem docs this feature touches.
