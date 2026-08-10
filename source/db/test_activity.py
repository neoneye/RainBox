"""Aggregation over `llm_call` — the queries behind every panel on /activity.

Each test tags its rows with a unique model name and cleans up after itself,
so the suite is safe to run against a database that already holds real calls.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import LlmCall


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def model(app_ctx) -> str:
    """A model name no other test or real call will collide with."""
    name = f"test-model-{uuid4().hex[:8]}"
    yield name
    db.db.session.query(LlmCall).filter(LlmCall.model == name).delete(
        synchronize_session=False
    )
    db.db.session.commit()


NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def add_call(model: str, **overrides) -> LlmCall:
    row = {
        "started_at": NOW,
        "finished_at": NOW,
        "provider": "ollama",
        "model": model,
        "caller": "test.caller",
        "ok": True,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prefill_ms": 500,
        "decode_ms": 1000,
        "total_ms": 1500,
        "prefix_chain": [],
    }
    row.update(overrides)
    return db.record_llm_call(row)


class TestRecordLlmCall:
    def test_a_row_is_written_and_readable(self, model):
        add_call(model)
        found = db.db.session.query(LlmCall).filter(LlmCall.model == model).all()
        assert len(found) == 1
        assert found[0].prompt_tokens == 1000

    def test_unknown_keys_are_refused_rather_than_silently_dropped(self, model):
        """A typo'd column name must fail loudly here, not quietly produce a
        dashboard that under-reports."""
        with pytest.raises(TypeError):
            add_call(model, promt_tokens=5)


class TestSessionIsolation:
    """Telemetry must not share a transaction with the work it observes.

    The recorder runs inside whatever call happens to be in flight. Sharing
    `db.session` meant a failed insert poisoned the caller's transaction, so
    the next unrelated query anywhere in the process died with a
    PendingRollbackError naming an llm_call INSERT — which is how a benchmark
    run reported a database error instead of a benchmark result.
    """

    def test_recording_does_not_commit_the_callers_pending_work(self, model):
        """The caller's half-finished transaction is not ours to commit."""
        pending = LlmCall(model=f"{model}-pending", caller="not-ready")
        db.db.session.add(pending)
        try:
            add_call(model)
            db.db.session.rollback()  # the caller changes its mind
            left = (
                db.db.session.query(LlmCall)
                .filter(LlmCall.model == f"{model}-pending")
                .count()
            )
            assert left == 0, "the recorder committed someone else's row"
        finally:
            db.db.session.query(LlmCall).filter(
                LlmCall.model == f"{model}-pending"
            ).delete(synchronize_session=False)
            db.db.session.commit()

    def test_recording_works_even_when_the_callers_transaction_is_broken(
        self, model
    ):
        """A caller that has already blown up should not also lose its
        telemetry — that is exactly the call worth having a record of."""
        try:
            db.db.session.execute(sa.text("select * from table_that_is_not_there"))
        except Exception:
            pass  # the session is now in a failed transaction
        add_call(model)
        db.db.session.rollback()
        assert (
            db.db.session.query(LlmCall).filter(LlmCall.model == model).count() == 1
        )

    def test_a_failed_recording_leaves_the_callers_session_usable(self, model):
        """The defect that made this fatal: one bad insert, and every later
        query anywhere in the process raised PendingRollbackError."""
        clash = uuid4()
        add_call(model, uuid=clash)
        with pytest.raises(Exception):  # same primary key twice
            add_call(model, uuid=clash)
        # The caller's session must be unharmed by our failure.
        assert db.db.session.query(LlmCall).filter(LlmCall.model == model).count() == 1


