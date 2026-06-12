# Package restructure — execution status

**Paused:** 2026-06-12 (operator away). Resume by following
`2026-06-12-package-restructure.md` from **Task 4**, using the
superpowers:subagent-driven-development workflow (implementer + spec review +
quality review per task).

**Branch:** `restructure-packages` (off `main`). Working tree clean at the
commit that adds this file. A partial Task 4 start was rolled back — Task 4
begins fresh.

## Completed

| Plan task | Result | Commits |
|---|---|---|
| 1. Preflight baseline | recorded (below) | — |
| 2. `db/` package | done + reviewed | `a7ed499`, `03f9ea3` (polish) |
| 3. `chat/` package | done + reviewed | `f3b8cbe`, `2150399` (docstring fix) |

Unplanned-but-necessary fix in Task 2: `db/__init__.py make_app()` now passes
an explicit `static_folder` anchored to the source root, because
`Flask(__name__)` inside a package resolves `root_path` to `db/` instead of
the source root. Verified necessary-and-correct by review.

## Remaining

Tasks 4–11 per the plan: `memory/`+`data/` (4), `evals/` (5), `llm/` +
models_test_worker spawn (6), `benchmarks/` + worker spawns (7), `backup/` (8),
`agents/` + agent.py split + supervisor spawn (9), remaining tests +
docs/README (10), final verification + whole-branch review (11).

## Baseline every task must reproduce

```
venv/bin/python -m pytest -q --ignore=whisper_service --ignore=kokoro_service
```

- **2 failed, 993 passed, 10 skipped.** The 2 failures are PRE-EXISTING on
  `main`: `test_query_filter_router_memory_ops.py` —
  `AttributeError: 'QueryFilterRouterAgent' object has no attribute
  'model_group_uuid'` (`query_filter_router_agent.py:339`). Out of scope; do
  not fix silently as part of the migration.
- 0–3 flaky `too many clients` psycopg ERRORs may appear per run; the affected
  tests pass when rerun in isolation (pre-existing connection exhaustion).
- pyright: **740 errors** (pre-existing). Compare sorted error *sets* via
  `pyright --outputjson`, not raw counts — and note `grep -c error` overcounts
  by 1 (summary line).

## Gotchas learned so far

- Plain `python -m pytest` fails on `main` too: `kokoro_service/test_server.py`
  vs `whisper_service/test_server.py` basename collision (no `__init__.py` in
  the service dirs) — hence the `--ignore` flags above.
- IDE pyright diagnostics lag after package moves and show false
  "Import could not be resolved" errors; trust a fresh CLI `pyright` run.
- All moves must be `git mv` (verify `R` status in `git show --name-status`).
- Manual app boots for verification must use
  `DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude` — never
  production.
