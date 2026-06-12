# Package Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the 119 flat root Python files into nine sibling packages (`agents/`, `db/`, `chat/`, `memory/`, `evals/`, `benchmarks/`, `llm/`, `backup/`, plus colocated tests) per the approved spec `docs/superpowers/specs/2026-06-12-package-restructure-design.md`, with zero behavior change.

**Architecture:** Behavior-preserving migration. Each task moves one package's modules with `git mv`, rewrites every import of those modules repo-wide in the same commit, and must leave the full test suite green before committing. `db.py` and `llm.py` become package `__init__.py` facades so `import db` / `import llm` call sites don't change. Four subprocess spawn sites switch from file paths to `python -m <module>` with an explicit `PYTHONPATH`.

**Tech Stack:** Python 3.14, Flask/SQLAlchemy, pytest (suite is the safety net — there is no new code to TDD; the existing ~892 tests define "behavior preserved"), pyright, git.

---

## Ground rules (apply to every task)

**Test database safety.** All `pytest` runs use `rainbox_claude` automatically (root `conftest.py`). Any manual `python3 main.py` verification MUST set
`DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude` — never boot the restructured app against production until the migration is verified.

**Commands.** Run everything from the repo's `source/` directory with the venv python:

```bash
cd /Users/neoneye/git/rainbox/source
venv/bin/python -m pytest -q        # full suite; expected: all pass / skip, 0 failures
```

**Import rewrite rules.** When module `old` moves to `pkg/new.py`:

1. `from old import X, Y` → `from pkg.new import X, Y` (usages unchanged — this covers most sites).
2. `import old` → `from pkg import new` and rename attribute usages in that file: `old.f()` → `new.f()` (e.g. `sed -i '' 's/\bold\./new./g' file.py`). If `new` collides with a name already used in the file, alias instead: `from pkg import new as old` and leave usages alone.
3. `import db` and `import llm` stay exactly as they are (facade packages).

To find every importer of a moved module:

```bash
grep -rln --include="*.py" -E "^(import|from) old_name\b" . --exclude-dir=venv --exclude-dir=__pycache__
```

**Every task ends the same way:** run the full suite, then commit. The two closing steps are written out in Task 2 and abbreviated as "**Verify & commit**" afterwards — they are identical each time:

```bash
venv/bin/python -m pytest -q          # expected: same pass/skip counts as the Task 1 baseline, 0 failures
git add -A && git commit -m "<message from the task>"
```

---

### Task 1: Preflight baseline

**Files:** none modified.

- [ ] **Step 1: Record the baseline test results**

```bash
venv/bin/python -m pytest -q 2>&1 | tail -5
```

