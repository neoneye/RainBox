"""The instrumentation handler that turns LlamaIndex chat events into
`llm_call` rows.

Driven with real event objects and a fake sink, so these tests exercise the
actual extraction paths — native-Ollama dicts and OpenAI-compatible response
objects — without a database, a provider, or a network.
"""

from types import SimpleNamespace

import pytest

from llama_index.core.base.llms.types import ChatResponse
from llama_index.core.llms import ChatMessage
from llama_index.core.instrumentation.events.llm import (
    LLMChatEndEvent,
    LLMChatStartEvent,
)

from llm.activity import (
    ActivityRecorder,
    call_origin,
    prompt_text,
    provider_for_base_url,
)
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

    def test_an_untagged_call_is_named_by_the_code_that_made_it(self):
        """Untagged used to mean "unknown", which lumped every unattributed
        subsystem into one useless bucket. It now falls back to the calling
        function — here, this very test."""
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        caller = rows[0]["caller"]
        assert caller != "unknown"
        assert caller.endswith("test_an_untagged_call_is_named_by_the_code_that_made_it")
        assert "test_activity_recorder.py:" in rows[0]["origin"]


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


class TestStoredText:
    """What the row carries for the /activity call detail view: the outgoing
    messages and the model's reply."""

    def test_the_row_carries_the_outgoing_messages_by_role(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event(messages=[
            ChatMessage(role="system", content="you are a calculator"),
            ChatMessage(role="user", content="2+2"),
        ]))
        recorder.handle(end_event())
        assert rows[0]["messages"] == [
            {"role": "system", "content": "you are a calculator"},
            {"role": "user", "content": "2+2"},
        ]

    def test_the_row_carries_the_response_text(self):
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["response_text"] == "hi"

    def test_messages_come_from_the_start_event_not_the_end(self):
        """The messages the provider was given are a Start-event fact; the End
        event's own `messages` are the wrapper's view and may differ."""
        recorder, rows = make_recorder()
        recorder.handle(start_event(messages=[
            ChatMessage(role="user", content="the real prompt")]))
        recorder.handle(end_event())
        assert rows[0]["messages"] == [
            {"role": "user", "content": "the real prompt"}]

    def test_the_stored_text_is_the_text_that_was_hashed(self):
        """The prefix chain and the stored copy must read the message list the
        same way, or the row's own bytes would not explain its own cache
        reading."""
        messages = [ChatMessage(role="system", content="a" * 100),
                    ChatMessage(role="user", content="b" * 100)]
        recorder, rows = make_recorder()
        recorder.handle(start_event(messages=messages))
        recorder.handle(end_event())
        rebuilt = "\n".join(
            f"<{m['role']}>{m['content']}" for m in rows[0]["messages"])
        assert rebuilt == prompt_text(messages)
        assert rows[0]["prefix_chain"] == prefix_chain(rebuilt)

    def test_an_empty_response_stores_no_text(self):
        """A call that streamed nothing gets NULL, not "" — the detail view
        says "no response text", which is true, rather than showing a blank
        block that looks like the model answered with silence."""
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(LLMChatEndEvent(
            span_id="span-1", messages=[],
            response=ChatResponse(
                message=ChatMessage(role="assistant", content=""),
                raw=OLLAMA_RAW_WARM),
        ))
        assert rows[0]["response_text"] is None

    def test_a_none_content_message_stores_an_empty_string(self):
        """A tool-call turn carries no content. It still occupies a position
        in the list, and dropping it would misalign the transcript."""
        recorder, rows = make_recorder()
        recorder.handle(start_event(messages=[
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="user", content="go on"),
        ]))
        recorder.handle(end_event())
        assert [m["content"] for m in rows[0]["messages"]] == ["", "go on"]


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


