# LLM Providers

## Purpose

`rainbox` talks to LLM servers through a small **provider registry**.
**Ollama is the preferred default**; Jan and LM Studio are supported local
alternatives, and OpenRouter reaches several hundred cloud models. All four can
be active at the same time. Adding a future backend is a matter of dropping in
one module.

This doc covers:

- What a provider is and where it lives in the code.
- How sync reconciles `model_config` rows with each provider.
- How a model name is resolved at call time.
- How to add a new provider.

## Preference policy

`providers/base.py` defines one Ollama-first provider order. That order controls:

- the default provider for newly created `model_config` rows;
- provider registration, startup sync, headers, and provider-grouped model lists;
- the automatic direct-chat model choice while `chat.default_model` is unset.

The preference never rewrites an existing model config, overrides an explicit
room/default-model choice, or changes a model group's operator-defined fallback
priority. Selecting Jan or LM Studio remains fully supported.

## What a provider is

A provider is a Python class that satisfies the `Provider` Protocol in
`providers/base.py`:

```python
class Provider(Protocol):
    id: ProviderId            # "ollama" | "jan" | "lm_studio" | "openrouter"
    display_name: str         # "Ollama", "Jan", … — badges/logs
    curated: bool             # True => sync never creates rows (see below)

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
  ollama.py           Ollama (REST only, ensure_loaded is a no-op)
  jan.py              Jan (REST only, ensure_loaded is a no-op)
  lm_studio.py        LM Studio (REST + `lms` CLI)
  openrouter.py       OpenRouter (cloud, curated — rows are added by hand)
```

Per-provider quirks live inside the provider module. Examples:

- Ollama's `ensure_loaded` is a no-op — it auto-loads on first request
  and its OpenAI shim doesn't accept `options.num_ctx`, so context size
  is configured per-model in Ollama itself (`OLLAMA_NUM_CTX`, Modelfile).
- Ollama's `fetch_native_models` reads `/api/tags` (richer than the
  OpenAI shim's `/v1/models`) and renames `name` → `id` so the sync
  layer and /model detail panel work without provider-specific shims.
  `/api/tags` includes `size` in bytes, so `fetch_model_sizes` is fully
  populated. Capability info isn't present in `/api/tags`, so
  `is_function_calling_model` defaults to `False` on new rows.
- Jan's `ensure_loaded` is a no-op — Jan auto-loads on first request
  using whatever context length is set in Jan's UI.
- Jan's `fetch_model_sizes` returns `{}` (no equivalent CLI).
- Jan's `/v1/models` is plain OpenAI shape, so `fetch_native_models`
  returns entries without capability metadata. Capability detection
  (`is_function_calling_model`) falls back to the default (`False`) on
  new Jan rows; the user flips it manually in an override if needed.
- LM Studio's `ensure_loaded` shells out to `lms load --context-length N`
  because its OpenAI-compat endpoint won't let you change context size
  per request.
