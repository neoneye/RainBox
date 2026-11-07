"""Demo page for speech-to-text with LFM3-Audio on Ollama.

The LFM3 counterpart of `stt_gemma4_views.py`, and the same page: it renders
that module's STT_TEMPLATE (mic picker, level meter, live passes, file upload,
the browser's re-encode to 16 kHz mono WAV) with its own title and proxies.
The model name comes from LFM3_STT_MODEL, never from the caller; Ollama's URL
from OLLAMA_BASE_URL.

LFM3 hears audio two ways, and the instruction field picks between them:

- **Empty instruction** — Ollama's OpenAI-compatible /v1/audio/transcriptions:
  plain speech recognition, in the language that was spoken.
- **An instruction** — native /api/chat with the clip in the user message's
  `images` list, as on the gemma4 page, so the model can translate, summarize
  or answer questions about the clip. Asked to "transcribe" there, LFM3 tends
  to answer in English, which is why plain transcription takes the other route.

Ollama decodes WAV only, so the proxy refuses anything that is not RIFF/WAVE.
Every call — live passes included — is recorded on /activity through
`record_direct_call`, since no LlamaIndex event fires for it.
"""

import base64
import os
from datetime import UTC, datetime

import requests
from flask import Response, jsonify, render_template_string, request, url_for
from markupsafe import Markup

from llm.activity import record_direct_call

from .core import app
from .stt_gemma4_views import STT_TEMPLATE, _is_wav, _wav_seconds

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "lfm3-audio:2b-mlx-bf16"
PROXY_TIMEOUT = 300  # seconds; the first request may also load the model
HEALTH_TIMEOUT = 3  # seconds; the status badge must not hang on a dead server

INTRO = ("Transcribes speech with <code>{model}</code> on Ollama. Leave the "
         "instruction empty for plain transcription in the spoken language "
         "(<code>/v1/audio/transcriptions</code>); write one to have the model "
         "translate, summarize or answer about the clip (<code>/api/chat</code>). "
         "The clip is sent as 16&nbsp;kHz mono WAV, the one audio format Ollama decodes.")


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("LFM3_STT_MODEL", DEFAULT_MODEL)


def _error_message(r: requests.Response, what: str) -> str:
    """Ollama's error text, native ({"error": "..."}) or OpenAI-style."""
    try:
        body = r.json()
    except ValueError:
        return f"{what} failed ({r.status_code})"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        err = err.get("message")
    return err or f"{what} failed ({r.status_code})"


@app.route("/demo_stt_lfm3")
def demo_stt_lfm3() -> str:
    model = _model()
    return render_template_string(
        STT_TEMPLATE, title="LFM3-Audio STT", ollama_url=_ollama_url(),
        intro=Markup(INTRO).format(model=model),
        default_prompt="",
        prompt_placeholder="Empty: transcribe in the spoken language. "
                           "Or write an instruction about the clip.",
        download_name="lfm3-clip.wav",
        health_url=url_for("demo_stt_lfm3_health"),
        transcribe_url=url_for("demo_stt_lfm3_transcribe"))


@app.route("/demo_stt_lfm3/health")
def demo_stt_lfm3_health() -> Response | tuple[Response, int]:
    url, model = _ollama_url(), _model()
    try:
        r = requests.post(f"{url}/api/show", json={"model": model}, timeout=HEALTH_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"reachable": False, "url": url, "model": model,
                        "error": f"Ollama unreachable: {e}"}), 502
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        return jsonify({"reachable": False, "url": url, "model": model,
                        "error": body.get("error") or f"Ollama answered {r.status_code}"}), 502
    capabilities = body.get("capabilities") or []
    return jsonify({"reachable": True, "url": url, "model": model,
                    "capabilities": capabilities, "audio": "audio" in capabilities})


@app.route("/demo_stt_lfm3/transcribe", methods=["POST"])
def demo_stt_lfm3_transcribe() -> Response | tuple[Response, int]:
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"error": "missing 'audio' file field"}), 400
    raw = audio.read()
    if not _is_wav(raw):
        return jsonify({"error": "the clip must be a WAV file (RIFF/WAVE); "
                                 "Ollama decodes no other audio format"}), 400
    prompt = (request.form.get("prompt") or "").strip()
    url, model = _ollama_url(), _model()
    duration = _wav_seconds(raw)
    clip = f"[audio: {duration if duration is not None else '?'}s WAV, {len(raw)} bytes]"
    activity = {
        "started_at": datetime.now(UTC), "provider": "ollama", "model": model,
        "caller": "demo_stt_lfm3",
        "messages": [{"role": "user",
                      "content": f"{prompt}\n\n{clip}" if prompt else f"[transcription]\n\n{clip}"}],
    }
    try:
        if prompt:
            r = requests.post(f"{url}/api/chat", timeout=PROXY_TIMEOUT, json={
                "model": model,
                "stream": False,
                "think": False,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt,
                              "images": [base64.b64encode(raw).decode("ascii")]}],
            })
        else:
            r = requests.post(f"{url}/v1/audio/transcriptions", timeout=PROXY_TIMEOUT,
                              data={"model": model},
                              files={"file": ("clip.wav", raw, "audio/wav")})
    except requests.RequestException as e:
        record_direct_call(**activity, exc=e)
        return jsonify({"error": f"Ollama unreachable at {url}: {e}"}), 502
    if r.status_code != 200:
        error = _error_message(r, "transcription")
        record_direct_call(**activity, error=error)
        return jsonify({"error": error}), r.status_code
    try:
        reply = r.json()
    except ValueError:
        record_direct_call(**activity, error=f"Ollama answered {r.status_code} without JSON")
        return jsonify({"error": f"transcription failed ({r.status_code})"}), 502
    if prompt:
        text = ((reply.get("message") or {}).get("content") or "").strip()
        record_direct_call(**activity, raw=reply, response=text)
        return jsonify({"text": text, "model": model, "duration": duration,
                        "prompt_tokens": reply.get("prompt_eval_count")})
    text = (reply.get("text") or "").strip()
    record_direct_call(**activity, response=text)
    return jsonify({"text": text, "model": model, "duration": duration})
