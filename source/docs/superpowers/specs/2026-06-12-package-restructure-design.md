# Package restructure: flat root → sibling packages

**Date:** 2026-06-12
**Status:** approved design, pending implementation plan

## Problem

The source root holds 119 Python files (61 tests, 58 modules) in one flat
directory, organized only by name prefix (`agent_*`, `db_*`, `eval_*`,
`benchmark_*`, `test_*`). The `webapp/` and `tools/` packages already prove the
better shape; this restructure extends it to the rest.

## Decisions (made with the operator)

1. **Topology:** sibling top-level packages next to `main.py` (not a single
   `rainbox/` package, not a minimal regroup). No `pyproject.toml` needed;
   pyright `extraPaths: ["."]` and `python3 main.py` keep working.
2. **Tests:** colocated inside the package of the module they test, following
   the `tools/` precedent. Root `conftest.py` stays at root so the
   `rainbox_claude` DB-safety pin loads before any test import.
3. **Naming:** moved modules drop their old prefix (`agent_followup.py` →
   `agents/followup.py`); the package name carries the context.
4. **Migration:** clean break — all imports rewritten in one pass, no root
   shim modules. The test suite (~892 tests) is the safety net.
5. **`memory/` collision:** the data dir `memory/` (one file,
   `question_answer.jsonl`) is renamed to `data/`, freeing `memory/` for the
   code package.

## Target layout

```
source/
├── main.py                      # entrypoint (unchanged role)
├── conftest.py                  # DB-safety pin (stays at root)
│
├── agents/
│   ├── __init__.py
│   ├── __main__.py              # from agent.py: main(), role registry → `python -m agents`
│   ├── base.py                  # from agent.py: Agent, ModelGroupAgent, StructuredLLMAgent
│   ├── config.py                # agent_config.py
│   ├── followup.py              # agent_followup.py
│   ├── chat_structured.py       # agent_chat_structured.py
│   ├── chat_unstructured.py     # agent_chat_unstructured.py
│   ├── tool_demo.py             # agent_tool_demo.py
│   ├── mcp.py                   # agent_mcp.py
│   ├── mcp_config.py            # mcp_config.py
│   ├── conversation.py          # agent_conversation.py
│   ├── kanban_worker.py         # agent_kanban_worker.py
│   ├── edit_document_v1.py … edit_document_v6.py
│   ├── router.py                # router_agent.py
│   ├── query.py                 # query_agent.py
│   ├── query_router.py          # query_router_agent.py
│   ├── query_filter_router.py   # query_filter_router_agent.py
│   ├── query_handlers.py
│   ├── query_kb_helpers.py
│   ├── persona.py
│   └── patch_apply.py           # the edit_document patch language lives with its agents
│
├── db/
│   ├── __init__.py              # today's db.py facade — `import db` call sites unchanged
│   ├── models.py                # db_models.py
│   ├── queue.py, model_config.py, chat.py, conversation.py, memory.py,
│   └── feedback.py, eval.py, cron.py, kanban.py, settings.py
│
├── chat/
│   ├── transcript.py            # chat_transcript.py
│   └── streaming.py             # chat_streaming.py
│
├── memory/
│   ├── ops.py                   # memory_ops.py
│   └── retrieval.py             # memory_retrieval.py
│
├── evals/
│   ├── runner.py, compare.py, optimizer.py, monitor.py
│
├── benchmarks/
│   ├── basic.py                 # benchmark.py (definitions behind /benchmark_basic)
│   ├── runner.py                # benchmark_runner.py
│   ├── worker.py                # benchmark_worker.py
│   ├── subproc.py               # benchmark_subprocess.py (renamed: avoid stdlib-confusing `benchmarks.subprocess`)
│   ├── kanban.py                # benchmark_kanban.py
│   ├── editdocument.py, editdocument_runner.py, editdocument_worker.py
│
├── llm/
│   ├── __init__.py              # today's llm.py — `import llm` call sites unchanged
│   └── models_test_worker.py    # models_test_worker.py (spawned by webapp/models_views.py)
│
├── backup/
│   ├── dump.py                  # backup_db.py  → CLI becomes `python -m backup.dump`
│   └── remote.py                # backup_remote.py → `python -m backup.remote`
│
├── data/
│   └── question_answer.jsonl    # was memory/question_answer.jsonl
│
└── webapp/, tools/, providers/, mcp_servers/,
    kokoro_service/, whisper_service/, agent_profiles/, static/, docs/   # unchanged
```

Every package gets an `__init__.py` (real packages; also keeps pytest
discovery unambiguous with colocated tests).

