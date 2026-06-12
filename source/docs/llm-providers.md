# LLM Providers

## Purpose

`rainbox` talks to local LLM servers through a small **provider
registry**. Three providers ship today: **LM Studio**, **Jan**, and
**Ollama**. All three can be active at the same time. Adding OpenRouter
or any future backend is a matter of dropping in one module.

This doc covers:

- What a provider is and where it lives in the code.
- How sync reconciles `model_config` rows with each provider.
- How a model name is resolved at call time.
- How to add a new provider.

## What a provider is

A provider is a Python class that satisfies the `Provider` Protocol in
`providers/base.py`:

```python
class Provider(Protocol):
    id: ProviderId            # "lm_studio" | "jan" | "ollama"
    display_name: str         # "LM Studio", "Jan", "Ollama" — for badges/logs

    def base_url(self) -> str: ...
    def list_models(self) -> list[str]: ...
    def fetch_native_models(self) -> list[dict] | None: ...
    def fetch_model_sizes(self) -> dict[str, int]: ...
    def default_arguments(self) -> dict[str, Any]: ...
    def ensure_loaded(self, model: str, context_window: int) -> None: ...
```

Each method either talks to the backend's HTTP API or does provider-local
work (CLI shellouts, env-var reads). The webapp never imports a backend
module directly — it goes through `providers.get(id)` or
`providers.all_providers()`.

## File layout

```
providers/
  __init__.py         re-exports get(), all_providers(), Provider
  base.py             Protocol + ProviderId literal
  registry.py         _PROVIDERS dict, get(id), all_providers()
  lm_studio.py        LM Studio (REST + `lms` CLI)
  jan.py              Jan (REST only, ensure_loaded is a no-op)
  ollama.py           Ollama (REST only, ensure_loaded is a no-op)
```

Per-provider quirks live inside the provider module. Examples:

- LM Studio's `ensure_loaded` shells out to `lms load --context-length N`
  because its OpenAI-compat endpoint won't let you change context size
  per request.
- LM Studio's `fetch_model_sizes` calls `lms ls --json`.
- Jan's `ensure_loaded` is a no-op — Jan auto-loads on first request
  using whatever context length is set in Jan's UI.
- Jan's `fetch_model_sizes` returns `{}` (no equivalent CLI).
- Jan's `/v1/models` is plain OpenAI shape, so `fetch_native_models`
  returns entries without capability metadata. Capability detection
  (`is_function_calling_model`) falls back to the default (`False`) on
  new Jan rows; the user flips it manually in an override if needed.
- Ollama's `ensure_loaded` is a no-op — it auto-loads on first request
  and its OpenAI shim doesn't accept `options.num_ctx`, so context size
  is configured per-model in Ollama itself (`OLLAMA_NUM_CTX`, Modelfile).
- Ollama's `fetch_native_models` reads `/api/tags` (richer than the
  OpenAI shim's `/v1/models`) and renames `name` → `id` so the sync
  layer and /models detail panel work without provider-specific shims.
  `/api/tags` includes `size` in bytes, so `fetch_model_sizes` is fully
  populated. Capability info isn't present in `/api/tags`, so
  `is_function_calling_model` defaults to `False` on new rows.

### Environment variables

- `LM_STUDIO_BASE_URL` — defaults to `http://127.0.0.1:1234`.
- `JAN_BASE_URL` — defaults to `http://127.0.0.1:1337`.
- `OLLAMA_BASE_URL` — defaults to `http://127.0.0.1:11434`.
- `LMS` — explicit path to LM Studio's `lms` CLI. Otherwise PATH, then
  `~/.cache/lm-studio/bin/lms`.

## DB schema

`model_config` has a `provider` column (`Text NOT NULL DEFAULT 'lm_studio'`).
The unique constraint is `(provider, model_name)` — the same model name
can legitimately exist under multiple providers (e.g. `llama3.2:latest`
under both LM Studio and Ollama) without collision.

`model_config_override` does NOT have a `provider` column. An override
inherits its parent config's provider through the
`model_config_uuid` FK; resolving an override always reads the parent's
provider field.

The migration is idempotent (`init_db()` runs the `ADD COLUMN IF NOT
EXISTS` and `CREATE UNIQUE INDEX IF NOT EXISTS` statements every
startup).

## Sync: how `model_config` rows track provider state

Two things keep `model_config` in step with what each backend exposes:

1. **Startup auto-sync.** `webapp/core.py` calls
   `sync_models_from_providers()` once per process start.
2. **Manual reload.** The Reload button on `/models` (POST
   `/models/api/reload`) and the `--force-model-sync` CLI flag both call
   the same function.

