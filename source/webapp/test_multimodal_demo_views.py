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
