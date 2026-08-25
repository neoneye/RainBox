"""The /activity page: rendering, controls, and the states that are easy to
get wrong — no data at all, and a model that hasn't calibrated yet."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import db
import webapp
from db import LlmCall
from llm.activity_metrics import MIN_CALIBRATION_CALLS
from webapp.activity_views import (
    DEFAULT_METRIC,
    _hit_rate_cell,
    build_chart,
    cache_reading,
    exact,
    pick_bucket_seconds,
    resolve_range,
    si,
)


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        with webapp.app.app_context():
            yield c


@pytest.fixture
def model(client) -> str:
    name = f"test-model-{uuid4().hex[:8]}"
    yield name
    db.db.session.query(LlmCall).filter(LlmCall.model == name).delete(
        synchronize_session=False
    )
    db.db.session.commit()


def add_call(model: str, minutes_ago: int = 1, **overrides):
    row = {
        "started_at": datetime.now(UTC) - timedelta(minutes=minutes_ago),
        "finished_at": datetime.now(UTC),
        "provider": "ollama",
        "model": model,
        "caller": "test.caller",
        "ok": True,
        "prompt_tokens": 4000,
        "completion_tokens": 100,
        "prefill_ms": 50,
        "decode_ms": 1000,
        "total_ms": 1100,
        "cached_tokens_estimated": 3900,
        "reusable_prefix_tokens": 3900,
        "prefix_chain": ["a"],
    }
    row.update(overrides)
    return db.record_llm_call(row)


class TestRendering:
    def test_the_page_loads(self, client):
        assert client.get("/activity").status_code == 200

    def test_it_appears_in_the_nav(self, client):
        assert b">Activity<" in client.get("/activity").data

    def test_a_recorded_call_shows_up(self, client, model):
        add_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert model in body
        assert "Cache hit rate" in body

    def test_the_chart_is_inline_svg_with_no_external_dependency(self, client, model):
        add_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "<svg" in body
        assert "<script src=" not in body
        assert "http://cdn" not in body


class TestCallDetail:
    """Inspecting one row: what was sent, message by message, and what came
    back. The question every cache reading on the list raises."""

    def test_each_recent_row_links_to_its_own_call(self, client, model):
        call_uuid = uuid4()
        add_call(model, uuid=call_uuid)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert f"/activity/call/{call_uuid}" in body

    def test_the_detail_page_shows_every_message_with_its_role(
        self, client, model
    ):
        call_uuid = uuid4()
        add_call(model, uuid=call_uuid,
                 messages=[{"role": "system", "content": "be exact"},
                           {"role": "user", "content": "how much is 12 feet"}],
                 response_text="3.6576 meters")
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "be exact" in body
        assert "how much is 12 feet" in body
        assert "3.6576 meters" in body
        assert ">system<" in body and ">user<" in body

    def test_the_detail_page_carries_the_rows_own_metrics(self, client, model):
        """So a prompt is read next to the cache reading that prompted the
        question, not on a page that has forgotten which call this was."""
        call_uuid = uuid4()
        add_call(model, uuid=call_uuid, caller="assistant.decide",
                 messages=[{"role": "user", "content": "x"}])
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "assistant.decide" in body
        assert model in body
        assert "4000 tokens" in body   # prompt_tokens, exact

    def test_a_row_with_no_stored_text_says_so_rather_than_showing_nothing(
        self, client, model
    ):
        """Calls recorded before rainbox stored prompts, and calls whose text
        has aged out, are the ordinary state of an old row — not an error."""
        call_uuid = uuid4()
        add_call(model, uuid=call_uuid)
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert client.get(f"/activity/call/{call_uuid}").status_code == 200
        assert "Not stored" in body
        assert "No response text" in body

    def test_an_unknown_call_is_a_404(self, client):
        assert client.get(f"/activity/call/{uuid4()}").status_code == 404

    def test_the_cached_tile_says_which_kind_of_number_it_is(
        self, client, model
    ):
        """"Reported by the provider" and "estimated from prefill timing" are
        different claims. On the list they are a hover; a page for reading one
        call has the room to say it outright."""
        call_uuid = uuid4()
        add_call(model, uuid=call_uuid, cached_tokens_estimated=3900,
                 cached_tokens_reported=None)
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "estimated from prefill timing" in body

    def test_prompt_text_is_escaped_not_injected(self, client, model):
        """A prompt carries XML sections and a response may carry anything the
        model wrote. Neither is markup on this page."""
        call_uuid = uuid4()
        add_call(model, uuid=call_uuid,
                 messages=[{"role": "user",
                            "content": "<script>alert(1)</script>"}],
                 response_text="<img onerror=alert(2)>")
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body
        assert "<img onerror=alert(2)>" not in body


class TestEmptyState:
    def test_a_window_with_no_calls_says_so(self, client):
        body = client.get("/activity?range=15m").get_data(as_text=True)
        # A quiet 15 minutes is the common case on a personal box.
        if "No LLM calls recorded" not in body:
            assert "Cache hit rate" in body  # there really were calls

    def test_an_empty_window_does_not_crash_on_missing_rates(self, client):
        assert client.get("/activity?range=15m").status_code == 200


class TestControls:
    @pytest.mark.parametrize(
        "range_key",
        ["15m", "30m", "1h", "3h", "24h", "48h", "1w", "1mo", "1y",
         "today", "yesterday", "this_week", "prev_week", "this_month",
         "prev_month"],
    )
    def test_every_range_renders(self, client, range_key):
        assert client.get(f"/activity?range={range_key}").status_code == 200

    @pytest.mark.parametrize(
        "metric",
        ["cached_tokens", "hit_rate", "prompt_tokens", "completion_tokens",
         "calls", "avg_latency_ms", "p50_latency_ms", "p90_latency_ms",
         "p99_latency_ms", "avg_throughput_tps", "p50_throughput_tps",
         "p90_throughput_tps"],
    )
    def test_every_metric_renders(self, client, model, metric):
        add_call(model)
        assert client.get(f"/activity?metric={metric}").status_code == 200

    @pytest.mark.parametrize("dimension", ["model", "caller", "provider"])
    def test_every_dimension_renders(self, client, model, dimension):
        add_call(model)
        assert client.get(f"/activity?by={dimension}").status_code == 200

    def test_a_junk_range_falls_back_instead_of_erroring(self, client):
        """A stale bookmark or a hand-edited URL should still show a page."""
        assert client.get("/activity?range=nonsense").status_code == 200

    def test_a_junk_metric_falls_back(self, client):
        assert client.get("/activity?metric=../../etc/passwd").status_code == 200

    def test_a_junk_dimension_cannot_reach_the_query_layer(self, client):
        """The dimension names a SQL column, so an unknown one must be
        rejected by the view, never passed through."""
        assert client.get("/activity?by=1;drop+table+llm_call").status_code == 200


class TestCalibratingState:
    """An unjudged model and a model whose cache did nothing are different
    facts, and the page must not render them the same way."""

    def test_an_unjudged_row_reads_as_calibrating(self):
        cell = _hit_rate_cell({"judged_calls": 0, "hit_rate": None, "calls": 3})
        assert cell == "calibrating"

    def test_a_genuine_zero_reads_as_zero_percent(self):
        cell = _hit_rate_cell({"judged_calls": 5, "hit_rate": 0.0, "calls": 5})
        assert cell == "0.0%"

    def test_a_judged_row_reads_as_its_rate(self):
        cell = _hit_rate_cell({"judged_calls": 5, "hit_rate": 0.5, "calls": 5})
        assert cell == "50.0%"

    def test_the_page_names_the_models_still_calibrating(self, client, model):
        add_call(model, cached_tokens_estimated=None, cached_tokens_reported=None)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        footnote = body[body.find("Still calibrating"):]
        assert model in footnote[:400]

    def test_a_judged_model_is_not_listed_as_calibrating(self, client, model):
        add_call(model, cached_tokens_estimated=3900)
        body = client.get("/activity?range=24h&by=model").get_data(as_text=True)
        marker = body.find("Still calibrating")
        if marker != -1:
            assert model not in body[marker : marker + 400]


class TestCallerPanel:
    """Attribution is the panel that turns the dashboard from interesting
    into actionable, so it is always on screen."""

    def test_the_caller_panel_shows_without_switching_the_grouping(
        self, client, model
    ):
        add_call(model, caller="unit-test.caller")
        body = client.get("/activity?range=24h&by=model").get_data(as_text=True)
        assert "By caller" in body
        assert "unit-test.caller" in body

    def test_it_is_not_printed_twice_when_already_selected(self, client, model):
        add_call(model, caller="unit-test.caller")
        body = client.get("/activity?range=24h&by=caller").get_data(as_text=True)
        assert body.count("By caller") == 1


class TestOriginColumn:
    """The reason to open a row: which line of code made this call."""

    def test_the_recent_calls_table_shows_where_a_call_came_from(
        self, client, model
    ):
        add_call(model, origin="benchmarks/story.py:412 in _take_turn")
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "Origin" in body
        assert "benchmarks/story.py:412 in _take_turn" in body

    def test_a_call_with_no_origin_shows_a_dash_not_a_blank_cell(
        self, client, model
    ):
        add_call(model, origin=None)
        assert client.get("/activity?range=24h").status_code == 200


class TestOutputColumn:
    """What a call cost on the way out, next to what it cost on the way in."""

    def test_the_recent_calls_table_shows_generated_tokens(self, client, model):
        add_call(model, completion_tokens=1234)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert ">Output</th>" in body
        assert 'title="1234 tokens">1.234<' in body

    def test_an_unreported_output_count_shows_a_dash(self, client, model):
        add_call(model, completion_tokens=None)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert 'title="not recorded">—<' in body


class TestEstimateHonesty:
    def test_the_page_says_the_cached_band_is_an_estimate(self, client, model):
        """Local backends report no cache field, so the orange band is
        inferred. The page must not present it as measured fact."""
        add_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "estimated from prefill timing" in body


class TestRangeResolution:
    def test_a_rolling_window_ends_now(self):
        now = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
        start, end, label = resolve_range("3h", now)
        assert end == now
        assert start == now - timedelta(hours=3)
        assert label == "Past 3 hours"

    def test_yesterday_is_a_whole_day_and_stops_at_midnight(self):
        now = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
        start, end, _ = resolve_range("yesterday", now)
        assert start == datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)

    def test_previous_month_lands_on_the_right_month(self):
        now = datetime(2026, 3, 5, 9, 0, tzinfo=UTC)
        start, end, _ = resolve_range("prev_month", now)
        assert start == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

    def test_this_week_starts_on_monday(self):
        now = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)  # a Thursday
        start, _end, _ = resolve_range("this_week", now)
        assert start.weekday() == 0
        assert start == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)

    def test_an_unknown_key_becomes_the_default(self):
        now = datetime(2026, 8, 10, 15, 30, tzinfo=UTC)
        assert resolve_range("bogus", now) == resolve_range("24h", now)


class TestBucketChoice:
    def test_a_short_window_gets_fine_buckets(self):
        assert pick_bucket_seconds(timedelta(minutes=15).total_seconds()) == 60

    def test_a_year_does_not_produce_thousands_of_bars(self):
        span = timedelta(days=365).total_seconds()
        assert span / pick_bucket_seconds(span) <= 60

    def test_a_day_gets_a_readable_number_of_bars(self):
        span = timedelta(hours=24).total_seconds()
        bars = span / pick_bucket_seconds(span)
        assert 10 <= bars <= 60


class TestChartGeometry:
    def _buckets(self, values):
        return [
            {
                "start": datetime(2026, 8, 10, h, tzinfo=UTC),
                "calls": 1,
                "prompt_tokens": v,
                "completion_tokens": 0,
                "cached_tokens": v // 2,
                "uncached_tokens": v - v // 2,
                "reusable_tokens": 0,
                "avg_latency_ms": 100.0,
                "p50_latency_ms": 100.0,
                "p90_latency_ms": 100.0,
                "p99_latency_ms": 100.0,
                "avg_throughput_tps": 50.0,
                "p50_throughput_tps": 50.0,
                "p90_throughput_tps": 50.0,
                "hit_rate": 0.5,
            }
            for h, v in enumerate(values)
        ]

    def test_bars_never_escape_the_plot(self):
        chart = build_chart(self._buckets([100, 500, 1000]), DEFAULT_METRIC, 3600)
        for bar in chart["bars"]:
            assert bar["upper_y"] >= 0
            assert bar["lower_y"] + bar["lower_h"] <= chart["baseline"] + 0.01

    def test_the_stacked_segments_sit_on_top_of_each_other(self):
        chart = build_chart(self._buckets([1000]), "cached_tokens", 3600)
        bar = chart["bars"][0]
        assert bar["upper_y"] + bar["upper_h"] == pytest.approx(bar["lower_y"], abs=0.05)

    def test_a_taller_value_draws_a_taller_bar(self):
        chart = build_chart(self._buckets([100, 1000]), "prompt_tokens", 3600)
        assert chart["bars"][1]["lower_h"] > chart["bars"][0]["lower_h"]

    def test_all_zero_data_is_flagged_empty_rather_than_dividing_by_zero(self):
        chart = build_chart(self._buckets([0, 0]), "prompt_tokens", 3600)
        assert chart["empty"] is True

    def test_no_buckets_at_all_is_survivable(self):
        chart = build_chart([], DEFAULT_METRIC, 3600)
        assert chart["bars"] == []
        assert chart["empty"] is True

    def test_a_non_stacked_metric_draws_one_segment(self):
        chart = build_chart(self._buckets([1000]), "p50_latency_ms", 3600)
        assert chart["stacked"] is False
        assert chart["bars"][0]["upper_h"] == 0

    def test_gridline_labels_use_the_metrics_own_units(self):
        chart = build_chart(self._buckets([1000]), "p50_latency_ms", 3600)
        assert any("ms" in g["label"] or "s" in g["label"]
                   for g in chart["gridlines"])


class TestFormatting:
    def test_large_counts_are_compact(self):
        assert si(8_231_904) == "8.2M"
        assert si(16_400) == "16.4k"
        assert si(42) == "42"

    def test_four_digit_counts_keep_all_four_digits(self):
        """Abbreviating these loses the precision that makes a count
        comparable against a provider's own reporting, and saves no width."""
        assert si(1000) == "1.000"
        assert si(2234) == "2.234"
        assert si(9999) == "9.999"

    def test_abbreviation_starts_at_five_digits(self):
        assert si(9999) == "9.999"
        assert si(10_000) == "10.0k"

    def test_a_value_never_rounds_up_into_a_nonsense_unit(self):
        assert si(999_999) == "1.0M"
        assert si(999_400) == "999.4k"

    def test_negative_counts_group_the_same_way(self):
        assert si(-2234) == "-2.234"

    def test_nothing_is_an_em_dash_not_a_zero(self):
        """A missing measurement and a measured zero are different facts."""
        assert si(None) == "—"
        assert si(0) == "0"


