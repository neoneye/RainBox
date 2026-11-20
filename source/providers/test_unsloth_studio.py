"""Unsloth Studio provider unit tests. HTTP is mocked; no live Studio."""

import importlib
from unittest.mock import patch

import pytest
import requests

import llm
from providers import unsloth_studio as studio_mod
from providers.unsloth_studio import PROVIDER, needs_load


class _Resp:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv(studio_mod.API_KEY_ENV, "sk-unsloth-test")


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.delenv(studio_mod.API_KEY_ENV, raising=False)


def test_default_base_url_is_localhost_8888(monkeypatch):
    monkeypatch.delenv("UNSLOTH_STUDIO_BASE_URL", raising=False)
    assert PROVIDER.base_url() == "http://127.0.0.1:8888"


def test_base_url_honors_env_var(monkeypatch):
    monkeypatch.setenv("UNSLOTH_STUDIO_BASE_URL", "http://example.test:9999/")
    importlib.reload(studio_mod)
    try:
        assert studio_mod.PROVIDER.base_url() == "http://example.test:9999"
    finally:
        monkeypatch.delenv("UNSLOTH_STUDIO_BASE_URL", raising=False)
        importlib.reload(studio_mod)


def test_provider_is_not_curated():
    assert PROVIDER.curated is False


def test_default_arguments_carry_no_key(monkeypatch):
    """The key would be persisted to ModelConfig.arguments and printed on
    /model — it is injected from the environment instead."""
    monkeypatch.delenv("UNSLOTH_STUDIO_BASE_URL", raising=False)
    args = PROVIDER.default_arguments()
    assert "api_key" not in args
    assert args["api_base"] == "http://127.0.0.1:8888/v1"
    assert args["is_chat_model"] is True
    assert args["should_use_structured_outputs"] is True


def test_list_models_raises_without_a_key(without_key):
    with patch("providers.unsloth_studio.requests.get") as get:
        with pytest.raises(RuntimeError, match="UNSLOTH_STUDIO_API_KEY"):
            PROVIDER.list_models()
    get.assert_not_called()


def test_fetch_native_models_is_none_without_a_key(without_key):
    with patch("providers.unsloth_studio.requests.get") as get:
        assert PROVIDER.fetch_native_models() is None
    get.assert_not_called()


def test_list_models_sends_the_bearer_key_and_keeps_text_models(with_key):
    payload = {"data": [
        {"id": "unsloth/chat-model", "loaded": True},
        {"id": "unsloth/text-model", "output_modalities": ["text"]},
        {"id": "unsloth/image-model", "output_modalities": ["image"]},
        {"no": "id"},
    ]}
    with patch("providers.unsloth_studio.requests.get",
               return_value=_Resp(payload)) as get:
        assert PROVIDER.list_models() == ["unsloth/chat-model", "unsloth/text-model"]
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer sk-unsloth-test"}


def test_fetch_native_models_is_none_when_unreachable(with_key):
    with patch("providers.unsloth_studio.requests.get",
               side_effect=requests.ConnectionError("down")):
        assert PROVIDER.fetch_native_models() is None


def test_needs_load_when_not_resident():
    assert needs_load({"model_identifier": "other", "context_length": 32768},
                      "wanted", 8192) is True


def test_needs_load_when_resident_with_a_smaller_window():
    assert needs_load({"model_identifier": "wanted", "context_length": 4096},
                      "wanted", 8192) is True


def test_no_load_when_resident_with_enough_context():
    assert needs_load({"active_model": "wanted", "context_length": 16384},
                      "wanted", 8192) is False


def test_no_load_when_resident_window_is_unreported():
    assert needs_load({"loaded": ["wanted"]}, "wanted", 8192) is False


def test_needs_load_reads_loaded_objects():
    status = {"loaded": [{"id": "wanted"}], "context_length": 8192}
    assert needs_load(status, "wanted", 8192) is False


def test_ensure_loaded_skips_when_resident(with_key):
    status = {"model_identifier": "m", "context_length": 32768}
    with patch("providers.unsloth_studio.requests.get", return_value=_Resp(status)), \
         patch("providers.unsloth_studio.requests.post") as post:
        PROVIDER.ensure_loaded("m", 8192)
    post.assert_not_called()


def test_ensure_loaded_loads_with_the_requested_window(with_key):
    status = {"model_identifier": "other", "context_length": 32768}
    with patch("providers.unsloth_studio.requests.get", return_value=_Resp(status)), \
         patch("providers.unsloth_studio.requests.post",
               return_value=_Resp({"status": "loaded"})) as post:
        PROVIDER.ensure_loaded("m", 8192)
    body = post.call_args.kwargs["json"]
    assert post.call_args.args[0].endswith("/api/inference/load")
    assert body["model_path"] == "m"
    assert body["max_seq_length"] == 8192
    assert body["use_saved_settings"] is True


def test_ensure_loaded_raises_when_the_load_fails(with_key):
    with patch("providers.unsloth_studio.requests.get",
               return_value=_Resp({"model_identifier": None})), \
         patch("providers.unsloth_studio.requests.post",
               return_value=_Resp(status_code=500, text="out of memory")):
        with pytest.raises(RuntimeError, match="out of memory"):
            PROVIDER.ensure_loaded("m", 8192)


def test_ensure_loaded_leaves_loading_to_studio_for_an_inference_only_key(with_key):
    with patch("providers.unsloth_studio.requests.get",
               return_value=_Resp(status_code=403)), \
         patch("providers.unsloth_studio.requests.post") as post:
        PROVIDER.ensure_loaded("m", 8192)
    post.assert_not_called()


def test_ensure_loaded_does_nothing_without_a_key(without_key):
    with patch("providers.unsloth_studio.requests.get") as get:
        PROVIDER.ensure_loaded("m", 8192)
    get.assert_not_called()


def test_prepare_llm_injects_the_key_from_the_environment(with_key):
    with patch.object(studio_mod.PROVIDER, "ensure_loaded"):
        the_llm = llm.prepare_llm(
            "unsloth_studio", "unsloth/chat-model",
            {**PROVIDER.default_arguments(), "context_window": 8192},
        )
    assert isinstance(the_llm, llm.ThinkingAwareOpenAILike)
    assert the_llm.api_key == "sk-unsloth-test"
    assert the_llm.model == "unsloth/chat-model"


def test_prepare_llm_names_the_missing_key(without_key):
    with patch.object(studio_mod.PROVIDER, "ensure_loaded"):
        with pytest.raises(RuntimeError, match="UNSLOTH_STUDIO_API_KEY"):
            llm.prepare_llm("unsloth_studio", "m", PROVIDER.default_arguments())
