"""Voice echo demo: speak -> transcribe (Whisper STT) -> speak it back (Kokoro TTS).

This page is pure orchestration in the browser. It reuses the same-origin proxy
routes already registered by stt_whisper_views.py and tts_kokoro_views.py, so it
adds no new proxy endpoints — just the combined page. Both backing services must
be running (see whisper_service/ and voice_tts_kokoro/).
"""

from flask import render_template_string

from .core import app

ECHO_TEMPLATE = """
<!doctype html>
<title>Voice echo &mdash; rainbox</title>
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
  select,input[type=range]{vertical-align:middle}
  #transcript{width:100%;max-width:640px;min-height:5em;font-family:inherit;font-size:1rem;padding:8px;white-space:pre-wrap}
  #timer{font-variant-numeric:tabular-nums;color:#dc2626;font-weight:600}
  .meter{display:inline-block;width:220px;height:12px;background:#e5e7eb;border-radius:6px;overflow:hidden;vertical-align:middle}
  .meterbar{display:block;height:100%;width:0;background:#16a34a;transition:width 0.05s linear}
  ol.flow{color:#6b7280;font-size:0.9rem}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Voice echo</h1>
<p class="muted">Say something — it gets transcribed by
<a href="{{ url_for('demo_stt_whisper') }}">Whisper STT</a>, then spoken back by
<a href="{{ url_for('demo_tts_kokoro') }}">Kokoro TTS</a>. Both services must be running.</p>

<div class="row">
  STT: <span id="sttstatus" class="badge unknown">checking&hellip;</span>
  &nbsp; TTS: <span id="ttsstatus" class="badge unknown">checking&hellip;</span>
</div>

<div class="row">
  <label for="device">Microphone</label>
  <select id="device" onchange="ppSaveDevice()"><option value="">Default device</option></select>
  <button id="testmic" onclick="ppTestToggle()" style="background:#0f766e">Test mic</button>
</div>

<div class="row">
  <label for="voice">Voice</label>
  <select id="voice"><option>loading&hellip;</option></select>
  &nbsp;&nbsp;
  <label for="speed">Speed</label>
  <input type="range" id="speed" min="0.5" max="2.0" step="0.1" value="1.0">
  <span id="speedval">1.0&times;</span>
</div>

<div class="row">
  <button id="record" onclick="ppToggle()">&#9679; Record</button>
  <span id="timer"></span>
  <span class="muted" id="hint">Click record, allow the mic, speak, then stop to hear it back.</span>
</div>

<div class="row" id="meterrow" style="display:none">
  <span class="muted">Mic&nbsp;level</span>
  <span class="meter"><span id="meterbar" class="meterbar"></span></span>
  <span class="muted" id="metermsg"></span>
</div>

<div class="row">
  <label for="transcript">Transcript</label><br>
  <textarea id="transcript" placeholder="What you said appears here&hellip;" readonly></textarea>
  <div class="muted" id="meta"></div>
</div>

<div class="row" id="player" style="display:none">
  <div class="muted">Spoken back:</div>
  <audio id="audio" controls></audio>
</div>

<div class="err" id="error"></div>
</div>

<script>
let mediaRecorder = null;
let chunks = [];
let timerId = null;
let startedAt = 0;
let mimeType = 'audio/webm';
let audioCtx = null;
let analyser = null;
let meterRaf = 0;
let sawSignal = false;
let testStream = null;

const DEVICE_KEY = 'stt_device_id';  // shared with the STT page
const SIGNAL_RMS = 0.01;

function ppSaveDevice() {
  try { localStorage.setItem(DEVICE_KEY, document.getElementById('device').value); } catch (e) {}
}
function ppSavedDevice() {
  try { return localStorage.getItem(DEVICE_KEY) || ''; } catch (e) { return ''; }
}
function ppExt() {
  return (mimeType && mimeType.indexOf('ogg') >= 0) ? 'ogg' : 'webm';
}

// --- service health badges ------------------------------------------------
async function ppHealthOne(url, el) {
  const badge = document.getElementById(el);
  try {
    const r = await fetch(url);
    const j = await r.json();
    if (r.ok && j.reachable) { badge.className = 'badge ok'; badge.textContent = 'reachable'; return true; }
  } catch (e) {}
  badge.className = 'badge bad'; badge.textContent = 'unreachable';
  return false;
}
function ppHealth() {
  ppHealthOne('{{ url_for("demo_stt_whisper_health") }}', 'sttstatus');
  ppHealthOne('{{ url_for("demo_tts_kokoro_health") }}', 'ttsstatus');
}

// --- TTS voices -----------------------------------------------------------
async function ppVoices() {
  const sel = document.getElementById('voice');
  try {
    const r = await fetch('{{ url_for("demo_tts_kokoro_voices") }}');
    const j = await r.json();
    sel.innerHTML = '';
    (j.voices || []).forEach(function(v) {
      const o = document.createElement('option');
      o.value = v.id; o.textContent = v.name + ' — ' + v.lang;
      sel.appendChild(o);
    });
    if (!sel.options.length) sel.innerHTML = '<option value="">(no voices)</option>';
  } catch (e) {
    sel.innerHTML = '<option value="">(TTS unreachable)</option>';
  }
}
document.getElementById('speed').addEventListener('input', function() {
  document.getElementById('speedval').textContent = parseFloat(this.value).toFixed(1) + '×';
});

// --- mic device picker + level meter (same approach as the STT page) -------
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
function ppStartMeter(stream) {
  const row = document.getElementById('meterrow');
  const bar = document.getElementById('meterbar');
  document.getElementById('metermsg').textContent = '';
  row.style.display = ''; sawSignal = false;
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const src = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser(); analyser.fftSize = 1024;
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
  } catch (e) { row.style.display = 'none'; }
}
function ppStopMeter() {
  cancelAnimationFrame(meterRaf);
  if (audioCtx) { audioCtx.close().catch(function() {}); audioCtx = null; }
  analyser = null;
  document.getElementById('meterbar').style.width = '0';
  document.getElementById('metermsg').textContent = sawSignal
    ? ''
    : '⚠ No mic signal detected — the browser captured silence. Check your input device/level.';
}
async function ppTestToggle() {
  const btn = document.getElementById('testmic');
  if (testStream) { ppStopTest(); return; }
  const errEl = document.getElementById('error'); errEl.textContent = '';
  try { testStream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints()); }
  catch (e) { errEl.textContent = 'Could not open the selected microphone: ' + e; testStream = null; return; }
  await ppRefreshDevices();
  ppStartMeter(testStream);
  btn.textContent = 'Stop test';
  document.getElementById('hint').textContent = 'Testing — speak and watch the level; pick the device whose bar moves.';
}
function ppStopTest() {
  if (testStream) { testStream.getTracks().forEach(function(t) { t.stop(); }); testStream = null; }
  ppStopMeter();
  document.getElementById('testmic').textContent = 'Test mic';
  document.getElementById('hint').textContent = 'Click record, allow the mic, speak, then stop to hear it back.';
}

// --- record -> transcribe -> speak back -----------------------------------
function ppTick() {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  document.getElementById('timer').textContent = mm + ':' + ss;
}

async function ppToggle() {
  if (mediaRecorder && mediaRecorder.state === 'recording') { mediaRecorder.stop(); return; }
  const errEl = document.getElementById('error');
  errEl.textContent = '';
  ppStopTest();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints()); }
  catch (e) { errEl.textContent = 'Microphone access denied or unavailable: ' + e; return; }
  ppRefreshDevices();
  chunks = [];
  document.getElementById('transcript').value = '';
  document.getElementById('meta').textContent = '';
  document.getElementById('player').style.display = 'none';
  ppStartMeter(stream);
  mediaRecorder = new MediaRecorder(stream);
  mimeType = mediaRecorder.mimeType || 'audio/webm';
  mediaRecorder.ondataavailable = function(e) { if (e.data.size) chunks.push(e.data); };
  mediaRecorder.onstop = function() {
    stream.getTracks().forEach(function(t) { t.stop(); });
    clearInterval(timerId);
    ppStopMeter();
    ppRun(new Blob(chunks, {type: mimeType}));
  };
  mediaRecorder.start(1000);
  startedAt = Date.now(); ppTick();
  timerId = setInterval(ppTick, 250);
  const btn = document.getElementById('record');
  btn.classList.add('rec'); btn.innerHTML = '&#9632; Stop';
  document.getElementById('hint').textContent = 'Recording… click stop to transcribe and hear it back.';
}

async function ppRun(blob) {
  const errEl = document.getElementById('error');
  const btn = document.getElementById('record');
  btn.disabled = true; btn.classList.remove('rec'); btn.innerHTML = '&#9679; Record';
  document.getElementById('timer').textContent = '';

  // 1) transcribe
  document.getElementById('hint').textContent = 'Transcribing…';
  let text = '';
  const tStt = performance.now();
  try {
    const fd = new FormData();
    fd.append('audio', blob, 'clip.' + ppExt());
    const r = await fetch('{{ url_for("demo_stt_whisper_transcribe") }}', {method: 'POST', body: fd});
    if (!r.ok) {
      let msg = 'Transcription failed (' + r.status + ').';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      errEl.textContent = msg; ppDone(); return;
    }
    const j = await r.json();
    text = (j.text || '').trim();
    const sttSecs = ((performance.now() - tStt) / 1000).toFixed(1);
    document.getElementById('transcript').value = text || '(no speech detected)';
    const bits = [];
    if (j.language) bits.push('language: ' + j.language);
    bits.push('transcribed in ' + sttSecs + 's');
    document.getElementById('meta').textContent = bits.join(' · ');
  } catch (e) {
    errEl.textContent = 'STT request failed: ' + e; ppDone(); return;
  }

  if (!text) { document.getElementById('hint').textContent = 'Nothing to speak — say something and try again.'; ppDone(); return; }

  // 2) speak it back
  document.getElementById('hint').textContent = 'Synthesizing speech…';
  const tTts = performance.now();
  try {
    const payload = {
      text: text,
      voice: document.getElementById('voice').value,
      speed: parseFloat(document.getElementById('speed').value),
    };
    const r = await fetch('{{ url_for("demo_tts_kokoro_synthesize") }}', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    if (!r.ok) {
      let msg = 'Synthesis failed (' + r.status + ').';
      try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
      errEl.textContent = msg; ppDone(); return;
    }
    const wav = await r.blob();
    const audioEl = document.getElementById('audio');
    if (audioEl.dataset.objurl) URL.revokeObjectURL(audioEl.dataset.objurl);
    const url = URL.createObjectURL(wav);
    audioEl.dataset.objurl = url; audioEl.src = url;
    document.getElementById('player').style.display = '';
    const ttsSecs = ((performance.now() - tTts) / 1000).toFixed(1);
    document.getElementById('meta').textContent += ' · spoken in ' + ttsSecs + 's';
    audioEl.play().catch(function() {});
  } catch (e) {
    errEl.textContent = 'TTS request failed: ' + e;
  }
  ppDone();
}

function ppDone() {
  const btn = document.getElementById('record');
  btn.disabled = false;
  document.getElementById('hint').textContent = 'Click record, allow the mic, speak, then stop to hear it back.';
}

ppHealth();
ppVoices();
ppRefreshDevices();
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', ppRefreshDevices);
}
</script>
"""


@app.route("/demo_voice_echo")
def demo_voice_echo() -> str:
    return render_template_string(ECHO_TEMPLATE)
