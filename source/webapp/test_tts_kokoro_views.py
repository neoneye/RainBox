"""Tests for webapp/tts_kokoro_views.py.

The Kokoro service is never started; `requests` is monkeypatched so the proxy
routes can be exercised without torch or a running service.
"""

from unittest.mock import patch

from webapp.core import app


class FakeResponse:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def test_page_renders_with_nav_and_service_url():
    client = app.test_client()
    resp = client.get("/demo_tts_kokoro")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Kokoro" in body
    assert "pp-nav" in body           # shared nav included
    assert "127.0.0.1:5005" in body   # default service URL shown


def test_health_proxy_marks_reachable():
    client = app.test_client()
    fake = FakeResponse(json_data={"status": "ok", "model_loaded": True, "voices": 7})
    with patch("webapp.tts_kokoro_views.requests.get", return_value=fake):
        resp = client.get("/demo_tts_kokoro/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["reachable"] is True
    assert body["status"] == "ok"


def test_health_proxy_handles_unreachable():
    import requests as real_requests

    client = app.test_client()
    with patch("webapp.tts_kokoro_views.requests.get",
               side_effect=real_requests.ConnectionError("refused")):
        resp = client.get("/demo_tts_kokoro/health")
    body = resp.get_json()
    assert resp.status_code == 502
    assert body["reachable"] is False


def test_voices_proxy_forwards_catalog():
    client = app.test_client()
    fake = FakeResponse(json_data={"voices": [{"id": "af_heart", "name": "Heart (F)", "lang": "American English"}]})
    with patch("webapp.tts_kokoro_views.requests.get", return_value=fake):
        resp = client.get("/demo_tts_kokoro/voices")
    assert resp.status_code == 200
    assert resp.get_json()["voices"][0]["id"] == "af_heart"


def test_synthesize_proxy_returns_wav():
    client = app.test_client()
    fake = FakeResponse(content=b"RIFFfake", status_code=200)
    with patch("webapp.tts_kokoro_views.requests.post", return_value=fake):
        resp = client.post("/demo_tts_kokoro/synthesize",
                           json={"text": "hi", "voice": "af_heart", "speed": 1.0})
    assert resp.status_code == 200
    assert resp.content_type == "audio/wav"
    assert resp.data == b"RIFFfake"


def test_synthesize_proxy_passes_through_upstream_error():
    client = app.test_client()
    fake = FakeResponse(json_data={"error": "text must not be empty"}, status_code=400)
    with patch("webapp.tts_kokoro_views.requests.post", return_value=fake):
        resp = client.post("/demo_tts_kokoro/synthesize", json={"text": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "text must not be empty"


def test_synthesize_proxy_handles_unreachable():
    import requests as real_requests

    client = app.test_client()
    with patch("webapp.tts_kokoro_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = client.post("/demo_tts_kokoro/synthesize", json={"text": "hi"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_nav_has_tts_link():
    client = app.test_client()
    resp = client.get("/demo_tts_kokoro")
    body = resp.get_data(as_text=True)
    # The shared nav links to the TTS page and marks it active on this page.
    assert ">TTS<" in body
    assert "pp-active" in body
