# Testing

## Running the suite

From `source/`:

```bash
venv/bin/python -m pytest -q \
  --ignore=whisper_service --ignore=kokoro_service --ignore=telegram_service
```

The `--ignore` flags are required: a bare `pytest` **fails at collection**
because `whisper_service/test_server.py` and `kokoro_service/test_server.py`
share a basename (both directories are standalone services without
`__init__.py`, meant to be tested inside their own venvs — see below). The
full main suite runs in ~1.5 minutes.

Targeted runs need no flags: `venv/bin/python -m pytest db/ memory/ -q`.

## The sandbox database

`conftest.py` at the `source/` root **forces every pytest run onto
`rainbox_claude`** by overwriting `DATABASE_URL` at conftest import — before
any test module or the app itself reads it. Running `pytest` with a
production `DATABASE_URL` exported is therefore safe. Create the sandbox once
with `createdb rainbox_claude`; override with `RAINBOX_TEST_DATABASE_URL`
(e.g. a throwaway CI database).

This guarantee covers pytest only. Ad-hoc scripts and REPLs default to
`rainbox_production` — see `CLAUDE.md` for the rules there.

In sandboxed/containerized runs, localhost Postgres may be blocked at the
network layer; rerun with the normal approval path so the process can reach
`localhost:5432`.

## Known failures and environment sensitivity

- `webapp/test_admin_chatmessage_view.py::test_edit_page_shows_resolved_trace_field`
  — a pre-existing failure on `main`, unrelated to memory/assistant work.
- `agents/test_chat_memory.py::test_user_prompt_omits_irrelevant_memory` and
  `…::test_handle_does_not_post_debug_memory_when_no_memories` — pass when
  the Ollama embedder is unreachable (hybrid retrieval degrades to
  lexical-only, retrieving nothing for the irrelevant query) and **fail when
  Ollama + `embeddinggemma:300m` is live**, because the vector channel
  scores semantically-unrelated facts above the retrieval threshold. Point
  `OLLAMA_BASE_URL` at a dead port to reproduce the passing behavior.

Everything else is green (≈1600 passed, 10 skipped as of this writing). If
you see other failures, suspect your environment first: which local services
are running changes what the retrieval tests observe.

## Service suites

The three standalone services test inside their own directories (their
suites mock the heavy dependencies, but their runtime deps live in their own
venvs):

```bash
cd whisper_service && venv/bin/python -m pytest -q
cd kokoro_service && venv/bin/python -m pytest -q
cd telegram_service && venv/bin/python -m pytest -q
```

## What tests can and cannot catch

- **LLM seams are faked.** Agent tests drive loops through scripted seams
  (`agents/assistant_fakes.py`, faked structured calls); no test needs a
  live model. The `/models` probes and benchmarks are the live-model tools.
- **Marker tests don't execute the frontend.** The page tests for
  `/chat`, `/cron`, `/kanban`, `/git` (`test_*_views.py`) assert that named
  symbols appear in the served HTML — they will **not** catch a broken
  inline script (e.g. the non-raw-string escaping gotcha in
  `chat-frontend-rules.md`) or CSS/layout regressions. Verify UI changes in
  a real browser (see the hard-won process note in
  `ui-left-panel-tree.md` §8).
- **Embedding tests use fakes** (`memory/test_embeddings.py`), so they pass
  without Ollama; only live retrieval *quality* needs the real embedder.

## See also

- `CLAUDE.md` — the production-vs-sandbox database rules for ad-hoc work.
- `memory-trust-hardening-tryout.md` — hands-on verification of the memory
  trust guarantees, including its targeted test list.
- `eval-playbook.md` — the eval loop, which is separate from pytest.
