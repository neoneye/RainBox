import io
import wave

from server import create_app
from voices import create_voice

WAV = b"RIFF....WAVEfake"


def _fake_synth(text, reference_wav_path, transcript, opts):
    # One second of silence at 48 kHz, ignoring the inputs.
    return [0.0] * 48000, 48000


def _client(tmp_path, synth=_fake_synth, with_voice=True):
    if with_voice:
        create_voice(tmp_path, "Test Voice", WAV, "reference transcript")
    app = create_app(synthesize_fn=synth, voices_dir=tmp_path)
    app.config.update(TESTING=True)
    return app.test_client()


def test_health_reports_loaded(tmp_path):
    resp = _client(tmp_path).get("/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["voices"] == 1


def test_voices_lists_library(tmp_path):
    resp = _client(tmp_path).get("/voices")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["voices"] == [
        {"id": "test-voice", "name": "Test Voice", "transcript": "reference transcript"}
    ]


def test_voices_create(tmp_path):
    client = _client(tmp_path, with_voice=False)
    resp = client.post(
        "/voices",
        data={
            "name": "New Voice",
            "transcript": "spoken words",
            "audio": (io.BytesIO(WAV), "ref.wav"),
        },
    )
    assert resp.status_code == 201
    assert resp.get_json()["voice"]["id"] == "new-voice"
    ids = [v["id"] for v in client.get("/voices").get_json()["voices"]]
    assert ids == ["new-voice"]


def test_voices_create_rejects_missing_fields(tmp_path):
    client = _client(tmp_path, with_voice=False)
    resp = client.post("/voices", data={"name": "x", "transcript": "y"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    resp = client.post(
        "/voices", data={"name": "", "audio": (io.BytesIO(WAV), "ref.wav"), "transcript": "y"}
    )
    assert resp.status_code == 400


def test_voices_delete(tmp_path):
    client = _client(tmp_path)
    resp = client.delete("/voices/test-voice")
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == "test-voice"
    assert client.get("/voices").get_json()["voices"] == []
    assert client.delete("/voices/test-voice").status_code == 404


def test_tts_returns_wav_audio(tmp_path):
    resp = _client(tmp_path).post("/tts", json={"text": "hello", "voice": "test-voice"})
    assert resp.status_code == 200
    assert resp.content_type == "audio/wav"
    with wave.open(io.BytesIO(resp.data), "rb") as w:
        assert w.getframerate() == 48000
        assert w.getnframes() == 48000


def test_tts_passes_reference_and_options(tmp_path):
    captured = {}

    def synth(text, reference_wav_path, transcript, opts):
        captured.update(
            text=text, path=reference_wav_path, transcript=transcript, opts=opts
        )
        return [0.0] * 10, 48000

    client = _client(tmp_path, synth=synth)
    client.post(
        "/tts",
        json={"text": "hi", "voice": "test-voice", "seed": 7, "num_steps": 99},
    )
    assert captured["text"] == "hi"
    assert captured["path"].endswith("test-voice/reference.wav")
    assert captured["transcript"] == "reference transcript"
    assert captured["opts"].seed == 7
    assert captured["opts"].num_steps == 32  # clamped to the max
    assert captured["opts"].guidance_scale == 1.2


def test_tts_rejects_empty_text(tmp_path):
    resp = _client(tmp_path).post("/tts", json={"text": "  ", "voice": "test-voice"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_tts_rejects_unknown_voice(tmp_path):
    resp = _client(tmp_path).post("/tts", json={"text": "hi", "voice": "bogus"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_tts_rejects_unparsable_params(tmp_path):
    client = _client(tmp_path)
    for payload in (
        {"seed": "abc"},
        {"num_steps": "abc"},
        {"guidance_scale": "abc"},
        {"speaker_scale": []},
    ):
        resp = client.post("/tts", json={"text": "hi", "voice": "test-voice", **payload})
        assert resp.status_code == 400, payload
        assert "error" in resp.get_json()
