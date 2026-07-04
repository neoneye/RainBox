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
from collections import namedtuple
from uuid import UUID

import requests
from flask import Response, jsonify, render_template_string, request
from werkzeug.datastructures import FileStorage

import providers
from db import (
    get_model_config,
    get_model_config_override,
    list_model_configs_with_overrides,
    resolved_model_kwargs,
)

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


# The one image MIME type we send to the model untouched. Every other type
# (jpeg, webp, avif, gif, …) is transcoded to PNG first: this normalizes the
# encoding/color/orientation quirks (CMYK jpegs, progressive jpegs, EXIF
# rotation) that make some otherwise-valid images fail at the model.
_MODEL_IMAGE_MIMES = {"image/png"}


class ImageConversionError(Exception):
    """A selected image could not be decoded/transcoded to a model-accepted
    format (unsupported or corrupt image)."""


def _image_to_model_format(mime: str, raw: bytes) -> tuple[str, bytes]:
    """Return (mime, bytes) for an image the model accepts. PNG passes through
    untouched; every other image type (jpeg, webp, avif, gif, …) is decoded and
    re-encoded as PNG, applying EXIF orientation. Raises ImageConversionError if
    the bytes cannot be decoded as an image."""
    if mime in _MODEL_IMAGE_MIMES:
        return mime, raw
    import io

    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise ImageConversionError(f"could not decode {mime or 'image'}: {e}") from e
    # Bake in EXIF rotation (common on phone jpegs) so the model sees it upright.
    img = ImageOps.exif_transpose(img)
    # PNG can't store CMYK / exotic modes; normalize to one it can.
    if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        img = img.convert("RGBA")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return "image/png", out.getvalue()


def _build_completion_body(
    model_name: str, system: str, user: str, files: list[FileStorage]
) -> dict:
    """OpenAI-compatible /chat/completions body. The user message is a
    content-parts array: a text part followed by one part per attached file —
    an image_url (image/*) or input_audio (audio/*) — in the order given. Every
    image except PNG (jpeg, webp, avif, …) is transcoded to PNG first."""
    parts: list[dict] = [{"type": "text", "text": user}]
    for file in files:
        if file is None or not file.filename:
            continue
        raw = file.read()
        mime = file.mimetype or ""
        if mime.startswith("image/"):
            out_mime, out_raw = _image_to_model_format(mime, raw)
            b64 = base64.b64encode(out_raw).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{out_mime};base64,{b64}"}}
            )
        elif mime.startswith("audio/"):
            b64 = base64.b64encode(raw).decode("ascii")
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


def _backend_base(provider: str, arguments: dict) -> str | None:
    """The OpenAI-compatible base URL (no trailing slash) to send this
    model's requests to. Jan/LM Studio models store the full base
    (including `/v1`) in `arguments["api_base"]`. Ollama models store none —
    they use the provider's default base URL, discovered via the provider
    registry, with `/v1` appended. Returns None if neither source yields a
    base (e.g. an unregistered provider id)."""
    api_base = (arguments.get("api_base") or "").strip()
    if api_base:
        return api_base.rstrip("/")
    try:
        return providers.get(provider).base_url().rstrip("/") + "/v1"
    except KeyError:
        return None


_Target = namedtuple("_Target", "uuid kind display_name provider model_name arguments")


