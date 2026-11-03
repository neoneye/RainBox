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

Every transcribe call — live passes included — is recorded on /activity
through `record_direct_call`, since no LlamaIndex event fires for it; the
row's prompt shows the instruction and a line describing the clip.
"""

import base64
import io
import os
import wave
from datetime import UTC, datetime

import requests
from flask import Response, jsonify, render_template_string, request

from llm.activity import record_direct_call

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
  button.secondary{background:#0f766e}
  button.secondary:hover{background:#115e59}
  .mics{list-style:none;margin:0.5em 0 0;padding:0;max-width:640px}
  .mics li{border:1px solid #e5e7eb;border-radius:8px;margin:0 0 6px}
  .mics li.chosen{border-color:#2563eb;background:#eff6ff}
  .mics label{display:flex;align-items:center;gap:10px;padding:8px 10px;font-weight:400;cursor:pointer}
  .mics .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mics .meter{width:140px;flex:none}
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
  <label>Microphone</label>
  <button id="allowmics" class="secondary" onclick="ppAllowMics()" style="display:none">Show microphones</button>
  <button id="testmics" class="secondary" onclick="ppTestToggle()" style="display:none">Test mics</button>
  <span class="muted" id="michint"></span>
  <ul class="mics" id="mics"></ul>
</div>

<div class="row">
  <button id="record" onclick="ppToggle()">&#9679; Record</button>
  <span id="timer"></span>
  <label class="muted" style="font-weight:400"><input type="checkbox" id="live" checked> Live transcript</label>
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
let interimId = null;     // rolling live-transcription timer
let startedAt = 0;
let mimeType = 'audio/webm';
let take = 0;             // counts recordings, so a late live pass can't land in the next one
let inflight = false;     // a live pass is in flight
let stopped = true;       // recording stopped; the final pass owns the answer
let audioCtx = null;      // Web Audio graph for the live mic-level meter
let meterRaf = 0;
let sawSignal = false;    // did the mic ever produce a non-trivial level?

const SIGNAL_RMS = 0.01;  // RMS above this counts as "the mic is picking up sound"
const WAV_RATE = 16000;   // what the model's audio encoder works at
const INTERIM_MS = 2500;  // how often to re-transcribe the growing recording
const DEVICE_KEY = 'stt_device_id';  // shared with the Whisper page

let chosenId = '';        // the picked mic's deviceId; '' is the browser's default
let testing = false;      // "Test mics" is running
let testStreams = [];     // one open stream per mic while testing
let testCtx = null;
let testRaf = 0;

function ppSaveDevice() {
  try { localStorage.setItem(DEVICE_KEY, chosenId); }
  catch (e) { /* storage may be unavailable (private mode); not fatal */ }
}

function ppSavedDevice() {
  try { return localStorage.getItem(DEVICE_KEY) || ''; }
  catch (e) { return ''; }
}

function ppAudioConstraints() {
  return {audio: chosenId ? {deviceId: {exact: chosenId}} : true};
}

function ppRms(analyser, data) {
  analyser.getByteTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
  return Math.sqrt(sum / data.length);
}

function ppChoose(id) {
  chosenId = id;
  ppSaveDevice();
  document.querySelectorAll('#mics li').forEach(function(li) {
    li.classList.toggle('chosen', li.dataset.id === id);
    li.querySelector('input').checked = li.dataset.id === id;
  });
}

// The browser names its microphones only once the site may use one, so this
// asks for access a single time and closes the stream straight away.
async function ppAllowMics() {
  try {
    const s = await navigator.mediaDevices.getUserMedia({audio: true});
    s.getTracks().forEach(function(t) { t.stop(); });
  } catch (e) {
    document.getElementById('michint').textContent =
      'The browser refused the microphone (' + e.name + '). Allow it for this site in the site settings, then reload.';
    return;
  }
  await ppRefreshDevices();
}

async function ppRefreshDevices() {
  const list = document.getElementById('mics');
  const hint = document.getElementById('michint');
  let devices;
  try { devices = await navigator.mediaDevices.enumerateDevices(); }
  catch (e) { hint.textContent = 'This browser cannot list microphones.'; return; }
  const mics = devices.filter(function(d) { return d.kind === 'audioinput'; });
  const named = mics.some(function(d) { return d.label; });
  document.getElementById('allowmics').style.display = named ? 'none' : '';
  document.getElementById('testmics').style.display = named ? '' : 'none';
  if (!named) {
    list.innerHTML = '';
    chosenId = '';
    hint.textContent = mics.length
      ? 'The browser names its microphones only once this site may use the mic.'
      : 'No microphone found.';
    return;
  }
  const wasTesting = testing;
  if (wasTesting) ppStopTest();
  const desired = chosenId || ppSavedDevice();
  list.innerHTML = '';
  mics.forEach(function(d, i) {
    const li = document.createElement('li');
    li.dataset.id = d.deviceId;
    const label = document.createElement('label');
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'mic';
    radio.onchange = function() { ppChoose(d.deviceId); };
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = d.label || ('Microphone ' + (i + 1));
    name.title = name.textContent;
    const meter = document.createElement('span');
    meter.className = 'meter';
    meter.innerHTML = '<span class="meterbar"></span>';
    label.append(radio, name, meter);
    li.appendChild(label);
    list.appendChild(li);
  });
  const ids = mics.map(function(d) { return d.deviceId; });
  const pick = ids.indexOf(desired) >= 0 ? desired
    : (ids.indexOf('default') >= 0 ? 'default' : ids[0]);
  ppChoose(pick);
  hint.textContent = (desired && pick !== desired)
    ? 'The microphone picked last time is not connected; picked ' + mics[ids.indexOf(pick)].label + ' instead.'
    : mics.length + (mics.length === 1 ? ' microphone.' : ' microphones. Test mics shows which one hears you.');
  if (wasTesting) ppTestStart();
}

function ppTestToggle() {
  if (testing) ppStopTest(); else ppTestStart();
}

// Open every listed mic at once and give each row its own level bar: the one
// whose bar moves while you speak is the one that hears you.
async function ppTestStart() {
  testing = true;
  const btn = document.getElementById('testmics');
  btn.textContent = 'Stop test';
  testCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (testCtx.state === 'suspended') testCtx.resume();
  const meters = [];
  for (const li of document.querySelectorAll('#mics li')) {
    let s;
    try { s = await navigator.mediaDevices.getUserMedia({audio: {deviceId: {exact: li.dataset.id}}}); }
    catch (e) { li.querySelector('.name').title = 'could not open: ' + e.name; continue; }
    if (!testing) { s.getTracks().forEach(function(t) { t.stop(); }); return; }
    testStreams.push(s);
    const analyser = testCtx.createAnalyser();
    analyser.fftSize = 1024;
    testCtx.createMediaStreamSource(s).connect(analyser);
    meters.push({analyser: analyser, bar: li.querySelector('.meterbar')});
  }
  const data = new Uint8Array(1024);
  (function tick() {
    meters.forEach(function(m) {
      m.bar.style.width = Math.min(100, Math.round(ppRms(m.analyser, data) * 300)) + '%';
    });
    testRaf = requestAnimationFrame(tick);
  })();
}

function ppStopTest() {
  testing = false;
  cancelAnimationFrame(testRaf);
  testStreams.forEach(function(s) { s.getTracks().forEach(function(t) { t.stop(); }); });
  testStreams = [];
  if (testCtx) { testCtx.close().catch(function() {}); testCtx = null; }
  document.querySelectorAll('#mics .meterbar').forEach(function(b) { b.style.width = '0'; });
  document.getElementById('testmics').textContent = 'Test mics';
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
      const rms = ppRms(analyser, data);
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
  ppStopTest();  // don't hold the mics open in two places at once
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(ppAudioConstraints());
  } catch (e) {
    errEl.textContent = 'Microphone access denied or unavailable: ' + e;
    return;
  }
  const micName = stream.getAudioTracks()[0].label;
  ppRefreshDevices();
  chunks = [];
  take++;
  stopped = false;
  document.getElementById('transcript').value = '';
  document.getElementById('meta').textContent = '';
  ppStartMeter(stream);
  mediaRecorder = new MediaRecorder(stream);
  mimeType = mediaRecorder.mimeType || 'audio/webm';
  mediaRecorder.ondataavailable = function(e) { if (e.data.size) chunks.push(e.data); };
  mediaRecorder.onstop = function() {
    stream.getTracks().forEach(function(t) { t.stop(); });
    clearInterval(timerId);
    clearInterval(interimId);
    stopped = true;
    ppStopMeter();
    const btn = document.getElementById('record');
    btn.classList.remove('rec');
    btn.innerHTML = '&#9679; Record';
    document.getElementById('timer').textContent = '';
    ppTranscribe(new Blob(chunks, {type: mimeType}), micName ? 'mic: ' + micName : '');
  };
  // The container header lives only in the first chunk, so the chunks so far
  // always make a decodable file for a live pass. A chunk every second.
  mediaRecorder.start(1000);
  startedAt = Date.now();
  ppTick();
  timerId = setInterval(ppTick, 250);
  interimId = setInterval(ppInterim, INTERIM_MS);
  const btn = document.getElementById('record');
  btn.classList.add('rec');
  btn.innerHTML = '&#9632; Stop';
  document.getElementById('hint').textContent = document.getElementById('live').checked
    ? 'Recording… the transcript updates live; click stop for the final pass.'
    : 'Recording… click stop to transcribe.';
}

// Live pass: re-transcribe everything recorded so far. Skipped while one is
// already in flight (a pass over a long recording outlasts the timer), when
// live is off, or once stopped; errors are left to the final pass.
async function ppInterim() {
  if (inflight || stopped || !chunks.length || !document.getElementById('live').checked) return;
  const mine = take;
  inflight = true;
  try {
    const wav = await ppToWav(new Blob(chunks, {type: mimeType}));
    const res = await ppAsk(wav);
    if (stopped || mine !== take || !res.ok) return;
    document.getElementById('transcript').value = res.j.text || '';
    document.getElementById('meta').textContent = 'live — ' +
      (res.j.duration ? res.j.duration + 's heard, ' : '') + 'refining…';
  } catch (e) {
    /* a half-written chunk may not decode yet; the next pass or the final one covers it */
  } finally {
    inflight = false;
  }
}

async function ppAsk(wav) {
  const fd = new FormData();
  fd.append('audio', wav, 'clip.wav');
  fd.append('prompt', document.getElementById('prompt').value);
  const r = await fetch('{{ url_for("demo_stt_gemma4_transcribe") }}', {method: 'POST', body: fd});
  let j = {};
  try { j = await r.json(); } catch (e) {}
  return {ok: r.ok, status: r.status, j: j};
}

function ppPickFile(input) {
  const f = input.files && input.files[0];
  input.value = '';
  if (f) {
    document.getElementById('error').textContent = '';
    document.getElementById('transcript').value = '';
    ppTranscribe(f, 'file: ' + f.name);
  }
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

// `source` names where the clip came from (the mic the browser actually
// opened, or the file), shown with the answer.
async function ppTranscribe(blob, source) {
  const errEl = document.getElementById('error');
  const btn = document.getElementById('record');
  const out = document.getElementById('transcript');
  const meta = document.getElementById('meta');
  let wav;
  try { wav = await ppToWav(blob); }
  catch (e) { errEl.textContent = 'Could not decode that audio: ' + e; return; }
  ppPlay(wav);
  btn.disabled = true;
  meta.textContent = out.value ? 'live — finishing…' : '';
  document.getElementById('hint').textContent = 'Transcribing…';
  const t0 = performance.now();
  try {
    const res = await ppAsk(wav);
    const elapsed = (performance.now() - t0) / 1000;
    const j = res.j;
    if (!res.ok) { errEl.textContent = j.error || ('Transcription failed (' + res.status + ').'); return; }
    out.value = j.text || '(empty answer)';
    const bits = [];
    if (source) bits.push(source);
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
// Granting the mic from the address bar names the devices without a reload.
if (navigator.permissions && navigator.permissions.query) {
  navigator.permissions.query({name: 'microphone'})
    .then(function(p) { p.onchange = ppRefreshDevices; })
    .catch(function() {});
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
    duration = _wav_seconds(raw)
    activity = {
        "started_at": datetime.now(UTC), "provider": "ollama", "model": model,
        "caller": "demo_stt_gemma4",
        "messages": [{"role": "user", "content": f"{prompt}\n\n[audio: "
                      f"{duration if duration is not None else '?'}s WAV, {len(raw)} bytes]"}],
    }
    try:
        r = requests.post(f"{url}/api/chat", json=body, timeout=PROXY_TIMEOUT)
    except requests.RequestException as e:
        record_direct_call(**activity, exc=e)
        return jsonify({"error": f"Ollama unreachable at {url}: {e}"}), 502
    try:
        reply = r.json()
    except ValueError:
        record_direct_call(**activity, error=f"Ollama answered {r.status_code} without JSON")
        return jsonify({"error": f"transcription failed ({r.status_code})"}), 502
    if r.status_code != 200:
        error = reply.get("error") or f"transcription failed ({r.status_code})"
        record_direct_call(**activity, raw=reply, error=error)
        return jsonify({"error": error}), r.status_code
    text = ((reply.get("message") or {}).get("content") or "").strip()
    record_direct_call(**activity, raw=reply, response=text)
    return jsonify({
        "text": text,
        "model": model,
        "duration": duration,
        "prompt_tokens": reply.get("prompt_eval_count"),
    })
