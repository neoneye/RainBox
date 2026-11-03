"""Tests for webapp/stt_gemma4_views.py.

Ollama is never called; `requests` is monkeypatched so the proxy routes can be
exercised without a running server or the model.
"""

import base64
import io
import wave
from unittest.mock import patch

import requests as real_requests

from webapp.core import app


class FakeResponse:
    def __init__(self, *, json_data=None, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _wav(seconds=2, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)
    return buf.getvalue()


def _upload(data, **form):
    return {"audio": (io.BytesIO(data), "clip.wav"), **form}


def test_page_renders_with_nav_model_and_prompt():
    body = app.test_client().get("/demo_stt_gemma4").get_data(as_text=True)
    assert "gemma4 STT" in body
    assert "pp-nav" in body
    assert "gemma4:e4b-mlx" in body
    assert "127.0.0.1:11434" in body
    assert "Transcribe this audio exactly." in body
    assert "STT gemma4" in body          # the Voice menu links the page


def test_model_and_server_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMMA4_STT_MODEL", "gemma4:e2b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999/")
    body = app.test_client().get("/demo_stt_gemma4").get_data(as_text=True)
    assert "gemma4:e2b" in body
    assert "http://127.0.0.1:9999 " in body


def test_health_reports_the_audio_capability():
    fake = FakeResponse(json_data={"capabilities": ["completion", "vision", "audio"]})
    with patch("webapp.stt_gemma4_views.requests.post", return_value=fake) as post:
        resp = app.test_client().get("/demo_stt_gemma4/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["reachable"] is True and body["audio"] is True
    assert post.call_args.kwargs["json"] == {"model": "gemma4:e4b-mlx"}
    assert post.call_args.args[0].endswith("/api/show")


def test_health_says_when_the_model_cannot_hear():
    fake = FakeResponse(json_data={"capabilities": ["completion"]})
    with patch("webapp.stt_gemma4_views.requests.post", return_value=fake):
        body = app.test_client().get("/demo_stt_gemma4/health").get_json()
    assert body["reachable"] is True and body["audio"] is False


def test_health_relays_a_missing_model():
    fake = FakeResponse(json_data={"error": "model 'gemma4:e4b-mlx' not found"},
                        status_code=404)
    with patch("webapp.stt_gemma4_views.requests.post", return_value=fake):
        resp = app.test_client().get("/demo_stt_gemma4/health")
    assert resp.status_code == 502
    assert "not found" in resp.get_json()["error"]


def test_health_handles_unreachable():
    with patch("webapp.stt_gemma4_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = app.test_client().get("/demo_stt_gemma4/health")
    assert resp.status_code == 502
    assert resp.get_json()["reachable"] is False


def test_transcribe_sends_the_wav_as_an_image_part_of_the_message():
    raw = _wav(seconds=2)
    fake = FakeResponse(json_data={"message": {"content": " The shed is blue. "},
                                   "prompt_eval_count": 80})
    with patch("webapp.stt_gemma4_views.requests.post", return_value=fake) as post:
        resp = app.test_client().post(
            "/demo_stt_gemma4/transcribe", data=_upload(raw, prompt="Translate to French."),
            content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json() == {"text": "The shed is blue.", "model": "gemma4:e4b-mlx",
                               "duration": 2.0, "prompt_tokens": 80}
    sent = post.call_args.kwargs["json"]
    assert post.call_args.args[0].endswith("/api/chat")
    assert sent["model"] == "gemma4:e4b-mlx"
    assert sent["stream"] is False and sent["think"] is False
    message = sent["messages"][0]
    assert message["content"] == "Translate to French."
    assert base64.b64decode(message["images"][0]) == raw


def test_an_empty_prompt_falls_back_to_transcribing():
    fake = FakeResponse(json_data={"message": {"content": "ok"}})
    with patch("webapp.stt_gemma4_views.requests.post", return_value=fake) as post:
        app.test_client().post("/demo_stt_gemma4/transcribe",
                               data=_upload(_wav(), prompt="  "),
                               content_type="multipart/form-data")
    assert post.call_args.kwargs["json"]["messages"][0]["content"] == \
        "Transcribe this audio exactly."


def test_a_clip_that_is_not_wav_is_refused_before_ollama():
    with patch("webapp.stt_gemma4_views.requests.post") as post:
        resp = app.test_client().post("/demo_stt_gemma4/transcribe",
                                      data=_upload(b"\x1aE\xdf\xa3webm"),
                                      content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "WAV" in resp.get_json()["error"]
    post.assert_not_called()


def test_missing_audio_is_a_400():
    resp = app.test_client().post("/demo_stt_gemma4/transcribe", data={},
                                  content_type="multipart/form-data")
    assert resp.status_code == 400


def test_an_ollama_error_is_relayed():
    fake = FakeResponse(json_data={"error": "media expansion produced no tokens"},
                        status_code=400)
    with patch("webapp.stt_gemma4_views.requests.post", return_value=fake):
        resp = app.test_client().post("/demo_stt_gemma4/transcribe",
                                      data=_upload(_wav()),
                                      content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "media expansion produced no tokens"


def test_transcribe_handles_unreachable():
    with patch("webapp.stt_gemma4_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = app.test_client().post("/demo_stt_gemma4/transcribe",
                                      data=_upload(_wav()),
                                      content_type="multipart/form-data")
    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]
