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
