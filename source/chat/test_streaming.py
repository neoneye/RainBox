"""Unit tests for chat_streaming — the StreamingReplyWriter and the per-chunk
delta extractor. No database or LM Studio: the writer takes fake create/update
callables and a fake clock, and the extractor is fed synthetic chunk objects."""

from types import SimpleNamespace

from chat.streaming import StreamingReplyWriter, extract_stream_deltas


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _writer(clock=None, **kw):
    """Build a writer over recording fakes. Returns (writer, created, updates).
    `created` is the list of (kind) in creation order; `updates` is the list of
    (message_id, text, streaming) for every flush write."""
    created = []
    updates = []
    next_id = [0]

    def create(kind, streaming):
        next_id[0] += 1
        created.append((next_id[0], kind, streaming))
        return next_id[0]

    def update(mid, text, streaming):
        updates.append((mid, text, streaming))

    w = StreamingReplyWriter(
        create=create, update=update, clock=clock or FakeClock(), **kw
    )
    return w, created, updates


def test_rows_created_lazily_per_stream():
    clock = FakeClock()
    w, created, _ = _writer(clock=clock)
    # No tokens yet -> no rows.
    assert created == []
    w.add_reasoning("think")
    assert [c[1] for c in created] == ["thinking"]
    w.add_answer("hi")
    assert [c[1] for c in created] == ["thinking", "message"]


def test_no_reasoning_means_no_thinking_row():
    w, created, _ = _writer()
    w.add_answer("just an answer")
    w.finish()
    assert [c[1] for c in created] == ["message"]
    assert w.reasoning_id is None


def test_throttle_batches_until_due_then_final_flush():
    clock = FakeClock()
    # Big thresholds so nothing flushes mid-stream until finish().
    w, _created, updates = _writer(clock=clock, throttle_seconds=10.0, throttle_chars=10_000)
    w.add_answer("a")
    w.add_answer("b")
    assert updates == []  # not due yet
    final = w.finish()
    assert final == "ab"
    # The final flush writes the completed text with streaming=False.
    assert updates[-1] == (w.answer_id, "ab", False)


def test_char_threshold_triggers_intermediate_flush():
    clock = FakeClock()
    w, _c, updates = _writer(clock=clock, throttle_seconds=10.0, throttle_chars=3)
    w.add_answer("abcd")  # 4 >= 3 -> flush
    assert updates == [(w.answer_id, "abcd", True)]


def test_reasoning_and_answer_route_to_separate_rows():
    clock = FakeClock()
    w, _c, updates = _writer(clock=clock, throttle_seconds=10.0, throttle_chars=10_000)
    w.add_reasoning("R1 ")
    w.add_reasoning("R2")
    w.add_answer("A1")
    w.finish()
    rid, aid = w.reasoning_id, w.answer_id
    finals = {mid: text for (mid, text, streaming) in updates if streaming is False}
    assert finals[rid] == "R1 R2"
    assert finals[aid] == "A1"


def test_finish_can_inject_answer_extracted_from_reasoning():
    """When the model emitted no content stream, the agent passes the answer it
    recovered from the reasoning; the writer creates the answer row then."""
    w, created, updates = _writer()
    w.add_reasoning("...thinking... </think> the answer")
    final = w.finish(final_answer="the answer")
    assert final == "the answer"
    assert [c[1] for c in created] == ["thinking", "message"]
    assert (w.answer_id, "the answer", False) in updates


def test_extract_stream_deltas_openai_shape():
    delta = SimpleNamespace(content="ans", reasoning_content="why")
    chunk = SimpleNamespace(raw=SimpleNamespace(choices=[SimpleNamespace(delta=delta)]))
    assert extract_stream_deltas(chunk) == ("why", "ans")


def test_extract_stream_deltas_openai_reasoning_alias():
    # Some providers name the field `reasoning` rather than `reasoning_content`.
    delta = SimpleNamespace(content="", reasoning="think")
    chunk = SimpleNamespace(raw=SimpleNamespace(choices=[SimpleNamespace(delta=delta)]))
    assert extract_stream_deltas(chunk) == ("think", "")


def test_extract_stream_deltas_ollama_shape():
    # No raw.choices -> native shape: content on .delta, reasoning in kwargs.
    chunk = SimpleNamespace(raw=None, delta="ans", additional_kwargs={"thinking_delta": "why"})
    assert extract_stream_deltas(chunk) == ("why", "ans")


def test_decode_byte_escape_runs():
    from chat.streaming import decode_byte_escape_runs
    # A run spelling one glyph, and a run spelling several in sequence.
    assert decode_byte_escape_runs("<0xE2><0x96><0xA8>") == "▨"
    assert decode_byte_escape_runs(
        "a <0xE2><0x97><0x93> b <0xE2><0x97><0x92> c") == "a ◓ b ◒ c"
    # Multi-glyph run: two characters back to back in one run.
    assert decode_byte_escape_runs(
        "<0xE2><0x96><0xA8><0xE2><0x96><0xA7>") == "▨▧"
    # Invalid UTF-8 stays verbatim (a lone continuation byte).
    assert decode_byte_escape_runs("x <0x96> y") == "x <0x96> y"
    # Non-matching notation is untouched.
    assert decode_byte_escape_runs("0xE2 <0xZZ> plain") == "0xE2 <0xZZ> plain"
    assert decode_byte_escape_runs("no escapes at all") == "no escapes at all"


def test_finish_decodes_byte_escapes_split_across_deltas():
    w, _created, updates = _writer()
    # The run arrives split across deltas, as a byte-fallback stream would.
    w.add_answer("frame: <0xE2>")
    w.add_answer("<0x96><0xA8> done")
    w.add_reasoning("emit <0xE2><0x97><0x93> now")
    final = w.finish()
    assert final == "frame: ▨ done"
    # The last persisted write of each row carries the decoded text.
    last_by_id = {mid: text for mid, text, _s in updates}
    assert last_by_id[w.answer_id] == "frame: ▨ done"
    assert last_by_id[w.reasoning_id] == "emit ◓ now"
