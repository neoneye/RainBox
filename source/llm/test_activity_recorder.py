"""The instrumentation handler that turns LlamaIndex chat events into
`llm_call` rows.

Driven with real event objects and a fake sink, so these tests exercise the
actual extraction paths — native-Ollama dicts and OpenAI-compatible response
objects — without a database, a provider, or a network.
"""

from types import SimpleNamespace

from llama_index.core.base.llms.types import ChatResponse
from llama_index.core.llms import ChatMessage
from llama_index.core.instrumentation.events.llm import (
    LLMChatEndEvent,
    LLMChatStartEvent,
)

from llm.activity import ActivityRecorder, prompt_text, provider_for_base_url
from llm.activity_metrics import MIN_CALIBRATION_CALLS, prefix_chain


class FakeHistory:
    """Stands in for the per-model lookups the recorder makes against
    `llm_call`."""

    def __init__(self, throughputs=None, chains=None):
        self._throughputs = throughputs or []
        self._chains = chains or []
        self.asked_for = []

    def recent_throughputs(self, model):
        self.asked_for.append(model)
        return list(self._throughputs)

    def recent_prefix_chains(self, model):
        return list(self._chains)


def make_recorder(history=None):
    rows = []
    recorder = ActivityRecorder(
        sink=rows.append, history=history or FakeHistory()
    )
    return recorder, rows


OLLAMA_MODEL_DICT = {
    "model": "llama3.2:3b",
    "base_url": "http://localhost:11434",
    "class_name": "Ollama_llm",
}

# Shaped exactly like the native wrapper's response.raw, verified against a
# live Ollama: durations are nanoseconds.
OLLAMA_RAW_WARM = {
    "model": "llama3.2:3b",
    "done": True,
    "prompt_eval_count": 4032,
    "prompt_eval_duration": 49_000_000,
    "eval_count": 120,
    "eval_duration": 900_000_000,
    "total_duration": 1_000_000_000,
}


def start_event(span="span-1", messages=None, model_dict=None, tags=None):
    return LLMChatStartEvent(
        span_id=span,
        tags=tags or {},
        messages=messages or [ChatMessage(role="user", content="hello")],
        model_dict=model_dict if model_dict is not None else OLLAMA_MODEL_DICT,
        additional_kwargs={},
    )


def end_event(span="span-1", raw=None, tags=None):
    return LLMChatEndEvent(
        span_id=span,
        tags=tags or {},
        messages=[ChatMessage(role="user", content="hello")],
        response=ChatResponse(
            message=ChatMessage(role="assistant", content="hi"),
            raw=OLLAMA_RAW_WARM if raw is None else raw,
        ),
    )


class TestNativeOllamaExtraction:
    def test_one_end_event_records_one_row(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert len(rows) == 1

    def test_token_counts_and_durations_come_off_the_raw_dict(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        row = rows[0]
        assert row["prompt_tokens"] == 4032
        assert row["completion_tokens"] == 120
        # Ollama reports nanoseconds; rows are milliseconds.
        assert row["prefill_ms"] == 49
        assert row["decode_ms"] == 900

    def test_ollama_reports_no_cache_field(self):
        """Not an oversight in the extraction — the field genuinely does not
        exist on this provider, which is why the estimate exists at all."""
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["cached_tokens_reported"] is None

    def test_the_model_and_provider_are_identified(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["model"] == "llama3.2:3b"
        assert rows[0]["provider"] == "ollama"


class TestOpenAICompatExtraction:
    def test_usage_is_read_off_a_choices_style_response(self):
        raw = SimpleNamespace(
            choices=[SimpleNamespace(index=0)],
            model="qwen3.5:9b",
            usage=SimpleNamespace(
                prompt_tokens=800, completion_tokens=64, total_tokens=864
            ),
        )
        recorder, rows = make_recorder()
        recorder.handle(start_event(model_dict={"model": "qwen3.5:9b",
                                                "base_url": "http://127.0.0.1:1337"}))
        recorder.handle(end_event(raw=raw))
        assert rows[0]["prompt_tokens"] == 800
        assert rows[0]["completion_tokens"] == 64
        assert rows[0]["provider"] == "jan"

    def test_a_provider_that_does_report_caching_is_believed(self):
        """DeepSeek-style fields. No local backend sends these today, but when
        one does the reported number must win over our estimate."""
        raw = SimpleNamespace(
            choices=[SimpleNamespace(index=0)],
            model="deepseek-chat",
            usage=SimpleNamespace(
                prompt_tokens=1631,
                completion_tokens=59,
                prompt_cache_hit_tokens=1600,
                prompt_cache_miss_tokens=31,
            ),
        )
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event(raw=raw))
        assert rows[0]["cached_tokens_reported"] == 1600

    def test_nested_cached_tokens_details_are_found(self):
        raw = SimpleNamespace(
            choices=[SimpleNamespace(index=0)],
            model="gpt-x",
            usage=SimpleNamespace(
                prompt_tokens=2000,
                completion_tokens=10,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1536),
            ),
        )
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event(raw=raw))
        assert rows[0]["cached_tokens_reported"] == 1536


