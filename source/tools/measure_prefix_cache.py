"""Ask a live backend whether it reuses KV cache across a shared prompt prefix.

Not a test -- it needs a running provider and reports numbers rather than
asserting. Run it after a prompt-ordering change to see whether the ordering
is buying anything. Importing `llm` pulls in `llm.activity`, which lazily
imports `db` once a chat call completes, so set DATABASE_URL to the sandbox
database (never rainbox_production) when running this:

    cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude \\
        ./venv/bin/python -m tools.measure_prefix_cache ollama <model>

Reads back the backend's own prefill counter: `prompt_eval_count` (Ollama) or
`timings.prompt_n` (llama.cpp, which Jan and LM Studio are built on). A second
call sharing a long prefix should report far fewer prefilled tokens than the
first. If both report the same, the backend is not reusing the prefix and the
tier ordering cannot help until its slot configuration changes.

Also prints the backend's own prefill *duration* (`prompt_eval_duration` for
Ollama, `timings.prompt_ms` for llama.cpp) alongside the token count. The
token count alone can't tell "the backend didn't reuse the prefix" apart from
"the counter always reports full prompt length regardless of what was
reused" -- if a run were cached, prefill duration for the same token count
would collapse toward zero even though the reported count stayed put. A flat
count *and* a flat duration together are the signal that the prefix was
genuinely reprocessed, not just under-reported.

Both calls send a single USER message (no system prompt): SHARED_PREFIX is
static filler, deliberately placed at the head of a *user* message rather
than in a system prompt, because that's the case this probe exists to check
-- prefix caching is positional, not message-boundary-aware, and Task 2's
`_append_static_head` puts shared material at the head of the user turn, not
in the system prompt. If the counter shows reuse here, it should show reuse
for the real builders too.
"""
import sys
from typing import Any

from llama_index.core.llms import ChatMessage, MessageRole

import llm
import providers

SHARED_PREFIX = "You are a helpful assistant.\n" + ("filler context line.\n" * 400)


def main(provider_id: str, model: str) -> None:
    provider = providers.get(provider_id)
    provider.ensure_loaded(model, 8192)
    print(f"probing {provider_id}/{model}")
    results = []
    for label, tail in (("cold", "Say A."), ("warm", "Say B.")):
        tokens, duration, unit = _one_call(provider_id, model, SHARED_PREFIX + tail)
        results.append((label, tokens, duration))
        duration_str = f"{duration} {unit}" if duration is not None else "n/a"
        print(f"  {label}: {tokens} prompt tokens prefilled (prefill duration {duration_str})")

    _, cold_tokens, cold_duration = results[0]
    _, warm_tokens, warm_duration = results[1]
    print()
    if cold_tokens is None or warm_tokens is None:
        print("Interpretation: the backend did not report a prefill token "
              "count at all -- this probe cannot answer the question for "
              "this provider/model. Try a different backend or inspect "
              "response.raw manually.")
    elif warm_tokens < cold_tokens:
        print(f"Interpretation: warm ({warm_tokens}) is below cold "
              f"({cold_tokens}) -- the prefix was reused.")
    else:
        # Equal token counts are ambiguous on their own: some backends report
        # total prompt length regardless of what was actually reprocessed.
        # Prefill duration disambiguates -- a real cache hit collapses
        # duration toward zero even when the reported count doesn't move.
        if cold_duration is not None and warm_duration is not None and warm_duration > 0:
            drop = 1 - (warm_duration / cold_duration)
            if drop > 0.5:
                print(f"Interpretation: token counts are equal ({cold_tokens}), "
                      f"but prefill duration dropped {drop:.0%} (cold "
                      f"{cold_duration} -> warm {warm_duration}). The counter "
                      "does not expose reuse on this backend, but the timing "
                      "does -- the prefix WAS reused.")
            else:
                print(f"Interpretation: token counts are equal ({cold_tokens}) "
                      f"and prefill duration did not collapse (cold "
                      f"{cold_duration} -> warm {warm_duration}, {drop:.0%} "
                      "change). This is genuine non-reuse, not a blind "
                      "counter: the backend reprocessed the full prefix both "
                      "times. The tier ordering buys nothing until slot "
                      "configuration (e.g. num_parallel) changes.")
        else:
            print(f"Interpretation: token counts are equal ({cold_tokens}) and "
                  "no duration signal is available to disambiguate. Cannot "
                  "tell 'no reuse' from 'counter blind to reuse' -- rerun "
                  "with a backend that reports prefill duration, or check "
                  "server-side logs/metrics instead.")


def _one_call(provider_id: str, model: str, prompt: str) -> tuple[int | None, int | None, str]:
    """One chat completion; return (prefilled_token_count, prefill_duration, unit).

    Both fields were found by inspecting a live `response.raw` in a REPL, not
    guessed:

    - Native Ollama wrapper (`llama_index.llms.ollama.Ollama`, used for
      provider_id == "ollama" -- see `llm._prepare_ollama_llm`):
      `response.raw` is the raw `/api/chat` JSON as a plain `dict`, e.g.
      `{"model": ..., "prompt_eval_count": 2034, "prompt_eval_duration":
      63158000, "eval_count": 26, ...}`. `prompt_eval_count` is the number of
      prompt tokens the server ran through prefill for *this* call;
      `prompt_eval_duration` is in nanoseconds -- confirmed by calling
      `llm.prepare_llm("ollama", model, ...).chat(...)` directly and printing
      `resp.raw`.
    - OpenAI-compat path (`ThinkingAwareOpenAILike`, used for every other
      provider_id -- Jan, LM Studio -- both llama.cpp-backed):
      `response.raw` is an OpenAI `ChatCompletion` pydantic object. llama.cpp
      adds a non-standard `timings` object (with `prompt_n` and `prompt_ms`,
      milliseconds not nanoseconds) beside the standard `usage` block; the
      openai SDK keeps unrecognised fields on `.model_extra` rather than
      dropping them, so both are read from there. Not exercised in this run
      -- Ollama was the only backend up -- so this branch is best-effort,
      structurally mirroring the Ollama branch rather than confirmed against
      a live response; if it never fires, `_one_call` returns (None, None,
      "ms") and the caller sees that plainly instead of a silently wrong
      number.
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
