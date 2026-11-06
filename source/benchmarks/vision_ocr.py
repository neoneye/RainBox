"""Opt-in benchmark: can a vision model read the text in an image?

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
other words as well does not fail a location case; the report lists them as
"also named", so a model that answers every question with the whole list is
visible without being scored as wrong.

    python -m benchmarks.vision_ocr <model-uuid> [<model-uuid> …] [--trials N]

A uuid is a ModelConfig or ModelConfigOverride, as on /model?id=…, resolved in
the database `DATABASE_URL` names. The script only reads those rows; it writes
nothing. Each request goes straight to the provider's OpenAI-compatible
/chat/completions endpoint with the image as a base64 data URL — the same
request /demo/multimodal sends.
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import requests

IMAGE_PATH = Path(__file__).parent / "fixtures" / "ocr_corners.png"
# Every word in the image, for the "also named" column.
IMAGE_WORDS = ("cat", "bug", "Testing", "ant", "dog")
# A cold vision model loads its weights and projector on the first request.
REQUEST_TIMEOUT = 300.0


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


def completion_body(model_name: str, case: OcrCase, image_png: bytes) -> dict:
    """OpenAI-compatible /chat/completions body: the case's system prompt, then
    a user message of its prompt text and the image."""
    data_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
    return {
        "model": model_name,
        "messages": [
            {"role": "system", "content": case.system},
            {"role": "user", "content": [
                {"type": "text", "text": case.user},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "stream": False,
    }


@dataclass
class Target:
    uuid: UUID
    label: str
    model_name: str
    base_url: str
    headers: dict[str, str]


def resolve_target(uid: UUID) -> Target:
    """Read the model row for `uid` (inside an app context). Raises LookupError
    when it is missing or its provider has no reachable base URL."""
    import providers
    from db import get_model_config, get_model_config_override, resolved_model_kwargs

    provider, model_name, arguments = resolved_model_kwargs(uid)
    row = get_model_config(uid) or get_model_config_override(uid)
    label = row.effective_display_name if row is not None else model_name
    base = providers.request_base_url(provider, arguments)
    if base is None:
        raise LookupError(f"{uid}: no base URL for provider {provider!r}")
    headers = {"Content-Type": "application/json"}
    api_key = providers.request_api_key(provider, arguments)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return Target(uid, label, model_name, base, headers)


def ask(target: Target, body: dict) -> str:
    """POST one completion and return the answer text. Raises on transport
    errors and non-2xx responses, with the backend's own message."""
    resp = requests.post(f"{target.base_url}/chat/completions", json=body,
                         headers=target.headers, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    choices = resp.json().get("choices") or []
    if not choices:
        raise RuntimeError("response has no choices")
    return (choices[0].get("message") or {}).get("content") or ""


@dataclass
class Trial:
    answer: str | None
    error: str | None
    elapsed: float
    missing: list[str]
    extra: list[str]

    @property
    def passed(self) -> bool:
        return self.error is None and not self.missing


def run_case(target: Target, case: OcrCase, image_png: bytes) -> Trial:
    body = completion_body(target.model_name, case, image_png)
    started = time.monotonic()
    try:
        answer = ask(target, body)
    except Exception as exc:                              # noqa: BLE001
        return Trial(None, str(exc), time.monotonic() - started, list(case.expected), [])
    elapsed = time.monotonic() - started
    return Trial(answer, None, elapsed, missing_words(answer, case.expected),
                 also_named(answer, case.expected))


def _one_line(text: str, width: int = 110) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _warm_up(target: Target) -> None:
    """One throwaway text request, so a cold model's load time stays out of
    the first case's timing."""
    body = {"model": target.model_name, "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}
    started = time.monotonic()
    try:
        ask(target, body)
        print(f"  warm-up {time.monotonic() - started:.1f}s")
    except Exception as exc:                              # noqa: BLE001
        print(f"  warm-up failed: {_one_line(str(exc))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.vision_ocr",
                                     description="Ask vision models to read the "
                                     "text in a fixed test image.")
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
        _warm_up(target)
        passes: dict[str, int] = {}
        for case in CASES:
            trials = [run_case(target, case, image_png) for _ in range(args.trials)]
            passes[case.name] = sum(t.passed for t in trials)
            mean = sum(t.elapsed for t in trials) / len(trials)
            print(f"  {case.name:<13} {passes[case.name]}/{args.trials}  "
                  f"{mean:5.1f}s  expects {', '.join(case.expected)}")
            for i, t in enumerate(trials, 1):
                if t.error is not None:
                    print(f"    #{i} ERROR {_one_line(t.error)}")
                    continue
                verdict = "pass" if t.passed else "miss " + ",".join(t.missing)
                extra = f"  also named {','.join(t.extra)}" if t.extra else ""
                print(f"    #{i} {verdict}{extra}  | {_one_line(t.answer or '')}")
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
