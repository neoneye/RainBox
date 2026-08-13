"""Ask a live backend whether it reuses KV cache across a shared prompt prefix.

Not a test -- it needs a running provider and reports numbers rather than
asserting. Run it after a prompt-ordering change to see whether the ordering
is buying anything. Importing `llm` pulls in `llm.activity`, which lazily
imports `db` once a chat call completes, so set DATABASE_URL to the sandbox
database (never rainbox_production) when running this:

    cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude \\
        ./venv/bin/python -m tools.measure_prefix_cache ollama <model>

## Two mistakes in an earlier version of this probe, and why they mattered

**`prompt_eval_count` cannot detect prefix reuse.** It reports the number of
tokens in the prompt Ollama was asked to process -- prompt *length*, not
what was actually recomputed. An earlier version of this probe treated a
flat `prompt_eval_count` across two calls as evidence the prefix was not
reused. That reasoning is wrong: Ollama reports the same count whether the
prefix was served from cache or reprocessed from scratch, because the count
describes the request, not the work. Verified directly against a live
Ollama instance: a cached call and an uncached call at the same prompt
length report identical `prompt_eval_count` but wildly different
`prompt_eval_duration` (tens of ms vs. seconds). Duration is the only field
here that moves when the cache is actually hit, so it is the primary signal
below; count is printed for reference only, explicitly labelled as blind to
reuse.

**A "cold" call is only cold if the exact prefix bytes were never sent
before.** Ollama's cache keys off the literal prompt content, not a
conceptual "this is a fresh run of the probe". If this probe used the same
static filler text on every invocation, the very first invocation's cold
call would be genuinely cold, but every run after that would find the
"cold" prefix already warm from a previous process's cache, and the
measurement would silently degrade to comparing warm against warm. `
_fresh_prefix()` below stamps a per-run nonce into the filler so the byte
sequence sent as the "cold" prefix has never been seen by the server before,
on every single run.

A third, separate cost this probe deliberately excludes: the very first
request to an unloaded model also pays a model-load cost (reading weights
off disk into memory/VRAM), which can dwarf prefill time and has nothing to
do with prefix caching. `main()` fires one throwaway warm-up call before the
cold measurement so the model is already loaded by the time prefill timing
starts; the numbers reported are prefill only.

## What's read off the response

- Native Ollama wrapper (`llama_index.llms.ollama.Ollama`, used for
  provider_id == "ollama" -- see `llm._prepare_ollama_llm`): `response.raw`
  is the raw `/api/chat` JSON as a plain `dict`, e.g. `{"model": ...,
  "prompt_eval_count": 2034, "prompt_eval_duration": 63158000,
  "eval_count": 26, ...}`. `prompt_eval_duration` is nanoseconds. Confirmed
  live: with a genuinely fresh ~2000-token prefix, a first call costs
  roughly 2.3s of prefill; a second call sharing that exact prefix (novel
  tail) costs roughly 55ms -- about 40x faster -- while `prompt_eval_count`
  stays at 2034 for both. That gap is prefix reuse; the flat count would
  have hidden it entirely.
- OpenAI-compat path (`ThinkingAwareOpenAILike`, used for every other
  provider_id -- Jan, LM Studio -- both llama.cpp-backed): `response.raw` is
  an OpenAI `ChatCompletion` pydantic object. llama.cpp adds a non-standard
  `timings` object (`prompt_n`, `prompt_ms` -- milliseconds, not
  nanoseconds) beside the standard `usage` block; the openai SDK keeps
  unrecognised fields on `.model_extra`, so both are read from there. This
  branch is **unverified** -- neither Jan nor LM Studio was running when
  this probe was written/tested, only Ollama. It is written defensively: if
  the expected shape isn't there it returns `(None, None, "ms")` rather than
  guessing, and the caller reports "cannot tell" plainly instead of forcing
  a verdict out of a number it doesn't trust.

Both calls send a single USER message (no system prompt): the shared prefix
is filler, deliberately placed at the head of a *user* message rather than
in a system prompt, because that's the case this probe exists to check --
prefix caching is positional, not message-boundary-aware, and Task 2's
`_append_static_head` puts shared material at the head of the user turn, not
in the system prompt.
"""
import random
import sys
import time
from typing import Any

from llama_index.core.llms import ChatMessage, MessageRole

import llm
import providers

# Below this fraction of the cold duration, warm is called "reused". Picked
# from live measurement, not guessed: repeated cold-vs-cold runs (ordinary
# system noise -- thermal state, other processes) varied by 13-23% run to
# run, while a genuine cache hit collapsed duration by ~97% (40x). 50% sits
# with wide margin above the noise floor and wide margin below a real hit,
# so it can't be tripped by noise alone.
_REUSE_THRESHOLD = 0.5

_WARMUP_PROMPT = "Say hello in one word."
_FILLER_LINE_COUNT = 400


