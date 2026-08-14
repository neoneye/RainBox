"""OpenRouter provider.

OpenRouter (https://openrouter.ai) is a cloud gateway in front of several
hundred models from many vendors. That size is what sets it apart from the
local providers: mirroring its whole catalog into `model_config` would bury the
/model tree, so this provider is **curated** — rows are created one at a time
through the Add-model overlay on /model, and sync only tracks whether a row's
model is still on offer (see notes/llm-providers.md).

The catalog at /api/v1/models is public, but a keyless OpenRouter can't answer
a single inference call, so a missing `OPENROUTER_API_KEY` is reported the same
way an unreachable local server is: `list_models()` raises and
`fetch_native_models()` returns None, which makes sync log a skip and leave
every row untouched. It also means no network traffic at startup on a machine
that has no key.

The key itself is deliberately absent from `default_arguments()` — it would be
persisted into `ModelConfig.arguments` (JSONB) and printed verbatim in the
`arguments` block on /model. `llm.prepare_llm` reads it from the environment at
construction time instead.
"""

import os
import time
from typing import Any

import requests

from .base import Provider, ProviderId

API_KEY_ENV: str = "OPENROUTER_API_KEY"
API_BASE: str = "https://openrouter.ai/api/v1"
_CATALOG_URL: str = f"{API_BASE}/models"
_CATALOG_TIMEOUT: float = 8.0
# Cloud round-trips on a large model can be slow, and a queued request on a
# busy upstream slower still — well beyond the local providers' 60s.
_COMPLETION_TIMEOUT: float = 300.0
# Opening the overlay, adding a model, and the page reload that follows all
# want the same catalog within a few seconds of each other.
_CATALOG_TTL: float = 60.0

_catalog_cache: tuple[float, list[dict[str, Any]]] | None = None


def api_key() -> str:
    """The configured OpenRouter key, or "" when none is set. Read from the
    environment on every call so a `.env` edit takes effect on the next
    process, and so tests can monkeypatch it."""
    return (os.environ.get(API_KEY_ENV) or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _normalize(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one raw catalog entry into the shape the rest of rainbox expects,
    or None if it has no usable id.

    `supported_parameters` is folded into a `capabilities` list carrying
    "tool_use", because that's the key `webapp/core._sync_one_provider` derives
    `is_function_calling_model` from for every provider — the same trick
    ollama.py uses to rename `name` → `id`, so the sync layer needs no
    OpenRouter-specific branch."""
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    supported = entry.get("supported_parameters") or []
    if not isinstance(supported, list):
        supported = []
    out = dict(entry)
    out["id"] = model_id
    out["capabilities"] = ["tool_use"] if "tools" in supported else []
    return out


def fetch_catalog(force: bool = False) -> list[dict[str, Any]] | None:
    """The full OpenRouter catalog, memoized for _CATALOG_TTL seconds. None
    when no key is configured or OpenRouter can't be reached."""
    global _catalog_cache
    if not is_configured():
        return None
    now = time.monotonic()
    if not force and _catalog_cache is not None:
        fetched_at, rows = _catalog_cache
        if now - fetched_at < _CATALOG_TTL:
            return rows
    try:
        resp = requests.get(_CATALOG_URL, timeout=_CATALOG_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return None
    rows = [e for e in (_normalize(r) for r in raw if isinstance(r, dict)) if e]
    _catalog_cache = (now, rows)
    return rows


def reset_catalog_cache() -> None:
    """Drop the memoized catalog. Used by tests and by the /model Reload
    button, so a reload really re-asks OpenRouter."""
    global _catalog_cache
    _catalog_cache = None


class _OpenRouterProvider:
    id: ProviderId = "openrouter"
    display_name: str = "OpenRouter"
    curated: bool = True

    def base_url(self) -> str:
        return API_BASE

    def list_models(self) -> list[str]:
        if not is_configured():
            raise RuntimeError(f"{API_KEY_ENV} is not set")
        rows = fetch_catalog()
        if rows is None:
            raise RuntimeError("could not fetch the OpenRouter model catalog")
        return [r["id"] for r in rows]

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        return fetch_catalog()

    def fetch_model_sizes(self) -> dict[str, int]:
        # Cloud models have no size on disk.
        return {}

    def default_arguments(self) -> dict[str, Any]:
        # No api_key here on purpose — see the module docstring.
        #
        # A row added through the /model overlay gets context_window,
        # max_tokens and both capability flags overwritten with the catalog's
        # real values for that model; these are the floor for any other
        # creation path.
        return {
            "api_base": API_BASE,
            "context_window": 8192,
            # llama-index's OpenRouter wrapper defaults max_tokens to 256,
            # which truncates almost everything rainbox asks a model for.
            "max_tokens": 4096,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
            "timeout": _COMPLETION_TIMEOUT,
        }

    def ensure_loaded(self, model: str, context_window: int) -> None:
        # OpenRouter routes each request to an upstream provider that already
        # has the model hot. Nothing to load at this layer.
        return None


PROVIDER: Provider = _OpenRouterProvider()