- LM Studio's `fetch_model_sizes` calls `lms ls --json`.
- OpenRouter is the only **curated** provider (`curated = True`): its catalog
  runs to several hundred cloud models, so rows are created one at a time
  through the Add-model overlay on `/model` and sync never creates. See
  [Curated providers](#curated-providers) below.
- OpenRouter's `ensure_loaded` is a no-op — each request is routed to an
  upstream that already has the model hot — and `fetch_model_sizes` returns
  `{}`, since a cloud model has no size on disk.
- OpenRouter's catalog (`/api/v1/models`) is public, but a key-less OpenRouter
  can't answer an inference call, so a missing `OPENROUTER_API_KEY` is reported
  the way an unreachable local server is: `list_models()` raises,
  `fetch_native_models()` returns `None`, sync logs a skip and touches nothing.
  It also means no network traffic at startup on a machine without a key.
- OpenRouter's `fetch_native_models` folds each entry's `supported_parameters`
  into the `capabilities: ["tool_use"]` shape the sync layer already reads, so
  `is_function_calling_model` lands correctly with no OpenRouter-specific
  branch in `_sync_one_provider` — the same trick Ollama uses to rename `name`
  → `id`. The catalog is memoized for 60 seconds per process; the `/model`
  Reload button drops that cache first so a reload really re-asks.

### Environment variables

- `OLLAMA_BASE_URL` — defaults to `http://127.0.0.1:11434`.
- `JAN_BASE_URL` — defaults to `http://127.0.0.1:1337`.
- `LM_STUDIO_BASE_URL` — defaults to `http://127.0.0.1:1234`.
- `LMS` — explicit path to LM Studio's `lms` CLI. Otherwise PATH, then
  `~/.cache/lm-studio/bin/lms`.
- `OPENROUTER_API_KEY` — required to use any OpenRouter model. No default.

### The repo-root `.env`

`OPENROUTER_API_KEY` is a secret, so it lives in a gitignored `.env` at the
repo root (next to `source/`), documented by the committed `.env.example`.
`env_file.load_env_file()` loads it at import of `providers/__init__.py` — the
one choke point every process that builds an LLM passes through, including the
killable `/model` test-worker subprocess. Variables already set in the
environment win, so `OPENROUTER_API_KEY=… python main.py` still overrides the
file.

The key is deliberately **absent** from `openrouter.default_arguments()`.
`ModelConfig.arguments` is JSONB that `/model` renders verbatim in its
`arguments` block, so a key stored there would be both persisted to the
database and printed on screen. `llm.prepare_llm` injects it from the
environment at construction time instead.

## DB schema

`model_config` has a `provider` column (`Text NOT NULL DEFAULT 'ollama'`).
The unique constraint is `(provider, model_name)` — the same model name can
legitimately exist under multiple providers (for example, under Ollama and
LM Studio) without collision.

`model_config_override` does NOT have a `provider` column. An override
inherits its parent config's provider through the
`model_config_uuid` FK; resolving an override always reads the parent's
provider field.

The migration is idempotent. `init_db()` adds missing columns/indexes and sets
the provider column's default to `ollama` for future rows. It never rewrites
the provider on existing rows.

## Sync: how `model_config` rows track provider state

Two things keep `model_config` in step with what each backend exposes:

1. **Startup auto-sync.** `webapp/core.py` calls
   `sync_models_from_providers()` once per process start.
2. **Manual reload.** The Reload button on `/model` (POST
   `/model/api/reload`) and the `--force-model-sync` CLI flag both call
   the same function.

`sync_models_from_providers()` iterates every registered provider in preferred
order (Ollama, Jan, LM Studio, OpenRouter) and runs `_sync_one_provider`
against each. **Providers are independent**: one being unreachable does not
flip another's rows.

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

### Curated providers

`_sync_one_provider` passes `create_missing=not prov.curated`. For a **curated**
provider — OpenRouter, today the only one — the creation half is dropped: a
catalog entry with no row is skipped rather than turned into one. Everything
else runs unchanged, so a hand-added OpenRouter row whose model is retired
upstream still flips to `available=False`, and one that reappears is
re-enabled.

Why: mirroring OpenRouter's several hundred models into rows would bury the
`/model` tree and churn on every reload. The operator picks instead, through
the Add-model overlay described below.

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
Use this after a provider's reported tool support changes and you want
existing rows refreshed.

## Resolving a model at call time

When an agent (or a `/model` test button) needs to actually call an LLM,
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
     `content` comes back empty). For **OpenRouter**, builds
     `ThinkingAwareOpenRouter` — the `llama-index-llms-openrouter` wrapper —
     injecting the API key from the environment and an `X-Title: rainbox`
     app-info header (merged, so an override's
     `additional_kwargs.extra_body` survives). For **every other provider**,
     builds a `ThinkingAwareOpenAILike(model=model_name, **args)` over the
     OpenAI-compat endpoint.
   - Returns it.

Both `ThinkingAwareOpenAILike` and `ThinkingAwareOpenRouter` share
`_recover_content_from_reasoning`: when an endpoint returns empty `content`
with the answer in `reasoning_content`, it pulls the final JSON out of there so
structured-output parsing doesn't crash on an empty string. OpenRouter needs it
for the same reason LM Studio's Qwen-style models do, and more so — the
upstream a request lands on can change between calls.

The `api_base` and `api_key` in `args` (written by the provider's
`default_arguments()` when the row was first synced) tell llama-index
which HTTP endpoint to hit — so once the LLM object is built, the
inference call goes directly to the right backend.

### Call sites

`prepare_llm` is the **single LLM constructor** — everything that needs an
LLM routes through it, so provider selection (Ollama native wrapper vs
`ThinkingAwareOpenAILike`) and `ensure_loaded` happen in exactly one place:

- `agents/base.py` — every `StructuredLLMAgent` subclass via `_structured_call`
  (covers the edit-document agents and so `benchmarks/editdocument.py`).
- `benchmarks/basic.py`, `agents/query_filter_router.py`, `agents/tool_demo.py`,
  `agents/mcp.py`.
- The `/model` page probes (`test_chat`, `test_structured_output`,
  `stream_test_streaming`, `test_tool_call`) via `_resolve_test_target`.

> Historically several of these hand-built `ThinkingAwareOpenAILike`
> directly and discarded `provider_id`. That bypass is what made Ollama
> reach the OpenAI-compat facade instead of its native class — and caused a
> structured-output hang on thinking-capable Ollama models. Routing
> everything through `prepare_llm` fixed it; don't reintroduce direct
> construction.

## /model page

The page renders one combined tree with a per-row provider badge:

- Header shows each registered provider, for example `[Ollama]
  http://127.0.0.1:11434 · [Jan] http://127.0.0.1:1337 · [LM Studio]
  http://127.0.0.1:1234` (each clickable). The list is generated from
  `providers.all_providers()` so any registered provider appears automatically.
- Each model row has a small badge (`pp-provider-badge`) carrying the
  provider's display name. All providers share the same badge styling.
