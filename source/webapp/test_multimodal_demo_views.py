"""Tests for webapp/multimodal_demo_views.py.

The model backend is never contacted; `requests` is monkeypatched so the
proxy route can be exercised without a running LLM server. A ModelConfig row
is seeded in the sandbox DB (conftest routes tests to rainbox_claude).
"""

import base64
import io
import json
from unittest.mock import patch

import pytest
import requests
from werkzeug.datastructures import FileStorage

from db import (
    ModelConfig,
    create_model_config_override,
    db,
    get_model_config,
    get_model_config_override,
    init_db,
    make_app,
)
from webapp.core import app
from webapp.multimodal_demo_views import (
    _audio_format,
    _build_completion_body,
)


def _file(data: bytes, filename: str, mimetype: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=filename, content_type=mimetype)


def test_audio_format_known_and_fallback():
    assert _audio_format("audio/mpeg") == "mp3"
    assert _audio_format("audio/wav") == "wav"
    assert _audio_format("audio/ogg") == "ogg"
    # Unknown MIME falls back to the subtype.
    assert _audio_format("audio/x-weird") == "x-weird"


def test_build_body_text_only():
    body = _build_completion_body("gemma", "be terse", "hello", None)
    assert body["model"] == "gemma"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    user_msg = body["messages"][1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == [{"type": "text", "text": "hello"}]


def test_build_body_omits_empty_system():
    body = _build_completion_body("gemma", "", "hi", None)
    assert all(m["role"] != "system" for m in body["messages"])


def test_build_body_image_part_is_data_url():
    raw = b"\x89PNGfake"
    body = _build_completion_body("gemma", "", "what is this", _file(raw, "x.png", "image/png"))
    parts = body["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    b64 = base64.b64encode(raw).decode("ascii")
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64}"},
    }


def test_build_body_audio_part_has_format():
    raw = b"RIFFfake"
    body = _build_completion_body("gemma", "", "transcribe", _file(raw, "c.wav", "audio/wav"))
    parts = body["messages"][-1]["content"]
    b64 = base64.b64encode(raw).decode("ascii")
    assert parts[1] == {
        "type": "input_audio",
        "input_audio": {"data": b64, "format": "wav"},
    }


@pytest.fixture
def seeded_model():
    """Seed one vision/audio model row in the sandbox DB; clean up after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        m = ModelConfig(
            provider="jan",
            model_name="gemma-multimodal-test",
            display_name="Gemma (multimodal test)",
            arguments={"api_base": "http://127.0.0.1:1337/v1", "api_key": "k"},
        )
        db.session.add(m)
        db.session.commit()
        uid = str(m.uuid)
        try:
            yield uid
        finally:
            db.session.delete(m)
            db.session.commit()


def test_page_renders_with_model_name(seeded_model):
    client = app.test_client()
    resp = client.get(f"/demo/multimodal?id={seeded_model}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "pp-nav" in body                       # shared nav included
    assert "Gemma (multimodal test)" in body      # resolved display name shown
    assert 'type="file"' in body                  # file input present
    assert 'id="system"' in body and 'id="user"' in body


def test_page_renders_not_found_for_unknown_id():
    client = app.test_client()
    # A well-formed but absent UUID.
    resp = client.get("/demo/multimodal?id=00000000-0000-0000-0000-000000000000")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "not found" in body.lower()


class FakeStreamResponse:
    """Stand-in for a streaming requests.Response."""

    def __init__(self, *, status_code=200, chunks=(), content=b"", content_type="text/event-stream"):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.content = content
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        pass


def test_complete_forwards_body_and_relays_stream(seeded_model):
    client = app.test_client()
    captured = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeStreamResponse(chunks=[
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"system": "be terse", "user": "hi"},
            content_type="multipart/form-data",
        )
        streamed = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert captured["url"] == "http://127.0.0.1:1337/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["json"]["model"] == "gemma-multimodal-test"
    assert captured["json"]["stream"] is True
    assert "Hel" in streamed and "lo" in streamed


def test_complete_404_for_unknown_model():
    client = app.test_client()
    resp = client.post(
        "/demo/multimodal/complete?id=00000000-0000-0000-0000-000000000000",
        data={"user": "hi"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 404


def test_complete_400_when_nothing_to_send(seeded_model):
    client = app.test_client()
    resp = client.post(
        f"/demo/multimodal/complete?id={seeded_model}",
        data={"system": "", "user": "   "},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


@pytest.fixture
def seeded_model_unresolvable_provider():
    """Seed a model row with no api_base and an unregistered provider id, so
    no backend base URL can be resolved at all; clean up after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        m = ModelConfig(
            provider="nonesuch",
            model_name="gemma-multimodal-unresolvable",
            display_name="Gemma (unresolvable provider)",
            arguments={},
        )
        db.session.add(m)
        db.session.commit()
        uid = str(m.uuid)
        try:
            yield uid
        finally:
            db.session.delete(m)
            db.session.commit()


