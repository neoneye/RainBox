# Testing

## Running the suite

From `source/`:

```bash
venv/bin/python -m pytest -q \
  --ignore=voice_stt_whisper --ignore=voice_tts_kokoro --ignore=telegram_service
```

The `--ignore` flags are required: a bare `pytest` **fails at collection**
because `voice_stt_whisper/test_server.py` and `voice_tts_kokoro/test_server.py`
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

## Environment sensitivity

There are no known failures: the suite is green (2384 passed, 10 skipped as
of this writing) both with the Ollama embedder live and with it unreachable.

**A test that creates a `memory_claim` must delete it.** Retrieval tests
assert on what recall returns, and recall returns the top-K of whatever is
in the database. A test that leaks one claim per run poisons every later
run: the lexical channel ignores the stale rows, so the suite stays green
until `embeddinggemma:300m` is reachable, and then the vector channel — which
ranks by similarity with no relevance floor — surfaces the accumulated noise
ahead of the claim the failing test just created. The symptom looks like a
retrieval-quality bug in a completely unrelated test, and pointing
`OLLAMA_BASE_URL` at a dead port "fixes" it, which is what makes the real
cause easy to miss. Use the module's `tag`/`fresh_subject` fixture as the
claim's `subject` and delete by that subject in a `finally`.

`SELECT count(*) FROM memory_claim;` against `rainbox_claude` should be 0
after a full run. Anything else is a leak.

Retrieval telemetry (`retrieval_event`) is not cleaned up and grows by
roughly 15k rows per full run. It is inert — nothing ranks on it — but the
table is worth truncating occasionally.

If you see a failure not listed here, suspect your environment first: which
local services are running changes what the retrieval tests observe.

## Service suites

The three standalone services test inside their own directories (their
suites mock the heavy dependencies, but their runtime deps live in their own
venvs):

```bash
cd voice_stt_whisper && venv/bin/python -m pytest -q
cd voice_tts_kokoro && venv/bin/python -m pytest -q
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
  `ui-left-panel-tree.md` §8). `python -m tools.serve_ui` serves the pages
  alone for that — port 5055 against `rainbox_claude`, so it neither
  collides with the running instance on 5000 nor touches real data.
- **Embedding tests use fakes** (`memory/test_embeddings.py`), so they pass
  without Ollama; only live retrieval *quality* needs the real embedder.

## See also

- `CLAUDE.md` — the production-vs-sandbox database rules for ad-hoc work.
- `memory-trust-hardening-tryout.md` — hands-on verification of the memory
  trust guarantees, including its targeted test list.
- `eval-playbook.md` — the eval loop, which is separate from pytest.
