"""Unsloth Studio provider.

Unsloth Studio (https://unsloth.ai) is a local web app for running and
fine-tuning models. Besides its own UI it serves an OpenAI-compatible API at
`<base>/v1` (an alias of `/api/inference/...`) whose `/v1/models` lists every
model the Studio can serve — the loaded one(s) plus everything downloaded or
cached, including the models it finds in Ollama's store (`ollama/<name>`) —
with a `loaded` flag on each entry. Every model it lists becomes a row, as
with the other local providers; that includes Studio's embedding and TTS
models, because the listing doesn't say which models are chat models.

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
  Studio's `lms load`. Those routes need a key with **full** access; an
  inference-only key gets 403 on them, in which case `ensure_loaded` logs it
  once and leaves loading to whatever is resident in Studio.

  The load route doesn't take every public id: a model Studio found in
  Ollama's store is listed as `ollama/<name>` but loads only by its opaque
  inventory reference (`ref:<hash>`), which `GET /api/models/local` pairs with
  the public id (`model_id`). Status then reports the reference, so a model
  counts as resident under either name. And the load route answers HTTP 200
  even when the load fails — the error is in a `_deferred_error` field of the
  body, so that is what decides success.
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


def load_identifier(model: str, local_models: list[dict[str, Any]]) -> str:
    """The id `POST /api/inference/load` accepts for the public id `model`:
    the inventory entry's `id` whose `model_id` is `model` (a `ref:<hash>`
    for an Ollama-store model, the repo id itself for a Hugging Face one), or
    `model` unchanged when the inventory doesn't list it."""
    for entry in local_models:
        if entry.get("model_id") == model and isinstance(entry.get("id"), str):
            return entry["id"]
    return model


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


def needs_load(
    status: dict[str, Any], names: set[str], context_window: int
) -> bool:
    """True when the model known by any of `names` (its public id and its
    load id) must be (re)loaded to serve `context_window` tokens: it isn't
    resident, or it is resident with a smaller runtime window. A resident
    model whose window Studio doesn't report is left alone — a reload can't be
    shown to help, and it costs a cold start."""
    if not names & _loaded_identifiers(status):
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
        return [str(m["id"]) for m in _get_models_payload()]

    def fetch_native_models(self) -> list[dict[str, Any]] | None:
        try:
            return _get_models_payload()
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
        local = requests.get(
            f"{base}/api/models/local", headers=_headers(),
            timeout=_STATUS_TIMEOUT,
        )
        local.raise_for_status()
        load_id = load_identifier(model, list(local.json().get("models") or []))
        if not needs_load(status, {model, load_id}, context_window):
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
                "model_path": load_id,
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
        try:
            body = load.json()
        except ValueError:
            body = {}
        deferred = body.get("_deferred_error") if isinstance(body, dict) else None
        if deferred:
            detail = deferred.get("detail") if isinstance(deferred, dict) else deferred
            raise RuntimeError(f"Unsloth Studio could not load {model!r}: {detail}")


PROVIDER: Provider = _UnslothStudioProvider()