`sync_models_from_providers()` iterates every registered provider and
runs `_sync_one_provider` against each. **Providers are independent**:
one being unreachable does not flip the other's rows.

For each provider the helper:

1. Calls `prov.list_models()`. If this raises (network error, server
   down), logs a warning and returns `None` for that provider — no rows
   of that provider are touched.
2. Calls `prov.fetch_model_sizes()` and `prov.fetch_native_models()`.
3. Derives `function_calling_by_name` from the native entries'
   `capabilities` array (LM Studio surfaces `["tool_use"]`; Jan doesn't,
   so the map ends up empty or `None`).
4. Calls `db.sync_model_configs(provider=prov.id, …)`.

`sync_model_configs` is **scoped by provider** — it only inspects and
mutates rows where `ModelConfig.provider == provider`:

- For each name in `available_model_names` not yet present: insert a
  fresh row with the provider's `default_arguments()`. If the name
  appears in `function_calling_by_name`, the
  `is_function_calling_model` flag is set on creation.
- For each existing row of this provider: ensure `available=True`,
  refresh `size_bytes`, and (only if `force_update_arguments=True`)
  refresh `is_function_calling_model` if it changed.
- For each existing row of this provider whose name is NOT in the
  available set: flip `available=False`. **Never deletes.**

Return shape (per provider): `{"created", "re_enabled", "disabled",
"function_calling_updated"}` — or `None` if the provider was unreachable.

### What sync NEVER touches

- An existing row's `model_name` — the row is identified by uuid; the
  name is part of the identity.
- An existing row's `arguments` blob — that's a permanent record of
  what was tried for that uuid. The single exception is when
  `force_update_arguments=True` refreshes
  `is_function_calling_model` to match what the provider currently
  reports.
- Rows from any other provider.

### Force-sync (operator override)

`python main.py --force-model-sync` runs the same sync but with
`force_update_arguments=True`, then exits without starting the server.
Use this after you've changed which models LM Studio reports tool support
for and you want existing rows refreshed.

## Resolving a model at call time

When an agent (or a `/models` test button) needs to actually call an LLM,
the resolution path is:

1. Caller has a uuid that points at either a `ModelConfig` or a
   `ModelConfigOverride`.
2. `db.resolved_model_kwargs(uuid)` returns
   `(provider_id, model_name, args)`.
   - For a `ModelConfig`: the row's own `provider`, `model_name`, and
     `arguments`.
   - For a `ModelConfigOverride`: the parent config's `provider` and
     `model_name`, with `arguments` being the parent's args
     shallow-merged with the override's `overrides` (override wins).
3. `llm.prepare_llm(provider_id, model_name, args)`:
   - Looks up the provider: `providers.get(provider_id)`.
   - Calls `provider.ensure_loaded(model_name, args["context_window"])`
     — no-op on Jan and Ollama, may trigger an `lms load` on LM Studio.
   - For **Ollama**, builds the native `llama-index-llms-ollama` `Ollama`
     wrapper (talks to `/api/chat`, so chain-of-thought surfaces as a
     `ThinkingBlock`). `thinking` is **off by default** and opt-in via a
     `thinking` arg, because thinking and structured output don't mix on
     Ollama (with thinking on, the answer goes to the thinking channel and
     `content` comes back empty). For **every other provider**, builds a
     `ThinkingAwareOpenAILike(model=model_name, **args)` over the
     OpenAI-compat endpoint (which also recovers JSON from LM Studio's
     Qwen-style `reasoning_content`).
   - Returns it.

The `api_base` and `api_key` in `args` (written by the provider's
`default_arguments()` when the row was first synced) tell llama-index
which HTTP endpoint to hit — so once the LLM object is built, the
inference call goes directly to the right backend.

### Call sites

`prepare_llm` is the **single LLM constructor** — everything that needs an
LLM routes through it, so provider selection (Ollama native wrapper vs
`ThinkingAwareOpenAILike`) and `ensure_loaded` happen in exactly one place:

- `agent.py` — every `StructuredLLMAgent` subclass via `_structured_call`
  (covers the edit-document agents and so `benchmark_editdocument`).
- `benchmark.py`, `query_filter_router_agent.py`, `agent_tool_demo.py`,
  `agent_mcp.py`.
- The `/models` page probes (`test_chat`, `test_structured_output`,
  `stream_test_streaming`, `test_tool_call`) via `_resolve_test_target`.

> Historically several of these hand-built `ThinkingAwareOpenAILike`
> directly and discarded `provider_id`. That bypass is what made Ollama
> reach the OpenAI-compat facade instead of its native class — and caused a
> structured-output hang on thinking-capable Ollama models. Routing
> everything through `prepare_llm` fixed it; don't reintroduce direct
> construction.

## /models page

