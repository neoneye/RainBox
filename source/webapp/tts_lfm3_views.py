"""Demo page for text-to-speech with LFM3-Audio on Ollama.

The Ollama counterpart of `tts_kokoro_views.py`. There is no side service: the
proxy calls Ollama (default http://127.0.0.1:11434, overridable with
OLLAMA_BASE_URL, the same variable providers/ollama.py reads). The model name
comes from LFM3_TTS_MODEL, never from the caller.

- The voice catalog is the `voices` list Ollama's /api/show reports for the
  model ("default", then "female-<language>" / "male-<language>").
- Synthesis goes to Ollama's OpenAI-compatible /v1/audio/speech, which answers
  with 24 kHz mono 16-bit WAV. Ollama writes that WAV as a stream, so the RIFF
  and data chunk sizes are the 0xFFFFFFFF placeholder; once the whole body is
  in, the proxy writes the real sizes so players and downloads show the true
  length. The endpoint supports speed 1 only, so the page has no speed control.
- Ollama's OpenAI-style errors ({"error": {"message": ...}}) are flattened to
  {"error": "..."} like the other demo proxies.

Every synthesize call is recorded on /activity through `record_direct_call`,
since no LlamaIndex event fires for it; the row shows the text and voice, and
its response describes the clip instead of carrying the audio.
"""

import io
import os
import struct
import wave
from datetime import UTC, datetime

import requests
from flask import Response, jsonify, render_template_string, request

from llm.activity import record_direct_call

from .core import app

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "lfm3-audio:2b-mlx-bf16"
PROXY_TIMEOUT = 300  # seconds; the first request may also load the model
HEALTH_TIMEOUT = 3  # seconds; the status badge must not hang on a dead server


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("LFM3_TTS_MODEL", DEFAULT_MODEL)


def _voice_label(voice: str) -> str:
    """"female-british-english" -> "British English (female)"."""
    gender, _, language = voice.partition("-")
    if not language or gender not in ("female", "male"):
        return voice
    return f"{language.replace('-', ' ').title()} ({gender})"


def _fix_wav_sizes(raw: bytes) -> bytes:
    """Write the real RIFF and data chunk sizes into a streamed WAV.

    Anything that is not RIFF/WAVE, or has no data chunk, is returned as is.
    """
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw
    out = bytearray(raw)
    i = 12
    while i + 8 <= len(out):
        chunk_id = bytes(out[i:i + 4])
        (size,) = struct.unpack("<I", out[i + 4:i + 8])
        if chunk_id == b"data":
            struct.pack_into("<I", out, i + 4, len(out) - i - 8)
            struct.pack_into("<I", out, 4, len(out) - 8)
            return bytes(out)
        i += 8 + size + (size & 1)
    return raw


