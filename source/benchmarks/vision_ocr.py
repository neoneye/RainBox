"""Vision benchmark: can a model read the text in an image, and what does it
cost to answer?

One fixed image, `fixtures/ocr_corners.png` (512×512): a word in each corner
and one in the center, in two fonts.

    cat   top-left       monospace, horizontal
    bug   top-right      monospace, horizontal
    Testing  center      serif, horizontal
    ant   bottom-left    monospace, rotated 45° (reads upward)
    dog   bottom-right   monospace, rotated 45° (reads downward)

Six cases ask about it. One asks for every word; five ask where one word is.
A case passes when the answer contains each expected word — case-insensitive,
as a whole word, with any formatting or extra words around it. Naming the
other words as well does not fail a location case; it is reported as "also
named", so a model that answers every question with the whole list is visible
without being scored as wrong.

Each trial also measures the call: wall time, time to the first token, time
spent thinking before the answer started, prompt/cached/output tokens, how
many of the output tokens were reasoning, and the provider's own prefill,
decode and load times where it reports them.

Two transports, picked by provider:

- **Ollama** — the native `/api/chat` stream. It is the API the app's own
  Ollama client speaks, so a config's `thinking`, `temperature`,
  `context_window` and `max_tokens` mean here what they mean in a chat; and it
  reports exact load, prefill and decode durations.
- **Everything else** — the OpenAI-compatible `/chat/completions` stream with
  `include_usage`, the request /demo/multimodal sends. Durations come from a
  llama.cpp-style `timings` object when the backend sends one.

Neither reports reasoning tokens on their own, only the output total. The
reasoning share is that total split by the characters of reasoning and answer
text, so it is an estimate and the page marks it ≈.

The page is /benchmark_vision. The CLI runs the same cases:

    python -m benchmarks.vision_ocr <model-uuid> [<model-uuid> …] [--trials N]

A uuid is a ModelConfig or ModelConfigOverride, as on /model?id=…, resolved in
the database `DATABASE_URL` names. The CLI only reads those rows.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

import requests

IMAGE_PATH = Path(__file__).parent / "fixtures" / "ocr_corners.png"
# Every word in the image, for the "also named" column.
IMAGE_WORDS = ("cat", "bug", "Testing", "ant", "dog")
# A cold vision model loads its weights and projector on the first request.
REQUEST_TIMEOUT = 300.0
# A runaway reasoner can stream tens of kilobytes of thinking; the stored
# trial keeps the head of it, which is where a failure is readable anyway.
MAX_STORED_REASONING = 20000


@dataclass(frozen=True)
class OcrCase:
    name: str
    system: str
    user: str
    expected: tuple[str, ...]


CASES: tuple[OcrCase, ...] = (
    OcrCase("all_texts", "do OCR on this image", "list all the texts", IMAGE_WORDS),
    OcrCase("center", "you do OCR", "what text is in the center", ("Testing",)),
    OcrCase("top_left", "you do OCR", "what text is in the top left corner", ("cat",)),
    OcrCase("top_right", "you do OCR", "what text is in the top right corner", ("bug",)),
    OcrCase("bottom_left", "you do OCR", "what text is in the bottom left corner", ("ant",)),
    OcrCase("bottom_right", "you do OCR", "what text is in the bottom right corner", ("dog",)),
)
CASES_BY_NAME: dict[str, OcrCase] = {c.name: c for c in CASES}


# --- scoring ----------------------------------------------------------------

def contains_word(answer: str, word: str) -> bool:
    """`word` appears in `answer` on its own: any case, any punctuation or
    markup around it, but not as part of a longer word ("cat" is not in
    "category")."""
    pattern = rf"(?<![0-9a-z]){re.escape(word.lower())}(?![0-9a-z])"
    return re.search(pattern, answer.lower()) is not None


def missing_words(answer: str, expected: tuple[str, ...]) -> list[str]:
    """The expected words the answer does not contain; empty means a pass."""
    return [w for w in expected if not contains_word(answer, w)]


def also_named(answer: str, expected: tuple[str, ...]) -> list[str]:
    """Words from the image the answer names that the case did not ask for."""
    return [w for w in IMAGE_WORDS if w not in expected and contains_word(answer, w)]


# --- target -----------------------------------------------------------------

@dataclass
class Target:
    """Everything a trial needs to reach one model, JSON-safe so the parent
    can resolve it from the database and hand it to a worker process that has
    none. `options` are the config's sampling settings, already in the
    transport's own shape."""
    uuid: str
    label: str
    provider: str
    model_name: str
    transport: str  # "ollama" | "openai"
    url: str
    headers: dict[str, str]
    options: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Target:
        return cls(**data)


