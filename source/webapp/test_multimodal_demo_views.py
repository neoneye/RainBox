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
from werkzeug.datastructures import FileStorage

from db import ModelConfig, db, init_db, make_app
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
