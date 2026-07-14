"""Ollama provider.

Ollama (https://ollama.com) exposes an OpenAI-compatible API at
http://127.0.0.1:11434/v1 alongside its native API at /api/. Like Jan it
auto-loads models on first request; unlike LM Studio it has no separate
`lms`-style CLI for forcing a context-window reload, so `ensure_loaded`
is a no-op here.

`/api/tags` (native) is richer than `/v1/models`: it includes model size
in bytes and a `details` dict (format, family, parameter_size,
quantization_level). The Ollama `/api/tags` shape uses `name` rather
than the `id` field the rest of rainbox expects — we rename in
fetch_native_models so the sync layer (which keys on `m["id"]`) and the
/model detail panel both work without provider-specific shims.
"""

import os
from typing import Any

import requests

from .base import Provider, ProviderId

_DEFAULT_BASE_URL: str = "http://127.0.0.1:11434"
_MODELS_TIMEOUT: float = 3.0
_COMPLETION_TIMEOUT: float = 60.0


def _base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _fetch_tags() -> list[dict[str, Any]] | None:
    """Hit /api/tags once. Returns the list of entries (each renamed so
    `id` is set), or None if the server is unreachable."""
    try:
        resp = requests.get(f"{_base_url()}/api/tags", timeout=_MODELS_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    rows = resp.json().get("models") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        entry = dict(row)
        entry["id"] = name
        out.append(entry)
    return out


class _OllamaProvider:
    id: ProviderId = "ollama"
    display_name: str = "Ollama"

    def base_url(self) -> str:
        return _base_url()

    def list_models(self) -> list[str]:
        resp = requests.get(f"{self.base_url()}/v1/models", timeout=_MODELS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        return _fetch_tags()

    def fetch_model_sizes(self) -> dict[str, int]:
        rows = _fetch_tags()
        if rows is None:
            return {}
        out: dict[str, int] = {}
        for row in rows:
            name = row.get("id")
            size = row.get("size")
            if isinstance(name, str) and isinstance(size, int):
                out[name] = size
        return out

    def default_arguments(self) -> dict[str, Any]:
        # Native llama-index-llms-ollama shape: these keys are passed straight
        # to the Ollama() constructor by _prepare_ollama_llm (no remapping).
        # `should_use_structured_outputs` is kept as an app-level capability
        # flag (filters/labels read it); it's not an Ollama constructor field,
        # so the constructor drops it.
        return {
            "base_url": self.base_url(),  # native /api root — no /v1 suffix
            "request_timeout": _COMPLETION_TIMEOUT,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
        }

    def ensure_loaded(self, model: str, context_window: int) -> None:
        # Ollama auto-loads on first request. The OpenAI shim doesn't
        # accept `options.num_ctx` so we can't push a context-length
        # override through it from here; the user sets that per model in
        # Ollama's own config (Modelfile / OLLAMA_NUM_CTX env).
        return None


PROVIDER: Provider = _OllamaProvider()