class TestDoubleCountGuard:
    def test_the_structured_wrapper_reconstruction_is_ignored(self):
        """A structured-output call fires a second end event whose `raw` is
        the parsed pydantic object, not a provider payload. Counting it would
        double every structured call — which is most of rainbox."""

        class ParsedModel:
            answer = "42"

        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event(raw=ParsedModel()))
        assert rows == []

    def test_a_none_raw_is_ignored(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event(raw=None))
        # raw=None means the default warm dict; pass an explicit None instead.
        rows.clear()
        recorder.handle(
            LLMChatEndEvent(
                span_id="span-2",
                messages=[],
                response=ChatResponse(
                    message=ChatMessage(role="assistant", content=""), raw=None
                ),
            )
        )
        assert rows == []


class TestCallerAttribution:
    def test_the_instrument_tag_lands_on_the_row(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event(tags={"caller": "assistant.decide"}))
        recorder.handle(end_event(tags={"caller": "assistant.decide"}))
        assert rows[0]["caller"] == "assistant.decide"

    def test_an_untagged_call_is_recorded_as_unknown(self):
        """Call sites get tagged incrementally; an untagged one must still be
        counted, just not attributed."""
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["caller"] == "unknown"


class TestPairing:
    def test_wall_clock_spans_start_to_end(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["total_ms"] >= 0
        assert rows[0]["started_at"] is not None
        assert rows[0]["finished_at"] is not None

    def test_an_end_without_its_start_falls_back_to_the_provider_clock(self):
        """A handler registered mid-flight, or a process that restarted
        between the two events. With no wall clock of our own, Ollama's
        `total_duration` is a better answer than nothing."""
        recorder, rows = make_recorder()
        recorder.handle(end_event(span="orphan"))
        assert len(rows) == 1
        assert rows[0]["total_ms"] == 1000  # 1_000_000_000 ns
        assert rows[0]["started_at"] is None

    def test_pending_starts_do_not_accumulate_forever(self):
        recorder, _ = make_recorder()
        for i in range(recorder.max_pending + 50):
            recorder.handle(start_event(span=f"span-{i}"))
        assert len(recorder.pending) <= recorder.max_pending


class TestCacheMetrics:
    def test_the_estimate_is_withheld_while_a_model_is_calibrating(self):
        recorder, rows = make_recorder(FakeHistory(throughputs=[1000.0] * 3))
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["cached_tokens_estimated"] is None

    def test_a_warm_call_against_a_cold_baseline_reads_as_cached(self):
        # Samples are (throughput, reusable fraction); a zero fraction marks
        # a call that had nothing to reuse and so measured the cold rate.
        history = FakeHistory(
            throughputs=[(1926.0, 0.0)] * MIN_CALIBRATION_CALLS
        )
        recorder, rows = make_recorder(history)
        recorder.handle(start_event())
        recorder.handle(end_event())  # 4032 tokens in 49 ms
        assert rows[0]["cached_tokens_estimated"] > 3900

    def test_the_time_saved_is_banked_on_the_row(self):
        """Rollups sum this rather than re-deriving it, so it has to be
        written at judgement time."""
        history = FakeHistory(
            throughputs=[(1926.0, 0.0)] * MIN_CALIBRATION_CALLS
        )
        recorder, rows = make_recorder(history)
        recorder.handle(start_event())
        recorder.handle(end_event())
        # ~4000 cached tokens at ~1926 tok/s is a bit over two seconds.
        assert 1800 <= rows[0]["saved_ms"] <= 2300

    def test_an_unjudged_call_banks_no_saving(self):
        recorder, rows = make_recorder(FakeHistory(throughputs=[]))
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["cached_tokens_estimated"] is None
        assert rows[0]["saved_ms"] is None

    def test_reusable_prefix_is_measured_against_recent_chains(self):
        prompt = "shared preamble " * 500
        message = ChatMessage(role="user", content=prompt)
        # Chains are hashed over the role-flattened prompt, which is what
        # earlier calls stored too — comparing raw content would never match.
        history = FakeHistory(chains=[prefix_chain(prompt_text([message]))])
        recorder, rows = make_recorder(history)
        recorder.handle(start_event(messages=[message]))
        recorder.handle(end_event())
        assert rows[0]["reusable_prefix_tokens"] > 0

    def test_a_prompt_nobody_has_seen_is_not_reusable(self):
        history = FakeHistory(chains=[prefix_chain("something else " * 500)])
        recorder, rows = make_recorder(history)
        recorder.handle(
            start_event(messages=[ChatMessage(role="user", content="fresh " * 500)])
        )
        recorder.handle(end_event())
        assert rows[0]["reusable_prefix_tokens"] == 0

    def test_the_prefix_chain_is_stored_for_later_calls_to_match_against(self):
        recorder, rows = make_recorder()
        recorder.handle(
            start_event(messages=[ChatMessage(role="user", content="x" * 5000)])
        )
        recorder.handle(end_event())
        # 5000 chars of content plus the "<user>" role marker the flattener
        # prepends — six 1000-char blocks, the last one barely started.
        assert len(rows[0]["prefix_chain"]) == 6


class TestRobustness:
    def test_a_malformed_event_records_nothing_and_raises_nothing(self):
        """Telemetry must never be able to break an LLM call."""
        recorder, rows = make_recorder()
        recorder.handle(SimpleNamespace(nonsense=True))
        assert rows == []

    def test_a_failing_sink_is_swallowed(self):
        def explode(_row):
            raise RuntimeError("database is on fire")

        recorder = ActivityRecorder(sink=explode, history=FakeHistory())
        recorder.handle(start_event())
        recorder.handle(end_event())  # must not raise

    def test_a_failing_history_lookup_still_records_the_call(self):
        class BrokenHistory:
            def recent_throughputs(self, model):
                raise RuntimeError("no db")

            def recent_prefix_chains(self, model):
                raise RuntimeError("no db")

        recorder, rows = make_recorder(BrokenHistory())
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert len(rows) == 1
        assert rows[0]["prompt_tokens"] == 4032
        assert rows[0]["cached_tokens_estimated"] is None


class TestProviderForBaseUrl:
    def test_ollamas_port_is_recognised(self):
        assert provider_for_base_url("http://localhost:11434") == "ollama"

    def test_the_host_alias_does_not_matter(self):
        assert provider_for_base_url("http://127.0.0.1:11434/v1") == "ollama"

    def test_an_unknown_endpoint_is_not_guessed(self):
        assert provider_for_base_url("http://example.com:9999") is None

    def test_no_url_is_not_a_crash(self):
        assert provider_for_base_url(None) is None