def _resolve_target(id_param: str | None) -> "_Target | None":
    """Resolve ?id (a ModelConfig OR ModelConfigOverride uuid; default
    DEFAULT_MODEL_UUID) into a _Target, or None if unparseable/absent."""
    raw = id_param or DEFAULT_MODEL_UUID
    try:
        uid = UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        provider, model_name, kwargs = resolved_model_kwargs(uid)
    except LookupError:
        return None
    cfg = get_model_config(uid)
    if cfg is not None:
        return _Target(uid, "config", cfg.effective_display_name, provider, model_name, kwargs)
    ov = get_model_config_override(uid)
    display = ov.effective_display_name if ov is not None else model_name
    return _Target(uid, "override", display, provider, model_name, kwargs)


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
  #preview{display:flex;flex-wrap:wrap;gap:10px}
  .pp-file{position:relative;border:1px solid #e5e7eb;border-radius:8px;padding:6px;max-width:210px}
  .pp-file img{max-width:190px;max-height:190px;border-radius:6px;display:block}
  .pp-file audio{max-width:190px;display:block}
  .pp-file .pp-cap{margin-top:4px;word-break:break-all}
  .pp-file .pp-rm{position:absolute;top:4px;right:4px;width:22px;height:22px;padding:0;border:none;
    border-radius:50%;background:#dc2626;color:#fff;font-size:14px;line-height:1;cursor:pointer}
  #status{font-weight:600}
  #response{width:100%;max-width:760px;min-height:8em;border:1px solid #e5e7eb;border-radius:8px;
            padding:12px;white-space:pre-wrap;font-size:1rem;background:#fbfbfb}
  #reasoning-box summary{cursor:pointer}
  #reasoning{max-width:760px;white-space:pre-wrap;color:#6b7280;font-size:0.9rem;
             border-left:3px solid #e5e7eb;padding:6px 12px;margin-top:6px;background:#fbfbfb}
  code{background:#eee;padding:1px 4px;border-radius:3px}
  #dropzone{position:fixed;inset:0;z-index:1000;display:none;align-items:center;justify-content:center;
            background:rgba(37,99,235,0.10);border:3px dashed #2563eb;pointer-events:none;
            font-size:1.4rem;font-weight:600;color:#1d4ed8}
  body.pp-dragging #dropzone{display:flex}
  ul.tree{list-style:none;margin:0;padding:0}
  ul.tree ul{list-style:none;margin:0;padding:0 0 0 1.2em}
  ul.tree li{margin:0.15em 0;line-height:1.3}
  ul.tree a{display:block;padding:0.2em 0.4em;border-radius:3px;text-decoration:none;color:inherit}
  ul.tree a:hover{background:#eef}
  ul.tree a.selected{background:#dde7ff;font-weight:600}
  .pp-provider-badge{display:inline-block;font-size:75%;padding:0 0.4em;
    border-radius:0.4em;margin-right:0.3em;background:#dbeafe;color:#1e40af;
    vertical-align:0.05em}
</style>
<div id="dropzone">Drop image or audio files anywhere</div>
{% include "_nav.html" %}
<div class="pp-content">
<h1>Multimodal demo</h1>

<details class="row" {% if not target %}open{% endif %}>
  <summary>Model: <b>{% if target %}{{ target.display_name }}{% else %}(none selected){% endif %}</b> &mdash; choose</summary>
  <ul class="tree">
    {% for cfg, overrides in tree %}
    <li>
      <a href="{{ url_for('demo_multimodal', id=cfg.uuid) }}"
         class="{% if target and target.kind == 'config' and target.uuid == cfg.uuid %}selected{% endif %}">
        <span class="pp-provider-badge">{% if cfg.provider == 'lm_studio' %}LM Studio{% elif cfg.provider == 'jan' %}Jan{% elif cfg.provider == 'ollama' %}Ollama{% else %}{{ cfg.provider }}{% endif %}</span>
        {{ cfg.model_name }}{% if not cfg.available %} <span class="muted">(unavailable)</span>{% endif %}
      </a>
      {% if overrides %}
      <ul>
        {% for ov in overrides %}
        <li><a href="{{ url_for('demo_multimodal', id=ov.uuid) }}"
               class="{% if target and target.kind == 'override' and target.uuid == ov.uuid %}selected{% endif %}">{{ ov.effective_display_name }}</a></li>
        {% endfor %}
      </ul>
      {% endif %}
    </li>
    {% endfor %}
  </ul>
</details>

{% if target %}
<p class="muted">Talking to <b>{{ target.display_name }}</b> (<code>{{ model_id }}</code>).
Attach one or more images or audio files, add prompts, and watch the streamed
response. Nothing is saved.</p>

<div class="row">
  <label for="system">System prompt</label>
  <textarea id="system" rows="3" placeholder="(optional)"></textarea>
</div>
<div class="row">
  <label for="user">User prompt</label>
  <textarea id="user" rows="3" placeholder="Describe the image / transcribe the audio&hellip;"></textarea>
</div>
<div class="row">
  <label for="file">Image or audio files</label>
  <input type="file" id="file" accept="image/*,.avif,.webp,audio/*" multiple onchange="ppPick(this)">
  <button type="button" onclick="ppClearFiles()" style="background:#6b7280">Clear all</button>
  <div id="preview" style="margin-top:0.6em"></div>
</div>
<div class="row">
  <button id="send" onclick="ppSend()">Send</button>
  <button id="stop" onclick="ppStop()" style="background:#dc2626;display:none">Stop</button>
  <span id="status" class="muted"></span>
</div>
<div class="row" id="reasoning-row" style="display:none">
  <details id="reasoning-box" open>
    <summary class="muted">Reasoning</summary>
    <div id="reasoning"></div>
  </details>
</div>
<div class="row">
  <label>Response</label>
  <div id="response"></div>
</div>

<script>
const MODEL_ID = {{ model_id | tojson }};

// Persist the prompts across reloads so a page refresh doesn't lose typed text.
// (Attached files aren't stored — they can't survive a reload.)
(function ppRestorePrompts() {
  const fields = [['system', 'pp-mm-system'], ['user', 'pp-mm-user']];
  try {
    for (const [id, key] of fields) {
      const el = document.getElementById(id);
      const saved = localStorage.getItem(key);
      if (saved !== null) el.value = saved;
      el.addEventListener('input', function() {
        try { localStorage.setItem(key, el.value); } catch (e) { /* quota/full */ }
      });
    }
  } catch (e) { /* localStorage unavailable (e.g. private mode) — skip */ }
})();

// The selected files, accumulated across picks and drops (the file input alone
// can't append, so this array is the single source of truth ppSend reads).
let ppFiles = [];

// File input onchange: append the picked files, then reset the input so the
// same file can be re-added and the input holds no stale selection.
function ppPick(input) {
  ppAddFiles(input.files);
  input.value = '';
}

function ppAddFiles(list) {
  const ignored = [];
  for (const f of list) {
    if (f.type.startsWith('image/') || f.type.startsWith('audio/')) ppFiles.push(f);
    else ignored.push(f.name || '(unnamed)');
  }
  document.getElementById('status').textContent =
    ignored.length ? ('ignored (not image/audio): ' + ignored.join(', ')) : '';
  ppRenderFiles();
}

function ppRemoveFile(i) { ppFiles.splice(i, 1); ppRenderFiles(); }
function ppClearFiles() { ppFiles = []; ppRenderFiles(); }

function ppRenderFiles() {
  const box = document.getElementById('preview');
  box.innerHTML = '';
  ppFiles.forEach(function(f, i) {
    const card = document.createElement('div');
    card.className = 'pp-file';
    const url = URL.createObjectURL(f);
    if (f.type.startsWith('image/')) {
      const img = document.createElement('img'); img.src = url; card.appendChild(img);
    } else {
      const a = document.createElement('audio'); a.controls = true; a.src = url; card.appendChild(a);
    }
    const cap = document.createElement('div');
    cap.className = 'muted pp-cap';
    cap.textContent = f.name + ' (' + (f.type || 'unknown type') + ')';
    card.appendChild(cap);
    const rm = document.createElement('button');
    rm.type = 'button'; rm.className = 'pp-rm'; rm.title = 'Remove';
    rm.textContent = '\\u00d7';
    rm.onclick = function() { ppRemoveFile(i); };
    card.appendChild(rm);
    box.appendChild(card);
  });
}

function ppDragHasFiles(e) {
  return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1;
}

let ppDragDepth = 0;
window.addEventListener('dragenter', function(e) {
  if (!ppDragHasFiles(e)) return;
  e.preventDefault();
  ppDragDepth++;
  document.body.classList.add('pp-dragging');
});
window.addEventListener('dragover', function(e) {
  if (ppDragHasFiles(e)) e.preventDefault();
});
window.addEventListener('dragleave', function(e) {
  if (!ppDragHasFiles(e)) return;
  ppDragDepth = Math.max(0, ppDragDepth - 1);
  if (ppDragDepth === 0) document.body.classList.remove('pp-dragging');
});
window.addEventListener('drop', function(e) {
  e.preventDefault();
  ppDragDepth = 0;
  document.body.classList.remove('pp-dragging');
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
    ppAddFiles(e.dataTransfer.files);
  }
});

// Holds the in-flight request's AbortController so the Stop button can cancel
// it. Aborting closes the response stream, which the proxy relays as a client
// disconnect — halting the backend's generation too.
let ppController = null;

function ppStop() {
  if (ppController) ppController.abort();
}

async function ppSend() {
  const btn = document.getElementById('send');
  const stop = document.getElementById('stop');
  const status = document.getElementById('status');
  const out = document.getElementById('response');
  const rzRow = document.getElementById('reasoning-row');
  const rz = document.getElementById('reasoning');
  out.className = '';
  out.textContent = '';
  rz.textContent = '';
  rzRow.style.display = 'none';
  btn.disabled = true;
  stop.style.display = '';
  status.textContent = 'sending\\u2026';

  ppController = new AbortController();
  const signal = ppController.signal;
  const fd = new FormData();
  fd.append('system', document.getElementById('system').value);
  fd.append('user', document.getElementById('user').value);
  for (const f of ppFiles) fd.append('file', f, f.name);

  try {
    let resp;
    try {
      resp = await fetch('/demo/multimodal/complete?id=' + encodeURIComponent(MODEL_ID),
                         {method: 'POST', body: fd, signal: signal});
    } catch (e) {
      if (signal.aborted) { status.textContent = 'stopped'; return; }
      status.textContent = 'error';
      out.className = 'err';
      out.textContent = 'network error: ' + e;
      return;
    }

    if (!resp.ok) {
      const text = await resp.text();
      status.textContent = 'error (' + resp.status + ')';
      out.className = 'err';
      out.textContent = text;
      return;
    }

    status.textContent = 'streaming\\u2026';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    try {
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
            // Reasoning tokens arrive separately from the answer; backends name
            // the field 'reasoning' (Ollama) or 'reasoning_content' (DeepSeek/vLLM).
            const rtext = delta && (delta.reasoning || delta.reasoning_content);
            if (rtext) { rzRow.style.display = ''; rz.textContent += rtext; }
            if (delta && delta.content) out.textContent += delta.content;
          } catch (e) { /* ignore keep-alives / non-JSON frames */ }
        }
      }
      status.textContent = 'done';
    } catch (e) {
      if (signal.aborted) { status.textContent = 'stopped'; }
      else { status.textContent = 'error'; out.className = 'err'; out.textContent += '\\n[stream error: ' + e + ']'; }
    }
  } finally {
    btn.disabled = false;
    stop.style.display = 'none';
    ppController = null;
  }
}
</script>
{% else %}
<p class="err">Model <code>{{ model_id }}</code> not found &mdash; pick one below.</p>
{% endif %}
</div>
"""


@app.route("/demo/multimodal")
def demo_multimodal() -> str:
    target = _resolve_target(request.args.get("id"))
    model_id = str(target.uuid) if target else (request.args.get("id") or DEFAULT_MODEL_UUID)
    return render_template_string(
        MULTIMODAL_TEMPLATE,
        target=target,
        model_id=model_id,
        tree=list_model_configs_with_overrides(),
    )


@app.route("/demo/multimodal/complete", methods=["POST"])
def demo_multimodal_complete() -> Response | tuple[Response, int]:
    target = _resolve_target(request.args.get("id"))
    if target is None:
        return jsonify({"error": "model not found"}), 404

    system = request.form.get("system", "")
    user = request.form.get("user", "")
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not user.strip() and not files:
        return jsonify({"error": "nothing to send: provide a prompt or a file"}), 400

    base = _backend_base(target.provider, target.arguments)
    if base is None:
        return jsonify({"error": "could not resolve a backend URL for this model"}), 400

    try:
        body = _build_completion_body(target.model_name, system, user, files)
    except ImageConversionError as e:
        return jsonify({"error": str(e)}), 400
    headers = {"Content-Type": "application/json"}
    api_key = target.arguments.get("api_key")
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
