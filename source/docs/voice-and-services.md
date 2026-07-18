# Voice & side services

rainbox keeps heavyweight or credentialed integrations **out of the main
process**: speech-to-text, text-to-speech, and the Telegram bridge each run as
a separate small HTTP service with its own venv, and the web app talks to
them over localhost HTTP. The multimodal demo is the odd one out — it lives in
the web app but proxies to an LLM provider backend.

Why separate processes: the voice models need their own dependency worlds
(faster-whisper's CTranslate2; Kokoro's torch — neither belongs in the main
venv), and the Telegram bridge holds a network credential the core app never
sees. The main app does **not** start these services; each is started by hand
and discovered via an env var. All demo pages degrade gracefully when a
service is down (a health banner, not a crash).

## Port / env map

| Service | Default | Env var (read by) |
|---|---|---|
| Main web app | `http://127.0.0.1:5000` | — (`RAINBOX_URL` for the bridge) |
| Whisper STT | `http://127.0.0.1:5006` | `WHISPER_STT_URL` (webapp) |
| Kokoro TTS | `http://127.0.0.1:5005` | `KOKORO_TTS_URL` (webapp) |
| dots.tts clone | `http://127.0.0.1:5007` | `DOTS_TTS_URL` (webapp) |
| Telegram bridge | outbound-only | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` (required); `TELEGRAM_ROOM_NAME`, `TELEGRAM_STATE_FILE`, `RAINBOX_URL` |

## Whisper STT (`whisper_service/`)

Speech-to-text over [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(CTranslate2 — no torch). Python 3.12 venv; model configured by
`WHISPER_MODEL` (default `small.en`; also `large-v3-turbo`, `medium.en`, …),
`WHISPER_COMPUTE_TYPE` (default `int8`), `WHISPER_CPU_THREADS`. The model
downloads from Hugging Face on the first transcription and loads lazily
behind a lock.

Run: `cd whisper_service && venv/bin/python server.py`

API: `GET /health` → `{status, model_loaded, model}`;
`POST /transcribe` (multipart `audio` + optional `language`) →
`{text, language, duration}`. A VAD filter drops non-speech, so silence
returns empty text instead of a hallucination.

## Kokoro TTS (`kokoro_service/`)

Text-to-speech over hexgrad/Kokoro-82M (torch; Python 3.12 — no 3.14 wheels
yet; needs system `espeak-ng` for phonemization: `brew install espeak-ng`).
Requirements are fully pinned, transitive deps included. Seven American-
English voices (`voices.py`; default `af_heart`).

Run: `cd kokoro_service && venv/bin/python server.py`

API: `GET /health`; `GET /voices`; `POST /tts` with
`{text, voice, speed}` (speed clamped 0.5–2.0) → `audio/wav` (mono 16-bit
PCM, 24 kHz — encoded by `audio.py:float_to_wav_bytes`, stdlib-only).

## dots.tts voice cloning (`voice_tts_dotstts/`)

Zero-shot voice cloning over
[rednote-hilab/dots.tts-soar](https://huggingface.co/rednote-hilab/dots.tts-soar)
(2B params, torch; Python 3.12; 48 kHz output). A voice is a reference audio
sample (~8-12 s) plus its exact transcript, stored one folder per voice under
`voices_data/` (gitignored). `pynini` has no macOS wheels — build it against
Homebrew's OpenFst first (see the service README). With CUDA the model runs in
bfloat16; otherwise it loads in float32 and moves to Apple MPS when available,
falling back to CPU automatically if an MPS synthesis fails. The ~5 GB model
downloads from Hugging Face on the first synthesis, which therefore takes
minutes (plus a one-off solver-compile cost from `optimize=True`); warm
synthesis on an M1 Max (MPS) runs ~2.5-3x real-time.

Run: `cd voice_tts_dotstts && venv/bin/python server.py`

API: `GET /health` → `{status, model_loaded, voices, device}`;
`GET /voices`; `POST /voices` (multipart `name`, `transcript`, `audio`);
`DELETE /voices/<id>`; `POST /tts` with
`{text, voice, seed?, num_steps?, guidance_scale?, speaker_scale?}` →
`audio/wav` (mono 16-bit PCM, 48 kHz).

## Telegram bridge (`telegram_service/`)

A two-way bridge between one Telegram bot and one rainbox chatroom
(`bridge.py`; deps: `requests` only). Inbound: allowed users' Telegram
messages are posted into the room as the human operator — which triggers the
room's agents like any human post. Outbound: agent replies
(`kind="message"` from agent senders; debug rows and the human's own messages
are not forwarded) go back via `sendMessage`, chunked at Telegram's
4096-char limit.

Run (core app already up, room created on `/chat` — default name
`telegram`):

```bash
cd telegram_service
TELEGRAM_BOT_TOKEN=123:abc TELEGRAM_ALLOWED_USER_IDS=987654321 \
  venv/bin/python bridge.py