### Facade preservation

`db/__init__.py` is today's `db.py` with its internal re-export lines updated
(`from db_models import *` → `from db.models import *`). Likewise
`llm/__init__.py` is today's `llm.py`. The two most-imported names
(`import db`: 85 files, `import llm`: 9 files) therefore do not change at call
sites.

### agent.py split

`agent.py` is both the class hierarchy and the spawned child process. It
splits: `agents/base.py` holds `Agent` / `ModelGroupAgent` /
`StructuredLLMAgent` (and the KNOWN ISSUES header comment); `agents/__main__.py`
holds `main()` (socket-fd parsing, config read, role→class registry, `run()`),
so the spawn target is `python -m agents`.

## Subprocess spawn mechanics

Four sites spawn root scripts by `__file__`-derived path today:

| Spawner | Old target | New invocation |
|---|---|---|
| `main.py` (`AGENT_SCRIPT`, posix_spawn) | `agent.py` | `-m agents` |
| `webapp/models_views.py:841` | `models_test_worker.py` | `-m llm.models_test_worker` |
| `benchmark_runner.py:29` | `benchmark_worker.py` | `-m benchmarks.worker` |
| `benchmark_editdocument_runner.py:42` | `benchmark_editdocument_worker.py` | `-m benchmarks.editdocument_worker` |

Uniform pattern: spawn `[sys.executable, "-m", "<module>", *args]` with an env
whose `PYTHONPATH` is prefixed by the source root (`ROOT = dirname(abspath(__file__))`
of the spawning module's root anchor). This makes child imports independent of
the parent's CWD — strictly more robust than today's `sys.path[0]` reliance.

`backup_db.py` / `backup_remote.py` CLI invocations (docs/backup.md, any cron
job payloads, operator shell habits) change to `python -m backup.dump` /
`python -m backup.remote`. The implementation plan must grep cron payloads and
docs for the old script names.

## Test colocation map

| Root test files | New home |
|---|---|
| `test_agent_*`, `test_patch_apply` | `agents/` |
| `test_db_*` | `db/` |
| `test_eval_*` | `evals/` |
| `test_benchmark_*` | `benchmarks/` |
| `test_*_views`, `test_cron_*`, `test_chat_feedback_api`, `test_stt_whisper_views`, `test_tts_kokoro_views`, `test_voice_echo_views`, `test_kanban_*` | `webapp/` |
| `test_providers`, `test_ollama_provider`, `test_jan_provider`, `test_lm_studio`, `test_sync_*`, `test_model_config_provider` | `providers/` |
| `test_chat_streaming`, `test_chat_transcript` | `chat/` |
| `test_memory_ops`, `test_memory_retrieval` | `memory/` |
| `test_backup_db`, `test_backup_remote` | `backup/` |

(Exact per-file assignment happens in the implementation plan; the rule is
"the package of the module under test". Tests that exercise webapp routes go
to `webapp/` even when named after a feature.)

## Mechanics & tooling

- All moves via `git mv` so history follows renames.
- Imports rewritten in the same commit (clean break, no shims).
- `data/question_answer.jsonl`: update the path constant in `agents/query.py`
  (the KB loader) and references in README/docs. The operator overlay
  (`customize.dir` setting) is unaffected.
- `pyrightconfig.json`: no change required (`extraPaths: ["."]`).
- `conftest.py`: no change.
- README/docs: update file references; trim the README per-file inventory to a
  per-package inventory (reduces future drift).
- Stdlib-shadowing check: `db/queue.py`, `db/eval.py` etc. are safe under
  Python 3 absolute imports; `benchmark_subprocess.py` is renamed `subproc.py`
  purely for reader clarity.

## Verification (definition of done)

1. `python -m pytest` passes in full (including deterministic
   `workspace_shell` + Postgres tests) against `rainbox_claude`.
2. `pyright` reports no new errors.
3. `python3 main.py` boots; **Run demo** completes the
   dreamer → critic → verifier pipeline.
4. One chat agent round-trips a reply in a room (proves the `-m agents` spawn,
   socket inheritance, and DB access end to end).
5. `python -m backup.dump` produces an encrypted backup file (proves the CLI
   rename).
6. No `*.py` files remain at root except `main.py` and `conftest.py`.

## Out of scope

- Moving `webapp/`, `tools/`, `providers/`, service dirs — already structured.
- Any behavior change, retry policy, routing work, or agent consolidation
  (e.g. archiving edit_document v1–v5) — separate efforts.
- Introducing `pyproject.toml` / packaging — can layer on later without
  re-moving files.
