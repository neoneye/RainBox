# Kokoro-82M TTS service

A standalone REST service that synthesizes speech with
[hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). Kept separate
from the main project so its heavy dependencies (`torch`, `kokoro`) never enter
the main venv. The main app's `/demo_tts_kokoro` page talks to it over HTTP.

## Setup

kokoro 0.9.4 requires Python `>=3.10,<3.13`, and `torch` has no Python 3.14
wheels yet — use **Python 3.12** for this venv. You also need `espeak-ng`
installed (Kokoro uses it for phonemization):

```bash
brew install espeak-ng            # macOS
```

```bash
cd voice_tts_kokoro
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

From inside this directory, with the venv active:

```bash
python server.py
```

Serves on `http://127.0.0.1:5005`. The main app finds it via the
`KOKORO_TTS_URL` env var (default `http://127.0.0.1:5005`) and talks to it over
HTTP only — it never imports this code.

## API

- `GET /health` → `{"status":"ok","model_loaded":bool,"voices":int}`
- `GET /voices` → `{"voices":[{"id","name","lang"}, ...]}`
- `POST /tts` `{"text","voice","speed"}` → `audio/wav`

## Tests

The tests mock the model, so they run without torch/kokoro installed. From
inside this directory:

```bash
python -m pytest -v
```

The main app's proxy/page tests live at the repo root in
`test_tts_kokoro_views.py` and run with the main project's venv.
