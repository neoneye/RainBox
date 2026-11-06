"""Provider registry — id → provider instance."""

from __future__ import annotations

from typing import Any, Iterable

from . import jan as _jan
from . import lm_studio as _lm_studio
from . import ollama as _ollama
from . import openrouter as _openrouter
from .base import PROVIDER_ORDER, Provider, ProviderId


_PROVIDER_INSTANCES: dict[ProviderId, Provider] = {
    "ollama": _ollama.PROVIDER,
    "jan": _jan.PROVIDER,
    "lm_studio": _lm_studio.PROVIDER,
    "openrouter": _openrouter.PROVIDER,
}
_PROVIDERS: dict[ProviderId, Provider] = {
    provider_id: _PROVIDER_INSTANCES[provider_id]
    for provider_id in PROVIDER_ORDER
}


def get(provider_id: str) -> Provider:
    """Look up a provider by id. Raises KeyError if not registered."""
    try:
        return _PROVIDERS[provider_id]  # type: ignore[index]
    except KeyError:
        raise KeyError(f"unknown provider id: {provider_id!r}") from None


def all_providers() -> Iterable[Provider]:
    """Every provider in preferred order: Ollama first, then alternatives."""
    return list(_PROVIDERS.values())


def request_api_key(provider_id: str, arguments: dict[str, Any]) -> str | None:
    """The bearer token for a raw HTTP call to `provider_id`'s
    OpenAI-compatible endpoint, or None when it needs none.

    Most providers carry their key in a model row's `arguments`, so it arrives
    with the row — for the local ones it is a dummy the server ignores ("jan",
    "lm-studio"), and Ollama has none at all. OpenRouter is the exception:
    its key is a real secret, deliberately absent from `default_arguments()`
    because that blob is JSONB-persisted and rendered verbatim on /model, so
    it is read from the environment here — the same source and the same
    failure as `llm.prepare_llm`.

    Anything talking to a provider WITHOUT going through `llm.prepare_llm`
    must come here for the key. Reading `arguments["api_key"]` directly works
    for three providers out of four and silently sends OpenRouter an
    unauthenticated request, which it answers by trying its browser cookie
    path: `{"error":{"message":"No cookie auth credentials found",...}}`.

    Raises RuntimeError when the provider requires a key and none is set, so
    the caller can say that plainly instead of relaying a 401 about cookies.
    """
    key = str(arguments.get("api_key") or "").strip()
    if key:
        return key
    if provider_id == _openrouter.PROVIDER.id:
        env_key = _openrouter.api_key()
        if not env_key:
            raise RuntimeError(
                f"{_openrouter.API_KEY_ENV} is not set — put it in the "
                "repo-root .env file (see .env.example)"
            )
        return env_key
    return None


def request_base_url(provider_id: str, arguments: dict[str, Any]) -> str | None:
    """The OpenAI-compatible base URL (no trailing slash) for a raw HTTP call
    to this model's provider — the companion of `request_api_key` for anything
    that bypasses `llm.prepare_llm`.

    Jan/LM Studio rows store the full base (including `/v1`) in
    `arguments["api_base"]`. Ollama rows store none, or only the native
    `base_url` without `/v1`; they use the provider's base URL with `/v1`
    appended. Returns None for an unregistered provider id with no
    `api_base`."""
    api_base = str(arguments.get("api_base") or "").strip()
    if api_base:
        return api_base.rstrip("/")
    try:
        return get(provider_id).base_url().rstrip("/") + "/v1"
    except KeyError:
        return None
