# Whisper STT service

A standalone REST service that transcribes speech to text with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2). Kept
separate from the main project so its heavy dependency (`ctranslate2` and
friends) never enters the main venv. The main app's `/demo_stt_whisper` page
talks to it over HTTP.

Defaults to `small.en` — the accuracy/latency sweet spot for real mic input
(~1.5s per clip). CTranslate2 is CPU-only on Apple Silicon, and `large-v3-turbo`
still runs a full 30s encoder window (~5-7s per clip on an M1). `base.en` is
faster (~0.5s) but its quality drops noticeably on real-world audio.

This is the speech-to-text counterpart of `voice_tts_kokoro/` (text-to-speech);
the two are structured the same way.

> **Note:** LM Studio lists `whisper-large-v3-turbo` among its models, but its
> OpenAI-compatible server does **not** implement `/v1/audio/transcriptions`
> (it returns `Unexpected endpoint or method`). That's why this service runs
> Whisper itself rather than proxying to LM Studio.

## Setup

faster-whisper uses CTranslate2 (not torch) and bundles PyAV/ffmpeg for audio
decoding, so the browser's webm/opus recordings transcribe without a system
`ffmpeg`. Use **Python 3.12** for this venv:

```bash
cd whisper_service
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

From inside this directory, with the venv active:

```bash
python server.py
```

Serves on `http://127.0.0.1:5006`. The model is downloaded from the Hugging
Face Hub on the **first** `/transcribe` call and cached locally thereafter, so
that first request is slow.

Environment overrides:

- `WHISPER_MODEL` — model name (default `small.en`; e.g. `large-v3-turbo`,
  `medium.en`, `base.en`).
- `WHISPER_COMPUTE_TYPE` — CTranslate2 compute type (default `int8`).
- `WHISPER_CPU_THREADS` — worker threads (default: all cores).

The main app finds this service via the `WHISPER_STT_URL` env var (default
`http://127.0.0.1:5006`) and talks to it over HTTP only — it never imports this
code.

## API

- `GET /health` → `{"status":"ok","model_loaded":bool,"model":str}`
- `POST /transcribe` — multipart form, file field `audio` (+ optional `language`)
  → `{"text","language","duration"}`

## Tests

The tests inject a fake transcriber, so they run without faster-whisper
installed. From inside this directory:

```bash
python -m pytest -v
```

The main app's proxy/page tests live at the repo root in
`test_stt_whisper_views.py` and run with the main project's venv.
