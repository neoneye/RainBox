# Eval Loop Architecture

## Purpose

The eval loop turns real chat feedback into repeatable checks. It exists so
memory and retrieval changes can be tested before they become trusted behavior.

The current loop is deterministic-first:

```text
agent reply
-> feedback_event
-> eval_case
-> eval_run / eval_result
-> baseline comparison
-> optimizer candidate decision
```

LLM-as-judge is intentionally out of scope for the current implementation.

## Core Tables

### `feedback_event`

Stores upvotes/downvotes on user-facing agent messages.

Important fields:

- `room_uuid`
- `message_uuid`
- `agent_uuid`
- `rating`
- `comment`
- `metadata`

The metadata snapshot includes the rated message text, latest prior human
message, and same-turn diagnostics such as `debug-memory` and `debug-query`.

### `eval_case`

Stores a benchmark case promoted from feedback or written by hand.

Important fields:

- `case_type`: `chat_reply`, `memory_retrieval`, `query_answer`, `tool_output`
- `split`: `train`, `holdout`, `regression`
- `status`: `candidate`, `active`, `archived`
- `input`, `expected`, `rubric`
- `source_feedback_uuid`

Downvoted feedback defaults to a regression case. Upvoted feedback defaults to a
train case.

### `eval_run`

One execution of a set of eval cases.

Important fields:

- `name`
- `agent_role`
- `config`
- `summary`
- `is_baseline`

The config records the case filter and candidate settings, such as
`memory_retrieval_limit`.

### `eval_result`

One case outcome inside one eval run.

Important fields:

- `eval_run_uuid`
- `eval_case_uuid`
- `score`
- `passed`
- `details`

## Runner

`evals/runner.py` runs active or explicitly selected eval cases.

Supported current case types:

- `chat_reply`: scores known output snapshots from `case.input["actual_output"]`.
- `memory_retrieval`: calls `memory.retrieval.retrieve_memories` (the
  deterministic lexical path, kept for reproducibility).

`query_answer` and `tool_output` exist in the schema but have no scorer yet.

A separate opt-in **live** runner, `evals/profile_guidance.py`, executes
chat_reply cases that carry `message` + `profile_uuid` (or an inline
`profile`) against the real assistant prompt-construction path and a real
model: three repetitions per case at production sampling, deterministic
scoring only, four prompt variants (baseline / formatting_only /
calibration_only / combined), and per-repetition
output/prompt-hash/token/model records stored on each EvalResult so release
gates can apply per-family rules over the raw repetitions. It never mutates
settings (the profile is a per-call override) and creates no chat rows; it
is not part of the default deterministic suite.

Supported candidate config keys:

- `memory_retrieval_limit`
- `memory_include_secret`

Unknown keys are recorded in `unsupported_config_keys` on the run config instead
of silently pretending they were evaluated.

## Comparison And Gate

`evals/compare.py` compares a candidate run against a baseline run
(CLI: `--baseline`, `--candidate`, `--max-mean-drop`, `--json`).

Current gate rules:

- fail if mean score drops beyond tolerance.
- fail if regression split cases go pass -> fail.
- fail if candidate omitted baseline cases.
- fail if candidate ran cases not present in the baseline (so a candidate
  cannot add easy unmatched cases to inflate its mean).
- warn if train improves while holdout drops.

The gate refuses to compare unequal case sets by default; an intentional
partial-comparison mode would be a future named option. The two case-set
rejection reasons share one formatter with the optimizer, so their wording
cannot drift between the two sites.

## Optimizer

`evals/optimizer.py` tries bounded candidate configurations. It is deliberately
not a free-form source or prompt rewriter.

Current candidate matrix:

- `memory_retrieval_limit`: `3`, `6`, `10`

Optimizer safety rules are stricter than the basic gate:

- candidate mean must not drop (the gate allows a small tolerated drop; the
  optimizer requires >= 0).
- regression pins must not break.
- holdout drop must stay within tolerance.
- forbidden-memory failures reject the candidate (even if the baseline leaked
  too).
- missing baseline cases and candidate-only cases both reject the candidate.

## Production Monitor

`evals/monitor.py` samples recent production chat outputs into an eval run. It is
a monitoring signal, not a runtime guardrail.

It ignores:

- human messages
- diagnostic rows
- progress/thinking rows

## Current Limits

- Chat-reply evals score snapshots, not live LLM calls.
- No LLM-as-judge scoring yet.
- Optimizer tests bounded configs; it does not autonomously edit code or prompts.
- Eval quality depends on promoted/hand-authored case quality.

## Design Principle

The eval loop should make improvements harder to fake. A candidate should only
look better when it improves the same important behavior, not because it skipped
hard cases or added easy ones.
