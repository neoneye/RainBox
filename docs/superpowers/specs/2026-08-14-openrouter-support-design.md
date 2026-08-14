# OpenRouter support — design

Add [OpenRouter](https://openrouter.ai) as a fourth provider, using LlamaIndex's
`llama-index-llms-openrouter` wrapper. The API key lives in a gitignored `.env`
at the repo root. `/model` gains an **Add model** button whose overlay lists
OpenRouter's catalog so the operator picks which models become rows.

## Why OpenRouter can't use the normal sync

Every existing provider serves a handful of locally-installed models, so
`sync_model_configs` creating a `model_config` row per available model is right.
OpenRouter serves ~500 cloud models. Auto-creating a row each would bury the
`/model` tree and make every subsequent reload churn.

So OpenRouter is a **curated** provider: rows are created by the operator, one
at a time, through the Add-model overlay. Sync still runs for OpenRouter — it
just never creates. A model OpenRouter retires flips to `available=False` like
any other, so a curated row still tracks reality.

## 1. `Provider.curated`

`providers/base.py` gains a `curated: bool` attribute on the Protocol,
`ProviderId` gains `"openrouter"`, and `PROVIDER_ORDER` gains it last (the
Ollama-first preference in `notes/llm-providers.md` is unchanged — OpenRouter
never becomes the default provider for new rows or the automatic direct-chat
model).

```python
ProviderId = Literal["ollama", "jan", "lm_studio", "openrouter"]
PROVIDER_ORDER: tuple[ProviderId, ...] = (
    PREFERRED_PROVIDER_ID,
    "jan",
    "lm_studio",
    "openrouter",
)


class Provider(Protocol):
    id: ProviderId
    display_name: str
    curated: bool
    """True when the provider's catalog is too large to mirror into
    model_config wholesale, so rows are created by hand (the /model Add-model
    overlay) and sync only tracks availability. False for providers whose
    entire model list should become rows."""
```

`ollama.py`, `jan.py`, `lm_studio.py` each set `curated: bool = False`.

## 2. `sync_model_configs(create_missing=...)`

`db/model_config.py`:

```python
def sync_model_configs(
    provider: str,
    available_model_names: list[str],
    default_arguments: dict[str, Any],
    sizes_by_name: dict[str, int] | None = None,
    function_calling_by_name: dict[str, bool] | None = None,
    force_update_arguments: bool = False,
    create_missing: bool = True,
) -> dict[str, int]:
```

Inside the create branch, `if cfg is None:` becomes `if cfg is None: if not
create_missing: continue` — a curated provider's catalog entry with no row is
simply skipped. Everything else (re-enable, size refresh, disable-when-absent,
never-delete) is unchanged, and the returned summary keeps its shape.

`webapp/core.py::_sync_one_provider` passes `create_missing=not prov.curated`.

## 3. `providers/openrouter.py`

Catalog: `GET https://openrouter.ai/api/v1/models` (public, no key required).
Each entry carries `id`, `name`, `context_length`, `pricing`, and
`supported_parameters`.

- **No key configured → provider is "not configured".** `list_models()` raises
  and `fetch_native_models()` returns `None`, so sync logs a skip and touches
  nothing. This also means no network call at startup on a machine without a
  key. The catalog itself needs no key, but a keyless OpenRouter is unusable, so
  reporting it as unreachable is the honest state.
- `fetch_native_models()` normalizes `supported_parameters` into the
  `capabilities: ["tool_use"]` shape `_sync_one_provider` already derives
  `is_function_calling_model` from, so no provider-specific shim is needed in
  the sync layer (same trick `ollama.py` uses to rename `name` → `id`).
- `fetch_model_sizes()` returns `{}` — cloud models have no size on disk.
- `ensure_loaded()` is a no-op.
- The catalog is memoized for 60s per process so opening the overlay, adding a
  model, and the page reload that follows don't each re-fetch.

`default_arguments()` deliberately **omits `api_key`** (see §4):

```python
def default_arguments(self) -> dict[str, Any]:
    return {
        "api_base": API_BASE,
        "context_window": 8192,
        # llama-index's OpenRouter defaults max_tokens to 256 — far too small
        # for anything rainbox asks a model to produce.
        "max_tokens": 4096,
        "is_function_calling_model": False,
        "should_use_structured_outputs": True,
        "timeout": _COMPLETION_TIMEOUT,
    }
```

`context_window`/`max_tokens`/the capability flags are overwritten with the
catalog's real values when a row is created through the overlay (§6);
`default_arguments()` is the floor for any other creation path.

## 4. The API key never enters the database

`ModelConfig.arguments` is JSONB that `/model` renders verbatim inside
`<pre>arguments</pre>`. Putting `api_key` in `default_arguments()` would both
persist the secret and print it on screen. Instead `prepare_llm` injects it from
`os.environ` at construction time, so the key exists only in `.env` and in the
process that makes the call.

## 5. `.env` loading

`.env` sits at the repo root, next to `source/`, and is added to the root
`.gitignore`. A committed `.env.example` documents the keys.

New `source/env_file.py`:

```python
def load_env_file() -> Path | None:
    """Load the repo-root .env into os.environ, once per process. Returns the
    path loaded, or None when no .env exists. Existing environment variables
    always win — .env is the fallback, so an explicit `OPENROUTER_API_KEY=… python
    …` still overrides the file."""
```

Called at import of `providers/__init__.py`. That is the one choke point every
process which builds an LLM passes through — the web app, `main.py`, the
benchmark runners, and the killable `llm/models_test_worker.py` subprocess all
import `providers` (directly or via `llm`), so none of them can miss the key.
Provider configuration already reads the environment (`OLLAMA_BASE_URL`,
`JAN_BASE_URL`, `LMS`), so `.env` is a natural extension of that layer rather
than a new concept.

`python-dotenv==1.2.2` moves into the declared direct dependencies (it is
already present transitively).

## 6. `llm/prepare_llm` — the OpenRouter branch

`llama-index-llms-openrouter==0.5.1` is added to `requirements.txt`.
`OpenRouter` subclasses `OpenAILike`, so it hits the same failure
`ThinkingAwareOpenAILike` exists to fix: a reasoning model returns empty
`content` with the answer in `reasoning_content`, and structured-output parsing
crashes. The recovery logic moves out of `ThinkingAwareOpenAILike.chat` into a
module-level `_recover_content_from_reasoning(response)`; both
`ThinkingAwareOpenAILike` and a new `ThinkingAwareOpenRouter(OpenRouter)` call
it. No behavior change for existing providers.

```python
def _prepare_openrouter_llm(model: str, arguments: dict[str, Any]) -> LLM:
    api_key = os.environ.get(providers.openrouter.API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(
            f"{providers.openrouter.API_KEY_ENV} is not set — put it in the "
            "repo-root .env (see .env.example)"
        )
    merged = dict(arguments)
    merged.pop("thinking", None)   # native-Ollama-only knob
    merged["api_key"] = api_key
    # App-info header, so calls are attributable on openrouter.ai. Merged
    # rather than assigned so an override's extra_body.reasoning.effort
    # survives.
    additional = dict(merged.get("additional_kwargs") or {})
    headers = dict(additional.get("extra_headers") or {})
    headers.setdefault("X-Title", "rainbox")
    additional["extra_headers"] = headers
    merged["additional_kwargs"] = additional
    return ThinkingAwareOpenRouter(model=model, **merged)
```

`prepare_llm` dispatches to it on `provider_id == "openrouter"`, before the
generic `ThinkingAwareOpenAILike` fallthrough. `ensure_loaded` still runs first
(a no-op here) so the shape of the function is unchanged.

The `/model` probe buttons, benchmarks, and agents all route through
`prepare_llm`, so they work against OpenRouter rows with no further changes.

## 7. `/model` — Add model button + overlay

Two endpoints in `webapp/models_views.py`:

**`GET /model/api/openrouter/models`** → `{"ok": true, "models": [...]}`, or
`{"ok": false, "error": …}` when the key is missing or OpenRouter is
unreachable. Each entry:

```json
{"id": "openai/gpt-4o-mini", "name": "OpenAI: GPT-4o-mini",
 "context_length": 128000, "prompt_price": "0.00000015",
 "completion_price": "0.0000006", "tools": true,
 "structured_outputs": true, "reasoning": false, "added": false}
```

`added` is true when a `model_config` row already exists for
`(provider='openrouter', model_name=id)`.

**`POST /model/api/openrouter/add`** `{"model_name": "..."}` → creates the row
and returns `{"ok": true, "uuid": …}`. It rejects a model that isn't in the
catalog (400) and returns the existing row's uuid if one is already there rather
than tripping the `(provider, model_name)` unique constraint. Arguments are
`default_arguments()` seeded from the catalog entry:

- `context_window` ← `context_length` (llama-index's OpenRouter otherwise
  defaults to 3900, which would silently truncate a 128k model)
- `is_function_calling_model` ← `"tools" in supported_parameters`
- `should_use_structured_outputs` ← `"structured_outputs" in supported_parameters`
- `max_tokens` ← `top_provider.max_completion_tokens` when present

UI: an **Add model** button in the left pane's reload bar. The overlay follows
`notes/ui-modals.md` exactly — the page's existing shared backdrop, a card that
is a *sibling* of it, `<h3>` title, right-aligned `.modal-actions` with
`.btn-cancel` / `.btn-primary` — widened to `min(760px,94vw)` for the list, which
scrolls inside the card. Behavior:

- Catalog fetched lazily on first open, then kept in memory for the page's life.
- A filter input matches on model id and name as you type.
- Each row shows the id, context length, per-1M-token prompt/completion price,
  and capability chips (tools / structured / reasoning). Rows already added are
  dimmed, marked `added`, and not selectable.
- Clicking a row selects it and enables **Add model**; confirming POSTs, then
  navigates to `/model?id=<new uuid>` so the new row is selected.
- Dirty guard per `notes/ui-modals.md`: Esc and backdrop-click dismiss only
  while nothing is typed in the filter and nothing is selected. Cancel always
  closes. This means extending the page's existing `ppDismissRenameIfClean` into
  a general "dismiss whichever modal is open, if clean" pair, as the note
  prescribes for a second modal.

## 8. Provider labels

Seven sites render a provider id as a friendly name and already fall back to the
raw id, so OpenRouter is legible without them — but each gets an arm for
consistency: the `pp-provider-badge` blocks in `models_views.py`,
`model_group_views.py`, and `multimodal_demo_views.py`; the JS `providerLabel`
helpers in `benchmark_views.py`, `benchmark_editdocument_views.py`, and
`model_group_views.py` (two); and `_PROVIDER_LABELS` in `agents/query_handlers.py`.

## 9. Tests

- `providers/test_openrouter.py` — catalog parsing (capabilities normalization,
  malformed entries skipped), the no-key path returning `None`/raising, the
  memoization window, and `default_arguments()` carrying no `api_key`.
- `db/test_sync_model_configs_curated.py` — `create_missing=False` creates no
  rows for unknown names while still re-enabling and disabling existing ones.
- `webapp/test_models_openrouter_views.py` — both endpoints (success, missing
  key, unknown model, duplicate add returning the existing uuid), the seeded
  arguments, and the presence of the Add-model button and modal markup in the
  rendered page.
- `llm/test_openrouter_prepare.py` — `prepare_llm` injects the key from the
  environment, raises a clear error without one, sets the `X-Title` header, and
  preserves an override's `additional_kwargs.extra_body`.

Tests stub the network (no live OpenRouter calls) and run on `rainbox_claude`
via the existing `conftest.py`.

## 10. Docs

`notes/llm-providers.md` is the canonical provider doc and is updated in place
to describe the current state: OpenRouter in the provider table and file layout,
the `curated` attribute and what it changes about sync, the `.env` key path and
why the key isn't stored in `arguments`, the `prepare_llm` OpenRouter branch,
and the Add-model overlay on `/model`. Its "Adding a new provider" checklist
gains the `curated` step. `.env.example` at the repo root documents
`OPENROUTER_API_KEY`.

## Out of scope

No spend guards. OpenRouter models cost real money per call, and `/model`'s probe
buttons plus the benchmark runners will call them like any local model. The
overlay surfaces per-token pricing at the moment of choosing a model, but nothing
warns or confirms before a paid call. Worth revisiting once there's usage.