def _wav_seconds(raw: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(raw)) as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def _error_message(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"Ollama answered {r.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        err = err.get("message")
    return err or f"Ollama answered {r.status_code}"


TTS_TEMPLATE = """
<!doctype html>
<title>LFM3-Audio TTS &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .row{margin:1em 0}
  textarea{width:100%;max-width:640px;font-family:inherit;font-size:1rem;padding:8px}
  select{vertical-align:middle}
  button{padding:8px 18px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.95rem}
  button:hover{background:#1d4ed8}
  button:disabled{background:#9ca3af;cursor:default}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:0.85rem;font-weight:600}
  .badge.ok{background:#dcfce7;color:#166534}
  .badge.bad{background:#fee2e2;color:#991b1b}
  .badge.unknown{background:#e5e7eb;color:#374151}
  .muted{color:#6b7280;font-size:0.85rem}
  .err{color:#991b1b;margin-top:0.5em}
  label{font-weight:600}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>LFM3-Audio TTS</h1>
<p class="muted">Synthesizes speech with <code>{{ model }}</code> on Ollama
(its OpenAI-compatible <code>/v1/audio/speech</code>). No side service: Ollama
must be running with the model pulled.</p>

<div class="row">
  Ollama: <span id="status" class="badge unknown">checking&hellip;</span>
  <span class="muted">{{ ollama_url }}</span>
</div>

<div class="row">
  <label for="text">Text</label><br>
  <textarea id="text" rows="4" placeholder="Type something to speak&hellip;">LFM3 Audio speaks and listens with one model.</textarea>
</div>

<div class="row">
  <label for="voice">Voice</label>
  <select id="voice"><option>loading&hellip;</option></select>
</div>

<div class="row">
  <button id="speak" onclick="ppSpeak()">Synthesize</button>
</div>

<div class="row" id="player" style="display:none">
  <audio id="audio" controls></audio>
  <div class="muted" id="meta"></div>
  <div><a id="download" download="lfm3.wav">Download .wav</a></div>
</div>

<div class="err" id="error"></div>
</div>

<script>
async function ppHealth() {
  const badge = document.getElementById('status');
  try {
    const r = await fetch('{{ url_for("demo_tts_lfm3_health") }}');
    const j = await r.json();
    if (r.ok && j.reachable && j.speech) {
      badge.className = 'badge ok'; badge.textContent = 'reachable';
    } else if (r.ok && j.reachable) {
      badge.className = 'badge bad'; badge.textContent = 'model cannot speak';
    } else {
      badge.className = 'badge bad'; badge.textContent = 'unreachable';
      if (j.error) badge.title = j.error;
    }
  } catch (e) {
    badge.className = 'badge bad'; badge.textContent = 'unreachable';
  }
}

async function ppVoices() {
  const sel = document.getElementById('voice');
  try {
    const r = await fetch('{{ url_for("demo_tts_lfm3_voices") }}');
    const j = await r.json();
    sel.innerHTML = '';
    (j.voices || []).forEach(function(v) {
      const o = document.createElement('option');
      o.value = v.id; o.textContent = v.name;
      sel.appendChild(o);
    });
    if (!sel.options.length) sel.innerHTML = '<option value="">(no voices)</option>';
  } catch (e) {
    sel.innerHTML = '<option value="">(Ollama unreachable)</option>';
  }
}

async function ppSpeak() {
  const errEl = document.getElementById('error');
  const btn = document.getElementById('speak');
  errEl.textContent = '';
  const text = document.getElementById('text').value.trim();
  if (!text) { errEl.textContent = 'Please enter some text.'; return; }
  const payload = {text: text, voice: document.getElementById('voice').value};
  btn.disabled = true; btn.textContent = 'Synthesizing…';
  const started = performance.now();
  try {
    const r = await fetch('{{ url_for("demo_tts_lfm3_synthesize") }}', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      let msg = 'Synthesis failed (' + r.status + ').';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      errEl.textContent = msg;
      return;
    }
    const blob = await r.blob();
    const took = ((performance.now() - started) / 1000).toFixed(1);
    const secs = r.headers.get('X-Audio-Seconds');
    document.getElementById('meta').textContent =
      (secs ? secs + 's of audio, ' : '') + 'synthesized in ' + took + 's';
    const audioEl = document.getElementById('audio');
    if (audioEl.dataset.objurl) { URL.revokeObjectURL(audioEl.dataset.objurl); }
    const url = URL.createObjectURL(blob);
    audioEl.dataset.objurl = url;
    audioEl.src = url;
    document.getElementById('download').href = url;
    document.getElementById('player').style.display = '';
    audioEl.play().catch(function() { /* autoplay may be blocked; user can press play */ });
  } catch (e) {
    errEl.textContent = 'Request failed: ' + e;
  } finally {
    btn.disabled = false; btn.textContent = 'Synthesize';
  }
}

ppHealth();
ppVoices();
</script>
"""


def _show() -> dict | tuple[Response, int]:
    """Ollama's /api/show body for the model, or the 502 to answer with."""
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
    return body


@app.route("/demo_tts_lfm3")
def demo_tts_lfm3() -> str:
    return render_template_string(TTS_TEMPLATE, ollama_url=_ollama_url(), model=_model())


@app.route("/demo_tts_lfm3/health")
def demo_tts_lfm3_health() -> Response | tuple[Response, int]:
    body = _show()
    if isinstance(body, tuple):
        return body
    capabilities = body.get("capabilities") or []
    return jsonify({"reachable": True, "url": _ollama_url(), "model": _model(),
                    "capabilities": capabilities, "speech": "speech" in capabilities,
                    "voices": len(body.get("voices") or [])})


@app.route("/demo_tts_lfm3/voices")
def demo_tts_lfm3_voices() -> Response | tuple[Response, int]:
    body = _show()
    if isinstance(body, tuple):
        return body
    voices = [{"id": v, "name": _voice_label(v)} for v in body.get("voices") or []]
    return jsonify({"voices": voices})


@app.route("/demo_tts_lfm3/synthesize", methods=["POST"])
def demo_tts_lfm3_synthesize() -> Response | tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text must not be empty"}), 400
    voice = str(payload.get("voice") or "").strip()
    url, model = _ollama_url(), _model()
    body = {"model": model, "input": text, "response_format": "wav"}
    if voice:
        body["voice"] = voice
    activity = {
        "started_at": datetime.now(UTC), "provider": "ollama", "model": model,
        "caller": "demo_tts_lfm3",
        "messages": [{"role": "user", "content": f"[voice: {voice or 'default'}]\n\n{text}"}],
    }
    try:
        r = requests.post(f"{url}/v1/audio/speech", json=body, timeout=PROXY_TIMEOUT)
    except requests.RequestException as e:
        record_direct_call(**activity, exc=e)
        return jsonify({"error": f"Ollama unreachable at {url}: {e}"}), 502
    if r.status_code != 200:
        error = _error_message(r)
        record_direct_call(**activity, error=error)
        return jsonify({"error": error}), r.status_code
    wav = _fix_wav_sizes(r.content)
    seconds = _wav_seconds(wav)
    record_direct_call(**activity, response=f"[audio: {seconds if seconds is not None else '?'}s "
                                            f"WAV, {len(wav)} bytes]")
    resp = Response(wav, status=200, content_type="audio/wav")
    if seconds is not None:
        resp.headers["X-Audio-Seconds"] = str(seconds)
    return resp
