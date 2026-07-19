"""Demo page for the dots.tts-soar voice-cloning TTS subproject.

The page is same-origin only: the browser talks to these proxy routes, which
forward to the dots.tts REST service (default http://127.0.0.1:5007,
overridable with the DOTS_TTS_URL env var) using `requests`. This keeps torch
out of the main app and avoids cross-origin requests from the browser.

Reference audio is captured in the browser (microphone or file upload) and
encoded to 16-bit PCM WAV client-side with the Web Audio API, so the service
only ever receives real WAV files. Transcripts can be auto-filled through the
existing Whisper proxy (stt_whisper_views.py).
"""

import os

import requests
from flask import Response, jsonify, render_template_string, request

from .core import app

DEFAULT_DOTS_TTS_URL = "http://127.0.0.1:5007"
SYNTH_TIMEOUT = 300  # seconds; cloning on MPS/CPU is much slower than Kokoro
VOICES_TIMEOUT = 60  # seconds; uploads carry ~10s of WAV audio
HEALTH_TIMEOUT = 3  # seconds; the status badge must not hang on a dead service


def _service_url() -> str:
    return os.environ.get("DOTS_TTS_URL", DEFAULT_DOTS_TTS_URL).rstrip("/")


TTS_TEMPLATE = """
<!doctype html>
<title>dots.tts clone &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .row{margin:1em 0}
  textarea,input[type=text]{width:100%;max-width:640px;font-family:inherit;font-size:1rem;padding:8px;box-sizing:border-box}
  select{vertical-align:middle}
  button{padding:8px 18px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.95rem}
  button:hover{background:#1d4ed8}
  button:disabled{background:#9ca3af;cursor:default}
  button.rec{background:#dc2626}
  button.rec:hover{background:#b91c1c}
  button.danger{background:#dc2626}
  button.danger:hover{background:#b91c1c}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:0.85rem;font-weight:600}
  .badge.ok{background:#dcfce7;color:#166534}
  .badge.bad{background:#fee2e2;color:#991b1b}
  .badge.unknown{background:#e5e7eb;color:#374151}
  .muted{color:#6b7280;font-size:0.85rem}
  .err{color:#991b1b;margin-top:0.5em}
  label{font-weight:600}
  fieldset{max-width:640px;border:1px solid #e5e7eb;border-radius:8px;margin:1.5em 0;padding:0.5em 1em 1em}
  legend{font-weight:600;padding:0 6px}
  #timer{font-variant-numeric:tabular-nums;color:#dc2626;font-weight:600}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>dots.tts voice cloning</h1>
<p class="muted">Zero-shot voice cloning with the
<a href="https://huggingface.co/rednote-hilab/dots.tts-soar" target="_blank" rel="noopener">rednote-hilab/dots.tts-soar</a>
service. Start it from <code>voice_tts_dotstts/</code> (see its README).</p>

<div class="row">
  Service: <span id="status" class="badge unknown">checking&hellip;</span>
  <span class="muted">{{ service_url }}</span>
</div>

<fieldset>
<legend>Synthesize</legend>
<div class="row">
  <label for="voice">Voice</label>
  <select id="voice"><option>loading&hellip;</option></select>
  <button id="delvoice" class="danger" onclick="ppDeleteVoice()">Delete</button>
</div>
<div class="row">
  <label for="text">Text</label><br>
  <textarea id="text" rows="4" placeholder="Type something to speak&hellip;">Voice cloning turns ten seconds of speech into a reusable voice.</textarea>
</div>
<details class="row">
<summary>Advanced</summary>
<div class="row">
  <label for="spkscale">Speaker scale</label>
  <input type="range" id="spkscale" min="1.0" max="3.0" step="0.1" value="1.5">
  <span id="spkscaleval">1.5</span>
  <span class="muted">how strongly the reference voice and accent are applied; raise it if the
  output drifts toward generic English</span>
</div>
<div class="row">
  <label for="seed">Seed</label>
  <input type="number" id="seed" min="1" max="1000" placeholder="random" style="width:7em">
  <span class="muted">1&ndash;1000; the same seed reproduces the same rendition, so try a few and
  keep the one that sounds most like you</span>
</div>
</details>

<div class="row">
  <button id="speak" onclick="ppSpeak()">Synthesize</button>
  <span class="muted" id="speakhint"></span>
</div>
<div class="row" id="player" style="display:none">
  <audio id="audio" controls></audio>
  <div><a id="download" download="dotstts.wav">Download .wav</a></div>
</div>
</fieldset>

<fieldset>
<legend>Add a voice</legend>
<p class="muted">Record or upload 8&ndash;12 seconds of clean speech, make sure the
transcript matches it exactly, then save it as a reusable voice.</p>
<div class="row">
  <label for="device">Microphone</label>
  <select id="device" onchange="ppSaveDevice()"><option value="">Default device</option></select>
</div>
<div class="row">
  <button id="record" onclick="ppRecordToggle()">&#9679; Record</button>
  <span id="timer"></span>
  &nbsp; or upload: <input type="file" id="upload" accept="audio/*" onchange="ppUploadChanged()">
</div>
<div class="row" id="refplayer" style="display:none">
  <span class="muted">Reference:</span>
  <audio id="refaudio" controls></audio>
</div>
<div class="row">
  <label for="transcript">Transcript of the reference audio</label><br>
  <textarea id="transcript" rows="3" placeholder="The exact words spoken in the reference audio&hellip;"></textarea>
  <button id="transcribe" onclick="ppTranscribe()" disabled>Transcribe with Whisper</button>
  <span class="muted">uses the Whisper STT service; edit the result if it is not exact</span>
</div>
<div class="row">
  <label for="vname">Name</label><br>
  <input type="text" id="vname" placeholder="e.g. Simon (studio mic)">
</div>
<div class="row">
  <button id="save" onclick="ppSaveVoice()" disabled>Save voice</button>
  <span class="muted" id="savehint">record or upload a reference first</span>
</div>
</fieldset>

<div class="err" id="error"></div>
</div>

<script>
let mediaRecorder = null;
let chunks = [];
let timerId = null;
let startedAt = 0;
let refBlob = null;

const DEVICE_KEY = 'stt_device_id';  // shared with the STT/echo pages
const SCALE_KEY = 'dotstts_speaker_scale';
const SEED_KEY = 'dotstts_seed';

function ppSaveDevice() {
  try { localStorage.setItem(DEVICE_KEY, document.getElementById('device').value); } catch (e) {}
}
function ppSavedDevice() {
  try { return localStorage.getItem(DEVICE_KEY) || ''; } catch (e) { return ''; }
}
function ppErr(msg) { document.getElementById('error').textContent = msg; }

async function ppHealth() {
  const badge = document.getElementById('status');
  try {
    const r = await fetch('{{ url_for("demo_tts_dotstts_health") }}');
    const j = await r.json();
    if (r.ok && j.reachable) {
      badge.className = 'badge ok'; badge.textContent = 'reachable';
      if (j.device) badge.textContent = 'reachable (' + j.device + ')';
      return;
    }
  } catch (e) {}
  badge.className = 'badge bad'; badge.textContent = 'unreachable';
}

async function ppLoadVoices(selectId) {
  const sel = document.getElementById('voice');
  try {
    const r = await fetch('{{ url_for("demo_tts_dotstts_voices") }}');
    const j = await r.json();
    sel.innerHTML = '';
    (j.voices || []).forEach(function(v) {
      const o = document.createElement('option');
      o.value = v.id; o.textContent = v.name;
      sel.appendChild(o);
    });
    if (!sel.options.length) {
      sel.innerHTML = '<option value="">(no voices yet — add one below)</option>';
    } else if (selectId && Array.prototype.some.call(sel.options, function(o) { return o.value === selectId; })) {
      sel.value = selectId;
    }
  } catch (e) {
    sel.innerHTML = '<option value="">(service unreachable)</option>';
  }
}

// --- mic device picker (same approach as the STT/echo pages) ---------------
function ppAudioConstraints() {
  const id = document.getElementById('device').value;
  return {audio: id ? {deviceId: {exact: id}} : true};
}
async function ppRefreshDevices() {
  const sel = document.getElementById('device');
  let devices;
  try { devices = await navigator.mediaDevices.enumerateDevices(); } catch (e) { return; }
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

// --- capture a reference sample --------------------------------------------
function ppTick() {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  document.getElementById('timer').textContent = mm + ':' + ss;
}

async function ppRecordToggle() {
  if (mediaRecorder && mediaRecorder.state === 'recording') { mediaRecorder.stop(); return; }
  ppErr('');
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints()); }
  catch (e) { ppErr('Microphone access denied or unavailable: ' + e); return; }
  ppRefreshDevices();
  chunks = [];
  mediaRecorder = new MediaRecorder(stream);
  const mime = mediaRecorder.mimeType || 'audio/webm';
  mediaRecorder.ondataavailable = function(e) { if (e.data.size) chunks.push(e.data); };
  mediaRecorder.onstop = function() {
    stream.getTracks().forEach(function(t) { t.stop(); });
    clearInterval(timerId);
    document.getElementById('timer').textContent = '';
    const btn = document.getElementById('record');
    btn.classList.remove('rec'); btn.innerHTML = '&#9679; Record';
    ppSetReference(new Blob(chunks, {type: mime}));
  };
  mediaRecorder.start(1000);
  startedAt = Date.now(); ppTick();
  timerId = setInterval(ppTick, 250);
  const btn = document.getElementById('record');
  btn.classList.add('rec'); btn.innerHTML = '&#9632; Stop';
}

async function ppUploadChanged() {
  const input = document.getElementById('upload');
  if (input.files && input.files.length) { ppErr(''); await ppSetReference(input.files[0]); }
}

async function ppSetReference(blob) {
  try {
    refBlob = await ppBlobToWav(blob);
  } catch (e) {
    refBlob = null;
    ppErr('Could not decode that audio: ' + e);
    return;
  }
  const audioEl = document.getElementById('refaudio');
  if (audioEl.dataset.objurl) URL.revokeObjectURL(audioEl.dataset.objurl);
  const url = URL.createObjectURL(refBlob);
  audioEl.dataset.objurl = url; audioEl.src = url;
  document.getElementById('refplayer').style.display = '';
  document.getElementById('transcribe').disabled = false;
  document.getElementById('save').disabled = false;
  document.getElementById('savehint').textContent = '';
}

// Decode any browser-supported audio (webm/ogg/mp3/wav/...) and re-encode as
// mono 16-bit PCM WAV, which is what the dots.tts service expects on disk.
async function ppBlobToWav(blob) {
  const buf = await blob.arrayBuffer();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  let audio;
  try { audio = await ctx.decodeAudioData(buf); }
  finally { ctx.close().catch(function() {}); }
  const n = audio.length;
  const channels = audio.numberOfChannels;
  const mono = new Float32Array(n);
  for (let c = 0; c < channels; c++) {
    const data = audio.getChannelData(c);
    for (let i = 0; i < n; i++) mono[i] += data[i] / channels;
  }
  const out = new ArrayBuffer(44 + n * 2);
  const dv = new DataView(out);
  ppWriteStr(dv, 0, 'RIFF'); dv.setUint32(4, 36 + n * 2, true); ppWriteStr(dv, 8, 'WAVE');
  ppWriteStr(dv, 12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true); dv.setUint32(24, audio.sampleRate, true);
  dv.setUint32(28, audio.sampleRate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  ppWriteStr(dv, 36, 'data'); dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, mono[i]));
    dv.setInt16(44 + i * 2, Math.round(s * 32767), true);
  }
  return new Blob([out], {type: 'audio/wav'});
}
function ppWriteStr(dv, off, s) {
  for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i));
}

// --- transcript + save ------------------------------------------------------
async function ppTranscribe() {
  if (!refBlob) return;
  const btn = document.getElementById('transcribe');
  btn.disabled = true; btn.textContent = 'Transcribing…';
  ppErr('');
  try {
    const fd = new FormData();
    fd.append('audio', refBlob, 'reference.wav');
    const r = await fetch('{{ url_for("demo_stt_whisper_transcribe") }}', {method: 'POST', body: fd});
    if (!r.ok) {
      let msg = 'Transcription failed (' + r.status + '). Is the Whisper service running?';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      ppErr(msg);
      return;
    }
    const j = await r.json();
    document.getElementById('transcript').value = (j.text || '').trim();
  } catch (e) {
    ppErr('Transcription request failed: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = 'Transcribe with Whisper';
  }
}

async function ppSaveVoice() {
  if (!refBlob) return;
  const name = document.getElementById('vname').value.trim();
  const transcript = document.getElementById('transcript').value.trim();
  if (!name) { ppErr('Please give the voice a name.'); return; }
  if (!transcript) { ppErr('Please provide the transcript of the reference audio.'); return; }
  const btn = document.getElementById('save');
  btn.disabled = true; btn.textContent = 'Saving…';
  ppErr('');
  try {
    const fd = new FormData();
    fd.append('name', name);
    fd.append('transcript', transcript);
    fd.append('audio', refBlob, 'reference.wav');
    const r = await fetch('{{ url_for("demo_tts_dotstts_voices") }}', {method: 'POST', body: fd});
    const j = await r.json();
    if (!r.ok) { ppErr(j.error || ('Saving failed (' + r.status + ').')); return; }
    refBlob = null;
    document.getElementById('vname').value = '';
    document.getElementById('transcript').value = '';
    document.getElementById('upload').value = '';
    document.getElementById('refplayer').style.display = 'none';
    document.getElementById('transcribe').disabled = true;
    document.getElementById('savehint').textContent = 'saved';
    await ppLoadVoices(j.voice && j.voice.id);
    ppHealth();
  } catch (e) {
    ppErr('Save request failed: ' + e);
  } finally {
    btn.disabled = refBlob === null; btn.textContent = 'Save voice';
  }
}

async function ppDeleteVoice() {
  const sel = document.getElementById('voice');
  const id = sel.value;
  if (!id) return;
  const label = sel.options[sel.selectedIndex].textContent;
  if (!confirm('Delete voice "' + label + '"? The reference recording is removed from the service.')) return;
  ppErr('');
  try {
    const r = await fetch('{{ url_for("demo_tts_dotstts_voices") }}/' + encodeURIComponent(id), {method: 'DELETE'});
    if (!r.ok) {
      let msg = 'Delete failed (' + r.status + ').';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      ppErr(msg);
      return;
    }
    await ppLoadVoices(null);
    ppHealth();
  } catch (e) {
    ppErr('Delete request failed: ' + e);
  }
}

// --- synthesize -------------------------------------------------------------
async function ppSpeak() {
  const btn = document.getElementById('speak');
  ppErr('');
  const text = document.getElementById('text').value.trim();
  const voice = document.getElementById('voice').value;
  if (!text) { ppErr('Please enter some text.'); return; }
  if (!voice) { ppErr('Please add a voice first.'); return; }
  const payload = {
    text: text,
    voice: voice,
    speaker_scale: parseFloat(document.getElementById('spkscale').value),
  };
  const seedRaw = document.getElementById('seed').value.trim();
  if (seedRaw) payload.seed = parseInt(seedRaw, 10);
  btn.disabled = true; btn.textContent = 'Synthesizing…';
  document.getElementById('speakhint').textContent = 'the first request loads the model and can take minutes';
  const t0 = performance.now();
  try {
    const r = await fetch('{{ url_for("demo_tts_dotstts_synthesize") }}', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      let msg = 'Synthesis failed (' + r.status + ').';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      ppErr(msg);
      return;
    }
    const blob = await r.blob();
    const audioEl = document.getElementById('audio');
    if (audioEl.dataset.objurl) URL.revokeObjectURL(audioEl.dataset.objurl);
    const url = URL.createObjectURL(blob);
    audioEl.dataset.objurl = url; audioEl.src = url;
    document.getElementById('download').href = url;
    document.getElementById('player').style.display = '';
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    document.getElementById('speakhint').textContent = 'synthesized in ' + secs + 's';
    audioEl.play().catch(function() { /* autoplay may be blocked; user can press play */ });
  } catch (e) {
    ppErr('Request failed: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = 'Synthesize';
    ppHealth();
  }
}

// Persist the tuning settings: the right speaker scale / seed is a per-user
// discovery (too low and the voice drifts toward generic English), so the
// page remembers them like the mic device picker does.
document.getElementById('spkscale').addEventListener('input', function() {
  document.getElementById('spkscaleval').textContent = parseFloat(this.value).toFixed(1);
  try { localStorage.setItem(SCALE_KEY, this.value); } catch (e) {}
});
document.getElementById('seed').addEventListener('input', function() {
  try { localStorage.setItem(SEED_KEY, this.value.trim()); } catch (e) {}
});
(function ppRestoreTuning() {
  try {
    const scale = parseFloat(localStorage.getItem(SCALE_KEY));
    if (scale >= 1.0 && scale <= 3.0) {
      document.getElementById('spkscale').value = scale;
      document.getElementById('spkscaleval').textContent = scale.toFixed(1);
    }
    const seed = localStorage.getItem(SEED_KEY);
    if (seed) document.getElementById('seed').value = seed;
  } catch (e) {}
})();

ppHealth();
ppLoadVoices(null);
ppRefreshDevices();
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', ppRefreshDevices);
}
</script>
"""


