"""Ollama provider unit tests. HTTP is mocked via monkeypatch."""

import importlib
from unittest.mock import patch

import requests

from providers import ollama as ollama_mod
from providers.ollama import PROVIDER


def test_default_base_url_is_localhost_11434():
    assert PROVIDER.base_url() == "http://127.0.0.1:11434"


def test_base_url_honors_env_var(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.test:9999")
    importlib.reload(ollama_mod)
    try:
        assert ollama_mod.PROVIDER.base_url() == "http://example.test:9999"
    finally:
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        importlib.reload(ollama_mod)


def test_ensure_loaded_is_a_no_op():
    # Must not raise, must not call any external process.
    PROVIDER.ensure_loaded("any-model", 128_000)


def test_default_arguments_has_ollama_endpoint_and_key():
    # Native llama-index-llms-ollama shape (passed straight to Ollama()): the
    # endpoint is base_url at the /api root (no /v1) and the timeout is
    # request_timeout. No api_key/is_chat_model — those are OpenAI-compat-only.
    args = PROVIDER.default_arguments()
    assert args["base_url"] == "http://127.0.0.1:11434"
    assert args["is_function_calling_model"] is False
    assert args["should_use_structured_outputs"] is True
    assert "request_timeout" in args


def test_list_models_parses_openai_response():
    fake = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"data": [{"id": "llama3:latest"}, {"id": "qwen3:8b"}]},
    })()
    with patch("providers.ollama.requests.get", return_value=fake):
        assert PROVIDER.list_models() == ["llama3:latest", "qwen3:8b"]


def test_fetch_native_models_renames_name_to_id():
    """/api/tags returns rows keyed by `name`; we expose them under `id`
    so the sync layer and the /model detail panel (which both expect
    `m["id"]`) work without provider-specific shims."""
    fake = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {
            "models": [
                {"name": "llama3:latest", "size": 4_661_224_676,
                 "details": {"family": "llama"}},
                {"name": "qwen3:8b", "size": 5_000_000_000},
            ]
        },
    })()
    with patch("providers.ollama.requests.get", return_value=fake):
        out = PROVIDER.fetch_native_models()
    assert out is not None
    ids = [m["id"] for m in out]
    assert ids == ["llama3:latest", "qwen3:8b"]
    # Original keys still present.
    assert out[0]["size"] == 4_661_224_676
    assert out[0]["details"]["family"] == "llama"


def test_fetch_native_models_returns_none_on_network_error():
    def boom(*a, **kw):
        raise requests.ConnectionError("nope")
    with patch("providers.ollama.requests.get", side_effect=boom):
        assert PROVIDER.fetch_native_models() is None


def test_fetch_model_sizes_extracts_from_tags():
    fake = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {
            "models": [
                {"name": "llama3:latest", "size": 4_661_224_676},
                {"name": "qwen3:8b", "size": 5_000_000_000},
                {"name": "broken"},  # no size — skipped
            ]
        },
    })()
    with patch("providers.ollama.requests.get", return_value=fake):
        sizes = PROVIDER.fetch_model_sizes()
    assert sizes == {
        "llama3:latest": 4_661_224_676,
        "qwen3:8b": 5_000_000_000,
    }


def test_fetch_model_sizes_returns_empty_dict_when_unreachable():
    def boom(*a, **kw):
        raise requests.ConnectionError("down")
    with patch("providers.ollama.requests.get", side_effect=boom):
        assert PROVIDER.fetch_model_sizes() == {}
