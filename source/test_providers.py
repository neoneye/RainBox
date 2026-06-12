"""Provider registry contract tests. No HTTP, no subprocess."""

import pytest

import providers


def test_registry_lists_all_known_providers():
    ids = {p.id for p in providers.all_providers()}
    assert ids == {"lm_studio", "jan", "ollama"}


def test_get_ollama_returns_ollama_provider():
    p = providers.get("ollama")
    assert p.id == "ollama"
    assert p.display_name == "Ollama"


def test_get_lm_studio_returns_lm_studio_provider():
    p = providers.get("lm_studio")
    assert p.id == "lm_studio"
    assert p.display_name == "LM Studio"


def test_get_jan_returns_jan_provider():
    p = providers.get("jan")
    assert p.id == "jan"
    assert p.display_name == "Jan"


def test_get_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        providers.get("nope")


def test_each_provider_has_required_callables():
    for p in providers.all_providers():
        for name in (
            "base_url", "list_models", "fetch_native_models",
            "fetch_model_sizes", "default_arguments", "ensure_loaded",
        ):
            assert callable(getattr(p, name)), f"{p.id} missing {name}"


def test_each_provider_default_arguments_has_required_keys():
    for p in providers.all_providers():
        args = p.default_arguments()
        # Capability flags are common to every provider regardless of client shape.
        for key in ("is_function_calling_model", "should_use_structured_outputs"):
            assert key in args, f"{p.id}.default_arguments missing {key}"
        # Endpoint + timeout exist but spelling differs by client shape:
        # OpenAI-compat (LM Studio/Jan) use api_base/timeout; native Ollama uses
        # base_url/request_timeout.
        assert "api_base" in args or "base_url" in args, f"{p.id} missing an endpoint"
        assert "timeout" in args or "request_timeout" in args, f"{p.id} missing a timeout"
