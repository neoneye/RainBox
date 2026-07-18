# voice_tts_dotstts — zero-shot voice-cloning TTS service + demo page

## Goal

A second TTS subproject alongside `kokoro_service/`: wrap
[rednote-hilab/dots.tts-soar](https://huggingface.co/rednote-hilab/dots.tts-soar)
(2B-param zero-shot voice-cloning TTS, Apache-2.0, 48 kHz output) in a
standalone REST service, plus a `/demo_tts_dotstts` page in the main app that
mirrors the Kokoro demo page's look and feel. All Python — no bash helpers.

## Architecture

Same split as Kokoro/Whisper (see `docs/voice-and-services.md`):

- **`voice_tts_dotstts/`** — standalone Flask service, own Python 3.12 venv,
  pinned `requirements.txt`, port **5007** (kokoro 5005, whisper 5006). The
  heavy deps (`torch`, `dots.tts`) never enter the main venv.
- **`webapp/tts_dotstts_views.py`** — same-origin proxy routes + inline-template
  demo page. Env var `DOTS_TTS_URL` (default `http://127.0.0.1:5007`). The
  browser never talks to the service directly.

## Voice model: saved-voice library

Cloning needs a reference audio sample (~8–12 s clean speech) and its exact
transcript. Rather than uploading both on every request (the stateless approach
used by the reference demo this replaces), the service owns a voice library so
the demo page gets a Kokoro-style voice dropdown:

```
voice_tts_dotstts/voices_data/     # gitignored, user data
  <slug>/
    reference.wav                  # the reference sample
    transcript.txt                 # exact transcript (UTF-8)
    name.txt                       # display name
```

(The directory is `voices_data/`, not `voices/`, so it cannot shadow the
`voices.py` module.)

The slug is derived from the display name (lowercase, alnum + dashes,
`-2`/`-3`… suffix on collision) and doubles as the voice id.

## Service API

- `GET /health` → `{"status":"ok","model_loaded":bool,"voices":int}`
- `GET /voices` → `{"voices":[{"id","name","transcript"}, ...]}`
- `POST /voices` — multipart form: `name`, `transcript`, `audio` (WAV file)
  → `201 {"voice":{"id","name","transcript"}}`; `400` on missing/empty fields.
- `DELETE /voices/<id>` → `{"deleted":id}` or `404`.
- `POST /tts` — JSON `{"text","voice"}` plus optional `seed` (1–1000),
  `num_steps` (10–32, default 12), `guidance_scale` (default 1.2),
  `speaker_scale` (default 1.5) → `audio/wav`, or `{"error"}` on 4xx/5xx.

Like `kokoro_service/server.py`, `create_app(synthesize_fn=None, voices_dir=None)`
lets tests inject a fake synth and a tmp voice dir; the real model loads lazily
on first `/tts`. Synth signature:

```python
# (text, reference_wav_path, reference_transcript, options) -> (samples, sample_rate)
SynthFn = Callable[[str, str, str, SynthOptions], tuple]
```

`SynthOptions` is a small dataclass (seed, num_steps, guidance_scale,
speaker_scale). The real synth:

```python
from dots_tts.runtime import DotsTtsRuntime
runtime = DotsTtsRuntime.from_pretrained("rednote-hilab/dots.tts-soar", precision=...)
result = runtime.generate(text=..., prompt_audio_path=..., prompt_text=...,
                          num_steps=..., guidance_scale=..., speaker_scale=...)
# result: {"audio": tensor, "sample_rate": int, ...}
```

**Device**: the runtime itself only knows cuda-vs-cpu and refuses half
precision without CUDA. With CUDA: bfloat16 on the GPU. Without CUDA: load in
float32 on CPU, widen the runtime's single-thread cap, move the model to MPS
when available, and transparently fall back to CPU the first time an MPS
synthesis fails. The active device is exposed in `/health` as `device`.
The model loads with `optimize=True` — the flow-matching vocoder dominates
synthesis time and its cached/compiled solver path is ~5x faster on MPS with
identical output. (Verified on an M1 Max: warm synthesis ~2.5-3x real-time.)

## Demo page `/demo_tts_dotstts`

Proxy routes (mirroring `tts_kokoro_views.py`): `…/health` (3 s timeout),
`…/voices` GET/POST, `…/voices/<id>` DELETE (60 s), `…/synthesize` POST
(300 s — cloning on MPS/CPU is slower than Kokoro).

Page layout, top to bottom:

1. Status badge + service URL (as on the Kokoro page).
2. **Synthesize**: voice dropdown (from the library) · text area ·
   Synthesize button · audio player + "Download .wav". Advanced tuning params
   stay server-side defaults (no sliders in v1).
3. **Add voice**: record in-browser (MediaRecorder + device picker pattern from
   the voice-echo page, encoded to WAV client-side via Web Audio API since the
   service needs a real WAV) *or* upload a `.wav` file; transcript textarea with
   a "Transcribe with Whisper" button that reuses the existing
   `/demo_stt_whisper/transcribe` proxy (editable afterwards — the transcript
   must be exact); name field; Save.
4. Each voice in the dropdown has a Delete action behind a confirm dialog.

Inline JS follows the repo rule: no backslash escape sequences inside the
Python template string.

## Testing

- `voice_tts_dotstts/test_server.py` — endpoints with injected fake synth +
  tmp voices dir (no torch needed): health, voices CRUD, tts happy path,
  empty text / unknown voice / bad params → 400.
- `voice_tts_dotstts/test_voices.py` — voice store: create/list/delete,
  slug collisions, missing files ignored.
- `webapp/test_tts_dotstts_views.py` — page renders, proxies forward and map
  connection errors to 502 (mirrors `test_tts_kokoro_views.py`).
- Service tests run from inside `voice_tts_dotstts/` (own venv); webapp tests
  run with the main venv.

## Out of scope (v1)

Streaming synthesis, language override, exposing tuning sliders in the UI, and
wiring cloned voices into the chat/echo pages.
