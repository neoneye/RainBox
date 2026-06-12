"""Provider registry — id → provider instance."""

from __future__ import annotations

from typing import Iterable

from . import jan as _jan
from . import lm_studio as _lm_studio
from . import ollama as _ollama
from .base import Provider, ProviderId


_PROVIDERS: dict[ProviderId, Provider] = {
    "lm_studio": _lm_studio.PROVIDER,
    "jan": _jan.PROVIDER,
    "ollama": _ollama.PROVIDER,
}


def get(provider_id: str) -> Provider:
    """Look up a provider by id. Raises KeyError if not registered."""
    try:
        return _PROVIDERS[provider_id]  # type: ignore[index]
    except KeyError:
        raise KeyError(f"unknown provider id: {provider_id!r}") from None


def all_providers() -> Iterable[Provider]:
    """Every registered provider, in deterministic registration order."""
    return list(_PROVIDERS.values())