class TestRecentThroughputs:
    def test_throughput_is_tokens_per_second_of_prefill(self, model):
        add_call(model, prompt_tokens=2000, prefill_ms=1000,
                 reusable_prefix_tokens=0)
        assert db.recent_throughputs(model) == [(2000.0, 0.0)]

    def test_the_reuse_fraction_rides_along(self, model):
        """cold_rate needs it to tell a cold measurement from a warm one."""
        add_call(model, prompt_tokens=1000, prefill_ms=500,
                 reusable_prefix_tokens=900)
        assert db.recent_throughputs(model) == [(2000.0, 0.9)]

    def test_an_unmeasured_prefix_leaves_the_fraction_unknown(self, model):
        add_call(model, prompt_tokens=1000, prefill_ms=500,
                 reusable_prefix_tokens=None)
        assert db.recent_throughputs(model) == [(2000.0, None)]

    def test_rows_without_timing_are_skipped(self, model):
        """An OpenAI-compat provider reports no prefill duration at all, so
        those calls can be counted but can't inform a cold baseline."""
        add_call(model, prefill_ms=None)
        add_call(model, prompt_tokens=1000, prefill_ms=500,
                 reusable_prefix_tokens=0)
        assert db.recent_throughputs(model) == [(2000.0, 0.0)]

    def test_a_zero_prefill_is_skipped_rather_than_dividing_by_zero(self, model):
        add_call(model, prefill_ms=0)
        assert db.recent_throughputs(model) == []

    def test_only_the_most_recent_calls_count(self, model):
        """The baseline must track the machine as it is now — an old row from
        before a hardware or model change shouldn't anchor it forever."""
        for i in range(5):
            add_call(
                model,
                started_at=NOW - timedelta(minutes=i),
                prompt_tokens=1000,
                prefill_ms=1000 + i,
            )
        assert len(db.recent_throughputs(model, limit=3)) == 3

    def test_another_models_calls_are_not_borrowed(self, model):
        add_call(model)
        add_call(f"{model}-other", prompt_tokens=9999, prefill_ms=1)
        try:
            assert all(t < 5000 for t, _reuse in db.recent_throughputs(model))
        finally:
            db.db.session.query(LlmCall).filter(
                LlmCall.model == f"{model}-other"
            ).delete(synchronize_session=False)
            db.db.session.commit()


class TestRecentPrefixChains:
    def test_chains_come_back_newest_first(self, model):
        add_call(model, started_at=NOW - timedelta(minutes=2), prefix_chain=["old"])
        add_call(model, started_at=NOW, prefix_chain=["new"])
        assert db.recent_prefix_chains(model)[0] == ["new"]

    def test_empty_chains_are_not_offered_as_candidates(self, model):
        add_call(model, prefix_chain=[])
        assert db.recent_prefix_chains(model) == []

    def test_a_json_null_chain_does_not_abort_the_query(self, model):
        """The bug behind the benchmark failure. A call recorded with no chain
        stored the JSON scalar `null`, which is not SQL NULL — so it slipped
        past an IS NOT NULL filter and reached jsonb_array_length, which
        raises "cannot get array length of a scalar". That aborted the
        transaction, and everything downstream died with a
        PendingRollbackError naming an unrelated INSERT.
        """
        add_call(model, prefix_chain=["real"])
        db.db.session.execute(
            sa.text(
                "insert into llm_call (uuid, started_at, caller, ok, model,"
                " prefix_chain) values (:u, now(), 'x', true, :m, 'null'::jsonb)"
            ),
            {"u": uuid4(), "m": model},
        )
        db.db.session.commit()
        assert db.recent_prefix_chains(model) == [["real"]]

    def test_a_json_object_chain_is_ignored_too(self, model):
        """Anything that isn't an array is not a prefix chain."""
        add_call(model, prefix_chain=["real"])
        db.db.session.execute(
            sa.text(
                "insert into llm_call (uuid, started_at, caller, ok, model,"
                " prefix_chain) values (:u, now(), 'x', true, :m, '{}'::jsonb)"
            ),
            {"u": uuid4(), "m": model},
        )
        db.db.session.commit()
        assert db.recent_prefix_chains(model) == [["real"]]

    def test_a_missing_chain_is_stored_as_sql_null_not_json_null(self, model):
        """Fixing the reader is not enough — stop creating the bad rows."""
        add_call(model, prefix_chain=None)
        kind = db.db.session.execute(
            sa.text(
                "select jsonb_typeof(prefix_chain) from llm_call"
                " where model = :m"
            ),
            {"m": model},
        ).scalar()
        assert kind is None, f"stored JSON {kind!r} instead of SQL NULL"

    def test_the_window_is_small_because_the_runtime_holds_few_prefixes(self, model):
        for i in range(12):
            add_call(
                model,
                started_at=NOW - timedelta(minutes=i),
                prefix_chain=[f"h{i}"],
            )
        assert len(db.recent_prefix_chains(model, limit=8)) == 8


