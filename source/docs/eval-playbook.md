# Eval Playbook

## Purpose

This is the practical workflow for using the eval loop. For architecture, see
`docs/eval-loop.md`.

## 1. Capture Feedback

Use `/chat` normally. Upvote or downvote user-facing agent messages.

Feedback creates `FeedbackEvent` rows. Downvotes can also create
`RetrievalEvent(stage='downvoted')` rows for same-turn memory/Q&A context.

## 2. Promote Feedback Into Eval Cases

Promote meaningful feedback into `EvalCase` rows.

Recommended defaults:

- downvote -> `split="regression"`
- upvote -> `split="train"`
- new promoted cases start as `status="candidate"` until reviewed

Review the generated fields:

- `input`
- `expected`
- `rubric`
- `source_feedback_uuid`

Activate the case only when it expresses a behavior worth preserving or fixing.

## 3. Run The Eval Suite

Run all active cases:

```bash
venv/bin/python eval_runner.py --active
```

Run regression cases only:

```bash
venv/bin/python eval_runner.py --active --split regression
```

Run one case:

```bash
venv/bin/python eval_runner.py --case <eval-case-uuid>
```

## 4. Mark Or Select A Baseline

Use Flask-Admin on `EvalRun` to mark the trusted run as `is_baseline`, or pass
the baseline UUID explicitly to comparison commands.

Baselines should be stable and should include the important active cases.

## 5. Compare Candidate Runs

```bash
venv/bin/python eval_compare.py \
  --baseline <baseline-run-uuid> \
  --candidate <candidate-run-uuid>
```

The gate should fail on:

- excessive mean drop
- regression split pass -> fail
- missing baseline cases

The next hardening target is to also fail on candidate-only cases.

## 6. Run Optimizer Candidates

Use `eval_optimizer.py` from Python or a small wrapper to generate and run
bounded candidate configs.

Current meaningful knob:

- `memory_retrieval_limit`

Supported config key for secret memory access:

- `memory_include_secret`

Misnamed or unsupported keys should appear in `unsupported_config_keys`.

## 7. Monitor Production Samples

```bash
venv/bin/python eval_monitor.py --recent-chat --limit 50
```

This samples recent agent `kind="message"` rows. It ignores human and diagnostic
rows. Treat this as a quality signal, not a blocker.

## Case Authoring Guidance

Good eval cases are:

- specific
- deterministic
- tied to a real failure or requirement
- small enough to debug
- assigned to the correct split

Use `regression` for behavior that must not break.
Use `holdout` for behavior you want to protect from overfitting.
Use `train` for examples used while tuning.

## Avoid These Mistakes

- Do not compare runs with different case sets unless explicitly intended.
- Do not trust mean score alone.
- Do not promote every casual downvote into an active regression case.
- Do not tune from telemetry counters without representative eval cases.
- Do not use `memory_include_private` to test secret retrieval; use
  `memory_include_secret`.

## Recommended Routine

1. Review recent feedback.
2. Promote only useful examples.
3. Activate reviewed cases.
4. Run baseline and candidate.
5. Compare.
6. Inspect failures.
7. Only then tune retrieval or prompts.
