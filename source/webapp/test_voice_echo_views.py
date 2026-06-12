"""Tests for webapp/voice_echo_views.py.

The page reuses the STT and TTS proxy routes (covered by their own test files),
so here we only verify the combined page renders and wires up both services.
"""

from webapp.core import app


def test_page_renders_with_nav():
    client = app.test_client()
    resp = client.get("/demo_voice_echo")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Voice echo" in body
    assert "pp-nav" in body


def test_page_wires_both_services():
    client = app.test_client()
    body = client.get("/demo_voice_echo").get_data(as_text=True)
    # It calls the existing STT and TTS proxy endpoints.
    assert "/demo_stt_whisper/transcribe" in body
    assert "/demo_tts_kokoro/synthesize" in body
    assert "/demo_stt_whisper/health" in body
    assert "/demo_tts_kokoro/health" in body
    assert "/demo_tts_kokoro/voices" in body


def test_nav_has_echo_link():
    client = app.test_client()
    body = client.get("/demo_voice_echo").get_data(as_text=True)
    assert ">Echo<" in body
    assert "pp-active" in body