- The Reload button calls `POST /model/api/reload`, which runs
  `sync_models_from_providers()`. The response is a provider-keyed summary such
  as `{"ok": true, "summary": {"ollama": {…} | null, "jan": {…} | null,
  "lm_studio": {…} | null}}`.
  Unreachable providers come back as `null` in the summary and the page
  reloads to show the latest state.
- The model-info side panel uses the row's provider when rendering its
  heading ("Model info ({{ display_name }})") and the unreachable /
  not-found hints.

### Add model (OpenRouter)

Next to Reload sits **Add model**, the way a curated provider's rows get
created. It opens an overlay following `notes/ui-modals.md` (shared backdrop,
sibling card, `.btn-cancel` / `.btn-primary`, dirty-guarded Esc and
backdrop-click), widened to `min(760px,94vw)` for its scrolling list:

- `GET /model/api/openrouter/models` returns the catalog flattened for display
  — id, context length, USD per million prompt/completion tokens, and
  tools / structured / reasoning flags — with `added` marking ids that already
  have a row. It's fetched lazily on first open and filtered client-side.
  When no key is configured, the response is `{"ok": false, "error": …}` naming
  `OPENROUTER_API_KEY`, which the overlay shows in place of the list.
- Already-added models are dimmed and unselectable. Picking one and confirming
  calls `POST /model/api/openrouter/add`, which creates the row and redirects
  to `/model?id=<uuid>`. Adding the same model twice returns the existing row's
  uuid rather than tripping the `(provider, model_name)` unique constraint.
- The new row's `arguments` are the provider defaults **seeded from the catalog
  entry**: `context_window` from `context_length`, `max_tokens` from
  `top_provider.max_completion_tokens`, and both capability flags from
  `supported_parameters`. This matters — llama-index's OpenRouter wrapper
  otherwise defaults to `context_window=3900` and `max_tokens=256`, which would
  quietly cripple a 128k model.

## /model test probes

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

- `llm/models_test_worker.py` reads `{action, provider_id, model, arguments}`
  on stdin, runs `llm.run_named_test(...)`, and writes exactly one JSON
  result line to stdout. The test's own output and library chatter are
  redirected to stderr (which the parent discards) so they can't corrupt
  the result line the parent parses.
- `POST /model/api/test` is a streaming NDJSON endpoint. It spawns the
  worker, emits `{"running": true, "elapsed": s}` heartbeats while it runs,
  then yields the worker's result tagged `"done": true`.
- The **Stop** button aborts the client `fetch`. The disconnect surfaces in
  the Flask response generator as `GeneratorExit` at the next heartbeat
  yield, whose `finally` SIGKILLs the worker. Killing the process closes its
  HTTP socket to the provider, so the provider (e.g. Ollama) stops
  generating — a real cancel, not just a UI dismissal.

`Test streaming` cancels for free without a subprocess: it streams, so when
the client stops reading the upstream HTTP stream is GC-closed. That path is
`POST /model/api/test_streaming_live` (`stream_test_streaming`), and its
probe opts `thinking` back on so Ollama reasoning is visible.

Trade-off: spawning a fresh Python and importing llama-index adds ~2–4s of
startup per probe (shown as the live elapsed counter) — the price of making
the call killable.

## Adding a new provider

1. Create `providers/<id>.py` that defines a class implementing the
   `Provider` Protocol and exports an instance as `PROVIDER`.
2. Extend the `ProviderId` literal and place the provider in
   `PROVIDER_ORDER` in `providers/base.py`.
3. Add its instance to `_PROVIDER_INSTANCES` in `providers/registry.py`.
4. Set `curated` — `False` for a backend whose whole model list should become
   rows, `True` for a catalog too large to mirror (which then needs a way to
   add rows by hand, as OpenRouter has).
5. Add a friendly label to the model-page badge/provider-label helpers
   (otherwise the raw provider id remains as a legible fallback).

That's it — startup sync, the Reload button, the /model page, and all
probe paths pick the new provider up automatically.

## Known limitations

- **Embeddings use Ollama.** `memory/seed_memory.py` uses Ollama's
  OpenAI-compatible endpoint (default `http://127.0.0.1:11434/v1`) with
  `embeddinggemma:300m` (768-d) for Q&A and memory embeddings. Switching
  embedding model or provider would invalidate stored vectors, so embeddings
  remain a separate fixed path rather than following the chat-model provider
  registry.
- **`size_bytes` is `NULL` for Jan rows.** Jan exposes no equivalent of
  `lms ls`. The column is observational, so this is harmless.
- **Jan capability detection is coarser than LM Studio's.** Jan's
  `/v1/models` doesn't expose a `capabilities` array, so new Jan rows
  start with `is_function_calling_model=False`. Flip it via an override
  if a Jan model supports tools.
- **No spend guards on OpenRouter.** Its models cost real money per call, and
  the `/model` probe buttons, benchmark runners, and agents call them like any
  local model. The Add-model overlay shows per-token pricing at the moment of
  choosing, but nothing warns or confirms before a paid call.
- **`size_bytes` is `NULL` for OpenRouter rows.** A cloud model has no size on
  disk. The column is observational, so this is harmless.
