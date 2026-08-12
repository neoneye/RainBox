# Evals framework — design (the `evals/` package)

Deterministic runner, baseline comparison + gate, bounded config optimizer,
and a production monitor — all persisting to the `eval_case` / `eval_run` /
`eval_result` tables.

**Scope:** the framework internals only. The feedback→eval **loop
architecture** (why the pieces exist, gate/optimizer rules at the policy
level) is `docs/eval-loop.md`; the **operator workflow** (capture → promote →
run → compare routine) is `docs/eval-playbook.md`. Read those first — this
doc covers what they don't: module ownership, scoring math, persistence
semantics, and extension points.

## Where things live

| Piece | File |
|-------|------|
| Runner: score cases, persist runs/results, CLI | `evals/runner.py` |
| Comparison + gate: diff two runs, pass/fail verdict, CLI | `evals/compare.py` |
| Optimizer: candidate config matrix, safety selection | `evals/optimizer.py` |
| Production monitor: sample recent chat output, CLI | `evals/monitor.py` |
| Tables (`EvalCase`, `EvalRun`, `EvalResult`) | `db/models.py` |
| CRUD + feedback promotion (`promote_feedback_to_eval_case`) | `db/eval.py` (re-exported from the `db` facade) |
| Flask-Admin views (Feedback category) | `webapp/core.py` |
| Tests | `evals/test_runner.py`, `test_compare.py`, `test_optimizer.py`, `test_monitor.py`, `test_acceptance_spine.py` |

## Data model and persistence

Field-level inventory is in `docs/eval-loop.md`; the semantics that live in code:

- **`EvalCase`** — `input` / `expected` / `rubric` are JSONB blobs (default `{}`), editable in Flask-Admin. CHECK constraints pin `case_type` to `chat_reply | memory_retrieval | query_answer | tool_output`, `split` to `train | holdout | regression`, `status` to `candidate | active | archived`. `source_feedback_uuid` is a plain indexed UUID column (no FK): set for promoted cases, null for hand-authored ones.
- **`EvalRun`** — `started_at` auto-stamps on insert; `finished_at` stays NULL until `db.finish_eval_run(run_uuid, summary=...)` stamps it and stores the summary blob. `is_baseline` (default false) is flipped via `db.set_baseline_eval_run` or Flask-Admin.
- **`EvalResult`** — one row per case per run. FKs to run and case use `ondelete="CASCADE"`. A CheckConstraint enforces `score` ∈ [0.0, 1.0]. `details` is the per-criterion breakdown plus the threshold used.

All CRUD goes through `db/eval.py` helpers (`create_eval_case`, `list_eval_cases` with status/split/case_type/source filters, `create_eval_run`, `finish_eval_run`, `create_eval_result`, `list_eval_results_for_run`, …); nothing in `evals/` writes rows directly except `compare.py`'s read-only queries.

## The case model

A case is three JSONB blobs on one row:

- **`input`** — what the case runs against. For `chat_reply`: `actual_output` (the snapshot text to score) plus context fields (`room_history`, `current_message`, `debug_memory`, …). For `memory_retrieval`: `query`, optional `agent_uuid` / `room_uuid`.
- **`expected`** — the criteria: `must_include`, `must_not_include`, `expected_memories`, `forbidden_memories` (lists of strings), `requires_json` (bool), free-text `notes`.
- **`rubric`** — `threshold` (pass cutoff, default 0.7) and an advisory `criteria` list (named weights; not consumed by the runner today — scoring is the unweighted mean below).

**Hand-authored vs promoted.** Hand-authored cases are inserted via `db.create_eval_case` (or Flask-Admin) with `source_feedback_uuid=None`. `db.promote_feedback_to_eval_case(feedback_uuid)` builds a `chat_reply` case from a stored `FeedbackEvent`: the metadata snapshot becomes `input` (rated message text, prior human message as one-entry `room_history`, `debug_memory`/`debug_query`), `expected` starts with empty criterion lists and the feedback comment in `notes`, a default rubric is stamped, and the split defaults by rating (downvote → `regression`, upvote → `train`). Promoted cases start `status="candidate"` so a human edits the empty `expected` lists before flipping to `active` — a freshly promoted case has no criteria and would trivially score 1.0.

## Run lifecycle

