# Multimodal model poke page — design

## Purpose

A throwaway demo page to build intuition about how a local vision+audio model
(a Gemma variant already registered in `model_config`) handles image and audio
input. The user attaches one file (image OR audio) plus a system prompt and a
user prompt, hits Send, and watches the streamed model response.

Nothing is written to the database. The only DB access is a read of the one
`ModelConfig` row to discover where the backing LLM server lives.

## Non-goals (YAGNI)

- No DB writes, no persistence of prompts/files/responses.
- No conversation history / multi-turn.
- No model picker UI (single target model, overridable only by URL query param).
- No sending image AND audio in the same request.
- No auth, no file-size cap (local, single-user use).

## Architecture

One new self-contained view module, mirroring the existing demo pages
(`webapp/voice_echo_views.py`, `webapp/stt_whisper_views.py`):

- `webapp/multimodal_demo_views.py` — page template + one streaming proxy route.
- Registered by importing it in `webapp/__init__.py` (same as sibling views).
- Nav link added to the shared nav in `webapp/core.py` (Voice dropdown pattern
  or a top-level link — see Nav below).
- `webapp/test_multimodal_demo_views.py` — tests.

The page is pure browser orchestration talking to a same-origin proxy; the proxy
is a thin OpenAI-compatible passthrough to the model backend.

### Targeting the model

A module constant holds the default target model UUID:

```python
DEFAULT_MODEL_UUID = "00ea3152-ff12-40e1-a63b-8f572de49edf"
```

Both routes accept `?id=<uuid>` to override it. Resolution reads the
`ModelConfig` row by `uuid` (read-only). From it we take:

- The backend's OpenAI-compatible base URL. Jan/LM Studio models store this
  in `arguments["api_base"]` (e.g. `http://127.0.0.1:1337/v1`). Ollama models
  carry no `api_base`, so the base is resolved from the provider registry:
  `providers.get(model.provider).base_url()` (e.g. `http://127.0.0.1:11434`)
  with `/v1` appended. Explicit `arguments["api_base"]` wins when set; the
  registry default is the fallback. An unknown provider id yields no base and
  the proxy returns HTTP 400.
- `arguments.get("api_key")` — sent as `Authorization: Bearer …` if present.
- `model_name` — the id the backend expects in the request body (e.g.
  `gemma4:e4b` for the default Ollama model).

If no row matches, the page renders a clear "model not found" message and the
proxy returns HTTP 404 with a JSON error (no traceback).

## Routes

### `GET /demo/multimodal` → `demo_multimodal`

Renders the self-contained HTML/CSS/JS page. Shows the resolved model's
`effective_display_name` (or the not-found message).

### `POST /demo/multimodal/complete` → `demo_multimodal_complete`

Accepts `multipart/form-data`:

- `system` — system prompt text (may be empty → system message omitted).
- `user` — user prompt text.
- `file` — optional single upload (image/* or audio/*).

Behavior:

1. Resolve the target `ModelConfig` (see above). 404 if missing.
2. Build an OpenAI-compatible chat-completions body:
   - If `system` non-empty: `{"role": "system", "content": <system>}`.
   - User message content is a **parts array**:
     - `{"type": "text", "text": <user>}` (always).
     - If an image was uploaded:
       `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}}`.
     - If audio was uploaded:
       `{"type": "input_audio", "input_audio": {"data": <b64>, "format": <fmt>}}`
       where `<fmt>` is derived from the MIME subtype (e.g. `audio/wav` → `wav`,
       `audio/mpeg` → `mp3`).
   - `"model": model_name`, `"stream": true`.
3. POST to `<api_base>/chat/completions` with `stream=True` (requests), forward
   the raw SSE byte stream to the browser via a Flask streaming `Response`
   (`mimetype="text/event-stream"`, generator yields chunks, no buffering).
4. On a backend connection error or non-2xx, forward the status/body so the
   browser can display the raw error text — surfacing "this model can't do
   audio" verbatim is the point of the demo.

**Decision — direct OpenAI-compatible passthrough, not llama_index.** The demo's
value is seeing the raw multimodal request/response behavior; llama_index would
abstract away the payload shape and swallow backend-specific errors.

## Browser side

Inline in the template, no build step, matching sibling demo pages' style:

- Two `<textarea>`s: system prompt, user prompt.
- One `<input type="file" accept="image/*,audio/*">` with an inline preview:
  a thumbnail `<img>` for images, an `<audio controls>` player for audio,
  chosen from the picked file's type. A "clear file" affordance.
- A Send button and a status line (idle / sending / streaming / done / error).
- Response pane that fills live: `fetch` the proxy, read `response.body` via a
  `ReadableStream` reader, decode chunks, split on SSE `data:` lines, ignore
  `[DONE]`, `JSON.parse` each and append `choices[0].delta.content`.
- On error (non-2xx or network), show the raw response text in the response pane
  with an error style.

## Nav

Add a link so the page is reachable. Preferred: a new top-level "Multimodal"
link in `webapp/core.py`'s nav (`pp-links`), active-styled when
`request.endpoint == 'demo_multimodal'`. (A "Vision/Audio" dropdown is overkill
for a single page.)

## Error handling

- Model row not found → 404, friendly page message / JSON error.
- No `user` text and no `file` → 400 "nothing to send".
- Backend unreachable / non-2xx → forward status + raw body to the browser.
- Unsupported/blank file MIME → treat as no file (still send text), or 400 if it
  was clearly the only content; keep it lenient — the model's own error is
  informative.

## Testing

`webapp/test_multimodal_demo_views.py`, using the Flask test client and the
`rainbox_claude` sandbox (conftest handles DB routing):

- Page renders 200 and contains the form controls; renders the not-found message
  for an unknown `?id=`.
- Seed a `ModelConfig` in the sandbox; assert the proxy builds the correct
  request body: text-only, image (parts array with `image_url` data URL), audio
  (parts array with `input_audio` + derived format). Mock the outbound
  `requests.post` (streaming) — assert URL, headers (bearer when api_key set),
  and JSON body; feed a canned SSE stream and assert the proxy relays it.
- Proxy returns 404 for unknown model id, 400 for empty request.
- Assert no rows are written to any table by a completion call (DB untouched).

## Out-of-scope follow-ups (not now)

- Model picker / multi-model compare.
- Combined image+audio turns.
- Saving interesting prompt/response pairs.
