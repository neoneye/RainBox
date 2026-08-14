"""prepare_llm's OpenRouter branch: where the API key comes from, and what
gets passed to the llama-index wrapper."""

from unittest.mock import patch

import pytest

import llm
from providers import openrouter as openrouter_mod

_ARGS = {
    "api_base": "https://openrouter.ai/api/v1",
    "context_window": 128000,
    "max_tokens": 4096,
    "is_function_calling_model": True,
    "should_use_structured_outputs": True,
    "timeout": 300.0,
}


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv(openrouter_mod.API_KEY_ENV, "sk-or-v1-test")
    yield


def test_prepare_llm_builds_the_openrouter_wrapper(with_key):
    the_llm = llm.prepare_llm("openrouter", "vendor/model", _ARGS)
    assert isinstance(the_llm, llm.ThinkingAwareOpenRouter)
    assert the_llm.model == "vendor/model"
    assert the_llm.context_window == 128000


def test_the_key_comes_from_the_environment_not_the_saved_arguments(with_key):
    """The row's arguments never carry the key (they're JSONB, and /model
    prints them verbatim), so prepare_llm has to inject it."""
    assert "api_key" not in _ARGS
    the_llm = llm.prepare_llm("openrouter", "vendor/model", _ARGS)
    assert the_llm.api_key == "sk-or-v1-test"


def test_a_missing_key_fails_with_an_actionable_message(monkeypatch):
    monkeypatch.delenv(openrouter_mod.API_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        llm.prepare_llm("openrouter", "vendor/model", _ARGS)
    message = str(excinfo.value)
    assert openrouter_mod.API_KEY_ENV in message
    assert ".env" in message


def test_app_info_header_is_sent(with_key):
    the_llm = llm.prepare_llm("openrouter", "vendor/model", _ARGS)
    assert the_llm.additional_kwargs["extra_headers"]["X-Title"] == "rainbox"


def test_an_overrides_reasoning_effort_survives_the_header_merge(with_key):
    """The reasoning_effort control on /model writes
    additional_kwargs.extra_body; adding the app-info header must not drop it."""
    args = {
        **_ARGS,
        "additional_kwargs": {"extra_body": {"reasoning": {"effort": "high"}}},
    }
    the_llm = llm.prepare_llm("openrouter", "vendor/model", args)
    additional = the_llm.additional_kwargs
    assert additional["extra_body"] == {"reasoning": {"effort": "high"}}
    assert additional["extra_headers"]["X-Title"] == "rainbox"


def test_the_ollama_only_thinking_flag_is_dropped(with_key):
    """`thinking` is a native-Ollama constructor field; the OpenAI-shaped
    wrappers reject it, and callers pass arguments uniformly."""
    llm.prepare_llm("openrouter", "vendor/model", {**_ARGS, "thinking": True})


def test_ensure_loaded_is_still_called(with_key):
    with patch.object(openrouter_mod.PROVIDER, "ensure_loaded") as ensure:
        llm.prepare_llm("openrouter", "vendor/model", _ARGS)
    ensure.assert_called_once_with("vendor/model", 128000)