`run_eval_suite(case_uuids=None, *, name, split, config, case_filter, agent_role="chat")` in `evals/runner.py`:

1. **Select cases** — explicit `case_uuids`, or every `status="active"` case (optionally filtered by `split`). `case_filter` (`case_uuids` / `split` keys) is an alternative selector so the optimizer's default runner can thread its filter through.
2. **Create the run** — `db.create_eval_run` with a config blob recording the candidate config plus the selection (`case_uuids`, `split`) for reproducibility. Config keys outside `SUPPORTED_CONFIG_KNOBS` (`memory_retrieval_limit`, `memory_include_secret`) are recorded under `unsupported_config_keys` rather than silently ignored.
3. **Score each case** — `run_eval_case(case, eval_run_uuid=..., config=...)` persists one `EvalResult` per case. Called without a run uuid it creates a one-off `ad-hoc:` run first.
4. **Finish** — `db.finish_eval_run` stamps `finished_at` and a summary blob: `cases`, `passed`, `failed`, `mean_score`, and a `failures` list (case uuid/name, score, details).

Execution is **synchronous and in-process** — no subprocesses, timeouts, or sandboxing. No live LLM is driven anywhere: `chat_reply` cases score the stored snapshot in `input["actual_output"]`; `memory_retrieval` cases make a real call to `memory.retrieval.retrieve_memories(query, agent_uuid, room_uuid, limit, include_secret)` — the deterministic lexical path — with `limit` / `include_secret` taken from the candidate config (defaults 6 / False). Any other `case_type` scores 0.0 with `details.error = "unsupported case_type: …"` — `query_answer` and `tool_output` exist in the schema but have no scorer yet.

## Scoring

Deterministic, no LLM judge. Each configured criterion contributes a value in [0.0, 1.0]; the case score is the **unweighted mean of the criteria that actually ran** (empty lists are flagged `skipped` and excluded). A case with no configured criteria scores 1.0 with a `warnings: ["no criteria configured"]` flag in details. `passed = score >= rubric.threshold` (default `DEFAULT_THRESHOLD = 0.7`; a malformed threshold falls back to the default).

| Criterion | Applies to | Value |
|-----------|-----------|-------|
| `must_include` | chat_reply | fraction of strings found as substrings of the output text |
| `must_not_include` | chat_reply | fraction of strings *absent* from the output text |
| `expected_memories` | both | fraction present — an entry matches if it is a substring of any retrieved memory's uuid or text |
| `forbidden_memories` | both | fraction absent, same substring rule |
| `requires_json` | chat_reply | 1.0 if the output parses as JSON, else 0.0 |

For `chat_reply`, the memory criteria are scored against the reply's own text (the expected/forbidden entry must appear in `actual_output`); for `memory_retrieval` they are scored against the retrieved list. Per-criterion detail dicts (`matched`/`absent`/`total`) land in `EvalResult.details` alongside `threshold`, which is what the failure-reason heuristics in both CLIs read back.

## Comparison and gate

`evals/compare.py` is a **pure read-only consumer**: it loads two runs' results, joins to `EvalCase` for name/split, and computes an `EvalComparison` dataclass — mean/pass-count deltas (means read from the runs' summary blobs, not recomputed), `new_failures` (pass→fail, with a human-readable reason), `improved`, `regressed` (score dropped beyond `EPSILON = 1e-9`), `common` (every shared case with both scores), and `only_in_baseline` / `only_in_candidate`.

`gate_candidate_run(baseline, candidate, max_mean_drop=0.02)` returns a `GateDecision` (passed, reasons, warnings, comparison; serializable via `to_json()` / `to_text()`). The rule set and its rationale are documented in `docs/eval-loop.md`; the implementation details worth knowing: per-split means for the overfitting warning are computed over **all** common entries (not just flipped/regressed ones), and the two unequal-case-set rejection reasons are formatted by helpers shared with the optimizer (`_format_missing_baseline_cases_reason`, `_format_extra_candidate_cases_reason`) so the wording cannot drift between the two call sites.

## Optimizer

`evals/optimizer.py` owns three pieces:

