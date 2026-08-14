"""OpenRouter provider unit tests. HTTP is mocked; no live catalog fetch."""

from unittest.mock import patch

import pytest
import requests

from providers import openrouter as openrouter_mod
from providers.openrouter import PROVIDER

_CATALOG = {
    "data": [
        {
            "id": "openai/gpt-4o-mini",
            "name": "OpenAI: GPT-4o-mini",
            "context_length": 128000,
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            "supported_parameters": ["tools", "structured_outputs"],
        },
        {
            "id": "some/reasoner",
            "name": "Some Reasoner",
            "context_length": 32768,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["reasoning"],
        },
        {"name": "no id at all"},
    ]
}


def _fake_response(payload):
    return type(
        "R",
        (),
        {"raise_for_status": lambda self: None, "json": lambda self: payload},
    )()


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv(openrouter_mod.API_KEY_ENV, "sk-or-v1-test")
    openrouter_mod.reset_catalog_cache()
    yield
    openrouter_mod.reset_catalog_cache()


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.delenv(openrouter_mod.API_KEY_ENV, raising=False)
    openrouter_mod.reset_catalog_cache()
    yield
    openrouter_mod.reset_catalog_cache()


def test_base_url_is_the_openrouter_api(with_key):
    assert PROVIDER.base_url() == "https://openrouter.ai/api/v1"


def test_provider_is_curated():
    """Curated is what keeps sync from creating a row per catalog entry."""
    assert PROVIDER.curated is True


def test_default_arguments_carry_no_api_key():
    """The key would be persisted into model_config.arguments (JSONB) and
    printed verbatim on /model. prepare_llm injects it at call time instead."""
    args = PROVIDER.default_arguments()
    assert "api_key" not in args
    assert args["api_base"] == "https://openrouter.ai/api/v1"
    # llama-index's OpenRouter defaults these to 3900 / 256.
    assert args["context_window"] > 3900
    assert args["max_tokens"] > 256


def test_fetch_model_sizes_is_empty_dict():
    assert PROVIDER.fetch_model_sizes() == {}


def test_ensure_loaded_is_a_no_op(with_key):
    PROVIDER.ensure_loaded("openai/gpt-4o-mini", 128_000)


def test_without_a_key_nothing_hits_the_network(without_key):
    """No key means no usable provider, so it reports itself the way an
    unreachable local server does — and never makes a request."""
    with patch("providers.openrouter.requests.get") as get:
        assert PROVIDER.fetch_native_models() is None
        with pytest.raises(RuntimeError):
            PROVIDER.list_models()
    get.assert_not_called()


def test_list_models_returns_catalog_ids(with_key):
    with patch(
        "providers.openrouter.requests.get",
        return_value=_fake_response(_CATALOG),
    ):
        assert PROVIDER.list_models() == ["openai/gpt-4o-mini", "some/reasoner"]


def test_tool_support_is_normalized_into_capabilities(with_key):
    """_sync_one_provider derives is_function_calling_model from a
    `capabilities` list containing "tool_use", for every provider."""
    with patch(
        "providers.openrouter.requests.get",
        return_value=_fake_response(_CATALOG),
    ):
        rows = PROVIDER.fetch_native_models()
    assert rows is not None
    by_id = {r["id"]: r for r in rows}
    assert by_id["openai/gpt-4o-mini"]["capabilities"] == ["tool_use"]
    assert by_id["some/reasoner"]["capabilities"] == []


def test_entries_without_an_id_are_dropped(with_key):
    with patch(
        "providers.openrouter.requests.get",
        return_value=_fake_response(_CATALOG),
    ):
        rows = PROVIDER.fetch_native_models()
    assert rows is not None
    assert len(rows) == 2


def test_fetch_native_models_returns_none_on_network_error(with_key):
    with patch(
        "providers.openrouter.requests.get",
        side_effect=requests.ConnectionError("nope"),
    ):
        assert PROVIDER.fetch_native_models() is None


def test_catalog_is_memoized_then_droppable(with_key):
    with patch(
        "providers.openrouter.requests.get",
        return_value=_fake_response(_CATALOG),
    ) as get:
        openrouter_mod.fetch_catalog()
        openrouter_mod.fetch_catalog()
        assert get.call_count == 1
        openrouter_mod.reset_catalog_cache()
        openrouter_mod.fetch_catalog()
        assert get.call_count == 2
