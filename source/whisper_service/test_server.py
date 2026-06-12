import io

from server import MODEL_NAME, create_app


def _fake_transcribe(audio, language):
    # Echo back deterministic data; ignore the actual bytes.
    return {
        "text": "hello world",
        "language": language or "en",
        "duration": 1.23,
    }


def _client(transcribe=_fake_transcribe):
    app = create_app(transcribe_fn=transcribe)
    app.config.update(TESTING=True)
    return app.test_client()


def _wav_upload(data=b"RIFFfakeaudio"):
    return {"audio": (io.BytesIO(data), "clip.webm")}


def test_health_reports_loaded_and_model():
    resp = _client().get("/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["model"] == MODEL_NAME


def test_transcribe_returns_text():
    resp = _client().post(
        "/transcribe", data=_wav_upload(), content_type="multipart/form-data"
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["text"] == "hello world"
    assert body["language"] == "en"
    assert body["duration"] == 1.23


def test_transcribe_forwards_language():
    captured = {}

    def transcribe(audio, language):
        captured["language"] = language
        return {"text": "", "language": language or "", "duration": 0.0}

    client = _client(transcribe=transcribe)
    data = {"audio": (io.BytesIO(b"RIFFfakeaudio"), "clip.webm"), "language": "da"}
    client.post("/transcribe", data=data, content_type="multipart/form-data")
    assert captured["language"] == "da"


def test_transcribe_rejects_missing_file():
    resp = _client().post("/transcribe", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_transcribe_rejects_empty_file():
    resp = _client().post(
        "/transcribe",
        data={"audio": (io.BytesIO(b""), "clip.webm")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