- **`generate_candidate_configs(base_config)`** — one variant per value in `CANDIDATE_MATRIX` (today: `memory_retrieval_limit` ∈ {3, 6, 10}), each a full copy of `BASE_CONFIG` with one knob substituted. Deliberately no cartesian product across knobs.
- **`run_candidate_matrix(configs, case_filter, runner=None)`** — one `EvalRun` per config via an injectable runner (default: a thin wrapper over `run_eval_suite`, naming runs `optimizer-candidate: limit=N`). Output order preserves input order; the tie-break below depends on it.
- **`select_best_candidate(baseline_run_uuid, candidate_run_uuids, holdout_tolerance=0.05)`** — applies the stricter-than-gate safety rules (see `docs/eval-loop.md`) per candidate and returns an `OptimizerDecision`: the safe candidate with the highest mean, or `selected_uuid=None` with per-candidate rejection reasons. The forbidden-memory rule is **absolute**: any result whose `details.forbidden_memories` shows `absent < total` rejects the candidate even if the baseline leaked the same memory.

There is no optimizer CLI; it is driven from Python (see the playbook). Selected configs are not auto-applied anywhere — promotion is a human decision.

## Production monitor

`evals/monitor.py` samples recent agent chat output into an `EvalRun` with `config.source == "production_sample"` — the canonical production marker, since the `case_type`/`split` CHECK constraints have no production tier. All sampled messages attach to one shared synthetic `EvalCase` named `production_sample_message` (`chat_reply` / `holdout`, created idempotently) purely to satisfy the FK. Sampling takes the newest `limit` `kind="message"` rows whose sender is an agent-type `ChatUser` (human rows excluded by join; diagnostic/progress rows excluded by kind). Scoring is two validators: empty stripped text → 0.0 fail (`non_empty`), length > 8000 → 0.5 fail (`length_bounded`), else 1.0 pass. Monitoring signal only — it never blocks chat.

## CLI entry points

| Command | Does | Exit code |
|---------|------|-----------|
| `python -m evals.runner --active [--split S] \| --case <uuid>… [--name N]` | run a suite, print the summary + per-failure reasons | 0 |
| `python -m evals.compare --baseline <uuid> --candidate <uuid> [--max-mean-drop F] [--json]` | print the gate verdict (text or full JSON) | 0 pass / 1 fail |
| `python -m evals.monitor --recent-chat [--limit N]` | sample production, print the run summary | 0 |

`compare` and `monitor` deliberately skip `db.init_db(app)`: its `ALTER TABLE … IF NOT EXISTS` migrations need an AccessExclusive lock that deadlocks against any caller holding an open SQLAlchemy session (e.g. a test invoking the CLI as a subprocess). `runner` does call it. The Flask-Admin views (Feedback category, `webapp/core.py`) are the editing surface: `EvalCaseView` exposes the JSONB blobs in the row form; `EvalRunView` lets an admin toggle `is_baseline`.

## Extension points

- **New case type.** `query_answer` / `tool_output` are already allowed by the CHECK constraint; wire one up by adding a branch in `run_eval_case` (produce an output, call a scorer) plus a `score_<type>_case` function in `evals/runner.py`.
- **New criterion.** Add a `_score_<criterion>` helper returning `(value, detail)`, call it from the relevant `score_*_case`, and extend the failure-reason key list in **both** `evals/runner.py::_print_summary` and `evals/compare.py::_failure_reason` (they mirror each other by hand).
- **New config knob.** Add the key to `SUPPORTED_CONFIG_KNOBS` and thread it where it takes effect (today: the `retrieve_memories` call). To let the optimizer explore it, add values to `CANDIDATE_MATRIX`. An unthreaded key is not an error — it surfaces in `unsupported_config_keys`.
- **New monitor validator.** Extend `_score_message` in `evals/monitor.py` and the synthetic case's `rubric.validators` list.
- **Custom suite execution.** `run_candidate_matrix`'s injectable `runner` callable is the seam for driving cases through something other than `run_eval_suite` (tests use it; a future live-LLM runner would too).

## Deliberate limits

- Scoring is substring matching over snapshots — no live chat generation, no LLM-as-judge (see `docs/eval-loop.md` § Current Limits).
- `rubric.criteria` weights are stored but unused; the mean is unweighted.
- Case selection for a run is by status/split/uuid only; there is no tagging or per-case-type suite composition.
- No dedicated eval UI page — Flask-Admin plus the three CLIs are the whole surface.
