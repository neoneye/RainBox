"""Tests for webapp/tts_lfm3_views.py.

Ollama is never called; `requests` is monkeypatched so the proxy routes can be
exercised without a running server or the model.
"""

import io
import struct
import wave
from unittest.mock import patch

import pytest
import requests as real_requests

from webapp.core import app
from webapp.tts_lfm3_views import _fix_wav_sizes, _voice_label


@pytest.fixture(autouse=True)
def no_activity_rows():
    """Keep these proxy tests off /activity: the recorder is installed with
    the app, so an unpatched call would write a row to the sandbox database.
    Tests about the row patch it again and read what it was given."""
    with patch("webapp.tts_lfm3_views.record_direct_call"):
        yield


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _streamed_wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    """A WAV the way Ollama streams it: RIFF and data sizes are 0xFFFFFFFF."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    raw = bytearray(buf.getvalue())
    struct.pack_into("<I", raw, 4, 0xFFFFFFFF)
    struct.pack_into("<I", raw, 40, 0xFFFFFFFF)  # data size, after a 16-byte fmt chunk
    return bytes(raw)


SHOW = {"capabilities": ["completion", "audio", "speech"],
        "voices": ["default", "female-american-english", "male-british-english"]}


def test_page_renders_with_nav_model_and_ollama_url():
    resp = app.test_client().get("/demo_tts_lfm3")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "LFM3" in body
    assert "pp-nav" in body
    assert "lfm3-audio:2b-mlx-bf16" in body
    assert "127.0.0.1:11434" in body
    assert ">TTS lfm3<" in body


def test_model_comes_from_env(monkeypatch):
    monkeypatch.setenv("LFM3_TTS_MODEL", "other-speech:1b")
    with patch("webapp.tts_lfm3_views.requests.post",
               return_value=FakeResponse(content=_streamed_wav())) as post:
        app.test_client().post("/demo_tts_lfm3/synthesize", json={"text": "hi", "model": "evil"})
    assert post.call_args.kwargs["json"]["model"] == "other-speech:1b"


def test_health_reports_speech_capability():
    with patch("webapp.tts_lfm3_views.requests.post", return_value=FakeResponse(json_data=SHOW)):
        resp = app.test_client().get("/demo_tts_lfm3/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["reachable"] is True
    assert body["speech"] is True
    assert body["voices"] == 3


def test_health_handles_unreachable():
    with patch("webapp.tts_lfm3_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = app.test_client().get("/demo_tts_lfm3/health")
    assert resp.status_code == 502
    assert resp.get_json()["reachable"] is False


def test_health_handles_missing_model():
    fake = FakeResponse(json_data={"error": "model not found"}, status_code=404)
    with patch("webapp.tts_lfm3_views.requests.post", return_value=fake):
        resp = app.test_client().get("/demo_tts_lfm3/health")
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "model not found"


def test_voices_come_from_show_with_readable_names():
    with patch("webapp.tts_lfm3_views.requests.post", return_value=FakeResponse(json_data=SHOW)):
        resp = app.test_client().get("/demo_tts_lfm3/voices")
    assert resp.status_code == 200
    assert resp.get_json()["voices"] == [
        {"id": "default", "name": "default"},
        {"id": "female-american-english", "name": "American English (female)"},
        {"id": "male-british-english", "name": "British English (male)"},
    ]


def test_voice_label_leaves_unknown_shapes_alone():
    assert _voice_label("male-french") == "French (male)"
    assert _voice_label("narrator") == "narrator"
    assert _voice_label("robot-voice") == "robot-voice"


def test_synthesize_posts_openai_speech_and_fixes_wav_sizes():
    streamed = _streamed_wav(0.5)
    with patch("webapp.tts_lfm3_views.requests.post",
               return_value=FakeResponse(content=streamed)) as post:
        resp = app.test_client().post("/demo_tts_lfm3/synthesize",
                                      json={"text": " hi ", "voice": "male-french"})
    assert post.call_args.args[0].endswith("/v1/audio/speech")
    assert post.call_args.kwargs["json"] == {"model": "lfm3-audio:2b-mlx-bf16", "input": "hi",
                                             "response_format": "wav", "voice": "male-french"}
    assert resp.status_code == 200
    assert resp.content_type == "audio/wav"
    assert resp.headers["X-Audio-Seconds"] == "0.5"
    with wave.open(io.BytesIO(resp.data)) as w:
        assert w.getnframes() == 12000


def test_synthesize_without_voice_lets_ollama_pick_its_default():
    with patch("webapp.tts_lfm3_views.requests.post",
               return_value=FakeResponse(content=_streamed_wav())) as post:
        app.test_client().post("/demo_tts_lfm3/synthesize", json={"text": "hi", "voice": ""})
    assert "voice" not in post.call_args.kwargs["json"]


def test_synthesize_refuses_empty_text_without_calling_ollama():
    with patch("webapp.tts_lfm3_views.requests.post") as post:
        resp = app.test_client().post("/demo_tts_lfm3/synthesize", json={"text": "  "})
    assert resp.status_code == 400
    assert post.call_count == 0


def test_synthesize_flattens_openai_style_error():
    fake = FakeResponse(json_data={"error": {"message": 'voice "nope" is not supported'}},
                        status_code=400)
    with patch("webapp.tts_lfm3_views.requests.post", return_value=fake):
        resp = app.test_client().post("/demo_tts_lfm3/synthesize",
                                      json={"text": "hi", "voice": "nope"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == 'voice "nope" is not supported'


def test_synthesize_handles_unreachable():
    with patch("webapp.tts_lfm3_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = app.test_client().post("/demo_tts_lfm3/synthesize", json={"text": "hi"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_a_synthesis_is_recorded_on_activity_without_the_audio():
    with patch("webapp.tts_lfm3_views.requests.post",
               return_value=FakeResponse(content=_streamed_wav(0.5))), \
            patch("webapp.tts_lfm3_views.record_direct_call") as record:
        app.test_client().post("/demo_tts_lfm3/synthesize",
                               json={"text": "hello", "voice": "female-danish"})
    kwargs = record.call_args.kwargs
    assert kwargs["caller"] == "demo_tts_lfm3"
    assert "female-danish" in kwargs["messages"][0]["content"]
    assert "hello" in kwargs["messages"][0]["content"]
    assert kwargs["response"].startswith("[audio: 0.5s WAV")


def test_fix_wav_sizes_passes_non_wav_through():
    assert _fix_wav_sizes(b"not a wav") == b"not a wav"
