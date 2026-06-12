"""Jan provider unit tests. HTTP is mocked via monkeypatch."""

import importlib
from unittest.mock import patch

import requests

from providers import jan as jan_mod
from providers.jan import PROVIDER


def test_default_base_url_is_localhost_1337():
    assert PROVIDER.base_url() == "http://127.0.0.1:1337"


def test_base_url_honors_env_var(monkeypatch):
    monkeypatch.setenv("JAN_BASE_URL", "http://example.test:9999")
    importlib.reload(jan_mod)
    try:
        assert jan_mod.PROVIDER.base_url() == "http://example.test:9999"
    finally:
        monkeypatch.delenv("JAN_BASE_URL", raising=False)
        importlib.reload(jan_mod)


def test_ensure_loaded_is_a_no_op():
    # Must not raise, must not call any external process.
    PROVIDER.ensure_loaded("any-model", 128_000)


def test_default_arguments_has_jan_endpoint_and_key():
    args = PROVIDER.default_arguments()
    assert args["api_base"] == "http://127.0.0.1:1337/v1"
    assert args["api_key"] == "jan"
    assert args["is_chat_model"] is True
    assert args["is_function_calling_model"] is False
    assert args["should_use_structured_outputs"] is True
    assert "timeout" in args


def test_fetch_model_sizes_is_empty_dict():
    assert PROVIDER.fetch_model_sizes() == {}


def test_list_models_parses_openai_response():
    fake_response = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"data": [{"id": "model-a"}, {"id": "model-b"}]},
    })()
    with patch("providers.jan.requests.get", return_value=fake_response):
        assert PROVIDER.list_models() == ["model-a", "model-b"]


def test_fetch_native_models_returns_none_on_network_error():
    def boom(*a, **kw):
        raise requests.ConnectionError("nope")
    with patch("providers.jan.requests.get", side_effect=boom):
        assert PROVIDER.fetch_native_models() is None


def test_fetch_native_models_returns_data_list_on_success():
    fake_response = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {"data": [{"id": "x"}]},
    })()
    with patch("providers.jan.requests.get", return_value=fake_response):
        assert PROVIDER.fetch_native_models() == [{"id": "x"}]