Record the exact pass/skip counts. Every later task must reproduce these numbers (LLM-dependent tests skip unless LM Studio is up — either state is fine as long as it's *consistent* across tasks).

- [ ] **Step 2: Record the baseline pyright results**

```bash
venv/bin/pyright 2>/dev/null || pyright || npx pyright
```

Record the error/warning count (whichever invocation works — reuse it later).

- [ ] **Step 3: Confirm clean working tree**

```bash
git status --porcelain
```

Expected: empty output. If not, stop and ask the operator.

---

### Task 2: `db/` package

**Files:**
- Move: `db.py` → `db/__init__.py`; `db_models.py` → `db/models.py`; `db_queue.py` → `db/queue.py`; `db_model_config.py` → `db/model_config.py`; `db_chat.py` → `db/chat.py`; `db_conversation.py` → `db/conversation.py`; `db_memory.py` → `db/memory.py`; `db_feedback.py` → `db/feedback.py`; `db_eval.py` → `db/eval.py`; `db_cron.py` → `db/cron.py`; `db_kanban.py` → `db/kanban.py`; `db_settings.py` → `db/settings.py`
- Move tests: `test_db_chat_progress.py` → `db/test_chat_progress.py`; `test_db_chat_streaming.py` → `db/test_chat_streaming.py`; `test_db_eval_baseline.py` → `db/test_eval_baseline.py`; `test_db_eval_case.py` → `db/test_eval_case.py`; `test_db_eval_run.py` → `db/test_eval_run.py`; `test_db_feedback.py` → `db/test_feedback.py`; `test_db_memory.py` → `db/test_memory.py`; `test_db_retrieval_event.py` → `db/test_retrieval_event.py`; `test_db_settings.py` → `db/test_settings.py`; `test_cron_backup.py` → `db/test_cron_backup.py`; `test_cron_events.py` → `db/test_cron_events.py`; `test_cron_firing.py` → `db/test_cron_firing.py`; `test_model_config_provider.py` → `db/test_model_config_provider.py`; `test_sync_model_configs_provider.py` → `db/test_sync_model_configs_provider.py`
- Modify: every file importing `db_*` modules (enumerate with the grep below).

- [ ] **Step 1: Move the modules**

```bash
mkdir db
for pair in "db_models models" "db_queue queue" "db_model_config model_config" \
            "db_chat chat" "db_conversation conversation" "db_memory memory" \
            "db_feedback feedback" "db_eval eval" "db_cron cron" \
            "db_kanban kanban" "db_settings settings"; do
  set -- $pair; git mv "$1.py" "db/$2.py"
done
git mv db.py db/__init__.py
```

- [ ] **Step 2: Move the tests** (explicit `git mv` per the rename list in **Files** above)

- [ ] **Step 3: Rewrite the facade's internal re-exports**

In `db/__init__.py`, lines ~16–25: `from db_models import *` → `from db.models import *`, and likewise for the other nine `from db_* import *` lines and the `from db_chat import _chat_event_payload` line (→ `from db.chat import _chat_event_payload`).

- [ ] **Step 4: Rewrite all other importers**

```bash
grep -rln --include="*.py" -E "^(import|from) db_[a-z_]+\b" . --exclude-dir=venv --exclude-dir=__pycache__
```

Apply the import rewrite rules to every hit. Known shapes: the `db/*.py` files themselves cross-import (`from db_models import …` → `from db.models import …`, `from db_queue import enqueue` → `from db.queue import enqueue`, `from db_chat import post_chat_message, post_cron_event` → `from db.chat import …`, `from db_feedback import get_feedback_event` → `from db.feedback import …`); `test_chat_feedback_api.py` does `import db_feedback` → `from db import feedback as db_feedback`. Plain `import db` sites (85 files) need **no change**.

- [ ] **Step 5: Confirm no references remain**

```bash
grep -rn --include="*.py" -E "^(import|from) db_[a-z_]+\b" . --exclude-dir=venv --exclude-dir=__pycache__
```

Expected: no output.

- [ ] **Step 6: Run the full suite**

```bash
venv/bin/python -m pytest -q
```

Expected: baseline counts, 0 failures.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "refactor: move db_* modules into db/ package (db.py becomes the facade __init__)"
```

---

### Task 3: `chat/` package

**Files:**
- Move: `chat_transcript.py` → `chat/transcript.py`; `chat_streaming.py` → `chat/streaming.py`
- Move tests: `test_chat_transcript.py` → `chat/test_transcript.py`; `test_chat_streaming.py` → `chat/test_streaming.py`
- Create: `chat/__init__.py` (empty)
- Modify: importers of `chat_transcript` (8 files) and `chat_streaming`.

- [ ] **Step 1: Move**

```bash
mkdir chat && touch chat/__init__.py && git add chat/__init__.py
git mv chat_transcript.py chat/transcript.py
git mv chat_streaming.py chat/streaming.py
git mv test_chat_transcript.py chat/test_transcript.py
git mv test_chat_streaming.py chat/test_streaming.py
```

- [ ] **Step 2: Rewrite importers** — `from chat_transcript import format_history` → `from chat.transcript import format_history`, etc. Enumerate:

```bash
grep -rln --include="*.py" -E "^(import|from) chat_(transcript|streaming)\b" . --exclude-dir=venv --exclude-dir=__pycache__
```

Then confirm the same grep returns nothing.

- [ ] **Step 3: Verify & commit** — `git commit -m "refactor: move chat_transcript/chat_streaming into chat/ package"`

---

### Task 4: `memory/` package + `data/` rename

**Files:**
- Move: `memory/question_answer.jsonl` → `data/question_answer.jsonl` (data dir move FIRST — it currently occupies the `memory/` name)
- Move: `memory_ops.py` → `memory/ops.py`; `memory_retrieval.py` → `memory/retrieval.py`
- Move tests: `test_memory_ops.py` → `memory/test_ops.py`; `test_memory_retrieval.py` → `memory/test_retrieval.py`
- Create: `memory/__init__.py` (empty)
- Modify: `query_kb_helpers.py:33` (path constant), `query_handlers.py:84,416,431` (path + strings), `agent_config.py:119` and `db/settings.py:90` (description strings), importers of `memory_ops`/`memory_retrieval`.

- [ ] **Step 1: Move the data dir, then create the package**

```bash
mkdir data && git mv memory/question_answer.jsonl data/question_answer.jsonl
rmdir memory 2>/dev/null; mkdir memory && touch memory/__init__.py && git add memory/__init__.py
git mv memory_ops.py memory/ops.py
git mv memory_retrieval.py memory/retrieval.py
git mv test_memory_ops.py memory/test_ops.py
git mv test_memory_retrieval.py memory/test_retrieval.py
```

- [ ] **Step 2: Update the data path constants**

`query_kb_helpers.py:33`:

```python
QA_JSONL_PATH: Path = Path(__file__).resolve().parent / "data" / "question_answer.jsonl"
```

`query_handlers.py:416`: `kb_path = _REPO_DIR / "data" / "question_answer.jsonl"`. Also update the three human-readable strings that say `memory/question_answer.jsonl` → `data/question_answer.jsonl` (`query_handlers.py:84,431`, `agent_config.py:119`) and the comment at `db/settings.py:90`.

(Both files still live at root in this task; they move to `agents/` in Task 9, whose Step 4 adjusts these paths again for the new depth.)

- [ ] **Step 3: Rewrite importers** of `memory_ops` → `memory.ops` and `memory_retrieval` → `memory.retrieval` (same grep/confirm pattern as Task 3, pattern `memory_(ops|retrieval)`).

- [ ] **Step 4: Check for other data-path references**

```bash
grep -rn "memory/question_answer" --include="*.py" . --exclude-dir=venv --exclude-dir=__pycache__
```

Expected: no output. (Docs are handled in Task 10.)

- [ ] **Step 5: Verify & commit** — `git commit -m "refactor: memory/ code package; Q&A data moves to data/"`

---

### Task 5: `evals/` package

**Files:**
- Move: `eval_runner.py` → `evals/runner.py`; `eval_compare.py` → `evals/compare.py`; `eval_optimizer.py` → `evals/optimizer.py`; `eval_monitor.py` → `evals/monitor.py`
- Move tests: each existing `test_eval_*.py` → `evals/test_*.py` with the `eval_` prefix dropped (e.g. `test_eval_runner.py` → `evals/test_runner.py`); enumerate with `ls test_eval_*.py`
- Create: `evals/__init__.py` (empty)
- Modify: importers of `eval_runner|eval_compare|eval_optimizer|eval_monitor`.

- [ ] **Step 1: Move** (same `mkdir`/`touch`/`git mv` pattern as Task 3)
- [ ] **Step 2: Rewrite importers** (`from eval_runner import …` → `from evals.runner import …`; grep pattern `eval_(runner|compare|optimizer|monitor)`, then confirm zero hits)
- [ ] **Step 3: Verify & commit** — `git commit -m "refactor: move eval_* modules into evals/ package"`

---

### Task 6: `llm/` package + models_test_worker spawn

**Files:**
- Move: `llm.py` → `llm/__init__.py`; `models_test_worker.py` → `llm/models_test_worker.py`
- Modify: `webapp/models_views.py:840-842` (worker spawn).

- [ ] **Step 1: Move**

```bash
mkdir llm
git mv llm.py llm/__init__.py
git mv models_test_worker.py llm/models_test_worker.py
```

`import llm` / `from llm import …` call sites need **no change**.

- [ ] **Step 2: Rewire the worker spawn in `webapp/models_views.py`**

Replace the path constant (lines ~840–842):

```python
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _worker_env() -> dict[str, str]:
    """Env for spawned worker subprocesses: make the source root importable
    regardless of the parent's CWD."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT_DIR + (
        os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )
    return env
```

Find every use of the old constant (`grep -n _TEST_WORKER webapp/models_views.py`) and change the subprocess argv from `[sys.executable, _TEST_WORKER, …]` to `[sys.executable, "-m", "llm.models_test_worker", …]`, passing `env=_worker_env()` to that `subprocess.Popen`/`run` call (keep its other kwargs).

- [ ] **Step 3: Smoke-test the module entry**

```bash
venv/bin/python -c "import llm; import llm.models_test_worker; print('ok')"
```

Expected: `ok` (the worker module must tolerate import without args — it only acts under `if __name__ == \"__main__\"`; if it doesn't have that guard, check how it reads its input and preserve that behavior).

- [ ] **Step 4: Verify & commit** — `git commit -m "refactor: llm/ package; models_test_worker spawned via -m"`

---

### Task 7: `benchmarks/` package + worker spawns

**Files:**
- Move: `benchmark.py` → `benchmarks/basic.py`; `benchmark_runner.py` → `benchmarks/runner.py`; `benchmark_worker.py` → `benchmarks/worker.py`; `benchmark_subprocess.py` → `benchmarks/subproc.py`; `benchmark_kanban.py` → `benchmarks/kanban.py`; `benchmark_editdocument.py` → `benchmarks/editdocument.py`; `benchmark_editdocument_runner.py` → `benchmarks/editdocument_runner.py`; `benchmark_editdocument_worker.py` → `benchmarks/editdocument_worker.py`
- Move tests: `test_benchmark_editdocument.py` → `benchmarks/test_editdocument.py`; `test_benchmark_kanban.py` → `benchmarks/test_kanban.py`
- Create: `benchmarks/__init__.py` (empty)
- Modify: `benchmarks/runner.py:28-31` and `benchmarks/editdocument_runner.py:41-44` (worker spawns), all importers.

- [ ] **Step 1: Move** (same pattern; explicit `git mv` per rename above)

- [ ] **Step 2: Rewrite importers** — grep pattern `benchmark(_[a-z_]+)?\b` over `^(import|from)` lines; map `benchmark` → `benchmarks.basic`, `benchmark_runner` → `benchmarks.runner`, `benchmark_subprocess` → `benchmarks.subproc`, `benchmark_kanban` → `benchmarks.kanban`, `benchmark_editdocument*` → `benchmarks.editdocument*`. Confirm zero hits afterwards.

- [ ] **Step 3: Rewire both worker spawns**

In `benchmarks/runner.py`, replace

```python
_BENCHMARK_WORKER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "benchmark_worker.py"
)
```

with a `_ROOT_DIR`/`_worker_env()` pair exactly as in Task 6 Step 2 (here `_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` since the file now lives one level down), and change the spawn argv from `[sys.executable, _BENCHMARK_WORKER, …]` to `[sys.executable, "-m", "benchmarks.worker", …]` with `env=_worker_env()`. Same treatment in `benchmarks/editdocument_runner.py` for `_EDITDOC_WORKER` → `"-m", "benchmarks.editdocument_worker"`. Find the spawn call sites with `grep -n "_BENCHMARK_WORKER\|_EDITDOC_WORKER" benchmarks/*.py`.

- [ ] **Step 4: Verify & commit** — `git commit -m "refactor: benchmarks/ package; workers spawned via -m"`

---

### Task 8: `backup/` package

**Files:**
- Move: `backup_db.py` → `backup/dump.py`; `backup_remote.py` → `backup/remote.py`
- Move tests: `test_backup_db.py` → `backup/test_dump.py`; `test_backup_remote.py` → `backup/test_remote.py`
- Create: `backup/__init__.py` (empty)
- Modify: `db/cron.py:527` (in-process import), any other importers.

- [ ] **Step 1: Move** (same pattern)

- [ ] **Step 2: Rewrite importers** — `db/cron.py:527` has a function-level `import backup_db`; change to `from backup import dump as backup_db` (usages `backup_db.split_recipients` / `backup_db.backup_database` stay). Grep pattern `backup_(db|remote)` for the rest; confirm zero `.py` hits (docs handled in Task 10).

- [ ] **Step 3: Smoke-test the CLI entry points**

```bash
venv/bin/python -m backup.dump --help; venv/bin/python -m backup.remote --help
```

Expected: each prints usage and exits 0 (proves `-m` wiring; no real backup is made).

- [ ] **Step 4: Verify & commit** — `git commit -m "refactor: backup/ package; CLIs become python -m backup.dump/remote"`

---

### Task 9: `agents/` package (the big one)

**Files:**
- Split: `agent.py` → `agents/base.py` (header comment + imports + `Agent`/`ModelGroupAgent`/`StructuredLLMAgent` and helpers) and `agents/__main__.py` (the `main()` function + `if __name__ == "__main__": main()`)
- Move: `agent_config.py` → `agents/config.py`; `agent_followup.py` → `agents/followup.py`; `agent_chat_structured.py` → `agents/chat_structured.py`; `agent_chat_unstructured.py` → `agents/chat_unstructured.py`; `agent_conversation.py` → `agents/conversation.py`; `agent_kanban_worker.py` → `agents/kanban_worker.py`; `agent_mcp.py` → `agents/mcp.py`; `agent_tool_demo.py` → `agents/tool_demo.py`; `agent_edit_document_v1.py`…`_v6.py` → `agents/edit_document_v1.py`…`_v6.py`; `router_agent.py` → `agents/router.py`; `query_agent.py` → `agents/query.py`; `query_router_agent.py` → `agents/query_router.py`; `query_filter_router_agent.py` → `agents/query_filter_router.py`; `query_handlers.py` → `agents/query_handlers.py`; `query_kb_helpers.py` → `agents/query_kb_helpers.py`; `persona.py` → `agents/persona.py`; `mcp_config.py` → `agents/mcp_config.py`; `patch_apply.py` → `agents/patch_apply.py`
- Move tests (drop the `agent_` prefix; keep `query_*` stems): `test_agent_chat_memory.py` → `agents/test_chat_memory.py`; `test_agent_chat_unstructured.py` → `agents/test_chat_unstructured.py`; `test_agent_conversation.py` → `agents/test_conversation.py`; `test_agent_edit_document_v1.py`…`_v6.py` → `agents/test_edit_document_v1.py`…`_v6.py`; `test_agent_followup.py` → `agents/test_followup.py`; `test_agent_heartbeat.py` → `agents/test_heartbeat.py`; `test_agent_kanban_worker.py` → `agents/test_kanban_worker.py`; `test_agent_mcp.py` → `agents/test_mcp.py`; `test_patch_apply.py` → `agents/test_patch_apply.py`; `test_query_agent_memory_ops.py` → `agents/test_query_memory_ops.py`; `test_query_filter_router_memory_ops.py` → `agents/test_query_filter_router_memory_ops.py`; `test_query_filter_router_telemetry.py` → `agents/test_query_filter_router_telemetry.py`; `test_query_kb_overlay.py` → `agents/test_query_kb_overlay.py`
- Create: `agents/__init__.py` (empty)
- Modify: `main.py:32,49-54` (spawn), `agents/query_handlers.py:31` and `agents/query_kb_helpers.py:33` (`__file__` depth), all importers of the moved names.

- [ ] **Step 1: Move everything except `agent.py`** (explicit `git mv` per the rename map; create `agents/__init__.py`).

- [ ] **Step 2: Split `agent.py`**

```bash
git mv agent.py agents/base.py
```

Then create `agents/__main__.py` by **cutting** `main()` and the `if __name__ == "__main__":` block out of `agents/base.py` and pasting them into the new file with this header (the body of `main()` is exactly today's code — only the registry import block changes to the new module names):

```python
"""Agent child-process entrypoint: spawned by the supervisor as
`python -m agents --socket-fd N`."""
import argparse
import json
import logging
import socket
import sys
from typing import Any
from uuid import UUID

import db
from agents.base import Agent, ModelGroupAgent

logger = logging.getLogger(__name__)


def main() -> None:
    ...  # today's main() body, verbatim, EXCEPT the registry imports become:
    # from agents.chat_structured import StructuredChatAgent
    # from agents.chat_unstructured import UnstructuredChatAgent
    # from agents.edit_document_v1 import EditDocumentAgentV1
    # ... (v2..v6 likewise)
    # from agents.conversation import ConversationManagerAgent
    # from agents.followup import FollowUpClassifierAgent
    # from agents.kanban_worker import KanbanWorkerAgent
    # from agents.mcp import MCPAgent
    # from agents.tool_demo import ToolDemoAgent
    # from agents.query import QueryAgent
    # from agents.query_filter_router import QueryFilterRouterAgent
    # from agents.query_router import QueryRouterAgent
    # from agents.router import RouterAgent
    # from tools.workspace_shell_chat import WorkspaceShellChatAgent


if __name__ == "__main__":
    main()
```

Trim imports in both files to what each actually uses (pyright will flag leftovers). The KNOWN ISSUES header comment stays at the top of `agents/base.py`; its point 1 (config-read remainder) describes `main()` — move that paragraph into a comment atop `agents/__main__.py` instead.

- [ ] **Step 3: Rewire the supervisor spawn in `main.py`**

Replace line 32:

```python
ROOT_DIR: str = os.path.dirname(os.path.abspath(__file__))
```

and in `spawn()` (lines ~49–54):

```python
    argv = [
        sys.executable, "-m", "agents",
        "--socket-fd", str(agent_sock.fileno()),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT_DIR + (
        os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )
    pid = os.posix_spawn(sys.executable, argv, env)
```

(`AGENT_SCRIPT` is deleted; grep for any other use of it first.)

- [ ] **Step 4: Fix the two `__file__`-relative paths for the new depth**

`agents/query_kb_helpers.py:33`:

```python
QA_JSONL_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "question_answer.jsonl"
```

`agents/query_handlers.py:31`:

```python
_REPO_DIR = Path(__file__).resolve().parent.parent
```

- [ ] **Step 5: Rewrite all importers**

```bash
grep -rln --include="*.py" -E "^(import|from) (agent|agent_[a-z0-9_]+|router_agent|query_agent|query_router_agent|query_filter_router_agent|query_handlers|query_kb_helpers|persona|mcp_config|patch_apply)\b" . --exclude-dir=venv --exclude-dir=__pycache__
```

Mapping: `agent` → `agents.base` (e.g. `from agent import Agent, ModelGroupAgent` in role modules and `tools/workspace_shell_chat.py` → `from agents.base import …`); `agent_config` → `agents.config` (20 importers — most are `from agent_config import SOME_UUID`); `agent_<role>` → `agents.<role>`; `router_agent` → `agents.router`; `query_agent` → `agents.query`; `query_*` → `agents.query_*`; `persona` → `agents.persona`; `mcp_config` → `agents.mcp_config`; `patch_apply` → `agents.patch_apply`. Confirm the grep then returns zero hits.

- [ ] **Step 6: Smoke-test the spawn path manually**

```bash
PYTHONPATH=. venv/bin/python -m agents --help 2>&1 | head -3
```

Expected: argparse usage mentioning `--socket-fd` (proves the module entry resolves and imports cleanly).

- [ ] **Step 7: Run the full suite, boot the app against the claude DB**

```bash
venv/bin/python -m pytest -q
DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude venv/bin/python main.py
```

Expected: suite at baseline; the app boots, webserver on :5000. In a browser (or `curl -s localhost:5000/ | head`), confirm the index renders, then trigger **Run demo** and confirm in the log that dreamer/critic/verifier spawn and complete (this proves `-m agents` + socket inheritance + PYTHONPATH env end-to-end). Ctrl-C stops cleanly.

- [ ] **Step 8: Commit** — `git commit -m "refactor: agents/ package; agent.py split into base + __main__; spawn via -m agents"`

---

### Task 10: Remaining tests + docs/README pass

**Files:**
- Move tests: `test_chat_feedback_api.py`, `test_cron_admin.py`, `test_cron_api.py`, `test_cron_views.py`, `test_kanban_admin.py`, `test_kanban_api.py`, `test_kanban_views.py`, `test_models_views.py`, `test_settings_views.py`, `test_stt_whisper_views.py`, `test_tts_kokoro_views.py`, `test_voice_echo_views.py`, `test_sync_models_from_providers.py` → `webapp/` (names unchanged); `test_providers.py`, `test_lm_studio.py`, `test_ollama_provider.py`, `test_jan_provider.py` → `providers/` (names unchanged)
- Modify: `README.md`, `docs/*.md` references.

- [ ] **Step 1: Move the webapp and providers tests** (plain `git mv`, no renames). Then confirm the root is clean:

```bash
ls test_*.py 2>/dev/null; ls *.py
```

Expected: no `test_*.py` matches; only `main.py` and `conftest.py` remain.

- [ ] **Step 2: Update doc references**

```bash
grep -rln "agent_config\|backup_db\|backup_remote\|memory/question_answer\|chat_transcript\|memory_ops\|memory_retrieval\|eval_runner\|benchmark_runner\|query_agent\|agent\.py\|db_models\|patch_apply" README.md docs/*.md docs/proposals/*.md
```

Update each hit to the new path/name. CLI examples in `docs/backup.md` and `docs/operator-guide.md` become `python -m backup.dump` / `python -m backup.remote`. Leave `docs/proposals/*` mostly alone (they're dated historical documents) except where they give commands an operator might still copy-paste.

- [ ] **Step 3: Trim the README inventory**

In `README.md`, replace the per-file "## Files" bullet list with a per-package table:

```markdown
## Layout

| Package | What lives there |
|---|---|
| `main.py` | entrypoint: webserver + supervisor + signal handling |
| `agents/` | the agent child process (`python -m agents`), base class hierarchy, and every role implementation |
| `db/` | Flask-SQLAlchemy models and all Postgres helpers (`import db` is the facade) |
| `webapp/` | Flask app package, split by feature |
| `chat/` | transcript formatting and streaming-reply helpers shared by chat agents |
| `memory/` | explicit memory commands and deterministic retrieval |
| `evals/` | eval runner, baseline comparison, optimizer, production monitor |
| `benchmarks/` | benchmark definitions, runners, and subprocess workers |
| `llm/` | LM Studio / LlamaIndex connectivity and the /models test worker |
| `backup/` | encrypted DB backup (`python -m backup.dump`) and remote upload |
| `providers/` | LM Studio / Ollama / Jan provider registry |
| `tools/` | the no-LLM workspace_shell command runner |
| `data/` | the base Q&A knowledge file (`question_answer.jsonl`) |
```

Update the "How it's structured" prose where it names old filenames (e.g. `agent_config.py` → `agents/config.py`). Tests-location sentence: tests are colocated inside each package.

- [ ] **Step 4: Verify & commit** — `git commit -m "refactor: colocate remaining tests; update docs and README for package layout"`

---

### Task 11: Final verification

**Files:** none modified (fixes only if something fails).

- [ ] **Step 1: Full suite + pyright against baseline**

```bash
venv/bin/python -m pytest -q
venv/bin/pyright 2>/dev/null || pyright || npx pyright
```

Expected: pytest matches the Task 1 baseline exactly; pyright error count ≤ baseline.

- [ ] **Step 2: End-to-end app check (claude DB)**

```bash
DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude venv/bin/python main.py
```

With LM Studio running: open `/chat`, post a message in a room a responder agent belongs to, confirm a reply arrives (full spawn → LLM → SSE path). Without LM Studio: the Run-demo pipeline from Task 9 Step 7 plus a clean Ctrl-C shutdown suffices.

- [ ] **Step 3: Backup CLI check** — `venv/bin/python -m backup.dump --help` exits 0.

- [ ] **Step 4: Operator handoff notes** — tell the operator: (a) shell habits change: `python backup_db.py …` → `python -m backup.dump …`; (b) production boot is unchanged (`python3 main.py`); (c) if anything outside the repo (launchd, scripts, notebooks) imports old module names or runs old script paths, it needs the new names.

---

## Self-review notes (already applied)

- Spec coverage: layout (Tasks 2–9), spawn mechanics (6, 7, 9), data rename (4), test colocation (each task + 10), docs/README (10), verification DoD (9, 11). The spec's "no `*.py` at root except main/conftest" check is Task 10 Step 1.
- The `memory/` data dir must move before the package is created — ordering encoded in Task 4 Step 1.
- `query_handlers._REPO_DIR` and `QA_JSONL_PATH` are touched twice (Task 4 for the dir rename, Task 9 for the `__file__` depth change) — both steps cross-reference each other.