class TestSummary:
    def test_an_empty_window_reports_zeros_not_a_crash(self, model):
        summary = db.activity_summary(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
        assert summary["calls"] >= 0

    def test_the_hit_rate_is_token_weighted(self, model):
        """One big cached prompt and one small uncached one is a good day, not
        a 50% day. Weighting by calls would say otherwise."""
        add_call(model, prompt_tokens=9000, cached_tokens_estimated=9000)
        add_call(model, prompt_tokens=1000, cached_tokens_estimated=0)
        summary = db.activity_summary(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1), model=model
        )
        assert summary["cached_tokens"] == 9000
        assert summary["prompt_tokens"] == 10000
        assert summary["hit_rate"] == pytest.approx(0.9)

    def test_a_reported_number_beats_our_estimate(self, model):
        add_call(
            model,
            prompt_tokens=1000,
            cached_tokens_reported=800,
            cached_tokens_estimated=200,
        )
        summary = db.activity_summary(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1), model=model
        )
        assert summary["cached_tokens"] == 800

    def test_reusable_tokens_are_summed_alongside_cached_ones(self, model):
        add_call(
            model,
            prompt_tokens=1000,
            cached_tokens_estimated=100,
            reusable_prefix_tokens=900,
        )
        summary = db.activity_summary(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1), model=model
        )
        assert summary["reusable_tokens"] == 900
        assert summary["reusable_rate"] == pytest.approx(0.9)

    def test_calls_outside_the_window_are_excluded(self, model):
        add_call(model, started_at=NOW - timedelta(days=30))
        summary = db.activity_summary(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1), model=model
        )
        assert summary["calls"] == 0


class TestSeries:
    def test_calls_land_in_the_bucket_that_contains_them(self, model):
        add_call(model, started_at=NOW, prompt_tokens=1000,
                 cached_tokens_estimated=400)
        buckets = db.activity_series(
            NOW - timedelta(hours=1),
            NOW + timedelta(hours=1),
            bucket_seconds=3600,
            model=model,
        )
        filled = [b for b in buckets if b["prompt_tokens"]]
        assert len(filled) == 1
        assert filled[0]["cached_tokens"] == 400
        assert filled[0]["uncached_tokens"] == 600

    def test_empty_buckets_are_present_so_gaps_show_as_gaps(self, model):
        """A quiet Tuesday must render as an empty column, not vanish and let
        Monday sit next to Wednesday."""
        buckets = db.activity_series(
            NOW - timedelta(hours=3), NOW, bucket_seconds=3600, model=model
        )
        assert len(buckets) == 3
        assert all(b["prompt_tokens"] == 0 for b in buckets)

    def test_a_bucket_carries_the_latency_and_throughput_the_chart_needs(self, model):
        """The metric selector drives the chart, not just the tables, so a
        bucket has to answer for latency and throughput as well as tokens."""
        for ms in (100, 200, 300):
            add_call(model, total_ms=ms, completion_tokens=100, decode_ms=1000)
        bucket = next(
            b
            for b in db.activity_series(
                NOW - timedelta(hours=1),
                NOW + timedelta(hours=1),
                bucket_seconds=3600,
                model=model,
            )
            if b["calls"]
        )
        assert bucket["p50_latency_ms"] == pytest.approx(200, abs=1)
        assert bucket["avg_throughput_tps"] == pytest.approx(100, abs=1)
        assert bucket["hit_rate"] is not None

    def test_an_empty_bucket_has_no_latency_rather_than_a_zero(self, model):
        """Zero milliseconds would draw as a real, impossibly fast bar."""
        bucket = db.activity_series(
            NOW - timedelta(hours=1), NOW, bucket_seconds=3600, model=model
        )[0]
        assert bucket["p50_latency_ms"] is None
        assert bucket["hit_rate"] is None

    def test_separate_buckets_stay_separate(self, model):
        add_call(model, started_at=NOW - timedelta(hours=2, minutes=30))
        add_call(model, started_at=NOW - timedelta(minutes=30))
        buckets = db.activity_series(
            NOW - timedelta(hours=3), NOW, bucket_seconds=3600, model=model
        )
        assert [b["calls"] for b in buckets] == [1, 0, 1]


