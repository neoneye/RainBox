"""Provider Protocol — the contract every backend must satisfy.

A Provider is the integration layer between rainbox and one
local/remote LLM server (LM Studio, Jan, eventually Ollama, OpenRouter).
The webapp talks to all providers through this interface; per-backend
quirks live inside each provider module.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

ProviderId = Literal["lm_studio", "jan", "ollama"]


class Provider(Protocol):
    id: ProviderId
    display_name: str

    def base_url(self) -> str:
        """The provider's HTTP base, without trailing slash. Used for log
        messages and the /model page header link."""
        ...

    def list_models(self) -> list[str]:
        """Names of models the provider currently exposes. Raises on
        network failure — caller decides whether to log+skip or propagate."""
        ...

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        """Provider-native richer model entries (capabilities, contexts,
        state). None if the provider is unreachable, distinct from a
        reachable provider returning []."""
        ...

    def fetch_model_sizes(self) -> dict[str, int]:
        """{model_name: size_bytes} when discoverable; empty dict otherwise.
        Best-effort observational metadata only."""
        ...

    def default_arguments(self) -> dict[str, Any]:
        """ModelConfig.arguments defaults for newly-discovered models. Must
        include api_base, api_key, is_chat_model, is_function_calling_model,
        should_use_structured_outputs, timeout."""
        ...

    def ensure_loaded(self, model: str, context_window: int) -> None:
        """Make sure `model` is loaded with at least `context_window` tokens
        of context, blocking until ready. May be a no-op for providers
        without per-request context-window control (e.g. Jan)."""
        ...
