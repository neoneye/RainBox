"""The cache-metric arithmetic behind /activity, tested without a DB, a
provider, or a network.

Every number the dashboard shows is derived here, so these tests are where
the definitions are pinned down: what counts as a cache hit, how much of a
prompt was reusable, and when we admit we don't know yet.
"""

import pytest

from llm.activity_metrics import (
    BLOCK_CHARS,
    MIN_CALIBRATION_CALLS,
    cached_tokens_estimate,
    cold_rate,
    percentile,
    prefix_chain,
    reusable_prefix_tokens,
    shared_prefix_blocks,
)


class TestPrefixChain:
    def test_empty_text_has_no_blocks(self):
        assert prefix_chain("") == []

    def test_one_block_per_block_chars_rounded_up(self):
        assert len(prefix_chain("x" * (BLOCK_CHARS * 3))) == 3
        assert len(prefix_chain("x" * (BLOCK_CHARS * 3 + 1))) == 4

    def test_identical_text_hashes_identically(self):
        assert prefix_chain("hello " * 500) == prefix_chain("hello " * 500)

    def test_a_change_in_the_first_block_poisons_every_later_hash(self):
        """The chain is cumulative, which is the whole point: an edit near the
        top of a prompt invalidates the cache for everything after it, and the
        hashes have to say so."""
        base = "A" * BLOCK_CHARS + "B" * BLOCK_CHARS + "C" * BLOCK_CHARS
        edited = "Z" + base[1:]
        assert shared_prefix_blocks(prefix_chain(edited), prefix_chain(base)) == 0

    def test_a_change_in_the_last_block_leaves_earlier_hashes_intact(self):
        base = "A" * BLOCK_CHARS + "B" * BLOCK_CHARS + "C" * BLOCK_CHARS
        edited = base[: BLOCK_CHARS * 2] + "Z" * BLOCK_CHARS
        assert shared_prefix_blocks(prefix_chain(edited), prefix_chain(base)) == 2


class TestSharedPrefixBlocks:
    def test_no_candidate_blocks_share_nothing(self):
        assert shared_prefix_blocks(prefix_chain("x" * BLOCK_CHARS), []) == 0

    def test_counts_only_the_leading_run(self):
        a = ["h1", "h2", "h3", "h4"]
        b = ["h1", "h2", "zz", "h4"]  # h4 matches but is past the break
        assert shared_prefix_blocks(a, b) == 2

    def test_a_longer_candidate_still_matches_its_shared_head(self):
        assert shared_prefix_blocks(["h1", "h2"], ["h1", "h2", "h3"]) == 2


class TestReusablePrefixTokens:
    def test_a_fully_shared_prompt_is_fully_reusable(self):
        text = "x" * (BLOCK_CHARS * 4)
        chain = prefix_chain(text)
        assert reusable_prefix_tokens(chain, [chain], len(text), 1000) == 1000

    def test_nothing_shared_means_nothing_reusable(self):
        text = "x" * (BLOCK_CHARS * 4)
        other = prefix_chain("y" * (BLOCK_CHARS * 4))
        assert reusable_prefix_tokens(prefix_chain(text), [other], len(text), 1000) == 0

    def test_half_a_prompt_shared_is_half_the_tokens(self):
        text = "A" * (BLOCK_CHARS * 2) + "B" * (BLOCK_CHARS * 2)
        candidate = prefix_chain("A" * (BLOCK_CHARS * 2) + "C" * (BLOCK_CHARS * 2))
        got = reusable_prefix_tokens(prefix_chain(text), [candidate], len(text), 1000)
        assert got == 500

    def test_the_best_of_several_candidates_wins(self):
        """Ollama keeps several prefixes cached at once — the probe showed
        prefix A surviving an intervening call on prefix B — so a prompt is
        scored against its best match, not against the most recent call."""
        text = "A" * (BLOCK_CHARS * 4)
        poor = prefix_chain("A" * BLOCK_CHARS + "z" * (BLOCK_CHARS * 3))
        good = prefix_chain("A" * (BLOCK_CHARS * 3) + "z" * BLOCK_CHARS)
        got = reusable_prefix_tokens(prefix_chain(text), [poor, good], len(text), 1000)
        assert got == 750

    def test_matched_blocks_never_exceed_the_prompt(self):
        """The trailing block is usually partial, so blocks * BLOCK_CHARS can
        overshoot the real character count. Reusable tokens must still cap at
        the prompt's own token count."""
        text = "x" * (BLOCK_CHARS + 1)  # 2 blocks, but only 1001 chars
        chain = prefix_chain(text)
        assert reusable_prefix_tokens(chain, [chain], len(text), 300) == 300

    def test_an_empty_prompt_is_not_a_division_by_zero(self):
        assert reusable_prefix_tokens([], [], 0, 0) == 0

    def test_unknown_token_count_yields_nothing(self):
        text = "x" * BLOCK_CHARS
        chain = prefix_chain(text)
        assert reusable_prefix_tokens(chain, [chain], len(text), None) is None


class TestPercentile:
    def test_empty_input_has_no_percentile(self):
        assert percentile([], 50) is None

    def test_single_value_is_every_percentile(self):
        assert percentile([7.0], 5) == 7.0
        assert percentile([7.0], 99) == 7.0

    def test_median_of_an_odd_run(self):
        assert percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_interpolates_between_neighbours(self):
        assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    def test_ignores_input_order(self):
        assert percentile([9.0, 1.0, 5.0], 50) == 5.0

    def test_extremes_are_the_endpoints(self):
        assert percentile([1.0, 2.0, 3.0], 0) == 1.0
        assert percentile([1.0, 2.0, 3.0], 100) == 3.0