class TestCallOrigin:
    """Where a call came from, derived from the stack rather than from a tag.

    A tag has to be remembered at each call site; an origin cannot be
    forgotten. It is what turns "something made 200 calls" into "this function
    made 200 calls", which is the difference between noticing a problem and
    being able to go and fix it.
    """

    def test_the_first_application_frame_wins(self):
        caller, origin = call_origin(
            [
                _frame("llm.activity", "_on_start", 210),
                _frame("llama_index_instrumentation.dispatcher", "event", 147),
                _frame("llama_index.core.llms.callbacks", "wrapped_llm_chat", 161),
                _frame("benchmarks.story", "_take_turn", 412),
                _frame("benchmarks.runner", "_run", 88),
            ]
        )
        assert caller == "benchmarks.story._take_turn"
        assert origin == "benchmarks/story.py:412 in _take_turn"

    def test_library_frames_are_skipped_however_many_there_are(self):
        caller, _origin = call_origin(
            [
                _frame("llm.activity", "handle", 1),
                _frame("llama_index_instrumentation.dispatcher", "event", 2),
                _frame("llama_index.core.llms.callbacks", "wrapped", 3),
                _frame("openai._base_client", "post", 4),
                _frame("httpx._client", "send", 5),
                _frame("agents.query_handlers", "answer", 6),
            ]
        )
        assert caller == "agents.query_handlers.answer"

    def test_our_own_instrumentation_never_reports_itself(self):
        """Otherwise every row would trace back to the recorder."""
        caller, _origin = call_origin(
            [_frame("llm.activity", "_on_start", 1), _frame("llm", "prepare_llm", 2)]
        )
        assert caller != "llm.activity._on_start"

    def test_a_stack_of_nothing_but_libraries_yields_nothing(self):
        assert call_origin([_frame("llama_index.core", "x", 1)]) == (None, None)

    def test_an_empty_stack_is_not_a_crash(self):
        assert call_origin([]) == (None, None)

    def test_a_dunder_module_is_named_by_its_file(self):
        """A worker entry point runs as __main__, which names nothing."""
        caller, origin = call_origin(
            [_frame("__main__", "main", 42, filename="/x/benchmarks/worker.py")]
        )
        assert caller == "benchmarks.worker.main"
        assert "benchmarks/worker.py:42" in origin


def _frame(module, function, lineno, filename=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        module=module,
        function=function,
        lineno=lineno,
        filename=filename or ("/src/" + module.replace(".", "/") + ".py"),
    )


class TestOriginOnTheRow:
    def test_an_untagged_call_is_named_by_its_code_not_left_unknown(
        self, monkeypatch
    ):
        """"unknown" is the answer that makes the dashboard useless for
        debugging: it groups every untagged subsystem into one bucket."""
        import llm.activity as activity

        monkeypatch.setattr(
            activity,
            "call_origin",
            lambda: ("benchmarks.story._take_turn", "benchmarks/story.py:412 in x"),
        )
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["caller"] == "benchmarks.story._take_turn"
        assert rows[0]["origin"] == "benchmarks/story.py:412 in x"

    def test_an_explicit_tag_still_wins_over_the_derived_name(self, monkeypatch):
        """The tag is the curated label; the origin is the precise pointer.
        Both are kept, and the tag is what the page groups by."""
        import llm.activity as activity

        monkeypatch.setattr(
            activity, "call_origin", lambda: ("benchmarks.story._take_turn", "s.py:1")
        )
        recorder, rows = make_recorder()
        recorder.handle(start_event(tags={"caller": "benchmark.story_text"}))
        recorder.handle(end_event(tags={"caller": "benchmark.story_text"}))
        assert rows[0]["caller"] == "benchmark.story_text"
        assert rows[0]["origin"] == "s.py:1"

    def test_a_call_from_nowhere_recognisable_is_still_recorded(self, monkeypatch):
        import llm.activity as activity

        monkeypatch.setattr(activity, "call_origin", lambda: (None, None))
        recorder, rows = make_recorder()
        recorder.handle(start_event())
        recorder.handle(end_event())
        assert rows[0]["caller"] == "unknown"
        assert rows[0]["origin"] is None