def ollama_options(arguments: dict[str, Any]) -> dict[str, Any]:
    """A config's arguments as native /api/chat fields: `think` at the top
    level, the rest under `options`. A key the config does not set is left
    out, so the model's own default applies — a base config without
    `thinking` thinks or not as the model ships."""
    out: dict[str, Any] = {}
    if isinstance(arguments.get("thinking"), bool):
        out["think"] = arguments["thinking"]
    opts: dict[str, Any] = {}
    if arguments.get("temperature") is not None:
        opts["temperature"] = arguments["temperature"]
    if arguments.get("context_window"):
        opts["num_ctx"] = int(arguments["context_window"])
    if arguments.get("max_tokens"):
        opts["num_predict"] = int(arguments["max_tokens"])
    if opts:
        out["options"] = opts
    return out


def openai_options(arguments: dict[str, Any]) -> dict[str, Any]:
    """A config's arguments as OpenAI-compatible body fields, including any
    `additional_kwargs.extra_body` (where a Jan/LM Studio config keeps its
    reasoning effort)."""
    out: dict[str, Any] = {}
    if arguments.get("temperature") is not None:
        out["temperature"] = arguments["temperature"]
    if arguments.get("max_tokens"):
        out["max_tokens"] = int(arguments["max_tokens"])
    extra = (arguments.get("additional_kwargs") or {}).get("extra_body")
    if isinstance(extra, dict):
        out.update(extra)
    return out


def resolve_target(uid: UUID) -> Target:
    """Build the Target for a ModelConfig or override uuid. Needs an app
    context; only reads. Raises LookupError when the row is missing or its
    provider has no base URL, RuntimeError when a required API key is unset."""
    import providers
    from db import get_model_config, get_model_config_override, resolved_model_kwargs

    provider, model_name, arguments = resolved_model_kwargs(uid)
    row = get_model_config(uid) or get_model_config_override(uid)
    label = row.effective_display_name if row is not None else model_name
    headers = {"Content-Type": "application/json"}
    if provider == "ollama":
        base = str(arguments.get("base_url") or "").strip() or providers.get("ollama").base_url()
        return Target(str(uid), label or model_name, provider, model_name, "ollama",
                      base.rstrip("/") + "/api/chat", headers, ollama_options(arguments))
    base = providers.request_base_url(provider, arguments)
    if base is None:
        raise LookupError(f"{uid}: no base URL for provider {provider!r}")
    api_key = providers.request_api_key(provider, arguments)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return Target(str(uid), label or model_name, provider, model_name, "openai",
                  base + "/chat/completions", headers, openai_options(arguments))


# --- one call ---------------------------------------------------------------

@dataclass
class Reply:
    """One answered request, measured. Times are seconds; a None is a figure
    the backend did not report."""
    content: str = ""
    reasoning: str = ""
    elapsed: float = 0.0
    # From sending the request to the first streamed token of any kind.
    first_token: float | None = None
    # From the first token to the first answer token, when the model reasoned
    # first; 0.0 when it answered straight away.
    thinking: float | None = None
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    load_s: float | None = None
    prefill_s: float | None = None
    decode_s: float | None = None
    done_reason: str | None = None

    @property
    def decode_tps(self) -> float | None:
        if self.output_tokens and self.decode_s:
            return self.output_tokens / self.decode_s
        return None


def estimate_reasoning_tokens(output_tokens: int | None, reasoning: str, content: str) -> int | None:
    """The reasoning share of `output_tokens`, split by characters. Neither
    transport counts reasoning tokens separately, so this is an estimate."""
    if output_tokens is None:
        return None
    total = len(reasoning) + len(content)
    if total == 0 or not reasoning:
        return 0
    return round(output_tokens * len(reasoning) / total)


def ollama_body(target: Target, case: OcrCase, image_png: bytes) -> dict:
    return {
        "model": target.model_name,
        "stream": True,
        "messages": [
            {"role": "system", "content": case.system},
            {"role": "user", "content": case.user,
             "images": [base64.b64encode(image_png).decode("ascii")]},
        ],
        **target.options,
    }


