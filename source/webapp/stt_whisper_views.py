"""Demo page for the Whisper speech-to-text subproject.

The speech-to-text counterpart of `tts_kokoro_views.py`. The page is same-origin
only: the browser records mic audio and posts it to these proxy routes, which
forward to the Whisper REST service (default http://127.0.0.1:5006, overridable
with the WHISPER_STT_URL env var) using `requests`. This keeps the STT model out
of the main app and avoids cross-origin requests from the browser.
"""

import os

import requests
from flask import Response, jsonify, render_template_string, request

from .core import app

DEFAULT_WHISPER_STT_URL = "http://127.0.0.1:5006"
PROXY_TIMEOUT = 120  # seconds; transcribing a long clip can take a while
HEALTH_TIMEOUT = 3  # seconds; the status badge must not hang on a dead service


def _service_url() -> str:
    return os.environ.get("WHISPER_STT_URL", DEFAULT_WHISPER_STT_URL).rstrip("/")


STT_TEMPLATE = """
<!doctype html>
<title>Whisper STT &mdash; rainbox</title>
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
  #transcript{width:100%;max-width:640px;min-height:7em;font-family:inherit;font-size:1rem;padding:8px;white-space:pre-wrap}
  #timer{font-variant-numeric:tabular-nums;color:#dc2626;font-weight:600}
  .meter{display:inline-block;width:220px;height:12px;background:#e5e7eb;border-radius:6px;overflow:hidden;vertical-align:middle}
  .meterbar{display:block;height:100%;width:0;background:#16a34a;transition:width 0.05s linear}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Whisper STT</h1>
<p class="muted">Transcribes speech with the
<a href="https://github.com/SYSTRAN/faster-whisper" target="_blank" rel="noopener">faster-whisper</a>
build of <code>whisper-large-v3-turbo</code>. Start it from
<code>whisper_service/</code> (see its README).</p>

<div class="row">
  Service: <span id="status" class="badge unknown">checking&hellip;</span>
  <span class="muted">{{ service_url }} &mdash; model <span id="model">?</span></span>
</div>

<div class="row">
  <label for="device">Microphone</label>
  <select id="device" onchange="ppSaveDevice()"><option value="">Default device</option></select>
  <button id="testmic" onclick="ppTestToggle()" style="background:#0f766e">Test mic</button>
</div>

<div class="row">
  <button id="record" onclick="ppToggle()">&#9679; Record</button>
  <span id="timer"></span>
  <span class="muted" id="hint">Click record, allow the mic, speak, then stop to finish.</span>
</div>

<div class="row" id="meterrow" style="display:none">
  <span class="muted">Mic&nbsp;level</span>
  <span class="meter"><span id="meterbar" class="meterbar"></span></span>
  <span class="muted" id="metermsg"></span>
</div>

<div class="row" id="player" style="display:none">
  <audio id="audio" controls></audio>
</div>

<div class="row">
  <label for="transcript">Transcript</label><br>
  <textarea id="transcript" placeholder="Transcribed text will appear here&hellip;" readonly></textarea>
  <div class="muted" id="meta"></div>
</div>

<div class="err" id="error"></div>
</div>

<script>
let mediaRecorder = null;
let chunks = [];
let timerId = null;       // UI clock
let interimId = null;     // rolling live-transcription timer
let startedAt = 0;
let inflight = false;     // an interim request is in flight
let stopped = false;      // recording stopped; ignore late interim results
let mimeType = 'audio/webm';
let audioCtx = null;      // Web Audio graph for the live mic-level meter
let analyser = null;
let meterRaf = 0;
let sawSignal = false;    // did the mic ever produce a non-trivial level?
let testStream = null;    // open stream while "Test mic" is active

const SIGNAL_RMS = 0.01;  // RMS above this counts as "the mic is picking up sound"

const INTERIM_MS = 2500;  // how often to re-transcribe the growing buffer
const DEVICE_KEY = 'stt_device_id';  // remembered input device across visits

function ppSaveDevice() {
  try { localStorage.setItem(DEVICE_KEY, document.getElementById('device').value); }
  catch (e) { /* storage may be unavailable (private mode); not fatal */ }
}

function ppSavedDevice() {
  try { return localStorage.getItem(DEVICE_KEY) || ''; }
  catch (e) { return ''; }
}

function ppExt() {
  return (mimeType && mimeType.indexOf('ogg') >= 0) ? 'ogg' : 'webm';
}

// Constraints honoring the chosen input device (empty value => browser default).
function ppAudioConstraints() {
  const id = document.getElementById('device').value;
  return {audio: id ? {deviceId: {exact: id}} : true};
}

// Populate the device dropdown. Labels are only exposed once mic permission has
// been granted, so this is called again after the first getUserMedia.
async function ppRefreshDevices() {
  const sel = document.getElementById('device');
  let devices;
  try { devices = await navigator.mediaDevices.enumerateDevices(); }
  catch (e) { return; }
  const mics = devices.filter(function(d) { return d.kind === 'audioinput'; });
  // Prefer the current selection, else the one remembered from a past visit.
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

// Open the selected mic and run the meter without recording, so a working
// device can be found by watching which one makes the bar move.
async function ppTestToggle() {
  const btn = document.getElementById('testmic');
  if (testStream) { ppStopTest(); return; }
  const errEl = document.getElementById('error');
  errEl.textContent = '';
  try {
    testStream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints());
  } catch (e) {
    errEl.textContent = 'Could not open the selected microphone: ' + e;
    testStream = null;
    return;
  }
  await ppRefreshDevices();  // labels are available now that permission is granted
  ppStartMeter(testStream);
  btn.textContent = 'Stop test';
  document.getElementById('hint').textContent = 'Testing — speak and watch the level; pick the device whose bar moves.';
}

function ppStopTest() {
  if (testStream) { testStream.getTracks().forEach(function(t) { t.stop(); }); testStream = null; }
  ppStopMeter();
  document.getElementById('testmic').textContent = 'Test mic';
  document.getElementById('hint').textContent = 'Click record, allow the mic, speak, then stop to finish.';
}

// Live mic-level meter off the same MediaStream. If the bar never moves, the
// browser isn't capturing audio (wrong/muted input device) — that's the usual
// cause of an empty transcript even when you're clearly speaking.
function ppStartMeter(stream) {
  const row = document.getElementById('meterrow');
  const bar = document.getElementById('meterbar');
  const msg = document.getElementById('metermsg');
  row.style.display = '';
  msg.textContent = '';
  sawSignal = false;
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const src = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    src.connect(analyser);
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
  analyser = null;
  document.getElementById('meterbar').style.width = '0';
  document.getElementById('metermsg').textContent = sawSignal
    ? ''
    : '⚠ No mic signal detected the whole time — the browser captured silence. Check your input device, level, and the site\\'s mic permission.';
}

async function ppHealth() {
  const badge = document.getElementById('status');
  try {
    const r = await fetch('{{ url_for("demo_stt_whisper_health") }}');
    const j = await r.json();
    if (r.ok && j.reachable) {
      badge.className = 'badge ok'; badge.textContent = 'reachable';
      if (j.model) document.getElementById('model').textContent = j.model;
    } else {
      badge.className = 'badge bad'; badge.textContent = 'unreachable';
    }
  } catch (e) {
    badge.className = 'badge bad'; badge.textContent = 'unreachable';
  }
}

function ppTick() {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  document.getElementById('timer').textContent = mm + ':' + ss;
}

async function ppToggle() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    return;
  }
  const errEl = document.getElementById('error');
  errEl.textContent = '';
  ppStopTest();  // don't hold the device open in two streams at once
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints());
  } catch (e) {
    errEl.textContent = 'Microphone access denied or unavailable: ' + e;
    return;
  }
  ppRefreshDevices();
  chunks = [];
  stopped = false;
  inflight = false;
  document.getElementById('transcript').value = '';
  document.getElementById('meta').textContent = '';
  ppStartMeter(stream);
  mediaRecorder = new MediaRecorder(stream);
  // The header lives only in the first chunk, so the accumulated blob is a
  // valid file at every interim send. Emit a chunk every second.
  mimeType = mediaRecorder.mimeType || 'audio/webm';
  mediaRecorder.ondataavailable = function(e) { if (e.data.size) chunks.push(e.data); };
  mediaRecorder.onstop = function() {
    stream.getTracks().forEach(function(t) { t.stop(); });
    clearInterval(timerId);
    clearInterval(interimId);
    ppStopMeter();
    const blob = new Blob(chunks, {type: mimeType});
    ppPlay(blob);
    ppFinal(blob);
  };
  mediaRecorder.start(1000);
  startedAt = Date.now();
  ppTick();
  timerId = setInterval(ppTick, 250);
  interimId = setInterval(ppInterim, INTERIM_MS);
  const btn = document.getElementById('record');
  btn.classList.add('rec');
  btn.innerHTML = '&#9632; Stop';
  document.getElementById('hint').textContent = 'Recording… transcript updates live; click stop to finish.';
}

function ppPlay(blob) {
  const audioEl = document.getElementById('audio');
  if (audioEl.dataset.objurl) URL.revokeObjectURL(audioEl.dataset.objurl);
  const url = URL.createObjectURL(blob);
  audioEl.dataset.objurl = url;
  audioEl.src = url;
  document.getElementById('player').style.display = '';
}

// Live pass: re-transcribe the audio recorded so far. Skipped while a request
// is already in flight (whisper is slower than the timer) or after stop.
async function ppInterim() {
  if (inflight || stopped || chunks.length === 0) return;
  const fd = new FormData();
  fd.append('audio', new Blob(chunks, {type: mimeType}), 'clip.' + ppExt());
  inflight = true;
  try {
    const r = await fetch('{{ url_for("demo_stt_whisper_transcribe") }}', {method: 'POST', body: fd});
    if (!r.ok || stopped) return;
    const j = await r.json();
    if (stopped) return;  // the final pass owns the transcript once stopped
    document.getElementById('transcript').value = j.text || '';
    document.getElementById('meta').textContent = 'live — refining…';
  } catch (e) {
    /* ignore interim errors; the final pass surfaces any real problem */
  } finally {
    inflight = false;
  }
}

// Final pass on stop: authoritative transcript of the whole recording.
async function ppFinal(blob) {
  stopped = true;
  const errEl = document.getElementById('error');
  const btn = document.getElementById('record');
  const fd = new FormData();
  fd.append('audio', blob, 'clip.' + ppExt());

  btn.disabled = true;
  btn.classList.remove('rec');
  btn.innerHTML = '&#9679; Record';
  document.getElementById('hint').textContent = 'Finishing transcription…';
  const t0 = performance.now();
  try {
    const r = await fetch('{{ url_for("demo_stt_whisper_transcribe") }}', {
      method: 'POST',
      body: fd,
    });
    const elapsed = (performance.now() - t0) / 1000;
    if (!r.ok) {
      let msg = 'Transcription failed (' + r.status + ').';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      errEl.textContent = msg;
      return;
    }
    const j = await r.json();
    document.getElementById('transcript').value = j.text || '(no speech detected)';
    const bits = [];
    if (j.language) bits.push('language: ' + j.language);
    if (j.duration) bits.push('audio: ' + j.duration + 's');
    bits.push('transcribed in ' + elapsed.toFixed(1) + 's');
    document.getElementById('meta').textContent = bits.join(' · ');
  } catch (e) {
    errEl.textContent = 'Request failed: ' + e;
  } finally {
    btn.disabled = false;
    document.getElementById('hint').textContent = 'Click record, allow the mic, speak, then stop to finish.';
    document.getElementById('timer').textContent = '';
  }
}

ppHealth();
ppRefreshDevices();
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', ppRefreshDevices);
}
</script>
"""


@app.route("/demo_stt_whisper")
def demo_stt_whisper() -> str:
    return render_template_string(STT_TEMPLATE, service_url=_service_url())


@app.route("/demo_stt_whisper/health")
def demo_stt_whisper_health() -> Response | tuple[Response, int]:
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


@app.route("/demo_stt_whisper/transcribe", methods=["POST"])
def demo_stt_whisper_transcribe() -> Response | tuple[Response, int]:
    url = _service_url()
    audio = request.files.get("audio")
    if audio is None:
        return jsonify({"error": "missing 'audio' file field"}), 400
    files = {"audio": (audio.filename or "clip.webm", audio.stream, audio.mimetype)}
    data = {}
    language = request.form.get("language")
    if language:
        data["language"] = language
    try:
        r = requests.post(
            f"{url}/transcribe", files=files, data=data, timeout=PROXY_TIMEOUT
        )
    except requests.RequestException as e:
        return jsonify({"error": f"Whisper service unreachable at {url}: {e}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"error": f"transcription failed ({r.status_code})"}), r.status_code