def cold(throughput, n=1):
    """Samples from calls with nothing to reuse — necessarily cold."""
    return [(throughput, 0.0)] * n


def warm(throughput, n=1):
    """Samples from calls whose prompt almost entirely repeated an earlier
    one, so the runtime had every opportunity to serve them from cache."""
    return [(throughput, 0.99)] * n


class TestColdRate:
    def test_too_few_samples_means_we_admit_we_dont_know(self):
        """Reporting a hit rate off three calls would be a confident guess.
        The page says 'calibrating' instead, and that starts here."""
        assert cold_rate(cold(1000.0, MIN_CALIBRATION_CALLS - 1)) is None

    def test_calls_with_nothing_to_reuse_define_the_baseline(self):
        """A prompt that repeats nothing rainbox sent before *cannot* have
        been served from cache, whatever the runtime did. Those calls are
        cold by construction, which makes them the trustworthy sample — no
        clustering guesswork required."""
        rate = cold_rate(warm(90_000.0, 20) + cold(1_000.0, 3))
        assert rate == pytest.approx(1_000.0)

    def test_warm_calls_do_not_pollute_the_baseline(self):
        """The bug this replaced: with 23 warm samples and one cold one, any
        low percentile lands inside the warm cluster and the baseline comes
        out ~50x too fast — scoring 99%-cached calls as 20% cached, exactly
        when the cache is working best."""
        rate = cold_rate(warm(90_000.0, 40) + cold(1_100.0, 3))
        assert rate is not None
        assert rate < 5_000.0

    def test_a_lone_outlier_does_not_drag_the_baseline_down(self):
        """A stalled call is not a cold regime; the median of the cold
        samples ignores it."""
        rate = cold_rate(cold(1_000.0, 39) + cold(1.0, 1))
        assert rate is not None
        assert rate > 500.0

    def test_uniformly_cold_calls_give_their_own_rate(self):
        rate = cold_rate(cold(1_000.0, 25))
        assert rate == pytest.approx(1_000.0)

    def test_a_single_cold_call_is_too_thin_to_trust(self):
        """One measurement could be anything — a model load, a busy moment.
        Fall through to the regime split rather than anchoring on it."""
        rate = cold_rate(warm(90_000.0, 25) + cold(1_100.0, 1))
        # The split still finds the slow cluster, so a rate is available.
        assert rate is not None
        assert rate < 5_000.0

    def test_all_warm_and_nothing_definitely_cold_admits_it_cannot_tell(self):
        """The live failure this test exists for: an Ollama already warm from
        an earlier session served every call from cache, so the samples hold
        one cluster and no cold measurement at all. Reading that cluster as
        the cold rate reports 0% cached on calls that were ~99% cached.
        Better to say nothing."""
        assert cold_rate(warm(90_000.0, 24)) is None

    def test_a_split_needs_a_real_gap_not_just_ordinary_spread(self):
        """Prefill throughput varies call to call; jitter is not a boundary.
        With no definitely-cold samples and no real gap, we cannot tell which
        regime we are looking at."""
        samples = [(t, 0.99) for t in (900.0, 950.0, 1_000.0, 1_050.0, 1_100.0)] * 5
        assert cold_rate(samples) is None

    def test_an_unknown_reuse_fraction_is_not_assumed_cold(self):
        """A provider that reports no token count leaves the reuse fraction
        unknown. Unknown is not evidence of coldness."""
        assert cold_rate([(90_000.0, None)] * 24) is None


class TestCachedTokensEstimate:
    def test_no_baseline_means_no_estimate(self):
        assert cached_tokens_estimate(4000, 50, None) is None

    def test_missing_measurements_mean_no_estimate(self):
        assert cached_tokens_estimate(None, 50, 1000.0) is None
        assert cached_tokens_estimate(4000, None, 1000.0) is None

    def test_a_cold_call_caches_nothing(self):
        """4032 tokens at the model's own cold rate — exactly the measured
        cold case from the probe. Nothing was reused."""
        assert cached_tokens_estimate(4032, 2093, 1926.0) == 0

    def test_a_warm_call_caches_nearly_everything(self):
        """The same prompt, 49 ms instead of 2093 ms — the probe's warm case.
        Almost the whole prefix came from cache."""
        got = cached_tokens_estimate(4032, 49, 1926.0)
        assert got is not None
        assert got > 3900

    def test_a_sliver_below_the_noise_floor_reads_as_nothing(self):
        """cold_rate is a p5, so it sits slightly slower than a typical cold
        call and every cold call would otherwise report a token or two of
        phantom cache. Summed over thousands of calls that is a fake baseline
        hit rate, so anything under the floor is reported as zero."""
        # 1% of the prompt — real caching never looks like this.
        assert cached_tokens_estimate(10_000, 9_900, 1000.0) == 0

    def test_a_saving_above_the_noise_floor_is_reported(self):
        assert cached_tokens_estimate(10_000, 9_000, 1000.0) == 1000

    def test_a_half_cached_prompt_reads_as_half(self):
        # 2000 tokens, cold rate 1000 tok/s -> 2000 ms cold. Took 1000 ms.
        assert cached_tokens_estimate(2000, 1000, 1000.0) == 1000

    def test_slower_than_cold_never_goes_negative(self):
        assert cached_tokens_estimate(1000, 5000, 1000.0) == 0

    def test_an_instant_prefill_never_exceeds_the_prompt(self):
        assert cached_tokens_estimate(1000, 0, 1000.0) == 1000


@pytest.mark.parametrize("bad", [-1.0, 0.0])
def test_a_nonpositive_cold_rate_is_refused_rather_than_dividing_by_it(bad):
    assert cached_tokens_estimate(1000, 100, bad) is None