def _fresh_prefix() -> str:
    """Build filler text that has never been sent to the backend before.

    Prefix-cache matching is a contiguous match starting at token 0: the
    server walks the new request's tokens against a cached sequence and
    stops at the first mismatch. So a nonce (wall-clock nanoseconds + a
    random int) only needs to sit in the very first line -- once token 0
    differs from every previous run, nothing cached from a prior run can
    match this run's prefix at all, regardless of what the rest of the
    filler says. The bulk filler after it is plain and stable, which keeps
    the token count (and therefore cold-call latency) comparable run to
    run. Without the nonce, a second run of the probe would find its
    "cold" prefix already cached from a previous run and silently measure
    warm-vs-warm -- the exact bug this rewrite fixes.
    """
    nonce = f"{time.time_ns()}-{random.randint(0, 1_000_000_000)}"
    header = f"You are a helpful assistant. Session nonce: {nonce}.\n"
    line = "filler context line.\n"
    return header + line * _FILLER_LINE_COUNT


def _to_ms(duration: float | None, unit: str) -> float | None:
    if duration is None:
        return None
    return duration / 1_000_000 if unit == "ns" else duration


def _fmt_ms(duration_ms: float | None) -> str:
    return f"{duration_ms:8.1f} ms" if duration_ms is not None else "n/a"


def main(provider_id: str, model: str) -> None:
    provider = providers.get(provider_id)
    provider.ensure_loaded(model, 8192)
    print(f"probing {provider_id}/{model}")

    # Throwaway call: pays model-load cost (if the model wasn't already
    # resident) so that cost doesn't leak into the "cold" prefill number
    # measured next. Its own timing is discarded.
    _one_call(provider_id, model, _WARMUP_PROMPT)

    prefix = _fresh_prefix()
    cold_count, cold_raw, unit = _one_call(provider_id, model, prefix + "Say A.")
    warm_count, warm_raw, _ = _one_call(provider_id, model, prefix + "Say B.")
    cold_ms = _to_ms(cold_raw, unit)
    warm_ms = _to_ms(warm_raw, unit)

    print(f"  cold (novel prefix)   count={cold_count!s:<6} prefill={_fmt_ms(cold_ms)}")
    print(f"  warm (same prefix)    count={warm_count!s:<6} prefill={_fmt_ms(warm_ms)}")
    print()
    print("Note: prompt_eval_count/timings.prompt_n is prompt LENGTH, not a "
          "reuse signal -- Ollama/llama.cpp report it identically whether "
          "the prefix was cached or reprocessed. The verdict below is based "
          "entirely on prefill duration, the field that actually moves.")
    print()

    if cold_ms is None or warm_ms is None:
        print("Verdict: cannot tell. This backend/path did not report a "
              "prefill duration at all (see the OpenAI-compat caveat in "
              "the module docstring -- that branch is unverified). Rerun "
              "against Ollama, or inspect response.raw manually.")
        return

    if warm_ms <= 0:
        print(f"cold prefill: {_fmt_ms(cold_ms)}")
        print(f"warm prefill: {_fmt_ms(warm_ms)} (reported as ~0 -- "
              "effectively instant)")
        print("ratio: effectively infinite (division by ~0)")
        print("Verdict: the prefix WAS reused -- warm prefill collapsed to "
              "essentially nothing.")
        return

    ratio = cold_ms / warm_ms
    reused = warm_ms < cold_ms * _REUSE_THRESHOLD
    print(f"cold prefill: {_fmt_ms(cold_ms)}")
    print(f"warm prefill: {_fmt_ms(warm_ms)}")
    print(f"ratio: {ratio:.1f}x")
    if reused:
        print(f"Verdict: the prefix WAS reused -- warm prefill is {ratio:.1f}x "
              f"faster than cold, well under the {_REUSE_THRESHOLD:.0%} "
              "threshold used here to call it 'reused' (chosen because "
              "ordinary run-to-run noise measured 13-23%, and a real cache "
              "hit measured ~97% -- 50% sits clear of both).")
    else:
        print("Verdict: the prefix was NOT reused (or reuse could not be "
              f"detected) -- warm prefill did not drop below "
              f"{_REUSE_THRESHOLD:.0%} of cold.")


def _one_call(provider_id: str, model: str, prompt: str) -> tuple[int | None, int | None, str]:
    """One chat completion; return (prefilled_token_count, prefill_duration, unit).

    See the module docstring for how these fields were found and which
    branch is verified against a live backend vs. structurally best-effort.
    """
    arguments: dict[str, Any] = {
        **providers.get(provider_id).default_arguments(),
        "context_window": 8192,
    }
    the_llm = llm.prepare_llm(provider_id, model, arguments)
    response = the_llm.chat([ChatMessage(role=MessageRole.USER, content=prompt)])
    raw = response.raw

    if isinstance(raw, dict):
        return raw.get("prompt_eval_count"), raw.get("prompt_eval_duration"), "ns"

    timings = getattr(raw, "timings", None)
    if timings is None:
        extra = getattr(raw, "model_extra", None) or {}
        timings = extra.get("timings")
    if isinstance(timings, dict):
        return timings.get("prompt_n"), timings.get("prompt_ms"), "ms"
    return getattr(timings, "prompt_n", None), getattr(timings, "prompt_ms", None), "ms"


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
