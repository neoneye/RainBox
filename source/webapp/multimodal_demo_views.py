"""Demo page for poking a local vision+audio model (a Gemma variant).

A throwaway page to build intuition about how the model handles image/audio
input. The browser posts a system prompt, a user prompt, and one optional file
(image OR audio) to a same-origin proxy, which reads the target ModelConfig
(read-only) for its backend base URL/model_name (see `_backend_base`), builds
an OpenAI-compatible multimodal /chat/completions request, and streams the
backend's SSE response straight back. Nothing is persisted.

Direct OpenAI-compatible passthrough (not llama_index) is deliberate: the point
is to see the raw multimodal request/response behavior, including backend errors
like "this model can't do audio", verbatim.
"""

import base64
from uuid import UUID

import requests
from flask import Response, jsonify, render_template_string, request
from werkzeug.datastructures import FileStorage

import providers
from db import ModelConfig, db

from .core import app

# The vision+audio model to talk to by default; override per-request with ?id=.
DEFAULT_MODEL_UUID = "00ea3152-ff12-40e1-a63b-8f572de49edf"
PROXY_TIMEOUT = 300  # seconds; multimodal generation on a local box can be slow

_AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/aac": "aac",
}


def _audio_format(mime: str) -> str:
    """OpenAI `input_audio` format string for an audio MIME type, falling back
    to the MIME subtype when unknown."""
    if mime in _AUDIO_FORMATS:
        return _AUDIO_FORMATS[mime]
    return mime.split("/", 1)[-1] or "wav"


def _build_completion_body(
    model_name: str, system: str, user: str, file: FileStorage | None
) -> dict:
    """OpenAI-compatible /chat/completions body. The user message is a
    content-parts array: a text part plus, if a file is attached, an image_url
    (image/*) or input_audio (audio/*) part."""
    parts: list[dict] = [{"type": "text", "text": user}]
    if file is not None and file.filename:
        raw = file.read()
        b64 = base64.b64encode(raw).decode("ascii")
        mime = file.mimetype or ""
        if mime.startswith("image/"):
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        elif mime.startswith("audio/"):
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {"data": b64, "format": _audio_format(mime)},
                }
            )
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": parts})
    return {"model": model_name, "messages": messages, "stream": True}


def _backend_base(model: ModelConfig) -> str | None:
    """The OpenAI-compatible base URL (no trailing slash) to send this
    model's requests to. Jan/LM Studio models store the full base
    (including `/v1`) in `arguments["api_base"]`. Ollama models store none —
    they use the provider's default base URL, discovered via the provider
    registry, with `/v1` appended. Returns None if neither source yields a
    base (e.g. an unregistered provider id)."""
    args = model.arguments or {}
    api_base = (args.get("api_base") or "").rstrip("/")
    if api_base:
        return api_base
    try:
        provider = providers.get(model.provider)
    except KeyError:
        return None
    return provider.base_url().rstrip("/") + "/v1"


def _resolve_model(id_param: str | None) -> ModelConfig | None:
    """Look up the target model by UUID (defaulting to DEFAULT_MODEL_UUID).
    Returns None for an unparseable id or a missing row."""
    raw = id_param or DEFAULT_MODEL_UUID
    try:
        uid = UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None
    return db.session.query(ModelConfig).filter_by(uuid=uid).first()


