"""Cache arithmetic for the /activity dashboard — pure functions, no DB, no
provider, no clock.

Local runtimes do not tell us what they cached. Measured against the
operator's Ollama, an identical 4032-token prompt reported
`prompt_eval_count=4032` whether it prefilled in 2093 ms (cold) or 49 ms
(warm). The token count is blind to the cache; only the *time* moves.

So cache behaviour is reconstructed from two independent angles, deliberately
kept apart rather than blended into one number:

- `cached_tokens_estimate` — what the runtime evidently reused, inferred from
  how much faster the prefill ran than that model's own cold baseline.
  Timing-derived, so an estimate.
- `reusable_prefix_tokens` — what it *could* have reused, from how much of
  the prompt's leading text rainbox itself had already sent. Owes the
  provider nothing, so it is exact.

Reusable high but cached low means the runtime evicted the cache. Reusable
low means rainbox's own prompt assembly broke the shared prefix — the case a
hosted dashboard cannot see, because only we know how the prompt was built.
"""

from __future__ import annotations

import hashlib

# Prompts are hashed in fixed-size character blocks rather than tokens: a
# per-model tokenizer isn't available at the instrumentation layer, and the
# block index only has to be *proportional* to token position, which it is
# within a single prompt. 1000 chars ≈ 250 tokens — fine enough to localise a
# cache break to a paragraph, coarse enough that a 20k-char prompt costs 20
# cheap hashes.
BLOCK_CHARS: int = 1000

# A model needs this many recorded calls before its cold-prefill baseline
# means anything. Under it, the estimate is withheld and the page shows
# "calibrating" rather than a confident wrong number.
MIN_CALIBRATION_CALLS: int = 20

# Which percentile of observed prefill throughput counts as "cold". Not the
# minimum: a single pathological call (a model load, a busy machine) would
# redefine the baseline and inflate every later estimate.
COLD_RATE_PERCENTILE: float = 5.0

# Estimated savings below this fraction of the prompt are reported as zero.
# Because the baseline is a p5 it sits a little slower than a typical cold
# call, so an uncached call scores a token or two of "cache" — harmless once,
# a fabricated baseline hit rate across thousands of calls.
MIN_CACHE_FRACTION: float = 0.02


def prefix_chain(text: str, block_chars: int = BLOCK_CHARS) -> list[str]:
    """Cumulative hashes of `text`'s successive blocks, one entry per block.

    Entry *i* covers everything from the start of the prompt through block
    *i*, so two chains agree on entry *i* only if the prompts are identical up
    to that point — which is exactly the condition a KV cache needs. An edit
    in the first block therefore breaks every later entry, mirroring the way
    it invalidates the whole cached prefix.

    The prompt text is not retained anywhere; the chain is the entire record.
    """
    chain: list[str] = []
    running = b""
    for start in range(0, len(text), block_chars):
        block = text[start : start + block_chars].encode("utf-8", "replace")
        running = hashlib.blake2b(running + block, digest_size=16).digest()
        chain.append(running.hex())
    return chain


def shared_prefix_blocks(chain: list[str], candidate: list[str]) -> int:
    """How many leading blocks two chains agree on. Stops at the first
    difference — a later coincidental match is past the cache break and
    counts for nothing."""
    matched = 0
    for mine, theirs in zip(chain, candidate):
        if mine != theirs:
            break
        matched += 1
    return matched


def reusable_prefix_tokens(
    chain: list[str],
    candidates: list[list[str]],
    total_chars: int,
    prompt_tokens: int | None,
    block_chars: int = BLOCK_CHARS,
) -> int | None:
    """Tokens of this prompt that a warm cache could have supplied, scored
    against the best of `candidates` (recent prompts to the same model).

    Best-of, not most-recent: Ollama holds several prefixes at once — the
    probe watched prefix A stay warm across an intervening call on prefix B —
    so scoring only against the previous call would under-report.

    Returns None when the provider reported no token count, since there is
    then nothing to scale the character fraction onto.
    """
    if prompt_tokens is None:
        return None
    if not chain or not candidates or total_chars <= 0 or prompt_tokens <= 0:
        return 0
    best = max(shared_prefix_blocks(chain, c) for c in candidates)
    # The trailing block is usually partial, so blocks * block_chars can
    # overshoot the real length; cap before converting to a fraction.
    matched_chars = min(best * block_chars, total_chars)
    return int(round(prompt_tokens * matched_chars / total_chars))


def percentile(values: list[float], q: float) -> float | None:
    """The `q`-th percentile by linear interpolation, or None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def cold_rate(throughputs: list[float]) -> float | None:
    """A model's cold prefill rate in tokens/sec, from its recent observed
    throughputs. None until the model has enough calls to have plausibly
    shown a cold one.

    Throughput is bimodal by roughly fifty times — the probe measured ~1.9k
    tok/s cold against ~82k tok/s warm — so the slow tail of the distribution
    is the cold regime, and a low percentile finds it without needing to know
    the hardware.
    """
    if len(throughputs) < MIN_CALIBRATION_CALLS:
        return None
    return percentile(throughputs, COLD_RATE_PERCENTILE)


def cached_tokens_estimate(
    prompt_tokens: int | None,
    prefill_ms: int | None,
    model_cold_rate: float | None,
) -> int | None:
    """Tokens the runtime evidently served from cache.

    Continuous rather than a hit/miss flag, because partial hits are the
    common case: cache half a prompt's prefix and the prefill takes half as
    long. What a cold run of this prompt would have cost is
    `prompt_tokens / cold_rate`; whatever time was saved against that,
    expressed back in tokens, is what the cache supplied.

    None when any input is missing or the baseline isn't calibrated — the
    caller shows "calibrating", never a fabricated zero.
    """
    if prompt_tokens is None or prefill_ms is None:
        return None
    if model_cold_rate is None or model_cold_rate <= 0:
        return None
    evaluated = (prefill_ms / 1000.0) * model_cold_rate
    saved = max(0.0, min(float(prompt_tokens), prompt_tokens - evaluated))
    if saved < prompt_tokens * MIN_CACHE_FRACTION:
        return 0
    return int(round(saved))
