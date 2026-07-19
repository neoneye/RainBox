import io
import wave

from server import create_app


def _fake_synth(text, voice, speed):
    # Return one second of silence at 24 kHz, ignoring the inputs.
    return [0.0] * 24000, 24000


def _client(synth=_fake_synth):
    app = create_app(synthesize_fn=synth)
    app.config.update(TESTING=True)
    return app.test_client()


def test_health_reports_loaded():
    resp = _client().get("/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["voices"] > 0


def test_voices_lists_catalog():
    resp = _client().get("/voices")
    body = resp.get_json()
    assert resp.status_code == 200
    ids = [v["id"] for v in body["voices"]]
    assert "af_heart" in ids


def test_tts_returns_wav_audio():
    resp = _client().post("/tts", json={"text": "hello", "voice": "af_heart", "speed": 1.0})
    assert resp.status_code == 200
    assert resp.content_type == "audio/wav"
    with wave.open(io.BytesIO(resp.data), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnframes() == 24000


def test_tts_rejects_empty_text():
    resp = _client().post("/tts", json={"text": "  ", "voice": "af_heart"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_tts_rejects_unknown_voice():
    resp = _client().post("/tts", json={"text": "hi", "voice": "bogus"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_tts_clamps_speed():
    captured = {}

    def synth(text, voice, speed):
        captured["speed"] = speed
        return [0.0] * 10, 24000

    client = _client(synth=synth)
    client.post("/tts", json={"text": "hi", "voice": "af_heart", "speed": 9.0})
    assert captured["speed"] == 2.0
    client.post("/tts", json={"text": "hi", "voice": "af_heart", "speed": 0.1})
    assert captured["speed"] == 0.5
