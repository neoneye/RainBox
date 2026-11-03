"""Demo page for speech-to-text with gemma4 on Ollama.

The gemma4 counterpart of `stt_whisper_views.py`. gemma4's audio capability is
audio *input*: Ollama hands the model a clip and returns text, so this page
transcribes (or follows any other instruction about the clip) — it cannot
speak. There is no side service: the proxy calls Ollama's native /api/chat
(default http://127.0.0.1:11434, overridable with OLLAMA_BASE_URL, the same
variable providers/ollama.py reads) with the clip base64-encoded in the
message's `images` list, which is where Ollama takes audio too.

Ollama decodes WAV only — a webm/opus clip comes back as "media expansion
produced no tokens" — so the browser re-encodes every recording or upload to
16 kHz mono 16-bit WAV with the Web Audio API before posting it, and the proxy
refuses anything that is not RIFF/WAVE. The model name comes from
GEMMA4_STT_MODEL, never from the caller.
"""

import base64
import io
import os
import wave

import requests
from flask import Response, jsonify, render_template_string, request

from .core import app

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:e4b-mlx"
DEFAULT_PROMPT = "Transcribe this audio exactly."
PROXY_TIMEOUT = 300  # seconds; the first request may also load the model
HEALTH_TIMEOUT = 3  # seconds; the status badge must not hang on a dead server


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _model() -> str:
    return os.environ.get("GEMMA4_STT_MODEL", DEFAULT_MODEL)


