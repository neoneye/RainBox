"""Standalone dots.tts-soar voice-cloning TTS REST service.

Run it from inside this directory (with the local venv active):
`python server.py` serves the real model on port 5007.
`create_app(synthesize_fn=..., voices_dir=...)` lets tests inject a fake
synth and a temporary voice library so the endpoints can be exercised
without torch/dots.tts installed.

API:
  GET    /health       -> {"status":"ok","model_loaded":bool,"voices":int,"device":str|null}
  GET    /voices       -> {"voices":[{"id","name","transcript"}, ...]}
  POST   /voices       multipart form: name, transcript, audio (WAV file)
                       -> 201 {"voice":{...}} (or {"error"} 4xx)
  DELETE /voices/<id>  -> {"deleted":id} (or {"error"} 404)
  POST   /tts          {"text","voice",seed?,num_steps?,guidance_scale?,
                        speaker_scale?} => audio/wav (or {"error"} 4xx)
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from flask import Flask, Response, jsonify, request

from audio import float_to_wav_bytes
from voices import create_voice, delete_voice, get_voice, list_voices

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MODEL_NAME = "rednote-hilab/dots.tts-soar"

SEED_MIN, SEED_MAX = 1, 1000
NUM_STEPS_MIN, NUM_STEPS_MAX = 10, 32
DEFAULT_NUM_STEPS = 12
DEFAULT_GUIDANCE_SCALE = 1.2
DEFAULT_SPEAKER_SCALE = 1.5


@dataclass
class SynthOptions:
    """Tuning parameters forwarded to the model."""

    seed: int | None = None
    num_steps: int = DEFAULT_NUM_STEPS
    guidance_scale: float = DEFAULT_GUIDANCE_SCALE
    speaker_scale: float = DEFAULT_SPEAKER_SCALE


# synth signature:
# (text, reference_wav_path, reference_transcript, options) -> (samples, sample_rate)
SynthFn = Callable[[str, str, str, SynthOptions], tuple]

DEFAULT_VOICES_DIR = Path(__file__).parent / "voices_data"


def _parse_options(body: dict) -> SynthOptions | str:
    """Build SynthOptions from a request body. Returns an error string when a
    value cannot be parsed as a number; out-of-range values are clamped."""
    opts = SynthOptions()

    seed = body.get("seed")
    if seed is not None:
        try:
            opts.seed = max(SEED_MIN, min(SEED_MAX, int(seed)))
        except (TypeError, ValueError):
            return f"seed must be an integer, got: {seed!r}"

    num_steps = body.get("num_steps")
    if num_steps is not None:
        try:
            opts.num_steps = max(NUM_STEPS_MIN, min(NUM_STEPS_MAX, int(num_steps)))
        except (TypeError, ValueError):
            return f"num_steps must be an integer, got: {num_steps!r}"

    for field in ("guidance_scale", "speaker_scale"):
        value = body.get(field)
        if value is not None:
            try:
                setattr(opts, field, float(value))
            except (TypeError, ValueError):
                return f"{field} must be a number, got: {value!r}"

    return opts


def create_app(synthesize_fn: SynthFn | None = None, voices_dir: Path | str | None = None) -> Flask:
    """Build the Flask app. If synthesize_fn is None, the real dots.tts
    runtime is loaded lazily on first /tts call (so import never requires
    torch)."""
    app = Flask(__name__)
    base = Path(voices_dir) if voices_dir is not None else DEFAULT_VOICES_DIR
    state: dict = {"synth": synthesize_fn, "device": None}

    def get_synth() -> SynthFn:
        if state["synth"] is None:
            state["synth"] = _build_dots_synth(state)
        return state["synth"]

    @app.route("/health")
    def health() -> Response:
        return jsonify(
            {
                "status": "ok",
                "model_loaded": state["synth"] is not None,
                "voices": len(list_voices(base)),
                "device": state["device"],
            }
        )

    @app.route("/voices")
    def voices() -> Response:
        listed = [
            {"id": v["id"], "name": v["name"], "transcript": v["transcript"]}
            for v in list_voices(base)
        ]
        return jsonify({"voices": listed})

    @app.route("/voices", methods=["POST"])
    def voices_create() -> tuple[Response, int]:
        name = (request.form.get("name") or "").strip()
        transcript = (request.form.get("transcript") or "").strip()
        audio_file = request.files.get("audio")
        wav_bytes = audio_file.read() if audio_file is not None else b""
        try:
            voice = create_voice(base, name, wav_bytes, transcript)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        logger.info("created voice %s (%d bytes reference)", voice["id"], len(wav_bytes))
        return (
            jsonify(
                {
                    "voice": {
                        "id": voice["id"],
                        "name": voice["name"],
                        "transcript": voice["transcript"],
                    }
                }
            ),
            201,
        )

    @app.route("/voices/<voice_id>", methods=["DELETE"])
    def voices_delete(voice_id: str) -> Response | tuple[Response, int]:
        if not delete_voice(base, voice_id):
            return jsonify({"error": f"unknown voice: {voice_id}"}), 404
        logger.info("deleted voice %s", voice_id)
        return jsonify({"deleted": voice_id})

    @app.route("/tts", methods=["POST"])
    def tts() -> Response | tuple[Response, int]:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        voice_id = (body.get("voice") or "").strip()

        if not text:
            return jsonify({"error": "text must not be empty"}), 400
        voice = get_voice(base, voice_id) if voice_id else None
        if voice is None:
            return jsonify({"error": f"unknown voice: {voice_id}"}), 400
        opts = _parse_options(body)
        if isinstance(opts, str):
            return jsonify({"error": opts}), 400

        try:
            samples, sample_rate = get_synth()(
                text, voice["reference_path"], voice["transcript"], opts
            )
        except Exception as e:  # pragma: no cover - real-model failure path
            logger.exception("synthesis failed")
            return jsonify({"error": f"synthesis failed: {e}"}), 500

        wav = float_to_wav_bytes(samples, sample_rate=sample_rate)
        return Response(wav, status=200, content_type="audio/wav")

    return app


def _build_dots_synth(state: dict) -> SynthFn:  # pragma: no cover - requires dots.tts
    """Construct the real dots.tts runtime and return a synth function.
    `state["device"]` is kept up to date for /health.

    The runtime itself only knows cuda-vs-cpu. Without CUDA it insists on
    float32 and pins torch to a single thread; on Apple Silicon we widen the
    thread count, move the model to MPS, and transparently fall back to CPU
    (moving it back) the first time an MPS synthesis fails.
    """
    import os

    import torch
    from dots_tts.runtime import DotsTtsRuntime
    from dots_tts.utils.util import seed_everything

    use_cuda = torch.cuda.is_available()
    precision = "bfloat16" if use_cuda else "float32"
    logger.info("loading %s (precision=%s)...", MODEL_NAME, precision)
    # optimize=True switches the flow-matching solver to its cached/compiled
    # path — measured 5x faster on MPS (the FM stage dominates synthesis time)
    # with numerically identical output. Without CUDA the built-in warmup is
    # skipped, so the first request pays a one-off compile cost instead.
    runtime = DotsTtsRuntime.from_pretrained(MODEL_NAME, precision=precision, optimize=True)
    if not use_cuda:
        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 2))
        if torch.backends.mps.is_available():
            runtime.model = runtime.model.to("mps")
            runtime.device = torch.device("mps")
    state["device"] = runtime.device.type
    logger.info("model loaded (device=%s)", runtime.device.type)

    def generate(text: str, reference_wav_path: str, transcript: str, opts: SynthOptions):
        if opts.seed is not None:
            seed_everything(opts.seed)
        return runtime.generate(
            text=text,
            prompt_audio_path=reference_wav_path,
            prompt_text=transcript,
            num_steps=opts.num_steps,
            guidance_scale=opts.guidance_scale,
            speaker_scale=opts.speaker_scale,
        )

    def synth(text: str, reference_wav_path: str, transcript: str, opts: SynthOptions):
        try:
            result = generate(text, reference_wav_path, transcript, opts)
        except Exception:
            if runtime.device.type != "mps":
                raise
            logger.exception("MPS synthesis failed; falling back to CPU")
            runtime.model = runtime.model.to("cpu")
            runtime.device = torch.device("cpu")
            state["device"] = "cpu"
            result = generate(text, reference_wav_path, transcript, opts)
        samples = result["audio"].float().cpu().squeeze().tolist()
        return samples, int(result["sample_rate"])

    return synth


if __name__ == "__main__":  # pragma: no cover
    create_app().run(host="127.0.0.1", port=5007)
