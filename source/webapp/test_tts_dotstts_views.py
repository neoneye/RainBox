"""Tests for webapp/tts_dotstts_views.py.

The dots.tts service is never started; `requests` is monkeypatched so the
proxy routes can be exercised without torch or a running service.
"""

import io
from unittest.mock import patch

from webapp.core import app
from webapp.tts_dotstts_views import TTS_TEMPLATE


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
    resp = client.get("/demo_tts_dotstts")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "dots.tts" in body
    assert "pp-nav" in body           # shared nav included
    assert "127.0.0.1:5007" in body   # default service URL shown


def test_template_has_no_backslash_escapes():
    # The template is a non-raw Python string: any backslash would be
    # interpreted by Python and could silently corrupt the inline JS.
    assert "\\" not in TTS_TEMPLATE


def test_health_proxy_marks_reachable():
    client = app.test_client()
    fake = FakeResponse(json_data={"status": "ok", "model_loaded": False, "voices": 2, "device": None})
    with patch("webapp.tts_dotstts_views.requests.get", return_value=fake):
        resp = client.get("/demo_tts_dotstts/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["reachable"] is True
    assert body["status"] == "ok"


def test_health_proxy_handles_unreachable():
    import requests as real_requests

    client = app.test_client()
    with patch("webapp.tts_dotstts_views.requests.get",
               side_effect=real_requests.ConnectionError("refused")):
        resp = client.get("/demo_tts_dotstts/health")
    body = resp.get_json()
    assert resp.status_code == 502
    assert body["reachable"] is False


def test_voices_proxy_forwards_library():
    client = app.test_client()
    fake = FakeResponse(json_data={"voices": [{"id": "simon", "name": "Simon", "transcript": "hi"}]})
    with patch("webapp.tts_dotstts_views.requests.get", return_value=fake):
        resp = client.get("/demo_tts_dotstts/voices")
    assert resp.status_code == 200
    assert resp.get_json()["voices"][0]["id"] == "simon"


def test_voices_proxy_forwards_create():
    client = app.test_client()
    fake = FakeResponse(json_data={"voice": {"id": "simon", "name": "Simon", "transcript": "hi"}},
                        status_code=201)
    with patch("webapp.tts_dotstts_views.requests.post", return_value=fake) as post:
        resp = client.post(
            "/demo_tts_dotstts/voices",
            data={
                "name": "Simon",
                "transcript": "hi",
                "audio": (io.BytesIO(b"RIFFfake"), "reference.wav"),
            },
        )
    assert resp.status_code == 201
    assert resp.get_json()["voice"]["id"] == "simon"
    kwargs = post.call_args.kwargs
    assert kwargs["data"]["name"] == "Simon"
    assert "audio" in kwargs["files"]


def test_voice_delete_proxy_forwards():
    client = app.test_client()
    fake = FakeResponse(json_data={"deleted": "simon"})
    with patch("webapp.tts_dotstts_views.requests.delete", return_value=fake):
        resp = client.delete("/demo_tts_dotstts/voices/simon")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == "simon"


def test_synthesize_proxy_returns_wav():
    client = app.test_client()
    fake = FakeResponse(content=b"RIFFfake", status_code=200)
    with patch("webapp.tts_dotstts_views.requests.post", return_value=fake):
        resp = client.post("/demo_tts_dotstts/synthesize",
                           json={"text": "hi", "voice": "simon"})
    assert resp.status_code == 200
    assert resp.content_type == "audio/wav"
    assert resp.data == b"RIFFfake"


def test_synthesize_proxy_passes_through_upstream_error():
    client = app.test_client()
    fake = FakeResponse(json_data={"error": "text must not be empty"}, status_code=400)
    with patch("webapp.tts_dotstts_views.requests.post", return_value=fake):
        resp = client.post("/demo_tts_dotstts/synthesize", json={"text": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "text must not be empty"


def test_synthesize_proxy_handles_unreachable():
    import requests as real_requests

    client = app.test_client()
    with patch("webapp.tts_dotstts_views.requests.post",
               side_effect=real_requests.ConnectionError("refused")):
        resp = client.post("/demo_tts_dotstts/synthesize", json={"text": "hi"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_nav_has_clone_link():
    client = app.test_client()
    resp = client.get("/demo_tts_dotstts")
    body = resp.get_data(as_text=True)
    # The shared nav links to this page and marks it active.
    assert ">Clone<" in body
    assert "pp-active" in body
