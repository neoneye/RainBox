"""Standalone Kokoro-82M TTS REST service.

Run it from inside this directory (with the local venv active):
`python server.py` serves the real model on port 5005.
`create_app(synthesize_fn=...)` lets tests inject a fake synth so the
endpoints can be exercised without torch/kokoro installed.

API:
  GET  /health  -> {"status":"ok","model_loaded":bool,"voices":int}
  GET  /voices  -> {"voices":[{"id","name","lang"}, ...]}
  POST /tts     -> {"text","voice","speed"} => audio/wav (or {"error"} 4xx)
"""

import logging
from typing import Callable

from flask import Flask, Response, jsonify, request

from audio import SAMPLE_RATE, float_to_wav_bytes
from voices import VOICES, is_valid_voice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MIN_SPEED = 0.5
MAX_SPEED = 2.0
DEFAULT_VOICE = "af_heart"

# synth signature: (text: str, voice: str, speed: float) -> (samples, sample_rate)
SynthFn = Callable[[str, str, float], tuple]


def _clamp_speed(value: object) -> float:
    try:
        speed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, speed))


def create_app(synthesize_fn: SynthFn | None = None) -> Flask:
    """Build the Flask app. If synthesize_fn is None, the real Kokoro pipeline
    is loaded lazily on first /tts call (so import never requires torch)."""
    app = Flask(__name__)
    state: dict[str, SynthFn | None] = {"synth": synthesize_fn}

    def get_synth() -> SynthFn:
        if state["synth"] is None:
            state["synth"] = _build_kokoro_synth()
        return state["synth"]

    @app.route("/health")
    def health() -> Response:
        return jsonify(
            {"status": "ok", "model_loaded": state["synth"] is not None, "voices": len(VOICES)}
        )

    @app.route("/voices")
    def voices() -> Response:
        return jsonify({"voices": VOICES})

    @app.route("/tts", methods=["POST"])
    def tts() -> Response | tuple[Response, int]:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        voice = body.get("voice") or DEFAULT_VOICE
        speed = _clamp_speed(body.get("speed", 1.0))

        if not text:
            return jsonify({"error": "text must not be empty"}), 400
        if not is_valid_voice(voice):
            return jsonify({"error": f"unknown voice: {voice}"}), 400

        try:
            samples, sample_rate = get_synth()(text, voice, speed)
        except Exception as e:  # pragma: no cover - real-model failure path
            logger.exception("synthesis failed")
            return jsonify({"error": f"synthesis failed: {e}"}), 500

        wav = float_to_wav_bytes(samples, sample_rate=sample_rate)
        return Response(wav, status=200, content_type="audio/wav")

    return app


def _build_kokoro_synth() -> SynthFn:  # pragma: no cover - requires torch/kokoro
    """Construct the real Kokoro pipeline and return a synth function."""
    from kokoro import KPipeline

    logger.info("loading Kokoro KPipeline (lang_code='a')...")
    pipeline = KPipeline(lang_code="a")

    def synth(text: str, voice: str, speed: float):
        chunks: list[float] = []
        for _gs, _ps, audio in pipeline(text, voice=voice, speed=speed):
            chunks.extend(audio.detach().cpu().tolist())
        return chunks, SAMPLE_RATE

    return synth


if __name__ == "__main__":  # pragma: no cover
    create_app().run(host="127.0.0.1", port=5005)
