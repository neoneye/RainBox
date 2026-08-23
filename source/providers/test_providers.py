"""Provider registry contract tests. No HTTP, no subprocess."""

import pytest

import providers


def test_registry_lists_all_known_providers():
    ids = [p.id for p in providers.all_providers()]
    assert ids == ["ollama", "jan", "lm_studio", "openrouter"]


def test_ollama_is_the_preferred_provider():
    assert providers.PREFERRED_PROVIDER_ID == "ollama"
    assert providers.provider_sort_key("ollama") < providers.provider_sort_key("jan")
    assert providers.provider_sort_key("jan") < providers.provider_sort_key("lm_studio")


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


def test_request_api_key_prefers_the_key_stored_on_the_row():
    """The local providers persist a dummy key in their arguments; it wins."""
    assert providers.request_api_key("jan", {"api_key": "jan"}) == "jan"
    assert providers.request_api_key("lm_studio", {"api_key": "lm-studio"}) == "lm-studio"


def test_request_api_key_is_none_for_a_provider_that_needs_none():
    """Ollama is local and unauthenticated — no header should be sent."""
    assert providers.request_api_key("ollama", {}) is None


def test_request_api_key_reads_openrouter_from_the_environment(monkeypatch):
    """OpenRouter's key is deliberately absent from the row's arguments (see
    providers/openrouter.py). Anything not going through llm.prepare_llm has
    to get it from here, or it sends an unauthenticated request."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    assert providers.request_api_key("openrouter", {}) == "sk-or-test"


def test_request_api_key_raises_when_openrouter_has_no_key(monkeypatch):
    """Named so the caller can report the real problem. Returning None here
    would send an unauthenticated request, and OpenRouter answers those by
    trying its browser cookie path — a 401 about cookies, which describes
    nothing an operator can act on."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        providers.request_api_key("openrouter", {})
