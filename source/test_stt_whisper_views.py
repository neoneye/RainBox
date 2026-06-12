"""Tests for webapp/stt_whisper_views.py.

The Whisper service is never started; `requests` is monkeypatched so the proxy
routes can be exercised without faster-whisper or a running service.
"""

import io
from unittest.mock import patch

from webapp.core import app


class FakeResponse:
    def __init__(self, *, json_data=None, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _audio_upload(data=b"RIFFfakeaudio"):
    return {"audio": (io.BytesIO(data), "clip.webm")}


def test_page_renders_with_nav_and_service_url():
    client = app.test_client()
    resp = client.get("/demo_stt_whisper")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Whisper" in body
    assert "pp-nav" in body           # shared nav included
    assert "127.0.0.1:5006" in body   # default service URL shown


def test_health_proxy_marks_reachable():
    client = app.test_client()
    fake = FakeResponse(json_data={"status": "ok", "model_loaded": True, "model": "large-v3-turbo"})
    with patch("webapp.stt_whisper_views.requests.get", return_value=fake):
        resp = client.get("/demo_stt_whisper/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["reachable"] is True
    assert body["model"] == "large-v3-turbo"


def test_health_proxy_handles_unreachable():
    import requests as real_requests

    client = app.test_client()
    with patch("webapp.stt_whisper_views.requests.get",
               side_effect=real_requests.ConnectionError("refused")):
        resp = client.get("/demo_stt_whisper/health")
    body = resp.get_json()
    assert resp.status_code == 502
    assert body["reachable"] is False


def test_transcribe_proxy_returns_text():
    client = app.test_client()
    fake = FakeResponse(json_data={"text": "hello world", "language": "en", "duration": 1.2})
    with patch("webapp.stt_whisper_views.requests.post", return_value=fake):
        resp = client.post("/demo_stt_whisper/transcribe",
                           data=_audio_upload(), content_type="multipart/form-data")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["text"] == "hello world"
    assert body["language"] == "en"


def test_transcribe_proxy_rejects_missing_audio():
    client = app.test_client()
    resp = client.post("/demo_stt_whisper/transcribe",
                       data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_transcribe_proxy_passes_through_upstream_error():
    client = app.test_client()
    fake = FakeResponse(json_data={"error": "audio file is empty"}, status_code=400)
    with patch("webapp.stt_whisper_views.requests.post", return_value=fake):
        resp = client.post("/demo_stt_whisper/transcribe",
                           data=_audio_upload(), content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "audio file is empty"


def test_transcribe_proxy_handles_unreachable():
    import requests as real_requests

    client = app.test_client()
    with patch("webapp.stt_whisper_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = client.post("/demo_stt_whisper/transcribe",
                           data=_audio_upload(), content_type="multipart/form-data")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_nav_has_stt_link():
    client = app.test_client()
    resp = client.get("/demo_stt_whisper")
    body = resp.get_data(as_text=True)
    # The shared nav links to the STT page and marks it active on this page.
    assert ">STT<" in body
    assert "pp-active" in body