The page renders one combined tree with a per-row provider badge:

- Header shows `[LM Studio] http://127.0.0.1:1234 · [Jan]
  http://127.0.0.1:1337` (each clickable). The list is generated from
  `providers.all_providers()` so any registered provider appears
  automatically.
- Each model row has a small badge (`pp-provider-badge`) carrying the
  provider's display name. All providers share the same badge styling.
- The Reload button calls `POST /models/api/reload`, which runs
  `sync_models_from_providers()`. The response is
  `{"ok": true, "summary": {"lm_studio": {…} | null, "jan": {…} | null}}`.
  Unreachable providers come back as `null` in the summary and the page
  reloads to show the latest state.
- The model-info side panel uses the row's provider when rendering its
  heading ("Model info ({{ display_name }})") and the unreachable /
  not-found hints.

## /models test probes

Each model row, and the New-override form, exposes buttons that call the
model for real so you can validate a config before saving it. The
New-override save gate requires the relevant probe to pass first.

- **Test chat** — system "answer with 'pong'", user "ping"; passes if the
  reply contains "pong".
- **Test streaming** — a chain-of-thought prompt over `stream_chat`,
  reporting TTFT, chunk counts, and content vs reasoning lengths.
- **Test structured output** — a ping/pong structured-output call (forces
  `should_use_structured_outputs=true` for the probe itself).
- **Test function calling** — builds a `FunctionAgent` and checks the model
  invokes a `send_number` tool with the expected argument.

The New-override form shows one reasoning control, picked by provider: a
`thinking` checkbox for **Ollama** (off by default; drives the native
wrapper's `thinking` flag), or a `reasoning_effort` dropdown for **every
other provider** (written to `additional_kwargs.extra_body.reasoning.effort`).
They are mutually exclusive — Ollama ignores `reasoning_effort`, so showing
both would imply a knob that does nothing.

### Stop / cancellation

A blocking LLM call can't be cancelled in-process — a runaway model just
hangs the request thread until the provider's read timeout (~60s). So the
chat / structured / tool probes each run in a **throwaway subprocess** the
web layer can SIGKILL, mirroring how the supervisor (`main.py`) kills hung
agents:

- `models_test_worker.py` reads `{action, provider_id, model, arguments}`
  on stdin, runs `llm.run_named_test(...)`, and writes exactly one JSON
  result line to stdout. The test's own output and library chatter are
  redirected to stderr (which the parent discards) so they can't corrupt
  the result line the parent parses.
- `POST /models/api/test` is a streaming NDJSON endpoint. It spawns the
  worker, emits `{"running": true, "elapsed": s}` heartbeats while it runs,
  then yields the worker's result tagged `"done": true`.
- The **Stop** button aborts the client `fetch`. The disconnect surfaces in
  the Flask response generator as `GeneratorExit` at the next heartbeat
  yield, whose `finally` SIGKILLs the worker. Killing the process closes its
  HTTP socket to the provider, so the provider (e.g. Ollama) stops
  generating — a real cancel, not just a UI dismissal.

`Test streaming` cancels for free without a subprocess: it streams, so when
the client stops reading the upstream HTTP stream is GC-closed. That path is
`POST /models/api/test_streaming_live` (`stream_test_streaming`), and its
probe opts `thinking` back on so Ollama reasoning is visible.

Trade-off: spawning a fresh Python and importing llama-index adds ~2–4s of
startup per probe (shown as the live elapsed counter) — the price of making
the call killable.

## Adding a new provider

1. Create `providers/<id>.py` that defines a class implementing the
   `Provider` Protocol and exports an instance as `PROVIDER`.
2. Add the entry to `_PROVIDERS` in `providers/registry.py`.
3. Extend the `ProviderId` literal in `providers/base.py`.
4. Add the `<id>` branch to the badge `{% if cfg.provider == … %}` chain
   in `MODELS_TEMPLATE` (otherwise the badge text falls back to the raw
   id — still legible, just not a friendly display name).

That's it — startup sync, the Reload button, the /models page, and all
probe paths pick the new provider up automatically.

## Known limitations

- **Embeddings stay LM Studio-only.** `query_kb_helpers.py` hardcodes
  `http://127.0.0.1:1234/v1`; switching the embedding provider would
  invalidate stored vectors, so it's out of scope for the multi-provider
  abstraction.
- **`size_bytes` is `NULL` for Jan rows.** Jan exposes no equivalent of
  `lms ls`. The column is observational, so this is harmless.
- **Jan capability detection is coarser than LM Studio's.** Jan's
  `/v1/models` doesn't expose a `capabilities` array, so new Jan rows
  start with `is_function_calling_model=False`. Flip it via an override
  if a Jan model supports tools.
