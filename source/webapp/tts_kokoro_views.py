"""Demo page for the Kokoro-82M TTS subproject.

The page is same-origin only: the browser talks to these proxy routes, which
forward to the Kokoro REST service (default http://127.0.0.1:5005, overridable
with the KOKORO_TTS_URL env var) using `requests`. This keeps torch out of the
main app and avoids cross-origin requests from the browser.
"""

import os

import requests
from flask import Response, jsonify, render_template_string, request

from .core import app

DEFAULT_KOKORO_TTS_URL = "http://127.0.0.1:5005"
PROXY_TIMEOUT = 60  # seconds; synthesis of long text can take a while
HEALTH_TIMEOUT = 3  # seconds; the status badge must not hang on a dead service


def _service_url() -> str:
    return os.environ.get("KOKORO_TTS_URL", DEFAULT_KOKORO_TTS_URL).rstrip("/")


TTS_TEMPLATE = """
<!doctype html>
<title>Kokoro TTS &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .row{margin:1em 0}
  textarea{width:100%;max-width:640px;font-family:inherit;font-size:1rem;padding:8px}
  select,input[type=range]{vertical-align:middle}
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
<h1>Kokoro-82M TTS</h1>
<p class="muted">Synthesizes speech with the
<a href="https://huggingface.co/hexgrad/Kokoro-82M" target="_blank" rel="noopener">hexgrad/Kokoro-82M</a>
service. Start it from <code>voice_tts_kokoro/</code> (see its README).</p>

<div class="row">
  Service: <span id="status" class="badge unknown">checking&hellip;</span>
  <span class="muted">{{ service_url }}</span>
</div>

<div class="row">
  <label for="text">Text</label><br>
  <textarea id="text" rows="4" placeholder="Type something to speak&hellip;">Kokoro is an open-weight text to speech model.</textarea>
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
  <button id="speak" onclick="ppSpeak()">Synthesize</button>
</div>

<div class="row" id="player" style="display:none">
  <audio id="audio" controls></audio>
  <div><a id="download" download="kokoro.wav">Download .wav</a></div>
</div>

<div class="err" id="error"></div>
</div>

<script>
async function ppHealth() {
  const badge = document.getElementById('status');
  try {
    const r = await fetch('{{ url_for("demo_tts_kokoro_health") }}');
    const j = await r.json();
    if (r.ok && j.reachable) {
      badge.className = 'badge ok'; badge.textContent = 'reachable';
    } else {
      badge.className = 'badge bad'; badge.textContent = 'unreachable';
    }
  } catch (e) {
    badge.className = 'badge bad'; badge.textContent = 'unreachable';
  }
}

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
    sel.innerHTML = '<option value="">(service unreachable)</option>';
  }
}

document.getElementById('speed').addEventListener('input', function() {
  document.getElementById('speedval').textContent = parseFloat(this.value).toFixed(1) + '×';
});

async function ppSpeak() {
  const errEl = document.getElementById('error');
  const btn = document.getElementById('speak');
  errEl.textContent = '';
  const text = document.getElementById('text').value.trim();
  if (!text) { errEl.textContent = 'Please enter some text.'; return; }
  const payload = {
    text: text,
    voice: document.getElementById('voice').value,
    speed: parseFloat(document.getElementById('speed').value),
  };
  btn.disabled = true; btn.textContent = 'Synthesizing…';
  try {
    const r = await fetch('{{ url_for("demo_tts_kokoro_synthesize") }}', {
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


@app.route("/demo_tts_kokoro")
def demo_tts_kokoro() -> str:
    return render_template_string(TTS_TEMPLATE, service_url=_service_url())


@app.route("/demo_tts_kokoro/health")
def demo_tts_kokoro_health() -> Response | tuple[Response, int]:
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


@app.route("/demo_tts_kokoro/voices")
def demo_tts_kokoro_voices() -> Response | tuple[Response, int]:
    url = _service_url()
    try:
        r = requests.get(f"{url}/voices", timeout=PROXY_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"error": f"Kokoro service unreachable at {url}: {e}"}), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"error": "invalid response from service"}), 502


@app.route("/demo_tts_kokoro/synthesize", methods=["POST"])
def demo_tts_kokoro_synthesize() -> Response | tuple[Response, int]:
    url = _service_url()
    payload = request.get_json(silent=True) or {}
    try:
        r = requests.post(f"{url}/tts", json=payload, timeout=PROXY_TIMEOUT)
    except requests.RequestException as e:
        return jsonify({"error": f"Kokoro service unreachable at {url}: {e}"}), 502
    if r.status_code == 200:
        return Response(r.content, status=200, content_type="audio/wav")
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify({"error": f"TTS failed ({r.status_code})"}), r.status_code
