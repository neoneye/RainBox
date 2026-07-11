"""Live streaming of a chat reply into two in-place chat rows.

`StreamingReplyWriter` owns a reasoning row (kind="thinking") and an answer row
(kind="message"). It is fed `(reasoning_delta, content_delta)` pairs as the
model streams; it lazily creates each row on that stream's first token, grows
the row text in place, and flushes (persist + NOTIFY) on a throttle so browsers
get a smooth live update without a write per token. `finish()` does a final
flush and flips both rows out of streaming state.

The writer is decoupled from the database: it takes `create`/`update` callables
(bound to db.post_chat_message / db.update_chat_message by the agent), so it can
be unit-tested with fakes and no live Postgres.

`extract_stream_deltas` pulls the reasoning vs answer deltas out of one streamed
ChatResponse, covering both the OpenAI-compat shape (reasoning_content/reasoning
+ content on raw.choices[0].delta) and the native Ollama shape (thinking_delta
in additional_kwargs + chunk.delta). Mirrors llm.stream_test_streaming.

`decode_byte_escape_runs` repairs a model-output artifact: byte-fallback
tokenizers (Gemma family and friends) sometimes emit rare glyphs as the literal
notation '<0xE2><0x96><0xA8>' instead of the character itself. The writer
applies it once at finish() — a run can be split across stream deltas, so
per-delta decoding would misfire on partial runs.
"""

import re
import time
from collections.abc import Callable
from typing import Any

_BYTE_ESCAPE = re.compile(r"<0x([0-9A-Fa-f]{2})>")
_BYTE_ESCAPE_RUN = re.compile(r"(?:<0x[0-9A-Fa-f]{2}>)+")


def decode_byte_escape_runs(text: str) -> str:
    """Collapse literal byte-fallback notation into the characters it spells:
    '<0xE2><0x96><0xA8>' becomes '▨'. Only runs that decode to valid UTF-8 are
    replaced; anything else stays verbatim, so prose that merely mentions a
    malformed escape is untouched."""
    def _decode(m: re.Match) -> str:
        raw = bytes(int(h, 16) for h in _BYTE_ESCAPE.findall(m.group(0)))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return m.group(0)
    return _BYTE_ESCAPE_RUN.sub(_decode, text)

# create(kind, streaming) -> new message id ; update(message_id, text, streaming)
CreateRow = Callable[[str, bool], int]
UpdateRow = Callable[[int, str, bool], None]


def extract_stream_deltas(chunk: Any) -> tuple[str, str]:
    """Return (reasoning_delta, content_delta) for one streamed ChatResponse.
    Either may be empty; both may be present in the same chunk."""
    raw = getattr(chunk, "raw", None)
    if raw is not None and getattr(raw, "choices", None):
        delta = raw.choices[0].delta
        content = getattr(delta, "content", None) or ""
        reasoning = (
            getattr(delta, "reasoning_content", None)
            or getattr(delta, "reasoning", None)
            or ""
        )
    else:
        content = getattr(chunk, "delta", None) or ""
        ak = getattr(chunk, "additional_kwargs", None) or {}
        reasoning = ak.get("thinking_delta") or ""
    return reasoning, content


class StreamingReplyWriter:
    """Accumulates streamed reasoning/answer text into two chat rows, flushing
    on a throttle. Each row is created on its first non-empty delta, so a reply
    with no reasoning never makes a thinking bubble (and vice versa)."""

    def __init__(
        self,
        *,
        create: CreateRow,
        update: UpdateRow,
        throttle_seconds: float = 0.15,
        throttle_chars: int = 40,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._create = create
        self._update = update
        self._throttle_seconds = throttle_seconds
        self._throttle_chars = throttle_chars
        self._clock = clock

        self.reasoning_id: int | None = None
        self.answer_id: int | None = None
        self._reasoning_text = ""
        self._answer_text = ""
        # Text last persisted to each row, so a flush only writes rows that grew.
        self._reasoning_flushed = ""
        self._answer_flushed = ""
        self._last_flush_at = clock()
        self._chars_since_flush = 0

    def add_reasoning(self, delta: str) -> None:
        if not delta:
            return
        if self.reasoning_id is None:
            self.reasoning_id = self._create("thinking", True)
        self._reasoning_text += delta
        self._chars_since_flush += len(delta)
        self._maybe_flush()

    def add_answer(self, delta: str) -> None:
        if not delta:
            return
        if self.answer_id is None:
            self.answer_id = self._create("message", True)
        self._answer_text += delta
        self._chars_since_flush += len(delta)
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        due = (
            self._chars_since_flush >= self._throttle_chars
            or (self._clock() - self._last_flush_at) >= self._throttle_seconds
        )
        if due:
            self._flush(streaming=True)

    def _flush(self, *, streaming: bool) -> None:
        if self.reasoning_id is not None and self._reasoning_text != self._reasoning_flushed:
            self._update(self.reasoning_id, self._reasoning_text, streaming)
            self._reasoning_flushed = self._reasoning_text
        if self.answer_id is not None and self._answer_text != self._answer_flushed:
            self._update(self.answer_id, self._answer_text, streaming)
            self._answer_flushed = self._answer_text
        self._last_flush_at = self._clock()
        self._chars_since_flush = 0

    def finish(self, *, final_answer: str | None = None) -> str:
        """Final flush: optionally override the answer text (e.g. when a model
        emitted the answer inside its reasoning and the agent extracted it),
        then flip both rows out of streaming state. Returns the final answer
        text (so the caller can journal it / detect an empty reply)."""
        if final_answer is not None and final_answer != self._answer_text:
            if self.answer_id is None and final_answer:
                self.answer_id = self._create("message", True)
            self._answer_text = final_answer
        # Settle-time repair: literal byte-fallback notation the model emitted
        # for rare glyphs becomes the real characters (may flash raw while
        # streaming; the final flush below persists the clean text).
        self._reasoning_text = decode_byte_escape_runs(self._reasoning_text)
        self._answer_text = decode_byte_escape_runs(self._answer_text)
        # A streaming flag is always written on the last update so the row is
        # marked complete even if its text didn't change since the last flush.
        if self.reasoning_id is not None:
            self._update(self.reasoning_id, self._reasoning_text, False)
            self._reasoning_flushed = self._reasoning_text
        if self.answer_id is not None:
            self._update(self.answer_id, self._answer_text, False)
            self._answer_flushed = self._answer_text
        return self._answer_text