def _is_wav(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


def _wav_seconds(raw: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(raw)) as w:
            return round(w.getnframes() / w.getframerate(), 1)
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


STT_TEMPLATE = """
<!doctype html>
<title>gemma4 STT &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .row{margin:1em 0}
  button{padding:8px 18px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.95rem}
  button:hover{background:#1d4ed8}
  button:disabled{background:#9ca3af;cursor:default}
  button.rec{background:#dc2626}
  button.rec:hover{background:#b91c1c}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:0.85rem;font-weight:600}
  .badge.ok{background:#dcfce7;color:#166534}
  .badge.bad{background:#fee2e2;color:#991b1b}
  .badge.unknown{background:#e5e7eb;color:#374151}
  .muted{color:#6b7280;font-size:0.85rem}
  .err{color:#991b1b;margin-top:0.5em}
  label{font-weight:600}
  #prompt{width:100%;max-width:640px;font-family:inherit;font-size:1rem;padding:8px}
  #transcript{width:100%;max-width:640px;min-height:7em;font-family:inherit;font-size:1rem;padding:8px;white-space:pre-wrap}
  #timer{font-variant-numeric:tabular-nums;color:#dc2626;font-weight:600}
  .meter{display:inline-block;width:220px;height:12px;background:#e5e7eb;border-radius:6px;overflow:hidden;vertical-align:middle}
  .meterbar{display:block;height:100%;width:0;background:#16a34a;transition:width 0.05s linear}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>gemma4 STT</h1>
<p class="muted">Transcribes speech with <code>{{ model }}</code> on Ollama.
gemma4 hears audio but only answers in text, so this page listens; it cannot
speak. The clip is sent as 16&nbsp;kHz mono WAV, the one audio format Ollama
decodes.</p>

<div class="row">
  Model: <span id="status" class="badge unknown">checking&hellip;</span>
  <span class="muted">{{ ollama_url }} &mdash; <span id="statusmsg"></span></span>
</div>

<div class="row">
  <label for="prompt">Instruction</label><br>
  <textarea id="prompt" rows="2">{{ default_prompt }}</textarea>
</div>

<div class="row">
  <label for="device">Microphone</label>
  <select id="device" onchange="ppSaveDevice()"><option value="">Default device</option></select>
</div>

<div class="row">
  <button id="record" onclick="ppToggle()">&#9679; Record</button>
  <span id="timer"></span>
  <span class="muted" id="hint">Click record, allow the mic, speak, then stop to transcribe.</span>
</div>

<div class="row">
  <label for="file">Or an audio file</label>
  <input type="file" id="file" accept="audio/*" onchange="ppPickFile(this)">
</div>

<div class="row" id="meterrow" style="display:none">
  <span class="muted">Mic&nbsp;level</span>
  <span class="meter"><span id="meterbar" class="meterbar"></span></span>
  <span class="muted" id="metermsg"></span>
</div>

<div class="row" id="player" style="display:none">
  <audio id="audio" controls></audio>
  <div><a id="download" download="gemma4-clip.wav">Download the .wav that was sent</a></div>
</div>

<div class="row">
  <label for="transcript">Answer</label><br>
  <textarea id="transcript" placeholder="The model&rsquo;s answer will appear here&hellip;" readonly></textarea>
  <div class="muted" id="meta"></div>
</div>

<div class="err" id="error"></div>
</div>

<script>
let mediaRecorder = null;
let chunks = [];
let timerId = null;
let startedAt = 0;
let audioCtx = null;      // Web Audio graph for the live mic-level meter
let meterRaf = 0;
let sawSignal = false;    // did the mic ever produce a non-trivial level?

const SIGNAL_RMS = 0.01;  // RMS above this counts as "the mic is picking up sound"
const WAV_RATE = 16000;   // what the model's audio encoder works at
const DEVICE_KEY = 'stt_device_id';  // shared with the Whisper page

function ppSaveDevice() {
  try { localStorage.setItem(DEVICE_KEY, document.getElementById('device').value); }
  catch (e) { /* storage may be unavailable (private mode); not fatal */ }
}

function ppSavedDevice() {
  try { return localStorage.getItem(DEVICE_KEY) || ''; }
  catch (e) { return ''; }
}

function ppAudioConstraints() {
  const id = document.getElementById('device').value;
  return {audio: id ? {deviceId: {exact: id}} : true};
}

async function ppRefreshDevices() {
  const sel = document.getElementById('device');
  let devices;
  try { devices = await navigator.mediaDevices.enumerateDevices(); }
  catch (e) { return; }
  const mics = devices.filter(function(d) { return d.kind === 'audioinput'; });
  const desired = sel.value || ppSavedDevice();
  sel.innerHTML = '<option value="">Default device</option>';
  mics.forEach(function(d, i) {
    const o = document.createElement('option');
    o.value = d.deviceId;
    o.textContent = d.label || ('Microphone ' + (i + 1) + ' (allow mic to see name)');
    sel.appendChild(o);
  });
  if (desired && mics.some(function(d) { return d.deviceId === desired; })) sel.value = desired;
}

function ppStartMeter(stream) {
  const row = document.getElementById('meterrow');
  const bar = document.getElementById('meterbar');
  row.style.display = '';
  document.getElementById('metermsg').textContent = '';
  sawSignal = false;
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    audioCtx.createMediaStreamSource(stream).connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    (function tick() {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / data.length);
      bar.style.width = Math.min(100, Math.round(rms * 300)) + '%';
      if (rms >= SIGNAL_RMS) sawSignal = true;
      meterRaf = requestAnimationFrame(tick);
    })();
  } catch (e) {
    row.style.display = 'none';  // metering is best-effort; never block recording
  }
}

function ppStopMeter() {
  cancelAnimationFrame(meterRaf);
  if (audioCtx) { audioCtx.close().catch(function() {}); audioCtx = null; }
  document.getElementById('meterbar').style.width = '0';
  document.getElementById('metermsg').textContent = sawSignal
    ? ''
    : '⚠ No mic signal detected the whole time — the browser captured silence. Check your input device and the site’s mic permission.';
}

async function ppHealth() {
  const badge = document.getElementById('status');
  const msg = document.getElementById('statusmsg');
  try {
    const r = await fetch('{{ url_for("demo_stt_gemma4_health") }}');
    const j = await r.json();
    if (r.ok && j.reachable && j.audio) {
      badge.className = 'badge ok'; badge.textContent = 'hears audio';
    } else if (r.ok && j.reachable) {
      badge.className = 'badge bad'; badge.textContent = 'no audio';
    } else {
      badge.className = 'badge bad'; badge.textContent = 'unreachable';
    }
    msg.textContent = j.error || ('capabilities: ' + (j.capabilities || []).join(', '));
  } catch (e) {
    badge.className = 'badge bad'; badge.textContent = 'unreachable';
  }
}

function ppTick() {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  document.getElementById('timer').textContent =
    String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
}

async function ppToggle() {
  if (mediaRecorder && mediaRecorder.state === 'recording') { mediaRecorder.stop(); return; }
  const errEl = document.getElementById('error');
  errEl.textContent = '';
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints());
  } catch (e) {
    errEl.textContent = 'Microphone access denied or unavailable: ' + e;
    return;
  }
  ppRefreshDevices();
  chunks = [];
  ppStartMeter(stream);
  mediaRecorder = new MediaRecorder(stream);
  const mimeType = mediaRecorder.mimeType || 'audio/webm';
  mediaRecorder.ondataavailable = function(e) { if (e.data.size) chunks.push(e.data); };
  mediaRecorder.onstop = function() {
    stream.getTracks().forEach(function(t) { t.stop(); });
    clearInterval(timerId);
    ppStopMeter();
    const btn = document.getElementById('record');
    btn.classList.remove('rec');
    btn.innerHTML = '&#9679; Record';
    document.getElementById('timer').textContent = '';
    ppTranscribe(new Blob(chunks, {type: mimeType}));
  };
  mediaRecorder.start();
  startedAt = Date.now();
  ppTick();
  timerId = setInterval(ppTick, 250);
  const btn = document.getElementById('record');
  btn.classList.add('rec');
  btn.innerHTML = '&#9632; Stop';
  document.getElementById('hint').textContent = 'Recording… click stop to transcribe.';
}

function ppPickFile(input) {
  const f = input.files && input.files[0];
  input.value = '';
  if (f) { document.getElementById('error').textContent = ''; ppTranscribe(f); }
}

// Decode whatever the browser recorded or was given, resample to 16 kHz
// mono, and write it out as a 16-bit PCM WAV: the format Ollama decodes.
async function ppToWav(blob) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  let decoded;
  try { decoded = await ctx.decodeAudioData(await blob.arrayBuffer()); }
  finally { ctx.close().catch(function() {}); }
  const frames = Math.max(1, Math.ceil(decoded.duration * WAV_RATE));
  const off = new OfflineAudioContext(1, frames, WAV_RATE);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const view = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  function ascii(at, s) { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); }
  ascii(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); ascii(8, 'WAVE');
  ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, WAV_RATE, true);
  view.setUint32(28, WAV_RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ascii(36, 'data'); view.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view], {type: 'audio/wav'});
}

function ppPlay(wav) {
  const audioEl = document.getElementById('audio');
  if (audioEl.dataset.objurl) URL.revokeObjectURL(audioEl.dataset.objurl);
  const url = URL.createObjectURL(wav);
  audioEl.dataset.objurl = url;
  audioEl.src = url;
  document.getElementById('download').href = url;
  document.getElementById('player').style.display = '';
}

async function ppTranscribe(blob) {
  const errEl = document.getElementById('error');
  const btn = document.getElementById('record');
  const out = document.getElementById('transcript');
  const meta = document.getElementById('meta');
  let wav;
  try { wav = await ppToWav(blob); }
  catch (e) { errEl.textContent = 'Could not decode that audio: ' + e; return; }
  ppPlay(wav);
  const fd = new FormData();
  fd.append('audio', wav, 'clip.wav');
  fd.append('prompt', document.getElementById('prompt').value);
  btn.disabled = true;
  out.value = '';
  meta.textContent = '';
  document.getElementById('hint').textContent = 'Transcribing…';
  const t0 = performance.now();
  try {
    const r = await fetch('{{ url_for("demo_stt_gemma4_transcribe") }}', {method: 'POST', body: fd});
    const elapsed = (performance.now() - t0) / 1000;
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok) { errEl.textContent = j.error || ('Transcription failed (' + r.status + ').'); return; }
    out.value = j.text || '(empty answer)';
    const bits = [];
    if (j.duration) bits.push('audio: ' + j.duration + 's');
    if (j.prompt_tokens) bits.push(j.prompt_tokens + ' prompt tokens');
    bits.push('answered in ' + elapsed.toFixed(1) + 's');
    meta.textContent = bits.join(' · ');
  } catch (e) {
    errEl.textContent = 'Request failed: ' + e;
  } finally {
    btn.disabled = false;
    document.getElementById('hint').textContent = 'Click record, allow the mic, speak, then stop to transcribe.';
  }
}

ppHealth();
ppRefreshDevices();
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', ppRefreshDevices);
}
</script>
"""


@app.route("/demo_stt_gemma4")
def demo_stt_gemma4() -> str:
    return render_template_string(
        STT_TEMPLATE, ollama_url=_ollama_url(), model=_model(),
        default_prompt=DEFAULT_PROMPT)


@app.route("/demo_stt_gemma4/health")
def demo_stt_gemma4_health() -> Response | tuple[Response, int]:
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


@app.route("/demo_stt_gemma4/transcribe", methods=["POST"])
def demo_stt_gemma4_transcribe() -> Response | tuple[Response, int]:
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"error": "missing 'audio' file field"}), 400
    raw = audio.read()
    if not _is_wav(raw):
        return jsonify({"error": "the clip must be a WAV file (RIFF/WAVE); "
                                 "Ollama decodes no other audio format"}), 400
    prompt = (request.form.get("prompt") or "").strip() or DEFAULT_PROMPT
    url, model = _ollama_url(), _model()
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(raw).decode("ascii")]}],
    }
    try:
        r = requests.post(f"{url}/api/chat", json=body, timeout=PROXY_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"error": f"Ollama unreachable at {url}: {e}"}), 502
    try:
        reply = r.json()
    except ValueError:
        return jsonify({"error": f"transcription failed ({r.status_code})"}), 502
    if r.status_code != 200:
        return jsonify({"error": reply.get("error") or f"transcription failed ({r.status_code})"}), r.status_code
    return jsonify({
        "text": ((reply.get("message") or {}).get("content") or "").strip(),
        "model": model,
        "duration": _wav_seconds(raw),
        "prompt_tokens": reply.get("prompt_eval_count"),
    })
