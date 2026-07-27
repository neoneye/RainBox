"""Tests for default_chat_model_uuid / chat_model_choices (db.model_config).

The Ollama-first pick is tested against fabricated config/override rows
(list_model_configs_with_overrides monkeypatched), so the shared live DB's
contents cannot influence the ordering under test. No DB access is needed.
"""

from types import SimpleNamespace
from uuid import uuid4

from db import model_config


def _cfg(provider: str, name: str, available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=uuid4(), provider=provider, effective_display_name=name,
        available=available,
    )


def _override(name: str) -> SimpleNamespace:
    return SimpleNamespace(uuid=uuid4(), effective_display_name=name)


def test_default_chat_model_uuid_prefers_ollama(monkeypatch):
    """Provider preference beats alphabetical label order."""
    ollama = _cfg("ollama", "llama3")
    jan = _cfg("jan", "Qwen")
    ollama_override = _override("zeta")
    jan_override = _override("Alpha")
    monkeypatch.setattr(
        model_config, "list_model_configs_with_overrides",
        lambda **kw: [(jan, [jan_override]), (ollama, [ollama_override])],
    )
    assert model_config.default_chat_model_uuid() == ollama_override.uuid


def test_default_chat_model_uuid_sorts_labels_within_provider(monkeypatch):
    """Config and override labels break ties inside one provider."""
    later = _cfg("ollama", "Zulu")
    earlier = _cfg("ollama", "Alpha")
    later_override = _override("A")
    earlier_override = _override("Z")
    monkeypatch.setattr(
        model_config,
        "list_model_configs_with_overrides",
        lambda **kw: [
            (later, [later_override]),
            (earlier, [earlier_override]),
        ],
    )
    assert model_config.default_chat_model_uuid() == earlier_override.uuid


def test_default_chat_model_uuid_none_without_overrides(monkeypatch):
    """Base configs alone don't qualify — only overrides are candidates."""
    monkeypatch.setattr(
        model_config, "list_model_configs_with_overrides",
        lambda **kw: [(_cfg("ollama", "llama3"), [])],
    )
    assert model_config.default_chat_model_uuid() is None


def test_chat_model_choices_flattens_configs_and_overrides(monkeypatch):
    cfg = _cfg("lm_studio", "gemma", available=False)
    ov = _override("fast")
    monkeypatch.setattr(
        model_config, "list_model_configs_with_overrides",
        lambda **kw: [(cfg, [ov])],
    )
    choices = model_config.chat_model_choices()
    assert choices == [
        {"uuid": str(cfg.uuid), "label": "lm_studio · gemma", "available": False},
        {"uuid": str(ov.uuid), "label": "lm_studio · gemma — fast", "available": False},
    ]