class TestRecorderInstallation:
    """Where the recorder gets registered decides what /activity can see.

    It was registered in the webapp and agent-worker bootstraps only, so every
    LLM call made from a *child* process — the benchmark suites, the
    edit-document worker, the /model test button — went unrecorded and the
    dashboard silently under-reported. Registering at the one place every LLM
    is constructed closes that off, including for entry points not written
    yet.
    """

    @pytest.fixture(autouse=True)
    def fresh_dispatcher(self, monkeypatch):
        from llama_index.core.instrumentation import get_dispatcher

        import llm.activity as activity

        dispatcher = get_dispatcher()
        original = list(dispatcher.event_handlers)
        # Importing `webapp` anywhere in the session installs a recorder, so
        # clear them out rather than assuming a clean dispatcher — otherwise
        # this class passes or fails on test ordering.
        dispatcher.event_handlers[:] = [
            h for h in original if not isinstance(h, ActivityRecorder)
        ]
        monkeypatch.setattr(activity, "_installed", None)
        yield
        dispatcher.event_handlers[:] = original
        activity._installed = None

    def _prepared(self, monkeypatch):
        """Build an LLM without touching a provider or the network."""
        import providers

        class FakeProvider:
            id = "ollama"

            def base_url(self):
                return "http://localhost:11434"

            def ensure_loaded(self, model, context_window):
                pass

        monkeypatch.setattr(providers, "get", lambda _id: FakeProvider())
        import llm

        return llm.prepare_llm("ollama", "llama3.2:3b", {"context_window": 4096})

    def _recorders(self):
        from llama_index.core.instrumentation import get_dispatcher

        return [
            h
            for h in get_dispatcher().event_handlers
            if isinstance(h, ActivityRecorder)
        ]

    def test_building_an_llm_registers_the_recorder(self, monkeypatch):
        assert self._recorders() == []
        self._prepared(monkeypatch)
        assert len(self._recorders()) == 1

    def test_building_many_llms_does_not_stack_recorders(self, monkeypatch):
        """A benchmark builds a fresh LLM every turn. Registering per call
        would multiply every recorded row by the number of turns."""
        for _ in range(5):
            self._prepared(monkeypatch)
        assert len(self._recorders()) == 1


class TestProviderForBaseUrl:
    def test_ollamas_port_is_recognised(self):
        assert provider_for_base_url("http://localhost:11434") == "ollama"

    def test_the_host_alias_does_not_matter(self):
        assert provider_for_base_url("http://127.0.0.1:11434/v1") == "ollama"

    def test_an_unknown_endpoint_is_not_guessed(self):
        assert provider_for_base_url("http://example.com:9999") is None

    def test_no_url_is_not_a_crash(self):
        assert provider_for_base_url(None) is None


def test_a_tagged_call_records_the_run_it_belongs_to():
    """Without the linkage the assistant page cannot reach prefill/decode or
    cache reuse — the data that explains a slow call, which the step row has
    never carried."""
    from uuid import uuid4

    from llm.activity import _uuid_tag

    run_uuid = uuid4()
    assert _uuid_tag({"run_uuid": str(run_uuid)}, "run_uuid") == run_uuid


def test_an_untagged_or_malformed_call_records_no_run():
    """Every non-assistant call still records; the column is simply empty. A
    bad tag must never break the inference call it was riding on."""
    from llm.activity import _uuid_tag

    assert _uuid_tag({"caller": "benchmark.story"}, "run_uuid") is None
    assert _uuid_tag({"run_uuid": "not-a-uuid"}, "run_uuid") is None
    assert _uuid_tag(None, "run_uuid") is None


def test_the_agent_tags_its_calls_with_the_run():
    """The tag has to be set at the call site or the column stays empty."""
    from types import SimpleNamespace
    from uuid import uuid4

    from agents.base import StructuredLLMAgent

    agent = SimpleNamespace(_log_run_uuid=uuid4())
    tags = StructuredLLMAgent._instrument_tags(agent, "assistant.decide")

    assert tags["caller"] == "assistant.decide"
    assert tags["run_uuid"] == str(agent._log_run_uuid)

    # An agent that tracks no run tags only the caller.
    plain = StructuredLLMAgent._instrument_tags(SimpleNamespace(), "eval.x")
    assert plain == {"caller": "eval.x"}