class TestRollup:
    def test_grouping_by_model(self, model):
        add_call(model, prompt_tokens=1000, cached_tokens_estimated=500)
        add_call(f"{model}-b", prompt_tokens=2000, cached_tokens_estimated=0)
        rows = db.activity_rollup(
            NOW - timedelta(hours=1), NOW + timedelta(hours=1), dimension="model"
        )
        by_key = {r["key"]: r for r in rows}
        try:
            assert by_key[model]["hit_rate"] == pytest.approx(0.5)
            assert by_key[f"{model}-b"]["hit_rate"] == pytest.approx(0.0)
        finally:
            db.db.session.query(LlmCall).filter(
                LlmCall.model == f"{model}-b"
            ).delete(synchronize_session=False)
            db.db.session.commit()

    def test_grouping_by_caller(self, model):
        add_call(model, caller="assistant.decide")
        add_call(model, caller="chat.reply")
        rows = db.activity_rollup(
            NOW - timedelta(hours=1),
            NOW + timedelta(hours=1),
            dimension="caller",
            model=model,
        )
        assert {r["key"] for r in rows} == {"assistant.decide", "chat.reply"}

    def test_latency_percentiles_are_reported(self, model):
        for ms in (100, 200, 300, 400, 500):
            add_call(model, total_ms=ms)
        row = next(
            r
            for r in db.activity_rollup(
                NOW - timedelta(hours=1),
                NOW + timedelta(hours=1),
                dimension="model",
                model=model,
            )
            if r["key"] == model
        )
        assert row["p50_latency_ms"] == pytest.approx(300, abs=1)
        assert row["p90_latency_ms"] >= row["p50_latency_ms"]

    def test_seconds_saved_sums_what_each_call_banked(self, model):
        """Each row records the prefill time it avoided, judged against its
        model's cold rate at the time. The rollup sums those rather than
        re-deriving them, so a saving already banked doesn't shift when the
        baseline moves."""
        add_call(model, saved_ms=2000)
        add_call(model, saved_ms=500)
        add_call(model, saved_ms=None)  # never judged; contributes nothing
        row = next(
            r
            for r in db.activity_rollup(
                NOW - timedelta(hours=1),
                NOW + timedelta(hours=1),
                dimension="model",
                model=model,
            )
            if r["key"] == model
        )
        assert row["seconds_saved"] == pytest.approx(2.5)

    def test_calls_with_no_cache_verdict_are_counted_separately(self, model):
        """A model still calibrating produces rows with no cache verdict at
        all. The page has to tell that apart from a genuine zero hit rate —
        "we don't know yet" and "the cache did nothing" look identical in the
        totals otherwise."""
        add_call(model, cached_tokens_estimated=None, cached_tokens_reported=None)
        add_call(model, cached_tokens_estimated=0)
        row = next(
            r
            for r in db.activity_rollup(
                NOW - timedelta(hours=1),
                NOW + timedelta(hours=1),
                dimension="model",
                model=model,
            )
            if r["key"] == model
        )
        assert row["calls"] == 2
        assert row["judged_calls"] == 1

    def test_an_unknown_dimension_is_refused(self, model):
        with pytest.raises(ValueError):
            db.activity_rollup(NOW, NOW, dimension="'; drop table llm_call--")


class TestRecentCalls:
    def test_newest_first(self, model):
        add_call(model, started_at=NOW - timedelta(minutes=5), caller="older")
        add_call(model, started_at=NOW, caller="newer")
        rows = db.recent_llm_calls(limit=50, model=model)
        assert rows[0].caller == "newer"

    def test_the_limit_is_honoured(self, model):
        for i in range(6):
            add_call(model, started_at=NOW - timedelta(minutes=i))
        assert len(db.recent_llm_calls(limit=3, model=model)) == 3


class TestMaybePrune:
    """The scheduler calls this about once a second; it must do real work at
    most once a day."""

    def test_the_first_call_prunes(self, model):
        db.reset_llm_call_prune_clock()
        assert db.maybe_prune_llm_calls(datetime.now(UTC)) is True

    def test_a_second_call_moments_later_does_not(self, model):
        db.reset_llm_call_prune_clock()
        now = datetime.now(UTC)
        db.maybe_prune_llm_calls(now)
        assert db.maybe_prune_llm_calls(now + timedelta(seconds=1)) is False

    def test_a_day_later_it_prunes_again(self, model):
        db.reset_llm_call_prune_clock()
        now = datetime.now(UTC)
        db.maybe_prune_llm_calls(now)
        assert db.maybe_prune_llm_calls(now + timedelta(days=1, seconds=1)) is True

    def test_a_failure_is_swallowed_so_the_scheduler_keeps_running(
        self, model, monkeypatch
    ):
        """Housekeeping must never be able to wedge the cron loop."""
        db.reset_llm_call_prune_clock()
        monkeypatch.setattr(
            db.activity, "prune_llm_calls", lambda **_: (_ for _ in ()).throw(
                RuntimeError("db gone")
            )
        )
        assert db.maybe_prune_llm_calls(datetime.now(UTC)) is False


class TestPrune:
    def test_old_rows_go_and_recent_ones_stay(self, model):
        add_call(model, started_at=datetime.now(UTC) - timedelta(days=200))
        add_call(model, started_at=datetime.now(UTC))
        deleted = db.prune_llm_calls(older_than_days=90)
        assert deleted >= 1
        left = db.db.session.query(LlmCall).filter(LlmCall.model == model).all()
        assert len(left) == 1
