"""Provider registry — id → provider instance."""

from __future__ import annotations

from typing import Iterable

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