@app.route("/demo_tts_dotstts")
def demo_tts_dotstts() -> str:
    return render_template_string(TTS_TEMPLATE, service_url=_service_url())


@app.route("/demo_tts_dotstts/health")
def demo_tts_dotstts_health() -> Response | tuple[Response, int]:
    url = _service_url()
    try:
        r = requests.get(f"{url}/health", timeout=HEALTH_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"reachable": False, "url": url, "error": str(e)}), 502
    try:
        body = r.json()
    except ValueError:
        body = {}
    body["reachable"] = r.status_code == 200
    body["url"] = url
    return jsonify(body), r.status_code


@app.route("/demo_tts_dotstts/voices", methods=["GET", "POST"])
def demo_tts_dotstts_voices() -> Response | tuple[Response, int]:
    url = _service_url()
    try:
        if request.method == "POST":
            audio = request.files.get("audio")
            files = {}
            if audio is not None:
                files["audio"] = (audio.filename or "reference.wav", audio.stream, "audio/wav")
            r = requests.post(
                f"{url}/voices",
                data={
                    "name": request.form.get("name", ""),
                    "transcript": request.form.get("transcript", ""),
                },
                files=files,
                timeout=VOICES_TIMEOUT,
            )
        else:
            r = requests.get(f"{url}/voices", timeout=VOICES_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"error": f"dots.tts service unreachable at {url}: {e}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"error": "invalid response from service"}), 502


@app.route("/demo_tts_dotstts/voices/<voice_id>", methods=["DELETE"])
def demo_tts_dotstts_voice_delete(voice_id: str) -> Response | tuple[Response, int]:
    url = _service_url()
    try:
        r = requests.delete(f"{url}/voices/{voice_id}", timeout=VOICES_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"error": f"dots.tts service unreachable at {url}: {e}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"error": "invalid response from service"}), 502


@app.route("/demo_tts_dotstts/synthesize", methods=["POST"])
def demo_tts_dotstts_synthesize() -> Response | tuple[Response, int]:
    url = _service_url()
    payload = request.get_json(silent=True) or {}
    try:
        r = requests.post(f"{url}/tts", json=payload, timeout=SYNTH_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"error": f"dots.tts service unreachable at {url}: {e}"}), 502
    if r.status_code == 200:
        return Response(r.content, status=200, content_type="audio/wav")
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"error": f"TTS failed ({r.status_code})"}), r.status_code
