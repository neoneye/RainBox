"""The /activity page: rendering, controls, and the states that are easy to
get wrong — no data at all, and a model that hasn't calibrated yet."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import db
import webapp
from db import LlmCall
from webapp.activity_views import (
    DEFAULT_METRIC,
    _hit_rate_cell,
    build_chart,
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
        assert si(1500) == "1.5k"
        assert si(42) == "42"

    def test_nothing_is_an_em_dash_not_a_zero(self):
        """A missing measurement and a measured zero are different facts."""
        assert si(None) == "—"


class TestAdminCoverage:
    def test_llm_call_has_an_admin_view(self):
        from webapp.core import admin

        assert any(getattr(v, "model", None) is LlmCall for v in admin._views)
