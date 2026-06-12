"""Jan provider.

Jan (https://github.com/janhq/jan) exposes an OpenAI-compatible API at
http://127.0.0.1:1337/v1 by default. Unlike LM Studio it has no `lms`-style
CLI for forcing a per-request context-window reload — models auto-load on
first request using whatever context length the user set in Jan's UI.
`ensure_loaded` is therefore a no-op.

Jan's /v1/models response is plain OpenAI shape (no capabilities array, no
size info), so fetch_model_sizes returns {} and capability detection for
new rows falls back to the default (False).
"""

import os
from typing import Any

import requests

from .base import Provider, ProviderId

_DEFAULT_BASE_URL: str = "http://127.0.0.1:1337"
_MODELS_TIMEOUT: float = 3.0
_COMPLETION_TIMEOUT: float = 60.0


def _base_url() -> str:
    return os.environ.get("JAN_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


class _JanProvider:
    id: ProviderId = "jan"
    display_name: str = "Jan"

    def base_url(self) -> str:
        return _base_url()

    def list_models(self) -> list[str]:
        resp = requests.get(f"{self.base_url()}/v1/models", timeout=_MODELS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        try:
            resp = requests.get(
                f"{self.base_url()}/v1/models", timeout=_MODELS_TIMEOUT
            )
            resp.raise_for_status()
            return list(resp.json().get("data", []))
        except requests.RequestException:
            return None

    def fetch_model_sizes(self) -> dict[str, int]:
        # Jan has no equivalent of `lms ls --json`.
        return {}

    def default_arguments(self) -> dict[str, Any]:
        return {
            "api_base": f"{self.base_url()}/v1",
            # Jan accepts any non-empty string when its API key is off.
            "api_key": "jan",
            "is_chat_model": True,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
            "timeout": _COMPLETION_TIMEOUT,
        }

    def ensure_loaded(self, model: str, context_window: int) -> None:
        # Jan auto-loads on first request using the context length set in
        # Jan's UI. Nothing to do at this layer.
        return None


PROVIDER: Provider = _JanProvider()
