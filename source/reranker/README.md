# Cross-encoder reranker service

A standalone REST service that scores (query, document) pairs with a
cross-encoder. The assistant's recall filter uses it as an alternative to its
LLM scorer: the LLM reads the whole turn and answers with three Likert scales
per candidate and a note (~20s); a cross-encoder answers "how well does this
candidate match this message" in milliseconds and nothing else.

Kept separate from the main project so its heavy dependencies (torch,
transformers) never enter the main venv — the same arrangement as
`voice_stt_whisper/` (STT) and `voice_tts_kokoro/` (TTS). The main app talks to
it over HTTP and never imports this code.

Two models are served:

| key | Hugging Face repo | notes |
| --- | --- | --- |
| `mmarco-mMiniLMv2-L12-H384-v1` | [`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1) | ~470 MB, L12/H384, distilled on mMARCO |
| `jina-reranker-v2-base-multilingual` | [`jinaai/jina-reranker-v2-base-multilingual`](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual) | ~1.1 GB, XLM-RoBERTa base, ships its own scoring code (`trust_remote_code`) |

## Setup

Use **Python 3.12** for this venv:

```bash
cd reranker
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
```

`transformers` is pinned to **4.x on purpose**: jina's remote code imports
`create_position_ids_from_input_ids` from `transformers.models.xlm_roberta`,
which 5.x removed, so the model fails to load there. The mMARCO cross-encoder
is fine on either.

## Run

```bash
venv/bin/python server.py
```

Serves on `http://127.0.0.1:5008`. Each model is downloaded from the Hugging
Face Hub on the **first** `/rerank` call that names it and cached locally
thereafter, so that first request is slow (minutes on a cold cache) and every
one after it is not.

Environment overrides:

- `RERANKER_DEVICE` — `auto` (default: MPS if available, then CUDA, then CPU),
  or force `cpu`. The first call on MPS pays a ~1-2s warm-up; both models are
  small enough that CPU is competitive.
- `RERANKER_MAX_LENGTH` — pair length in tokens (default 512).
- `RERANKER_BATCH_SIZE` — pairs per forward pass (default 16).

The main app finds the service via `RERANKER_URL` (default
`http://127.0.0.1:5008`) and picks a model with the
`memory.recall_filter_backend` setting on `/settings`:

- `llm` — the assistant's own scorer (default)
- `reranker:mmarco-mMiniLMv2-L12-H384-v1`
- `reranker:jina-reranker-v2-base-multilingual`

With the service down, the recall filter falls back to gated retrieval — the
turn still answers, with less recall.

## API

- `GET /health` → `{"status":"ok","models":{key:repo},"loaded":[key],"device":str}`
- `POST /rerank` — `{"model":key,"query":str,"documents":[{"id","text"}]}`
  → `{"model","ms","scores":[{"id","score"}]}`. Scores are relevance
  probabilities in 0..1, one per document, in the order given. `ms` is the
  scoring pass alone (no HTTP, no model load).

The **absolute** scores are not comparable between the two models — only the
ordering is. On one candidate list the correct answer scored 0.21 on mMARCO and
0.50 on jina, while jina's irrelevant candidates sat around 0.03 and mMARCO's
at 0.0001. That is why the keep/drop policy in
`agents/recall_reranker.py` is relative to the best score in the list, with a
single absolute floor beneath which a list counts as noise.

## Tests

The tests inject a fake scorer, so they run without torch installed — with this
venv, or as part of the main project's suite:

```bash
venv/bin/python -m pytest -v
```
