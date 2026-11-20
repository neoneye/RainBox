"""Unsloth Studio provider.

Unsloth Studio (https://unsloth.ai) is a local web app for running and
fine-tuning models. Besides its own UI it serves an OpenAI-compatible API at
`<base>/v1` (an alias of `/api/inference/...`) whose `/v1/models` lists every
model the Studio can serve — the loaded one(s) plus everything downloaded or
cached — with a `loaded` flag on each entry. Every model it lists becomes a
row, as with the other local providers.

Two things set it apart from Jan and LM Studio:

- **Every API call needs a key.** Studio has user accounts, and its `/v1`
  routes answer 401 without a bearer token. The key is created in Studio
  (Settings → API keys) and read from `UNSLOTH_STUDIO_API_KEY` in the
  repo-root `.env`. Like OpenRouter's, it stays out of `default_arguments()`
  — that blob is persisted to `ModelConfig.arguments` and printed verbatim on
  /model — and `llm.prepare_llm` / `providers.request_api_key` inject it from
  the environment. A missing key is reported the way an unreachable server
  is: `list_models()` raises, `fetch_native_models()` returns None, sync logs
  a skip and touches nothing.

- **Context length is set at load time**, through Studio's own
  `POST /api/inference/load` (`max_seq_length`). `ensure_loaded` reads
  `GET /api/inference/status` and loads the model only when it isn't the
  resident one or is resident with a smaller window — the same contract as LM
  Studio's `lms load`. Those two routes need a key with **full** access; an
  inference-only key gets 403 on them, in which case `ensure_loaded` logs it
  once and leaves loading to whatever is resident in Studio.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from .base import Provider, ProviderId

_log = logging.getLogger(__name__)

API_KEY_ENV: str = "UNSLOTH_STUDIO_API_KEY"
_BASE_URL_ENV: str = "UNSLOTH_STUDIO_BASE_URL"
_DEFAULT_BASE_URL: str = "http://127.0.0.1:8888"
_MODELS_TIMEOUT: float = 3.0
_STATUS_TIMEOUT: float = 5.0
# A cold load reads the weights off disk and, for a GGUF, starts a fresh
# llama-server; a large model takes minutes. Same bound as LM Studio's `lms`.
_LOAD_TIMEOUT: float = 180.0
_COMPLETION_TIMEOUT: float = 60.0

# Logged once per process: an inference-only key can't read status or load.
_warned_no_load_access: bool = False


def _base_url() -> str:
    return os.environ.get(_BASE_URL_ENV, _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    """The configured Unsloth Studio key, or "" when none is set. Read from
    the environment on every call so a `.env` edit takes effect on the next
    process, and so tests can monkeypatch it."""
    return (os.environ.get(API_KEY_ENV) or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}"}


def _get_models_payload() -> list[dict[str, Any]]:
    """Raw `/v1/models` entries. Raises when no key is configured or the
    Studio can't be reached / refuses the key."""
    if not is_configured():
        raise RuntimeError(f"{API_KEY_ENV} is not set")
    resp = requests.get(
        f"{_base_url()}/v1/models", headers=_headers(), timeout=_MODELS_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    return [m for m in data if isinstance(m, dict) and m.get("id")]


def _is_text_model(entry: dict[str, Any]) -> bool:
    """Studio also serves image, audio and embedding models from the same
    listing. Rainbox only calls chat models, so an entry that declares
    output modalities without text is left out. An entry that declares none
    is kept — older Studio builds don't send the field."""
    modalities = entry.get("output_modalities")
    if not isinstance(modalities, list) or not modalities:
        return True
    return "text" in modalities


def _loaded_identifiers(status: dict[str, Any]) -> set[str]:
    """Every identifier `/api/inference/status` uses for a resident model.
    `loaded` is a list of ids (or of objects carrying one) when several
    models are held at once; `model_identifier` / `active_model` name the
    active one."""
    out: set[str] = set()
    for key in ("model_identifier", "active_model"):
        value = status.get(key)
        if isinstance(value, str) and value:
            out.add(value)
    loaded = status.get("loaded")
    if isinstance(loaded, list):
        for item in loaded:
            if isinstance(item, str) and item:
                out.add(item)
            elif isinstance(item, dict):
                for key in ("id", "model", "model_identifier", "model_path"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        out.add(value)
    return out


def needs_load(status: dict[str, Any], model: str, context_window: int) -> bool:
    """True when `model` must be (re)loaded to serve `context_window` tokens:
    it isn't resident, or it is resident with a smaller runtime window. A
    resident model whose window Studio doesn't report is left alone — a
    reload can't be shown to help, and it costs a cold start."""
    if model not in _loaded_identifiers(status):
        return True
    ctx = status.get("context_length")
    if not isinstance(ctx, int) or ctx <= 0:
        return False
    return ctx < context_window


class _UnslothStudioProvider:
    id: ProviderId = "unsloth_studio"
    display_name: str = "Unsloth Studio"
    curated: bool = False

    def base_url(self) -> str:
        return _base_url()

    def list_models(self) -> list[str]:
        return [str(m["id"]) for m in _get_models_payload() if _is_text_model(m)]

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        try:
            return [m for m in _get_models_payload() if _is_text_model(m)]
        except (requests.RequestException, RuntimeError, ValueError):
            return None

    def fetch_model_sizes(self) -> dict[str, int]:
        # `/v1/models` carries no size, and the inventory routes that do
        # redact paths per model; not worth a second scan for an
        # observational column.
        return {}

    def default_arguments(self) -> dict[str, Any]:
        # No api_key here on purpose — see the module docstring.
        return {
            "api_base": f"{self.base_url()}/v1",
            "is_chat_model": True,
            "is_function_calling_model": False,
            "should_use_structured_outputs": True,
            "timeout": _COMPLETION_TIMEOUT,
        }

    def ensure_loaded(self, model: str, context_window: int) -> None:
        """Make sure Studio has `model` resident with at least
        `context_window` tokens of context, blocking until it is."""
        global _warned_no_load_access
        if not is_configured():
            # prepare_llm raises the readable error for this; nothing to load.
            return
        base = self.base_url()
        resp = requests.get(
            f"{base}/api/inference/status", headers=_headers(),
            timeout=_STATUS_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            if not _warned_no_load_access:
                _log.warning(
                    "unsloth_studio: the API key can't read inference status "
                    "(HTTP %d) — create a key with full access to let rainbox "
                    "load models at their configured context length",
                    resp.status_code,
                )
                _warned_no_load_access = True
            return
        resp.raise_for_status()
        status = resp.json()
        if not needs_load(status, model, context_window):
            _log.debug(
                "unsloth_studio: %s already loaded with >= %d ctx; skipping load",
                model, context_window,
            )
            return
        _log.info(
            "unsloth_studio: loading %s with max_seq_length=%d (resident: %s, ctx=%s)",
            model, context_window,
            sorted(_loaded_identifiers(status)) or "none",
            status.get("context_length"),
        )
        load = requests.post(
            f"{base}/api/inference/load",
            headers=_headers(),
            json={
                "model_path": model,
                "max_seq_length": int(context_window),
                # Everything else (GGUF variant, KV cache type, GPU layers…)
                # comes from what the operator saved for this model in Studio.
                "use_saved_settings": True,
            },
            timeout=_LOAD_TIMEOUT,
        )
        if load.status_code >= 400:
            raise RuntimeError(
                f"Unsloth Studio could not load {model!r} "
                f"(HTTP {load.status_code}): {load.text[:300]}"
            )


PROVIDER: Provider = _UnslothStudioProvider()
