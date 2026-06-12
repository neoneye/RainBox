"""LM Studio provider.

The OpenAI-compatible endpoint at :1234 does not let you change a loaded
model's context length per request — context is baked in at model load
time. To grow it, we call LM Studio's own management surface, the `lms`
CLI. Status comes from LM Studio's REST API; mutations go through the
CLI.
"""

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import requests

from .base import Provider, ProviderId

_log = logging.getLogger(__name__)

_DEFAULT_BASE_URL: str = "http://127.0.0.1:1234"
_MODELS_TIMEOUT: float = 3.0
_COMPLETION_TIMEOUT: float = 60.0

# Default keepalive: one hour bounds idle resident memory while surviving
# typical interactive gaps.
DEFAULT_TTL_SECONDS: int = 3600

# Standard install location on macOS; fallback when `lms` isn't on PATH.
_DEFAULT_LMS_PATHS = (
    Path.home() / ".cache" / "lm-studio" / "bin" / "lms",
)


def _base_url() -> str:
    return os.environ.get("LM_STUDIO_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _lms_path() -> str:
    env = os.environ.get("LMS")
    if env:
        return env
    on_path = shutil.which("lms")
    if on_path:
        return on_path
    for candidate in _DEFAULT_LMS_PATHS:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "could not find the `lms` CLI on PATH or in ~/.cache/lm-studio/bin/; "
        "install LM Studio's CLI or set $LMS to its path"
    )


def _list_native_models_via_api() -> list[dict[str, Any]]:
    url = f"{_base_url()}/api/v0/models"
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.loads(resp.read())
    return list(body.get("data") or [])


def find_instances(base_id: str, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All entries that map to `base_id`. LM Studio appends `:N` to a loaded
    instance's identifier when there's a name collision."""
    return [
        m for m in models
        if m.get("id") == base_id or str(m.get("id", "")).startswith(base_id + ":")
    ]


def _max_loaded_context(instances: list[dict[str, Any]]) -> int:
    """Largest `loaded_context_length` across loaded instances."""
    loaded = [i for i in instances if i.get("state") == "loaded"]
    if not loaded:
        return 0
    return max(int(i.get("loaded_context_length") or 0) for i in loaded)


class _LMStudioProvider:
    id: ProviderId = "lm_studio"
    display_name: str = "LM Studio"

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
                f"{self.base_url()}/api/v0/models", timeout=_MODELS_TIMEOUT
            )
            resp.raise_for_status()
            return resp.json().get("data", [])
        except requests.RequestException:
            return None

    def fetch_model_sizes(self) -> dict[str, int]:
        try:
            proc = subprocess.run(
                ["lms", "ls", "--json"],
                check=True, capture_output=True, text=True, timeout=5.0,
            )
            rows = json.loads(proc.stdout)
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, json.JSONDecodeError):
            return {}
        out: dict[str, int] = {}
        for row in rows:
            key = row.get("modelKey")
            size = row.get("sizeBytes")
            if isinstance(key, str) and isinstance(size, int):
                out[key] = size
        return out

    def default_arguments(self) -> dict[str, Any]:
        return {
            "api_base": f"{self.base_url()}/v1",
            "api_key": "lm-studio",
            "is_chat_model": True,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
            "timeout": _COMPLETION_TIMEOUT,
        }

    def ensure_loaded(
        self,
        model: str,
        context_window: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Make sure LM Studio has `model` loaded with at least
        `context_window` tokens of context, blocking until it is."""
        models = _list_native_models_via_api()
        instances = find_instances(model, models)
        if _max_loaded_context(instances) >= context_window:
            _log.debug(
                "lm_studio: %s already loaded with >= %d ctx; skipping reload",
                model, context_window,
            )
            return

        lms = _lms_path()
        for inst in instances:
            if inst.get("state") != "loaded":
                continue
            identifier = inst.get("id")
            if not identifier:
                continue
            _log.info(
                "lm_studio: unloading %s (loaded ctx=%s < required %d)",
                identifier, inst.get("loaded_context_length"), context_window,
            )
            subprocess.run(
                [lms, "unload", str(identifier)],
                check=True, capture_output=True, text=True,
            )
        _log.info(
            "lm_studio: loading %s with context-length=%d ttl=%ds",
            model, context_window, ttl_seconds,
        )
        subprocess.run(
            [
                lms, "load", model,
                "--context-length", str(context_window),
                "--gpu", "max",
                "--ttl", str(ttl_seconds),
            ],
            check=True, capture_output=True, text=True,
        )


PROVIDER: Provider = _LMStudioProvider()
