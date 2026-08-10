"""Always-on instrumentation that records every LLM call to `llm_call`.

One dispatcher handler, registered once at startup, sees every chat that
passes through LlamaIndex — assistant, chat, cron, kanban, benchmarks, evals
— so no call site has to remember to report anything. The same trick as
`capture_reasoning`'s `_ReasoningTally`, but global and permanent rather than
scoped to a `with` block.

The handler is observational and defensive in equal measure: nothing reads
these rows back into a decision, and every failure path swallows its
exception. A telemetry bug must never be able to break an inference call.

Caller attribution rides in on `instrument_tags({"caller": ...})` at the call
site; anything untagged records as "unknown" so coverage can grow gradually
without leaving holes in the totals.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable

from llama_index.core.instrumentation.event_handlers import BaseEventHandler
from llama_index.core.instrumentation.events.llm import (
    LLMChatEndEvent,
    LLMChatStartEvent,
)
from pydantic import Field, PrivateAttr

import providers
from llm.activity_metrics import (
    cached_tokens_estimate,
    cold_rate,
    prefix_chain,
    reusable_prefix_tokens,
)

logger = logging.getLogger(__name__)

# Ollama reports durations in nanoseconds; rows store milliseconds.
_NS_PER_MS = 1_000_000

# Start events whose End never arrived (a crash, a timeout, a killed worker)
# would otherwise pin their prompt chains in memory forever. The cap is far
# above any plausible number of genuinely concurrent calls on one box.
_MAX_PENDING = 256


def provider_for_base_url(base_url: str | None) -> str | None:
    """Which registered provider serves `base_url`, by host:port.

    Matching on the endpoint rather than the client class keeps this correct
    for both the native Ollama wrapper and the OpenAI-compat clients, which
    are the same Python class for Jan and LM Studio and so cannot be told
    apart any other way.
    """
    if not base_url:
        return None
    target = _host_port(base_url)
    if target is None:
        return None
    for provider in providers.all_providers():
        try:
            if _host_port(provider.base_url()) == target:
                return provider.id
        except Exception:  # a provider that can't state its own URL
            continue
    return None


def _host_port(url: str) -> tuple[str, int | None] | None:
    """The comparable part of a URL: port, and host with loopback aliases
    folded together so 127.0.0.1 and localhost aren't treated as different
    backends."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url if "//" in url else f"//{url}")
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host in ("127.0.0.1", "::1", "0.0.0.0"):
        host = "localhost"
    return host, parts.port


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _get(obj: Any, name: str) -> Any:
    """Read `name` off a dict or an object, whichever the provider returned."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def is_provider_response(raw: Any) -> bool:
    """Whether `raw` is a real provider payload rather than the structured /
    tool wrapper's reconstruction.

    Those wrappers fire a second end event carrying the parsed pydantic object
    in `raw`. Counting it would double every structured call, which is most of
    what rainbox does. Same guard as `_ReasoningTally` uses.
    """
    return isinstance(raw, dict) or hasattr(raw, "choices")


def extract_usage(raw: Any) -> dict[str, int | None]:
    """Token counts, prefill/decode times and any provider-reported cache
    number, from either response shape.

    Native Ollama hands back a flat dict with nanosecond durations; the
    OpenAI-compatible clients hand back an object with `.usage` and no timing
    at all — which is why a Jan or LM Studio call can be counted but not
    (yet) judged for cache behaviour.
    """
    out: dict[str, int | None] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "prefill_ms": None,
        "decode_ms": None,
        "total_ms_provider": None,
        "cached_tokens_reported": None,
    }
    if isinstance(raw, dict) and "prompt_eval_count" in raw:
        out["prompt_tokens"] = _int_or_none(raw.get("prompt_eval_count"))
        out["completion_tokens"] = _int_or_none(raw.get("eval_count"))
        prefill_ns = _int_or_none(raw.get("prompt_eval_duration"))
        decode_ns = _int_or_none(raw.get("eval_duration"))
        total_ns = _int_or_none(raw.get("total_duration"))
        out["prefill_ms"] = None if prefill_ns is None else prefill_ns // _NS_PER_MS
        out["decode_ms"] = None if decode_ns is None else decode_ns // _NS_PER_MS
        out["total_ms_provider"] = None if total_ns is None else total_ns // _NS_PER_MS

    usage = _get(raw, "usage")
    if usage is not None:
        if out["prompt_tokens"] is None:
            out["prompt_tokens"] = _int_or_none(_get(usage, "prompt_tokens"))
        if out["completion_tokens"] is None:
            out["completion_tokens"] = _int_or_none(_get(usage, "completion_tokens"))
        out["cached_tokens_reported"] = _extract_reported_cache(usage)
    return out


def _extract_reported_cache(usage: Any) -> int | None:
    """A provider's own count of cached prompt tokens, under whichever name it
    uses. None on every local backend today — Ollama's `prompt_eval_count` is
    identical on a hit and a miss, so there is nothing here to read."""
    direct = _int_or_none(_get(usage, "prompt_cache_hit_tokens"))
    if direct is not None:
        return direct
    details = _get(usage, "prompt_tokens_details")
    if details is not None:
        nested = _int_or_none(_get(details, "cached_tokens"))
        if nested is not None:
            return nested
    return _int_or_none(_get(usage, "cached_tokens"))


def prompt_text(messages: Any) -> str:
    """The outgoing prompt flattened to text, for prefix hashing.

    Roles are interleaved so that a change of speaker can't look like an
    unchanged prefix, and blocks line up the way the runtime's own template
    would lay them out.
    """
    parts: list[str] = []
    for message in messages or []:
        role = getattr(getattr(message, "role", None), "value", None) or str(
            getattr(message, "role", "")
        )
        content = getattr(message, "content", None)
        parts.append(f"<{role}>{content if content is not None else ''}")
    return "\n".join(parts)


class ActivityRecorder(BaseEventHandler):
    """Turns paired chat Start/End events into `llm_call` rows.

    `sink` takes the finished row dict and `history` answers the two per-model
    questions the cache metrics need. Both are injected rather than imported
    so the handler can be exercised without a database.
    """

    model_config = {"arbitrary_types_allowed": True}

    sink: Callable[[dict], None]
    history: Any
    max_pending: int = Field(default=_MAX_PENDING)
    _pending: dict[str, dict] = PrivateAttr(default_factory=dict)

    @classmethod
    def class_name(cls) -> str:
        return "ActivityRecorder"

    @property
    def pending(self) -> dict[str, dict]:
        return self._pending

    def handle(self, event: Any, **kwargs: Any) -> Any:
        try:
            if isinstance(event, LLMChatStartEvent):
                self._on_start(event)
            elif isinstance(event, LLMChatEndEvent):
                self._on_end(event)
        except Exception:
            # Never propagate: this handler runs inside the caller's LLM call.
            logger.debug("activity recorder failed on %r", event, exc_info=True)

    def _on_start(self, event: LLMChatStartEvent) -> None:
        model_dict = event.model_dict or {}
        text = prompt_text(event.messages)
        # Captured here, on the Start event, because this handler runs
        # synchronously inside the caller's own stack — by the End event the
        # frames that made the call may be long gone.
        derived_caller, origin = call_origin()
        self._pending[str(event.span_id)] = {
            "started_at": datetime.now(UTC),
            "model": model_dict.get("model"),
            "provider": provider_for_base_url(model_dict.get("base_url")),
            "caller": _caller_from(event.tags, derived_caller),
            "origin": origin,
            "prefix_chain": prefix_chain(text),
            "prompt_chars": len(text),
        }
        if len(self._pending) > self.max_pending:
            self._drop_oldest_pending()

    def _drop_oldest_pending(self) -> None:
        """Forget the least recent unmatched start. Its End is never coming —
        the call crashed, timed out, or the worker died mid-stream."""
        oldest = min(self._pending, key=lambda k: self._pending[k]["started_at"])
        self._pending.pop(oldest, None)

    def _on_end(self, event: LLMChatEndEvent) -> None:
        raw = getattr(event.response, "raw", None)
        if not is_provider_response(raw):
            return
        start = self._pending.pop(str(event.span_id), None) or {}
        usage = extract_usage(raw)
        model = start.get("model") or _get(raw, "model")

        row: dict[str, Any] = {
            "started_at": start.get("started_at"),
            "finished_at": datetime.now(UTC),
            "provider": start.get("provider"),
            "model": model,
            "caller": start.get("caller") or _caller_from(event.tags),
            "origin": start.get("origin"),
            "ok": True,
            "error_category": None,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "prefill_ms": usage["prefill_ms"],
            "decode_ms": usage["decode_ms"],
            "total_ms": _elapsed_ms(start.get("started_at"))
            if start
            else usage["total_ms_provider"],
            "cached_tokens_reported": usage["cached_tokens_reported"],
            "cached_tokens_estimated": None,
            "reusable_prefix_tokens": None,
            "saved_ms": None,
            "prefix_chain": start.get("prefix_chain"),
        }
        self._add_cache_metrics(row, start, model)
        try:
            self.sink(row)
        except Exception:
            logger.debug("activity sink rejected a row", exc_info=True)

    def _add_cache_metrics(self, row: dict, start: dict, model: Any) -> None:
        """Fill in the two derived cache columns, leaving them None if the
        history lookups fail — a call with unknown cache behaviour is still
        worth recording."""
        try:
            rate = cold_rate(self.history.recent_throughputs(model))
            row["cached_tokens_estimated"] = cached_tokens_estimate(
                row["prompt_tokens"], row["prefill_ms"], rate
            )
            # Bank the saving now, against the baseline as it stands. Summing
            # a stored figure keeps every rollup consistent with the estimate
            # the row was judged by, however the baseline drifts later.
            cached = row["cached_tokens_reported"]
            if cached is None:
                cached = row["cached_tokens_estimated"]
            if cached and rate:
                row["saved_ms"] = int(round(cached / rate * 1000))
        except Exception:
            logger.debug("cold-rate lookup failed for %r", model, exc_info=True)
        try:
            chain = start.get("prefix_chain")
            if chain:
                row["reusable_prefix_tokens"] = reusable_prefix_tokens(
                    chain,
                    self.history.recent_prefix_chains(model),
                    start.get("prompt_chars", 0),
                    row["prompt_tokens"],
                )
        except Exception:
            logger.debug("prefix history lookup failed for %r", model, exc_info=True)


class _DatabaseHistory:
    """The recorder's per-model lookups, answered from `llm_call` itself.

    Queried per call rather than cached in the process: two indexed reads of
    a couple of hundred rows are nothing beside an inference that takes
    seconds, and going to the database means every worker process shares one
    baseline instead of each slowly calibrating its own.
    """

    def recent_throughputs(self, model: str | None) -> list[float]:
        import db

        return db.recent_throughputs(model)

    def recent_prefix_chains(self, model: str | None) -> list[list[str]]:
        import db

        return db.recent_prefix_chains(model)


def _database_sink(row: dict) -> None:
    import db

    db.record_llm_call(row)


_installed: ActivityRecorder | None = None


def install_activity_recorder() -> ActivityRecorder | None:
    """Register the recorder on the global dispatcher, once per process.

    Called from the webapp bootstrap and from the agent worker bootstrap —
    the two places that own a Flask app context, which the database sink
    needs. Idempotent, so importing either twice is harmless.
    """
    global _installed
    if _installed is not None:
        return _installed
    from llama_index.core.instrumentation import get_dispatcher

    recorder = ActivityRecorder(sink=_database_sink, history=_DatabaseHistory())
    get_dispatcher().add_event_handler(recorder)
    _installed = recorder
    logger.info("LLM activity recording enabled")
    return recorder


# Frames from these package roots are plumbing, not a call site. The recorder
# runs inside the caller's own stack, so without this every row would trace
# back to the instrumentation that recorded it.
_PLUMBING_PREFIXES: tuple[str, ...] = (
    "llm.activity",
    "llama_index",
    "llama_index_instrumentation",
    "workflows",
    "openai",
    "httpx",
    "httpcore",
    "anyio",
    "asyncio",
    "concurrent",
    "threading",
)


def _is_plumbing(module: str) -> bool:
    return any(
        module == p or module.startswith(p + ".") for p in _PLUMBING_PREFIXES
    )


def _module_from_filename(filename: str) -> str:
    """A dotted name for a frame whose module is `__main__` or similar.

    Worker entry points run as `__main__`, which names nothing useful. Their
    file path does: /…/source/benchmarks/worker.py -> benchmarks.worker.
    """
    from pathlib import Path

    parts = Path(filename).with_suffix("").parts
    return ".".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")


def call_origin(frames: list[Any] | None = None) -> tuple[str | None, str | None]:
    """(caller, origin) for the code that made the current LLM call.

    Derived from the stack rather than from a tag, because a tag has to be
    remembered at every call site and an origin cannot be forgotten. `caller`
    is a dotted `module.function` suitable for grouping; `origin` is the
    precise `path:line in function` to open.

    Returns (None, None) when the stack is nothing but library frames — which
    shouldn't happen, but is not worth raising over inside telemetry.
    """
    if frames is None:
        frames = _live_frames()
    for frame in frames:
        module = getattr(frame, "module", "") or ""
        if not module or _is_plumbing(module):
            continue
        filename = getattr(frame, "filename", "") or ""
        if module.startswith("__"):
            module = _module_from_filename(filename)
        function = getattr(frame, "function", "") or "?"
        lineno = getattr(frame, "lineno", 0)
        short_path = "/".join(filename.rsplit("/", 2)[-2:]) if filename else module
        return f"{module}.{function}", f"{short_path}:{lineno} in {function}"
    return None, None


def _live_frames() -> list[Any]:
    """The current stack, innermost first, as objects `call_origin` can read."""
    import sys
    from types import SimpleNamespace

    out: list[Any] = []
    depth = 2  # skip _live_frames and call_origin
    while True:
        try:
            f = sys._getframe(depth)
        except ValueError:
            break
        out.append(
            SimpleNamespace(
                module=f.f_globals.get("__name__", ""),
                function=f.f_code.co_name,
                lineno=f.f_lineno,
                filename=f.f_code.co_filename,
            )
        )
        depth += 1
    return out


def _caller_from(tags: Any, derived: str | None = None) -> str:
    """The explicit tag if a call site set one, else the name derived from the
    stack. "unknown" only when neither is available — which means the label is
    almost never a dead end for debugging."""
    if isinstance(tags, dict):
        caller = tags.get("caller")
        if isinstance(caller, str) and caller:
            return caller
    return derived or "unknown"


def _elapsed_ms(started_at: datetime | None) -> int | None:
    if started_at is None:
        return None
    return int((datetime.now(UTC) - started_at).total_seconds() * 1000)
