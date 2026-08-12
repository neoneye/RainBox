# Multimodal Model Poke Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A throwaway demo page (`/demo/multimodal`) to poke a local vision+audio Gemma model: system + user prompts, one attached file (image OR audio), streamed response, nothing persisted.

**Architecture:** One self-contained Flask view module mirroring `webapp/voice_echo_views.py`. A `GET` route renders an inline HTML/CSS/JS page; a `POST` proxy route reads the target `ModelConfig` (read-only) for its backend `api_base`/`model_name`, builds an OpenAI-compatible multimodal `/chat/completions` body, and streams the backend's SSE bytes straight to the browser. No llama_index (we want the raw payload/behavior). No DB writes.

**Tech Stack:** Python 3, Flask (`render_template_string`, streaming `Response`), `requests` (streaming POST), SQLAlchemy (`db.session`, `ModelConfig`), vanilla browser JS (`fetch` + `ReadableStream` SSE parsing).

## Global Constraints

- **Never write to the database from this feature.** The only DB access is a read of one `ModelConfig` row. Tests must confirm no rows are created.
- **Databases:** ad-hoc/manual runs must target `rainbox_claude`, never `rainbox_production`. Tests are auto-routed to `rainbox_claude` by `conftest.py` — nothing to do in the test path.
- **Style:** match the existing demo views (`stt_whisper_views.py`, `voice_echo_views.py`) — inline `render_template_string` template with `{% include "_nav.html" %}`, `system-ui` fonts, the same button/badge CSS idiom.
- **Docs describe current state**, no change-history comments.
- **No PII** anywhere in tests/examples — use neutral placeholders.
- Default target model UUID (overridable via `?id=<uuid>`): `00ea3152-ff12-40e1-a63b-8f572de49edf`.

---

### Task 1: Pure request-body builder

The payload construction, isolated as pure functions so it can be TDD'd without HTTP or DB.

**Files:**
- Create: `webapp/multimodal_demo_views.py`
- Test: `webapp/test_multimodal_demo_views.py`