MULTIMODAL_TEMPLATE = """
<!doctype html>
<title>Multimodal demo &mdash; rainbox</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:0}
  .row{margin:1em 0}
  textarea{width:100%;max-width:760px;font-family:ui-monospace,monospace;font-size:0.95rem;padding:8px;box-sizing:border-box}
  label{font-weight:600;display:block;margin-bottom:0.3em}
  button{padding:8px 18px;border:none;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;font-size:0.95rem}
  button:hover{background:#1d4ed8}
  button:disabled{background:#9ca3af;cursor:default}
  .muted{color:#6b7280;font-size:0.85rem}
  .err{color:#991b1b;white-space:pre-wrap}
  #preview img{max-width:280px;max-height:280px;border:1px solid #e5e7eb;border-radius:8px}
  #status{font-weight:600}
  #response{width:100%;max-width:760px;min-height:8em;border:1px solid #e5e7eb;border-radius:8px;
            padding:12px;white-space:pre-wrap;font-size:1rem;background:#fbfbfb}
  code{background:#eee;padding:1px 4px;border-radius:3px}
</style>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Multimodal demo</h1>
{% if model_name %}
<p class="muted">Talking to <b>{{ model_name }}</b> (<code>{{ model_id }}</code>).
Attach one image or audio file, add prompts, and watch the streamed response.
Nothing is saved.</p>

<div class="row">
  <label for="system">System prompt</label>
  <textarea id="system" rows="3" placeholder="(optional)"></textarea>
</div>
<div class="row">
  <label for="user">User prompt</label>
  <textarea id="user" rows="3" placeholder="Describe the image / transcribe the audio&hellip;"></textarea>
</div>
<div class="row">
  <label for="file">Image or audio file</label>
  <input type="file" id="file" accept="image/*,audio/*" onchange="ppPreview()">
  <button type="button" onclick="ppClearFile()" style="background:#6b7280">Clear</button>
  <div id="preview" style="margin-top:0.6em"></div>
</div>
<div class="row">
  <button id="send" onclick="ppSend()">Send</button>
  <span id="status" class="muted"></span>
</div>
<div class="row">
  <label>Response</label>
  <div id="response"></div>
</div>

<script>
const MODEL_ID = {{ model_id | tojson }};

function ppPreview() {
  const f = document.getElementById('file').files[0];
  const box = document.getElementById('preview');
  box.innerHTML = '';
  if (!f) return;
  const url = URL.createObjectURL(f);
  if (f.type.startsWith('image/')) {
    const img = document.createElement('img');
    img.src = url;
    box.appendChild(img);
  } else if (f.type.startsWith('audio/')) {
    const a = document.createElement('audio');
    a.controls = true; a.src = url;
    box.appendChild(a);
  }
  const cap = document.createElement('div');
  cap.className = 'muted';
  cap.textContent = f.name + ' (' + (f.type || 'unknown type') + ')';
  box.appendChild(cap);
}

function ppClearFile() {
  document.getElementById('file').value = '';
  document.getElementById('preview').innerHTML = '';
}

async function ppSend() {
  const btn = document.getElementById('send');
  const status = document.getElementById('status');
  const out = document.getElementById('response');
  out.className = '';
  out.textContent = '';
  btn.disabled = true;
  status.textContent = 'sending\\u2026';

  const fd = new FormData();
  fd.append('system', document.getElementById('system').value);
  fd.append('user', document.getElementById('user').value);
  const f = document.getElementById('file').files[0];
  if (f) fd.append('file', f, f.name);

  let resp;
  try {
    resp = await fetch('/demo/multimodal/complete?id=' + encodeURIComponent(MODEL_ID),
                       {method: 'POST', body: fd});
  } catch (e) {
    status.textContent = 'error';
    out.className = 'err';
    out.textContent = 'network error: ' + e;
    btn.disabled = false;
    return;
  }

  if (!resp.ok) {
    const text = await resp.text();
    status.textContent = 'error (' + resp.status + ')';
    out.className = 'err';
    out.textContent = text;
    btn.disabled = false;
    return;
  }

  status.textContent = 'streaming\\u2026';
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    // SSE frames are separated by blank lines; each line may be "data: {...}".
    const lines = buf.split('\\n');
    buf = lines.pop();  // keep the (possibly partial) last line
    for (const line of lines) {
      const t = line.trim();
      if (!t.startsWith('data:')) continue;
      const payload = t.slice(5).trim();
      if (payload === '[DONE]' || payload === '') continue;
      try {
        const obj = JSON.parse(payload);
        const delta = obj.choices && obj.choices[0] && obj.choices[0].delta;
        if (delta && delta.content) out.textContent += delta.content;
      } catch (e) { /* ignore keep-alives / non-JSON frames */ }
    }
  }
  status.textContent = 'done';
  btn.disabled = false;
}
</script>
{% else %}
<p class="err">Model <code>{{ model_id }}</code> not found in model_config.
Pass a valid <code>?id=&lt;uuid&gt;</code> for a registered vision/audio model.</p>
{% endif %}
</div>
"""


@app.route("/demo/multimodal")
def demo_multimodal() -> str:
    model = _resolve_model(request.args.get("id"))
    return render_template_string(
        MULTIMODAL_TEMPLATE,
        model_name=(model.effective_display_name if model else None),
        model_id=(request.args.get("id") or DEFAULT_MODEL_UUID),
    )


@app.route("/demo/multimodal/complete", methods=["POST"])
def demo_multimodal_complete() -> Response | tuple[Response, int]:
    model = _resolve_model(request.args.get("id"))
    if model is None:
        return jsonify({"error": "model not found"}), 404

    system = request.form.get("system", "")
    user = request.form.get("user", "")
    file = request.files.get("file")
    has_file = file is not None and bool(file.filename)
    if not user.strip() and not has_file:
        return jsonify({"error": "nothing to send: provide a prompt or a file"}), 400

    base = _backend_base(model)
    if base is None:
        return jsonify({"error": "could not resolve a backend URL for this model"}), 400

    body = _build_completion_body(model.model_name, system, user, file if has_file else None)
    headers = {"Content-Type": "application/json"}
    api_key = (model.arguments or {}).get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        upstream = requests.post(
            f"{base}/chat/completions",
            json=body,
            headers=headers,
            stream=True,
            timeout=PROXY_TIMEOUT,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"backend unreachable at {base}: {e}"}), 502

    # Non-2xx: forward the raw error body verbatim — seeing the backend's own
    # complaint ("this model can't do audio") is the point of the demo.
    if upstream.status_code != 200:
        content_type = upstream.headers.get("Content-Type", "text/plain")
        mimetype = content_type.split(";")[0].strip() or "text/plain"
        return Response(
            upstream.content,
            status=upstream.status_code,
            mimetype=mimetype,
        )

    def relay():
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        relay(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