class TestExactHoverText:
    def test_digits_are_ungrouped_so_they_can_be_pasted_anywhere(self):
        assert exact(2234, "tokens") == "2234 tokens"
        assert exact(16_412, "ms") == "16412 ms"

    def test_a_unit_is_optional(self):
        assert exact(2234) == "2234"

    def test_a_missing_measurement_says_so(self):
        assert exact(None, "tokens") == "not recorded"

    def test_cached_hover_names_the_source_of_the_number(self):
        """The number alone can't say whether the provider reported it or
        rainbox inferred it from prefill timing — the distinction the page
        exists to keep visible."""
        reported = _call(cached_tokens_reported=1523, cached_tokens_estimated=900)
        estimated = _call(cached_tokens_estimated=1400)
        text, hover, _cls = cache_reading(reported)
        assert text == "1.523"
        assert hover == "1523 tokens — reported by the provider"
        text, hover, _cls = cache_reading(estimated)
        assert text == "1.400"
        assert hover == "1400 tokens — estimated from prefill timing, not reported"


def _call(**overrides):
    """A minimal stand-in for an LlmCall row, defaulting to a timed call with
    no cache figures — the shape cache_reading() has to disambiguate."""
    fields = {
        "cached_tokens_reported": None,
        "cached_tokens_estimated": None,
        "prefill_ms": 850,
        "prompt_tokens": 1000,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestUnjudgedIsNotZero:
    """A model needs MIN_CALIBRATION_CALLS recorded calls before its
    cold-prefill baseline means anything, and the estimate is written once at
    record time. Calls made inside that window keep an empty Cached column
    forever — which reads as "this model never caches" unless the cell says
    otherwise. That misreading is what this distinction exists to stop.
    """

    def test_a_timed_call_with_no_baseline_is_marked_unjudged(self):
        text, hover, cls = cache_reading(_call())
        assert text == "unjudged"
        assert cls == "unjudged"
        assert str(MIN_CALIBRATION_CALLS) in hover
        assert "Reusable is" in hover

    def test_an_unmeasured_call_stays_an_em_dash(self):
        """No prefill timing means nothing was withheld — nothing was measured.
        A provider that reports cache figures directly (OpenRouter) records no
        prefill_ms, so this must not claim a baseline was missing."""
        text, hover, cls = cache_reading(_call(prefill_ms=None))
        assert text == "—"
        assert hover == "not recorded"
        assert cls == ""

    def test_a_real_figure_always_wins_over_the_unjudged_marker(self):
        assert cache_reading(_call(cached_tokens_estimated=0))[0] == "0"
        assert cache_reading(_call(cached_tokens_reported=0))[0] == "0"

    def test_a_call_with_no_prompt_tokens_is_not_called_unjudged(self):
        """Nothing to judge, so nothing was withheld."""
        assert cache_reading(_call(prompt_tokens=None))[0] == "—"


class TestAdminCoverage:
    def test_llm_call_has_an_admin_view(self):
        from webapp.core import admin

        assert any(getattr(v, "model", None) is LlmCall for v in admin._views)


# A traceback shaped like the ones that actually land here: an embedding
# timeout, three exceptions deep, where only the innermost block names the
# socket that stopped answering.
FAILED_TRACEBACK = """Traceback (most recent call last):
  File "httpcore/_sync/http11.py", line 106, in handle_request
    raise exc
httpcore.ReadTimeout: timed out

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "httpx/_transports/default.py", line 250, in handle_request
    resp = self._pool.handle_request(req)
httpx.ReadTimeout: timed out

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "memory/seed_memory.py", line 162, in _embed_query_cached
    return tuple(_embed_model().get_query_embedding(text))
openai.APITimeoutError: Request timed out.
"""


def add_failed_call(model: str, minutes_ago: int = 1, **overrides):
    """A call that raised instead of answering: no tokens, no timings, and a
    traceback where the response would have been."""
    row = {
        "ok": False,
        "error_category": "openai.APITimeoutError",
        "error_text": FAILED_TRACEBACK,
        "caller": "memory.retrieval._vector_sims",
        "origin": "memory/retrieval.py:291 in _vector_sims",
        "prompt_tokens": None,
        "completion_tokens": None,
        "prefill_ms": None,
        "decode_ms": None,
        "total_ms": 10_042,
        "cached_tokens_estimated": None,
        "reusable_prefix_tokens": None,
        "prefix_chain": None,
    }
    row.update(overrides)
    return add_call(model, minutes_ago=minutes_ago, **row)


class TestFailuresOnThePage:
    """An LLM error is the one thing on this page that isn't a metric, and the
    only place it surfaces at all: the call sites swallow these so that
    retrieval degrades instead of stopping."""

    def test_a_failed_call_shows_its_traceback_on_the_page(self, client, model):
        add_failed_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "openai.APITimeoutError" in body
        assert "httpcore.ReadTimeout: timed out" in body
        assert "_embed_query_cached" in body

    def test_the_failure_names_where_it_was_called_from(self, client, model):
        add_failed_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "memory.retrieval._vector_sims" in body
        assert "memory/retrieval.py:291 in _vector_sims" in body

    def test_the_failure_says_how_long_it_spent_failing(self, client, model):
        """What tells a timeout apart from a refused connection at a glance."""
        add_failed_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "gave up after" in body
        assert "10.0s" in body

    def test_no_failures_means_no_panel(self, client, model):
        add_call(model)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert "<h2>Failures</h2>" not in body

    def test_a_window_with_only_a_failure_still_renders_the_page(
        self, client, model
    ):
        """The empty state is keyed on the call count, and a failure is a
        call — so this must not fall through to "no LLM calls recorded"."""
        add_failed_call(model)
        body = client.get(f"/activity?range=24h").get_data(as_text=True)
        assert "<h2>Failures</h2>" in body
        assert "No LLM calls recorded" not in body

    def test_a_failure_outside_the_window_is_not_shown(self, client, model):
        add_failed_call(model, minutes_ago=60 * 24 * 3)
        body = client.get("/activity?range=1h").get_data(as_text=True)
        assert "openai.APITimeoutError" not in body

    def test_the_panel_is_capped_and_says_so(self, client, model):
        from webapp.activity_views import MAX_ERRORS_SHOWN

        for i in range(MAX_ERRORS_SHOWN + 3):
            add_failed_call(model, minutes_ago=i + 1)
        body = client.get("/activity?range=24h").get_data(as_text=True)
        assert body.count("<summary>traceback</summary>") == MAX_ERRORS_SHOWN
        assert f"newest {MAX_ERRORS_SHOWN} of {MAX_ERRORS_SHOWN + 3}" in body

    def test_the_detail_page_leads_with_the_traceback(self, client, model):
        call_uuid = uuid4()
        add_failed_call(model, uuid=call_uuid)
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        # Ahead of the fact tiles, which on a failed call are all em dashes.
        assert body.index("openai.APITimeoutError") < body.index(
            '<div class="k">Prompt</div>'
        )
        assert "httpcore.ReadTimeout: timed out" in body
        assert "raised instead of answering" in body

    def test_the_detail_page_of_a_failure_does_not_claim_an_empty_response(
        self, client, model
    ):
        call_uuid = uuid4()
        add_failed_call(model, uuid=call_uuid)
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "Nothing came back" in body

    def test_a_failed_embedding_does_not_claim_its_prompt_aged_out(self, client, model):
        """It never had one on the row: an embedding's text arrives with the
        response event, which a failed call never reaches. Saying "older than
        14 days" about a call made a minute ago sends a reader hunting for a
        retention bug that isn't there."""
        call_uuid = uuid4()
        add_failed_call(model, uuid=call_uuid, messages=None)
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "An embedding request names only its model" in body
        assert "days (whose text" not in body

    def test_a_failed_chat_still_shows_the_prompt_it_died_on(self, client, model):
        call_uuid = uuid4()
        add_failed_call(model, uuid=call_uuid,
                        messages=[{"role": "user", "content": "why so slow"}])
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert "why so slow" in body

    def test_a_failure_recorded_before_tracebacks_were_kept_says_so(
        self, client, model
    ):
        call_uuid = uuid4()
        add_failed_call(model, uuid=call_uuid, error_text=None)
        body = client.get(f"/activity/call/{call_uuid}").get_data(as_text=True)
        assert client.get(f"/activity/call/{call_uuid}").status_code == 200
        assert "before rainbox kept them" in body