def openai_body(target: Target, case: OcrCase, image_png: bytes) -> dict:
    data_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
    return {
        "model": target.model_name,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": case.system},
            {"role": "user", "content": [
                {"type": "text", "text": case.user},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        **target.options,
    }


class _Clock:
    """Stamps the first token and the first answer token of a stream."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.first: float | None = None
        self.first_content: float | None = None

    def saw(self, reasoning: str, content: str) -> None:
        now = time.monotonic()
        if (reasoning or content) and self.first is None:
            self.first = now
        if content and self.first_content is None:
            self.first_content = now

    def fill(self, reply: Reply) -> None:
        reply.elapsed = time.monotonic() - self.started
        if self.first is not None:
            reply.first_token = self.first - self.started
            end = self.first_content if self.first_content is not None else time.monotonic()
            reply.thinking = end - self.first


def parse_ollama_stream(lines: Iterator[bytes], clock: _Clock) -> Reply:
    """Read a native /api/chat NDJSON stream. The final `done` line carries
    the counts and the nanosecond durations."""
    reply = Reply()
    content: list[str] = []
    reasoning: list[str] = []
    for raw in lines:
        if not raw:
            continue
        obj = json.loads(raw)
        if obj.get("error"):
            raise RuntimeError(str(obj["error"]))
        msg = obj.get("message") or {}
        r, c = msg.get("thinking") or "", msg.get("content") or ""
        clock.saw(r, c)
        reasoning.append(r)
        content.append(c)
        if obj.get("done"):
            reply.prompt_tokens = obj.get("prompt_eval_count")
            reply.cached_tokens = obj.get("prompt_eval_cached_count")
            reply.output_tokens = obj.get("eval_count")
            reply.load_s = _ns(obj.get("load_duration"))
            reply.prefill_s = _ns(obj.get("prompt_eval_duration"))
            reply.decode_s = _ns(obj.get("eval_duration"))
            reply.done_reason = obj.get("done_reason")
    reply.content, reply.reasoning = "".join(content), "".join(reasoning)
    clock.fill(reply)
    return reply


def parse_openai_stream(lines: Iterator[bytes], clock: _Clock) -> Reply:
    """Read an OpenAI-compatible SSE stream: content and reasoning deltas,
    then the usage chunk `include_usage` asks for, with llama.cpp `timings`
    when the backend adds them."""
    reply = Reply()
    content: list[str] = []
    reasoning: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            break
        obj = json.loads(payload)
        if obj.get("error"):
            raise RuntimeError(str(obj["error"]))
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            r = delta.get("reasoning") or delta.get("reasoning_content") or ""
            c = delta.get("content") or ""
            clock.saw(r, c)
            reasoning.append(r)
            content.append(c)
            if choice.get("finish_reason"):
                reply.done_reason = choice["finish_reason"]
        usage = obj.get("usage")
        if usage:
            reply.prompt_tokens = usage.get("prompt_tokens")
            reply.cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            reply.output_tokens = usage.get("completion_tokens")
        timings = obj.get("timings")
        if isinstance(timings, dict):
            reply.prefill_s = _ms(timings.get("prompt_ms"))
            reply.decode_s = _ms(timings.get("predicted_ms"))
    reply.content, reply.reasoning = "".join(content), "".join(reasoning)
    clock.fill(reply)
    return reply


def _ns(value: Any) -> float | None:
    return value / 1e9 if isinstance(value, (int, float)) else None


def _ms(value: Any) -> float | None:
    return value / 1e3 if isinstance(value, (int, float)) and value > 0 else None


def ask(target: Target, case: OcrCase, image_png: bytes) -> Reply:
    """Send one case and measure the reply. Raises on transport errors and
    non-2xx responses, with the backend's own message."""
    if target.transport == "ollama":
        body, parse = ollama_body(target, case, image_png), parse_ollama_stream
    else:
        body, parse = openai_body(target, case, image_png), parse_openai_stream
    clock = _Clock()
    with requests.post(target.url, json=body, headers=target.headers,
                       stream=True, timeout=REQUEST_TIMEOUT) as resp:
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        reply = parse(resp.iter_lines(), clock)
    reply.reasoning_tokens = estimate_reasoning_tokens(
        reply.output_tokens, reply.reasoning, reply.content)
    return reply


def warm_up(target: Target) -> float:
    """One throwaway text request, so a cold model's load time stays out of
    the first case's timing. Returns the seconds it took."""
    started = time.monotonic()
    # Both transports take this same minimal body.
    body = {"model": target.model_name, "stream": False,
            "messages": [{"role": "user", "content": "hi"}], **target.options}
    resp = requests.post(target.url, json=body, headers=target.headers, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return time.monotonic() - started


# --- one trial ----------------------------------------------------------------

@dataclass
class Trial:
    case: str
    trial_index: int
    passed: bool
    missing: list[str]
    extra: list[str]
    error: str | None
    reply: Reply

    def to_json(self) -> dict[str, Any]:
        reply = asdict(self.reply)
        reply["reasoning"] = reply["reasoning"][:MAX_STORED_REASONING]
        reply["decode_tps"] = self.reply.decode_tps
        return {"case": self.case, "trial_index": self.trial_index,
                "passed": self.passed, "missing": self.missing,
                "also_named": self.extra, "error": self.error, **reply}


def run_trial(target: Target, case: OcrCase, trial_index: int, image_png: bytes) -> Trial:
    started = time.monotonic()
    try:
        reply = ask(target, case, image_png)
    except Exception as exc:                              # noqa: BLE001
        reply = Reply(elapsed=time.monotonic() - started)
        return Trial(case.name, trial_index, False, list(case.expected), [],
                     f"{type(exc).__name__}: {exc}", reply)
    missing = missing_words(reply.content, case.expected)
    return Trial(case.name, trial_index, not missing, missing,
                 also_named(reply.content, case.expected), None, reply)


# --- CLI ----------------------------------------------------------------------

def _one_line(text: str, width: int = 100) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _mean(values: list[float | int | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.vision_ocr",
        description="Ask vision models to read the text in a fixed test image.")
    parser.add_argument("uuids", nargs="+", type=UUID, help="ModelConfig or override uuid")
    parser.add_argument("--trials", type=int, default=3, help="runs per case (default 3)")
    args = parser.parse_args(argv)

    import db
    image_png = IMAGE_PATH.read_bytes()
    app = db.make_app()
    targets: list[Target] = []
    with app.app_context():
        for uid in args.uuids:
            try:
                targets.append(resolve_target(uid))
            except (LookupError, RuntimeError) as exc:
                print(f"{uid}: {exc}", file=sys.stderr)
    if not targets:
        return 2

    summary: list[tuple[Target, dict[str, int]]] = []
    for target in targets:
        print(f"=== {target.label}  ({target.model_name}, {target.uuid}) ===")
        try:
            print(f"  warm-up {warm_up(target):.1f}s")
        except Exception as exc:                          # noqa: BLE001
            print(f"  warm-up failed: {_one_line(str(exc))}")
        passes: dict[str, int] = {}
        for case in CASES:
            trials = [run_trial(target, case, i, image_png) for i in range(args.trials)]
            passes[case.name] = sum(t.passed for t in trials)
            replies = [t.reply for t in trials]
            print(f"  {case.name:<13} {passes[case.name]}/{args.trials}  "
                  f"{_fmt(_mean([r.elapsed for r in replies]), '5.1f')}s  "
                  f"think {_fmt(_mean([r.thinking for r in replies]), '4.1f')}s  "
                  f"out {_fmt(_mean([r.output_tokens for r in replies]), '.0f')} tok "
                  f"(≈{_fmt(_mean([r.reasoning_tokens for r in replies]), '.0f')} reasoning)  "
                  f"{_fmt(_mean([r.decode_tps for r in replies]), '.0f')} tok/s")
            for t in trials:
                if t.error is not None:
                    print(f"    #{t.trial_index + 1} ERROR {_one_line(t.error)}")
                    continue
                verdict = "pass" if t.passed else "miss " + ",".join(t.missing)
                extra = f"  also named {','.join(t.extra)}" if t.extra else ""
                print(f"    #{t.trial_index + 1} {verdict}{extra}  | {_one_line(t.reply.content)}")
        summary.append((target, passes))
        print()

    width = max(len(t.label) for t, _ in summary)
    print("summary (passes out of", args.trials, "per case)")
    print(f"  {'model':<{width}}  " + "  ".join(f"{c.name:>12}" for c in CASES) + "   total")
    for target, passes in summary:
        total = sum(passes.values())
        cells = "  ".join(f"{passes[c.name]:>12}" for c in CASES)
        print(f"  {target.label:<{width}}  {cells}   {total}/{args.trials * len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
