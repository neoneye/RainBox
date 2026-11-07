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


def test_page_wires_every_engine_to_its_proxies():
    client = app.test_client()
    body = client.get("/demo_voice_echo").get_data(as_text=True)
    # It calls the existing STT and TTS proxy endpoints, one set per engine.
    for stt in ("whisper", "gemma4", "lfm3"):
        assert f"/demo_stt_{stt}/transcribe" in body
        assert f"/demo_stt_{stt}/health" in body
    for tts in ("kokoro", "lfm3"):
        assert f"/demo_tts_{tts}/synthesize" in body
        assert f"/demo_tts_{tts}/health" in body
        assert f"/demo_tts_{tts}/voices" in body


def test_pickers_list_the_engines():
    body = app.test_client().get("/demo_voice_echo").get_data(as_text=True)
    assert '<select id="stt"' in body and '<select id="tts"' in body
    for option in ('value="whisper">Whisper<', 'value="gemma4">gemma4<', 'value="lfm3">LFM3<',
                   'value="kokoro">Kokoro<'):
        assert option in body


def test_engines_that_take_only_wav_get_the_encoder():
    from webapp.voice_echo_views import _stt_engines, _tts_engines

    with app.test_request_context():
        stt = {e["id"]: e for e in _stt_engines()}
        tts = {e["id"]: e for e in _tts_engines()}
    assert stt["whisper"]["wav"] is False
    assert stt["gemma4"]["wav"] is True and stt["lfm3"]["wav"] is True
    assert tts["kokoro"]["speed"] is True
    assert tts["lfm3"]["speed"] is False and tts["lfm3"]["need"] == "speech"
    body = app.test_client().get("/demo_voice_echo").get_data(as_text=True)
    assert body.count("async function ppToWav") == 1


def test_nav_has_echo_link():
    client = app.test_client()
    body = client.get("/demo_voice_echo").get_data(as_text=True)
    assert ">Echo<" in body
    assert "pp-active" in body