```

Two worker threads share an atomically persisted state file
(`TELEGRAM_STATE_FILE`, default `./state.json`): `inbound` long-polls
`getUpdates` and posts to `POST /chat/api/rooms/<uuid>/messages`; `outbound`
subscribes to the app's `GET /chat/stream` SSE (with
`…/messages?after=<id>` catch-up). Properties worth knowing:

- **At-least-once inbound** — the Telegram offset advances only after a
  successful room post; a crash can duplicate one message.
- **No history replay** — the outbound cursor starts at the room's latest
  message; old history is never sent to Telegram. Outbound delivery starts
  after the first inbound message (that's how the bridge learns the
  `chat_id`).
- **Text-only v1** — photos/voice/stickers are logged and skipped.
- **Token hygiene** — the bot token is redacted from logged URLs.
- `TELEGRAM_ALLOWED_USER_IDS` are numeric **Telegram** user ids (ask
  @userinfobot), not rainbox uuids; anyone else is ignored.

Note the trust consequence: the bridge posts *as the human operator*, so
Telegram access (bounded by the allowlist) is operator access to that room's
agents.

## Demo pages (webapp proxies)

The browser never talks to a service directly (no CORS); each page proxies
same-origin through the web app:

- **`/demo_stt_whisper`** (`webapp/stt_whisper_views.py`) — mic recording
  with device picker + level meter, live rolling transcription (~2.5s
  interim re-transcribes of the growing buffer), final authoritative pass on
  stop. Proxies: `/demo_stt_whisper/health`, `/demo_stt_whisper/transcribe`
  (120s timeout).
- **`/demo_tts_kokoro`** (`webapp/tts_kokoro_views.py`) — text, voice
  dropdown (populated from the service), speed slider, synthesize + download
  WAV. Proxies: `…/health`, `…/voices`, `…/synthesize` (60s timeout).
- **`/demo_tts_dotstts`** (`webapp/tts_dotstts_views.py`) — voice cloning:
  a Kokoro-style synthesize section (voice dropdown, text, download WAV) plus
  an add-voice section that records via the mic (or accepts an upload),
  re-encodes to WAV client-side with the Web Audio API, and can auto-fill the
  transcript through the Whisper proxy. Proxies: `…/health`, `…/voices`
  (GET/POST), `…/voices/<id>` (DELETE), `…/synthesize` (300s timeout —
  cloning is slow off-GPU and the first request also downloads the model).
- **`/demo_voice_echo`** (`webapp/voice_echo_views.py`) — the round trip:
  record → transcribe → speak the transcript back, with per-leg latency.
  Adds no endpoints of its own; it reuses the STT and TTS proxies (needs
  both services up).

## Multimodal demo (`/demo/multimodal`)

`webapp/multimodal_demo_views.py` — a deliberately thin page for probing
what a local model does with **image and audio input**, streaming the
backend's OpenAI-compatible response verbatim (reasoning deltas included).

- The target model is a `ModelConfig`/`ModelConfigOverride` picked by
  `?id=<uuid>` (tree picker of available configs). `_backend_base` resolves
  the OpenAI-compatible base URL from the model's stored `api_base`, falling
  back to the provider registry's base URL + `/v1` — **never from caller
  input** (no SSRF).
- `POST /demo/multimodal/complete?id=<uuid>` builds a `/chat/completions`
  body: text + content-parts per uploaded file. Images are normalized
  server-side to PNG (EXIF rotation baked in, CMYK→RGBA); audio is passed as
  base64 `input_audio` with a format tag. Streamed back as SSE with a 300s
  timeout; non-200 backend responses are relayed raw on purpose, so you see
  the backend's own "this model can't hear" errors.
- If the model config carries an `api_key`, it is forwarded as a Bearer
  token.

> **Control-plane caveat.** The proxy is unauthenticated, so any local
> caller can drive the operator's configured models on the operator's API
> key — Finding 8c of `proposals/2026-06-25-security-review-mitigations.md`
> (the backend URL is not caller-controlled; the gap is auth and key spend,
> not SSRF).

## Tests

Every service tests against fakes, so no model/network is needed:
`whisper_service/test_server.py`, `kokoro_service/test_server.py` /
`test_voices.py` / `test_audio.py`, `telegram_service/test_bridge.py` /
`test_telegram_api.py` / `test_rainbox_api.py`, and the webapp proxy suites
`webapp/test_stt_whisper_views.py`, `test_tts_kokoro_views.py`,
`test_voice_echo_views.py`, `test_multimodal_demo_views.py`.

## See also

- Per-service READMEs: `whisper_service/README.md`,
  `kokoro_service/README.md`, `telegram_service/README.md` (setup detail
  lives there; this doc is the map).
- `llm-providers.md` — the provider registry the multimodal demo resolves
  backends from.
- `chat-frontend-rules.md` — the SSE stream the Telegram bridge consumes.
