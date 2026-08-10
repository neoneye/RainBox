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


class TestColdRate:
    def test_too_few_samples_means_we_admit_we_dont_know(self):
        """Reporting a hit rate off three calls would be a confident guess.
        The page says 'calibrating' instead, and that starts here."""
        assert cold_rate([1000.0] * (MIN_CALIBRATION_CALLS - 1)) is None

    def test_enough_samples_yields_the_slow_tail(self):
        # 19 fast (cached) calls and 20 slow (cold) ones: the cold baseline
        # must come from the slow end, not the average.
        samples = [80_000.0] * 19 + [1_000.0] * 20
        rate = cold_rate(samples)
        assert rate is not None
        assert 900.0 <= rate <= 1_100.0

    def test_a_lone_outlier_does_not_drag_the_baseline_down(self):
        """p5, not min — one pathologically slow call (a cold model load, a
        busy machine) must not redefine the model's cold rate and silently
        inflate every later hit estimate."""
        samples = [1_000.0] * 39 + [1.0]
        rate = cold_rate(samples)
        assert rate is not None
        assert rate > 500.0


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