def test_complete_400_when_backend_unresolvable(seeded_model_unresolvable_provider):
    client = app.test_client()
    resp = client.post(
        f"/demo/multimodal/complete?id={seeded_model_unresolvable_provider}",
        data={"user": "hi"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


@pytest.fixture
def seeded_model_ollama():
    """Seed an Ollama model row with no api_base — Ollama models rely on the
    provider's default base URL rather than a per-model override; clean up
    after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        m = ModelConfig(
            provider="ollama",
            model_name="gemma4:e4b-multimodal-test",
            display_name="Gemma4 (ollama test)",
            arguments={},
        )
        db.session.add(m)
        db.session.commit()
        uid = str(m.uuid)
        try:
            yield uid
        finally:
            db.session.delete(m)
            db.session.commit()


class _StubOllamaProvider:
    """Minimal Provider stand-in exposing only what _backend_base needs."""

    def base_url(self) -> str:
        return "http://127.0.0.1:11434"


def test_complete_resolves_ollama_base_from_provider_registry(seeded_model_ollama):
    client = app.test_client()
    captured = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        return FakeStreamResponse(chunks=[b"data: [DONE]\n\n"])

    with patch(
        "webapp.multimodal_demo_views.providers.get",
        return_value=_StubOllamaProvider(),
    ), patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model_ollama}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        )
        resp.get_data()

    assert resp.status_code == 200
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"


def test_complete_502_when_backend_unreachable(seeded_model):
    client = app.test_client()
    with patch(
        "webapp.multimodal_demo_views.requests.post",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 502
    body = resp.get_data(as_text=True)
    assert "http://127.0.0.1:1337/v1" in body
    assert "boom" in body


@pytest.fixture
def seeded_model_no_api_key():
    """Seed a model row with an api_base but no api_key; clean up after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        m = ModelConfig(
            provider="jan",
            model_name="gemma-multimodal-no-key",
            display_name="Gemma (no api_key)",
            arguments={"api_base": "http://127.0.0.1:1337/v1"},
        )
        db.session.add(m)
        db.session.commit()
        uid = str(m.uuid)
        try:
            yield uid
        finally:
            db.session.delete(m)
            db.session.commit()


def test_complete_omits_authorization_header_without_api_key(seeded_model_no_api_key):
    client = app.test_client()
    captured = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["headers"] = headers
        return FakeStreamResponse(chunks=[b"data: [DONE]\n\n"])

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model_no_api_key}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        )
        resp.get_data()

    assert "Authorization" not in captured["headers"]


def test_complete_forwards_backend_error_body(seeded_model):
    client = app.test_client()

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        return FakeStreamResponse(status_code=400, content=b'{"error":"no audio support"}',
                                  content_type="application/json")

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 400
    assert "no audio support" in resp.get_data(as_text=True)


def test_complete_does_not_write_to_db(seeded_model):
    client = app.test_client()
    a = make_app()
    init_db(a)
    with a.app_context():
        before = db.session.query(ModelConfig).count()

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        return FakeStreamResponse(chunks=[b"data: [DONE]\n\n"])

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        ).get_data()

    with a.app_context():
        after = db.session.query(ModelConfig).count()
    assert after == before


@pytest.fixture
def seeded_config_with_override():
    """Seed a ModelConfig plus one ModelConfigOverride under it; clean up
    both after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        cfg = ModelConfig(
            provider="jan",
            model_name="gemma-picker-test",
            display_name="Gemma (picker test)",
            arguments={"api_base": "http://127.0.0.1:1337/v1", "api_key": "k"},
        )
        db.session.add(cfg)
        db.session.commit()
        ov = create_model_config_override(
            model_config_uuid=cfg.uuid,
            overrides={"temperature": 0.2},
            display_name="Picker override",
        )
        cfg_uid, ov_uid = str(cfg.uuid), str(ov.uuid)
        try:
            yield cfg_uid, ov_uid
        finally:
            db.session.delete(get_model_config_override(ov.uuid))
            db.session.delete(get_model_config(cfg.uuid))
            db.session.commit()


def test_picker_tree_renders_configs_and_overrides(seeded_config_with_override):
    cfg_uid, ov_uid = seeded_config_with_override
    client = app.test_client()
    resp = client.get(f"/demo/multimodal?id={cfg_uid}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "gemma-picker-test" in body
    assert "Picker override" in body
    assert f"id={cfg_uid}" in body
    assert f"id={ov_uid}" in body


def test_selecting_override_renders_it_as_target(seeded_config_with_override):
    _cfg_uid, ov_uid = seeded_config_with_override
    client = app.test_client()
    resp = client.get(f"/demo/multimodal?id={ov_uid}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Picker override" in body


@pytest.fixture
def seeded_config_and_override_for_proxy():
    """Seed a base config (provider jan, api_base/api_key set) and an
    override that changes only api_base, so the proxy test can prove
    override resolution: base model_name + api_key win, override's
    api_base wins. Clean up both after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        cfg = ModelConfig(
            provider="jan",
            model_name="m-base",
            display_name="Base model",
            arguments={"api_base": "http://base-a/v1", "api_key": "k1"},
        )
        db.session.add(cfg)
        db.session.commit()
        ov = create_model_config_override(
            model_config_uuid=cfg.uuid,
            overrides={"api_base": "http://base-b/v1"},
            display_name="Override b",
        )
        ov_uid = str(ov.uuid)
        try:
            yield ov_uid
        finally:
            db.session.delete(get_model_config_override(ov.uuid))
            db.session.delete(get_model_config(cfg.uuid))
            db.session.commit()


def test_complete_resolves_override_to_base_model_name_and_merged_args(
    seeded_config_and_override_for_proxy,
):
    ov_uid = seeded_config_and_override_for_proxy
    client = app.test_client()
    captured = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeStreamResponse(chunks=[b"data: [DONE]\n\n"])

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={ov_uid}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        )
        resp.get_data()

    assert resp.status_code == 200
    assert captured["url"] == "http://base-b/v1/chat/completions"
    assert captured["json"]["model"] == "m-base"
    assert captured["headers"]["Authorization"] == "Bearer k1"
