"""Tests for webapp/stt_lfm3_views.py.

Ollama is never called; `requests` is monkeypatched so the proxy routes can be
exercised without a running server or the model.
"""

import base64
import io
import wave
from unittest.mock import patch

import pytest
import requests as real_requests

from webapp.core import app


@pytest.fixture(autouse=True)
def no_activity_rows():
    """Keep these proxy tests off /activity: the recorder is installed with
    the app, so an unpatched call would write a row to the sandbox database.
    Tests about the row patch it again and read what it was given."""
    with patch("webapp.stt_lfm3_views.record_direct_call"):
        yield


class FakeResponse:
    def __init__(self, *, json_data=None, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def _post(prompt: str = "", raw: bytes | None = None):
    data = {"audio": (io.BytesIO(_wav() if raw is None else raw), "clip.wav"), "prompt": prompt}
    return app.test_client().post("/demo_stt_lfm3/transcribe", data=data,
                                  content_type="multipart/form-data")


def test_page_renders_the_shared_stt_page_with_its_own_proxies():
    body = app.test_client().get("/demo_stt_lfm3").get_data(as_text=True)
    assert "<h1>LFM3-Audio STT</h1>" in body
    assert "lfm3-audio:2b-mlx-bf16" in body
    assert "/demo_stt_lfm3/health" in body
    assert "/demo_stt_lfm3/transcribe" in body
    assert "demo_stt_gemma4/" not in body
    assert 'download="lfm3-clip.wav"' in body
    assert ">STT lfm3<" in body
    # The instruction starts empty: plain transcription is the default.
    assert '<textarea id="prompt" rows="2" placeholder="Empty: transcribe' in body
    assert "></textarea>" in body


def test_gemma4_page_still_renders_its_own():
    body = app.test_client().get("/demo_stt_gemma4").get_data(as_text=True)
    assert "<h1>gemma4 STT</h1>" in body
    assert "/demo_stt_gemma4/transcribe" in body
    assert "Transcribe this audio exactly.</textarea>" in body
    assert 'download="gemma4-clip.wav"' in body


def test_health_reports_audio_capability():
    fake = FakeResponse(json_data={"capabilities": ["completion", "audio", "speech"]})
    with patch("webapp.stt_lfm3_views.requests.post", return_value=fake):
        resp = app.test_client().get("/demo_stt_lfm3/health")
    assert resp.status_code == 200
    assert resp.get_json()["audio"] is True


def test_health_handles_unreachable():
    with patch("webapp.stt_lfm3_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = app.test_client().get("/demo_stt_lfm3/health")
    assert resp.status_code == 502
    assert resp.get_json()["reachable"] is False


def test_empty_instruction_uses_the_transcription_endpoint():
    fake = FakeResponse(json_data={"text": " Hej, det her er en dansk stemme. "})
    with patch("webapp.stt_lfm3_views.requests.post", return_value=fake) as post:
        resp = _post(prompt="  ")
    assert post.call_args.args[0].endswith("/v1/audio/transcriptions")
    assert post.call_args.kwargs["data"] == {"model": "lfm3-audio:2b-mlx-bf16"}
    _, raw, mime = post.call_args.kwargs["files"]["file"]
    assert raw[:4] == b"RIFF" and mime == "audio/wav"
    assert resp.status_code == 200
    assert resp.get_json() == {"text": "Hej, det her er en dansk stemme.",
                               "model": "lfm3-audio:2b-mlx-bf16", "duration": 0.5}


def test_an_instruction_uses_chat_with_the_clip_in_images():
    fake = FakeResponse(json_data={"message": {"content": "A short greeting."},
                                   "prompt_eval_count": 45})
    with patch("webapp.stt_lfm3_views.requests.post", return_value=fake) as post:
        resp = _post(prompt="Summarize this clip.")
    assert post.call_args.args[0].endswith("/api/chat")
    body = post.call_args.kwargs["json"]
    assert body["think"] is False
    assert body["messages"][0]["content"] == "Summarize this clip."
    assert base64.b64decode(body["messages"][0]["images"][0])[:4] == b"RIFF"
    assert resp.get_json()["text"] == "A short greeting."
    assert resp.get_json()["prompt_tokens"] == 45


def test_model_comes_from_env(monkeypatch):
    monkeypatch.setenv("LFM3_STT_MODEL", "other-audio:1b")
    with patch("webapp.stt_lfm3_views.requests.post",
               return_value=FakeResponse(json_data={"text": "x"})) as post:
        _post()
    assert post.call_args.kwargs["data"]["model"] == "other-audio:1b"


def test_non_wav_is_refused_without_calling_ollama():
    with patch("webapp.stt_lfm3_views.requests.post") as post:
        resp = _post(raw=b"OggS not a wav")
    assert resp.status_code == 400
    assert post.call_count == 0


def test_missing_audio_field_is_refused():
    resp = app.test_client().post("/demo_stt_lfm3/transcribe", data={"prompt": ""},
                                  content_type="multipart/form-data")
    assert resp.status_code == 400


def test_openai_style_error_is_flattened():
    fake = FakeResponse(json_data={"error": {"message": "this LFM3 model does not support  input"}},
                        status_code=400)
    with patch("webapp.stt_lfm3_views.requests.post", return_value=fake):
        resp = _post()
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "this LFM3 model does not support  input"


def test_native_error_is_passed_on():
    fake = FakeResponse(json_data={"error": "model not found"}, status_code=404)
    with patch("webapp.stt_lfm3_views.requests.post", return_value=fake):
        resp = _post(prompt="Translate to French.")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "model not found"


def test_unreachable_is_a_502():
    with patch("webapp.stt_lfm3_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = _post()
    assert resp.status_code == 502


def test_a_transcription_is_recorded_on_activity_without_the_clip():
    with patch("webapp.stt_lfm3_views.requests.post",
               return_value=FakeResponse(json_data={"text": "hello"})), \
            patch("webapp.stt_lfm3_views.record_direct_call") as record:
        _post()
    kwargs = record.call_args.kwargs
    assert kwargs["caller"] == "demo_stt_lfm3"
    content = kwargs["messages"][0]["content"]
    assert content.startswith("[transcription]")
    assert "[audio: 0.5s WAV" in content
    assert "RIFF" not in content
    assert kwargs["response"] == "hello"