**Interfaces:**
- Produces:
  - `_audio_format(mime: str) -> str` — maps an audio MIME type to an OpenAI `input_audio` `format` string (e.g. `"audio/mpeg"` → `"mp3"`), falling back to the MIME subtype.
  - `_build_completion_body(model_name: str, system: str, user: str, file: FileStorage | None) -> dict` — builds the OpenAI-compatible `/chat/completions` request body. `file` is a Werkzeug `FileStorage` or `None`; when present and non-empty its bytes are base64-encoded into an `image_url` data URL (image/*) or an `input_audio` part (audio/*).

- [ ] **Step 1: Write the failing tests**

Create `webapp/test_multimodal_demo_views.py`:

```python
"""Tests for webapp/multimodal_demo_views.py.

The model backend is never contacted; `requests` is monkeypatched so the
proxy route can be exercised without a running LLM server. A ModelConfig row
is seeded in the sandbox DB (conftest routes tests to rainbox_claude).
"""

import base64
import io
import json
from unittest.mock import patch

import pytest
from werkzeug.datastructures import FileStorage

from db import ModelConfig, db, init_db, make_app
from webapp.core import app
from webapp.multimodal_demo_views import (
    _audio_format,
    _build_completion_body,
)


def _file(data: bytes, filename: str, mimetype: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=filename, content_type=mimetype)


def test_audio_format_known_and_fallback():
    assert _audio_format("audio/mpeg") == "mp3"
    assert _audio_format("audio/wav") == "wav"
    assert _audio_format("audio/ogg") == "ogg"
    # Unknown MIME falls back to the subtype.
    assert _audio_format("audio/x-weird") == "x-weird"


def test_build_body_text_only():
    body = _build_completion_body("gemma", "be terse", "hello", None)
    assert body["model"] == "gemma"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    user_msg = body["messages"][1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == [{"type": "text", "text": "hello"}]


def test_build_body_omits_empty_system():
    body = _build_completion_body("gemma", "", "hi", None)
    assert all(m["role"] != "system" for m in body["messages"])


def test_build_body_image_part_is_data_url():
    raw = b"\x89PNGfake"
    body = _build_completion_body("gemma", "", "what is this", _file(raw, "x.png", "image/png"))
    parts = body["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "what is this"}
    b64 = base64.b64encode(raw).decode("ascii")
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{b64}"},
    }


def test_build_body_audio_part_has_format():
    raw = b"RIFFfake"
    body = _build_completion_body("gemma", "", "transcribe", _file(raw, "c.wav", "audio/wav"))
    parts = body["messages"][-1]["content"]
    b64 = base64.b64encode(raw).decode("ascii")
    assert parts[1] == {
        "type": "input_audio",
        "input_audio": {"data": b64, "format": "wav"},
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest webapp/test_multimodal_demo_views.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.multimodal_demo_views'`.

- [ ] **Step 3: Write the module with the builder functions**

Create `webapp/multimodal_demo_views.py`:

```python
"""Demo page for poking a local vision+audio model (a Gemma variant).

A throwaway page to build intuition about how the model handles image/audio
input. The browser posts a system prompt, a user prompt, and one optional file
(image OR audio) to a same-origin proxy, which reads the target ModelConfig
(read-only) for its backend api_base/model_name, builds an OpenAI-compatible
multimodal /chat/completions request, and streams the backend's SSE response
straight back. Nothing is persisted.

Direct OpenAI-compatible passthrough (not llama_index) is deliberate: the point
is to see the raw multimodal request/response behavior, including backend errors
like "this model can't do audio", verbatim.
"""

import base64
from uuid import UUID

import requests
from flask import Response, jsonify, render_template_string, request
from werkzeug.datastructures import FileStorage

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest webapp/test_multimodal_demo_views.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add webapp/multimodal_demo_views.py webapp/test_multimodal_demo_views.py
git commit -m "feat(multimodal-demo): OpenAI-compatible multimodal request builder"
```

---

### Task 2: Page route, template, model resolution, and registration

The `GET` page plus the model lookup helper, registered so the URL is live.

**Files:**
- Modify: `webapp/multimodal_demo_views.py`
- Modify: `webapp/__init__.py` (add import to register routes)
- Modify: `webapp/core.py` (add nav link, ~line 151)
- Test: `webapp/test_multimodal_demo_views.py`

**Interfaces:**
- Consumes: `_build_completion_body` (Task 1).
- Produces:
  - `_resolve_model(id_param: str | None) -> ModelConfig | None` — parses `id_param` (or `DEFAULT_MODEL_UUID`) as a UUID and returns the matching `ModelConfig`, or `None` if unparseable/absent.
  - Route `GET /demo/multimodal` → endpoint name `demo_multimodal`.

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_multimodal_demo_views.py`:

```python
@pytest.fixture
def seeded_model():
    """Seed one vision/audio model row in the sandbox DB; clean up after."""
    a = make_app()
    init_db(a)
    with a.app_context():
        m = ModelConfig(
            provider="jan",
            model_name="gemma-multimodal-test",
            display_name="Gemma (multimodal test)",
            arguments={"api_base": "http://127.0.0.1:1337/v1", "api_key": "k"},
        )
        db.session.add(m)
        db.session.commit()
        uid = str(m.uuid)
        try:
            yield uid
        finally:
            db.session.delete(m)
            db.session.commit()


def test_page_renders_with_model_name(seeded_model):
    client = app.test_client()
    resp = client.get(f"/demo/multimodal?id={seeded_model}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "pp-nav" in body                       # shared nav included
    assert "Gemma (multimodal test)" in body      # resolved display name shown
    assert 'type="file"' in body                  # file input present
    assert 'id="system"' in body and 'id="user"' in body


def test_page_renders_not_found_for_unknown_id():
    client = app.test_client()
    # A well-formed but absent UUID.
    resp = client.get("/demo/multimodal?id=00000000-0000-0000-0000-000000000000")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "not found" in body.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest webapp/test_multimodal_demo_views.py -k "page_renders" -q`
Expected: FAIL — 404 (route not registered) / assertion errors.

- [ ] **Step 3: Add `_resolve_model`, the template, and the page route**

In `webapp/multimodal_demo_views.py`, add after `_build_completion_body`:

```python
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
```

- [ ] **Step 4: Register the module and add the nav link**

In `webapp/__init__.py`, after the `voice_echo_views` import line, add:

```python
from . import multimodal_demo_views  # noqa: F401,E402
```

In `webapp/core.py`, in the nav `pp-links` block, add a link before the `<span class="pp-spacer"></span>` line (currently around line 152), just after the Doctor link:

```html
    <a href="{{ url_for('demo_multimodal') }}" class="{{ 'pp-active' if request.endpoint == 'demo_multimodal' }}">Multimodal</a>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest webapp/test_multimodal_demo_views.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add webapp/multimodal_demo_views.py webapp/__init__.py webapp/core.py webapp/test_multimodal_demo_views.py
git commit -m "feat(multimodal-demo): page, model resolution, nav link, registration"
```

---

### Task 3: Streaming proxy route

The `POST` route that validates input, builds the body, forwards to the backend, and streams the SSE response through. Errors surface verbatim.

**Files:**
- Modify: `webapp/multimodal_demo_views.py`
- Test: `webapp/test_multimodal_demo_views.py`

**Interfaces:**
- Consumes: `_resolve_model`, `_build_completion_body`, `PROXY_TIMEOUT` (Tasks 1–2).
- Produces: Route `POST /demo/multimodal/complete` → endpoint `demo_multimodal_complete`.

- [ ] **Step 1: Write the failing tests**

Append to `webapp/test_multimodal_demo_views.py`:

```python
class FakeStreamResponse:
    """Stand-in for a streaming requests.Response."""

    def __init__(self, *, status_code=200, chunks=(), content=b"", content_type="text/event-stream"):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.content = content
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size=None):
        yield from self._chunks


def test_complete_forwards_body_and_relays_stream(seeded_model):
    client = app.test_client()
    captured = {}

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeStreamResponse(chunks=[
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            b"data: [DONE]\n\n",
        ])

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"system": "be terse", "user": "hi"},
            content_type="multipart/form-data",
        )
        streamed = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert captured["url"] == "http://127.0.0.1:1337/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["json"]["model"] == "gemma-multimodal-test"
    assert captured["json"]["stream"] is True
    assert "Hel" in streamed and "lo" in streamed


def test_complete_404_for_unknown_model():
    client = app.test_client()
    resp = client.post(
        "/demo/multimodal/complete?id=00000000-0000-0000-0000-000000000000",
        data={"user": "hi"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 404


def test_complete_400_when_nothing_to_send(seeded_model):
    client = app.test_client()
    resp = client.post(
        f"/demo/multimodal/complete?id={seeded_model}",
        data={"system": "", "user": "   "},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_complete_forwards_backend_error_body(seeded_model):
    client = app.test_client()

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        return FakeStreamResponse(status_code=400, content=b'{"error":"no audio support"}',
                                  content_type="application/json")

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        resp = client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        )
    assert resp.status_code == 400
    assert "no audio support" in resp.get_data(as_text=True)


def test_complete_does_not_write_to_db(seeded_model):
    client = app.test_client()
    a = make_app()
    init_db(a)
    with a.app_context():
        before = db.session.query(ModelConfig).count()

    def fake_post(url, json=None, headers=None, stream=None, timeout=None):
        return FakeStreamResponse(chunks=[b"data: [DONE]\n\n"])

    with patch("webapp.multimodal_demo_views.requests.post", side_effect=fake_post):
        client.post(
            f"/demo/multimodal/complete?id={seeded_model}",
            data={"user": "hi"},
            content_type="multipart/form-data",
        ).get_data()

    with a.app_context():
        after = db.session.query(ModelConfig).count()
    assert after == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest webapp/test_multimodal_demo_views.py -k complete -q`
Expected: FAIL — 404 (route not registered) for all `complete` tests.

- [ ] **Step 3: Add the proxy route**

Append to `webapp/multimodal_demo_views.py`:

```python
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

    args = model.arguments or {}
    api_base = (args.get("api_base") or "").rstrip("/")
    if not api_base:
        return jsonify({"error": "model has no api_base configured"}), 400

    body = _build_completion_body(model.model_name, system, user, file if has_file else None)
    headers = {"Content-Type": "application/json"}
    api_key = args.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        upstream = requests.post(
            f"{api_base}/chat/completions",
            json=body,
            headers=headers,
            stream=True,
            timeout=PROXY_TIMEOUT,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"backend unreachable at {api_base}: {e}"}), 502

    # Non-2xx: forward the raw error body verbatim — seeing the backend's own
    # complaint ("this model can't do audio") is the point of the demo.
    if upstream.status_code != 200:
        return Response(
            upstream.content,
            status=upstream.status_code,
            mimetype=upstream.headers.get("Content-Type", "text/plain"),
        )

    def relay():
        for chunk in upstream.iter_content(chunk_size=None):
            if chunk:
                yield chunk

    return Response(
        relay(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest webapp/test_multimodal_demo_views.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add webapp/multimodal_demo_views.py webapp/test_multimodal_demo_views.py
git commit -m "feat(multimodal-demo): streaming OpenAI-compatible proxy route"
```

---

### Task 4: Manual end-to-end verification

No new code — drive the real page against the actual model to confirm image and audio round-trips behave, and that the streamed response renders live.

**Files:** none (verification only).

- [ ] **Step 1: Start the app** (it normally runs on port 5000)

Run: `python main.py` (or however the app is already running for this operator).

- [ ] **Step 2: Open the page**

Visit `http://127.0.0.1:5000/demo/multimodal`. Confirm the model name renders (not the "not found" message) and the nav shows the **Multimodal** link.

- [ ] **Step 3: Text-only smoke**

Enter a user prompt (no file), Send. Confirm tokens stream into the Response pane and status ends at "done".

- [ ] **Step 4: Image round-trip**

Attach an image, prompt "describe this image", Send. Confirm the thumbnail preview appears and the model responds about the image. If the backend rejects the format, confirm the raw error text shows in red (that is acceptable — it is the intuition the demo exists to surface).

- [ ] **Step 5: Audio round-trip**

Attach a short audio clip (wav/mp3), prompt "transcribe this", Send. Confirm the `<audio>` preview appears and the model responds (or shows the raw backend error).

- [ ] **Step 6: Confirm no persistence**

After several sends, confirm nothing new was written — e.g. the model list / DB is unchanged. (The automated `test_complete_does_not_write_to_db` already covers this; this is a sanity glance.)

---

## Self-Review

**Spec coverage:**
- Purpose / no-DB-write → Task 1–3 (read-only `_resolve_model`), asserted by `test_complete_does_not_write_to_db`. ✓
- Target model + `?id=` override → `_resolve_model`, `DEFAULT_MODEL_UUID`. ✓
- `GET /demo/multimodal` page with system/user textareas + file input + preview + streaming JS → Task 2 template. ✓
- `POST /demo/multimodal/complete` proxy: build OpenAI body, image_url vs input_audio, stream:true, SSE passthrough, error passthrough → Task 1 builder + Task 3 route. ✓
- Not-found message; 400 empty; 404 unknown; backend error verbatim → Tasks 2–3 + tests. ✓
- Nav link, `__init__` registration → Task 2. ✓
- Tests using Flask client + sandbox DB + mocked requests → all tasks. ✓
- Direct passthrough (not llama_index) decision → reflected in module docstring + Task 3. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type consistency:** `_audio_format`, `_build_completion_body`, `_resolve_model`, `DEFAULT_MODEL_UUID`, `PROXY_TIMEOUT`, endpoints `demo_multimodal` / `demo_multimodal_complete` used consistently across tasks and tests. ✓
