"""Standalone Whisper speech-to-text REST service.

Mirror of the sibling `kokoro_service/` (TTS): kept separate so its heavy
dependency (`faster-whisper`, which pulls in CTranslate2) never enters the main
project's venv. The main app's `/demo_stt_whisper` page talks to it over HTTP.

Run it from inside this directory (with the local venv active):
`python server.py` serves the real model on port 5006.
`create_app(transcribe_fn=...)` lets tests inject a fake transcriber so the
endpoints can be exercised without faster-whisper installed.

API:
  GET  /health      -> {"status":"ok","model_loaded":bool,"model":str}
  POST /transcribe  -> multipart form: file field "audio" (+ optional "language")
                       => {"text","language","duration"} (or {"error"} 4xx/5xx)
"""

import io
import logging
import os
import threading
from typing import BinaryIO, Callable

from flask import Flask, Response, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Default to small.en: CTranslate2 is CPU-only on Apple Silicon, and the
# large-v3-turbo *encoder* still runs a full 30s window (~5-7s/clip on an M1).
# small.en transcribes short dictation in ~1.5s and is the accuracy/latency
# sweet spot for real mic input — base.en is faster (~0.5s) but its quality
# drops noticeably on real-world audio. Override with WHISPER_MODEL
# ("large-v3-turbo" for max accuracy / multilingual, "base.en"/"tiny.en" for
# lower latency on clean speech).
MODEL_NAME = os.environ.get("WHISPER_MODEL", "small.en")

# int8 keeps memory/latency low at no measurable speed cost vs the default on
# CPU. cpu_threads defaults to every core (CTranslate2's auto setting tends to
# under-subscribe). Both are overridable.
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", os.cpu_count() or 0))

# transcribe signature: (audio: BinaryIO, language: str | None)
#   -> {"text": str, "language": str, "duration": float}
TranscribeFn = Callable[[BinaryIO, "str | None"], dict]


def create_app(transcribe_fn: TranscribeFn | None = None) -> Flask:
    """Build the Flask app. If transcribe_fn is None, the real faster-whisper
    model is loaded lazily on the first /transcribe call (so import never
    requires faster-whisper/ctranslate2)."""
    app = Flask(__name__)
    state: dict[str, TranscribeFn | None] = {"transcribe": transcribe_fn}
    # The model isn't thread-safe and the live-transcription page fires
    # overlapping rolling requests, so serialize lazy-load + transcription.
    lock = threading.Lock()

    def get_transcribe() -> TranscribeFn:
        if state["transcribe"] is None:
            state["transcribe"] = _build_faster_whisper_transcribe()
        return state["transcribe"]

    @app.route("/health")
    def health() -> Response:
        return jsonify(
            {
                "status": "ok",
                "model_loaded": state["transcribe"] is not None,
                "model": MODEL_NAME,
            }
        )

    @app.route("/transcribe", methods=["POST"])
    def transcribe() -> Response | tuple[Response, int]:
        audio = request.files.get("audio")
        if audio is None:
            return jsonify({"error": "missing 'audio' file field"}), 400
        data = audio.read()
        if not data:
            return jsonify({"error": "audio file is empty"}), 400

        language = (request.form.get("language") or "").strip() or None

        try:
            with lock:
                result = get_transcribe()(io.BytesIO(data), language)
        except Exception as e:  # pragma: no cover - real-model failure path
            logger.exception("transcription failed")
            return jsonify({"error": f"transcription failed: {e}"}), 500

        return jsonify(result)

    return app


def _build_faster_whisper_transcribe() -> TranscribeFn:  # pragma: no cover - needs ctranslate2
    """Load the faster-whisper model once and return a transcribe function.

    Decoding is done by faster-whisper's bundled PyAV/ffmpeg, so the uploaded
    bytes can be any common container the browser produces (webm/opus, wav, mp4).
    """
    from faster_whisper import WhisperModel

    # device/compute_type="auto"/"default" lets CTranslate2 pick the best
    # available backend (CUDA if present, else CPU) without hard-coding it.
    logger.info(
        "loading faster-whisper model %r (compute_type=%s, cpu_threads=%d) ...",
        MODEL_NAME, COMPUTE_TYPE, CPU_THREADS,
    )
    model = WhisperModel(
        MODEL_NAME, device="auto", compute_type=COMPUTE_TYPE, cpu_threads=CPU_THREADS
    )

    def transcribe(audio: BinaryIO, language: str | None) -> dict:
        # vad_filter drops non-speech before decoding. Without it, Whisper
        # hallucinates training-data filler ("Thank you", "Thanks for watching")
        # on silent/near-silent audio; with it, silence yields no segments ->
        # empty text, which the page surfaces as "(no speech detected)".
        segments, info = model.transcribe(
            audio, language=language, beam_size=5, vad_filter=True
        )
        text = "".join(seg.text for seg in segments).strip()
        return {
            "text": text,
            "language": getattr(info, "language", language or ""),
            "duration": round(float(getattr(info, "duration", 0.0)), 2),
        }

    return transcribe


if __name__ == "__main__":  # pragma: no cover
    create_app().run(host="127.0.0.1", port=5006)
