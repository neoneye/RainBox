# dots.tts-soar voice-cloning TTS service

A standalone REST service that clones voices with
[rednote-hilab/dots.tts-soar](https://huggingface.co/rednote-hilab/dots.tts-soar)
(2B-param zero-shot TTS, 48 kHz output). Kept separate from the main project so
its heavy dependencies (`torch`, `dots.tts`) never enter the main venv. The
main app's `/demo_tts_dotstts` page talks to it over HTTP.

A voice is a reference audio sample (~8-12 s of clean speech) plus the exact
transcript of that sample. Voices are stored under `voices_data/` (gitignored),
one folder per voice; see `voices.py`.

## Setup

Use **Python 3.12** for this venv. `pynini` (pulled in via `WeTextProcessing`)
has no macOS wheels, so install OpenFst with Homebrew first and build against
it:

```bash
brew install openfst            # macOS; pynini 2.1.7 needs OpenFst 1.8.4
```

```bash
cd voice_tts_dotstts
python3.12 -m venv venv
source venv/bin/activate
CPPFLAGS="-I/opt/homebrew/include" LDFLAGS="-L/opt/homebrew/lib" pip install pynini==2.1.7
pip install -r requirements.txt
```

The model (~5 GB) is downloaded from Hugging Face on the first synthesis
request.

## Run

From inside this directory, with the venv active:

```bash
python server.py
```

Serves on `http://127.0.0.1:5007`. The main app finds it via the
`DOTS_TTS_URL` env var (default `http://127.0.0.1:5007`) and talks to it over
HTTP only — it never imports this code.

**Device**: with CUDA the model runs in bfloat16 on the GPU. Without CUDA it
loads in float32 and is moved to Apple MPS when available, falling back to CPU
automatically if an MPS synthesis fails. `/health` reports the active device.

## API

- `GET /health` → `{"status":"ok","model_loaded":bool,"voices":int,"device":str|null}`
- `GET /voices` → `{"voices":[{"id","name","transcript"}, ...]}`
- `POST /voices` — multipart form `name`, `transcript`, `audio` (WAV) → `201 {"voice":{...}}`
- `DELETE /voices/<id>` → `{"deleted":id}`
- `POST /tts` `{"text","voice",seed?,num_steps?,guidance_scale?,speaker_scale?}` → `audio/wav`

## Tests

The tests mock the model, so they run without torch/dots.tts installed. From
inside this directory:

```bash
python -m pytest -v
```

The main app's proxy/page tests live at `webapp/test_tts_dotstts_views.py` and
run with the main project's venv.
