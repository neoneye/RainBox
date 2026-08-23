"""One detail component per event kind, for the /assistant log.

The page renders a run as a stream of typed events (`db.assistant_log`), and
this is where each kind says what it is. The point of the split is that adding
a kind costs one renderer here rather than more markup in a page template that
already knows about everything.

Actions are the case that decides whether this scales. There are 32 of them and
more later, so the generic renderer is the one that matters: it lays out any
action's arguments and result well enough that a new action needs no code at
all. A bespoke renderer is a promotion a payload earns by genuinely differing —
a program and its output should not be JSON strings in a box — never a
requirement for the action to appear.

Everything untrusted is interpolated through `Markup.format`, which escapes
its arguments. An observation is model and tool output and a label can be an
action name that arrived as data, so neither may reach the page as markup.
"""

from __future__ import annotations

import json
from typing import Any

from markupsafe import Markup

#: The glyph the list and the gantt put in front of a row. Kept here so the
#: two surfaces cannot disagree about what a kind looks like.
EVENT_GLYPH: dict[str, str] = {
    "llm": "◆",
    "embedding": "◇",
    "action": "▸",
    "activity": "▤",
    "control": "●",
    "unaccounted": "·",
}

#: What each kind's pane says it is, under the label.
_KIND_CAPTION: dict[str, str] = {
    "llm": "model call",
    "embedding": "embedding call",
    "action": "action",
    "activity": "action's own work",
    "control": "operator",
    "unaccounted": "unmeasured",
}

_MAX_TEXT_CHARS: int = 40_000


def _fmt_ms(value: Any) -> str | None:
    if value is None:
        return None
    return f"{value / 1000:.1f}s"


def _fmt_int(value: Any) -> str | None:
    """Plain digits, no thousands separator — the operator's number format."""
    if value is None:
        return None
    return str(value)


#: KPI key → (display name, formatter). Order is the order they lay out in.
_KPI_FIELDS: list[tuple[str, str, Any]] = [
    ("model", "model", str),
    ("provider", "provider", str),
    ("input_tokens", "in", _fmt_int),
    ("output_tokens", "out", _fmt_int),
    ("cached_tokens", "cached", _fmt_int),
    ("prefill_ms", "prefill", _fmt_ms),
    ("decode_ms", "decode", _fmt_ms),
    ("status", "status", str),
    ("chars", "chars", _fmt_int),
    ("texts", "texts", _fmt_int),
]


def event_kpis(event: dict) -> list[tuple[str, str]]:
    """The KPI pairs a pane shows, in a fixed order.

    A KPI with nothing recorded is omitted rather than rendered as "None" — an
    empty slot says "not measured", which is true, where the word None reads
    like a value.
    """
    kpis = event.get("kpis") or {}
    pairs: list[tuple[str, str]] = []
    for key, name, fmt in _KPI_FIELDS:
        value = kpis.get(key)
        if value is None or value == "":
            continue
        shown = fmt(value)
        if shown is None or shown == "":
            continue
        pairs.append((name, str(shown)))
    duration = event.get("duration_ms")
    if duration is not None:
        pairs.insert(0, ("took", _fmt_ms(duration) or "—"))
    return pairs


def _kpi_html(event: dict) -> Markup:
    pairs = event_kpis(event)
    if not pairs:
        return Markup("")
    cells = Markup("").join(
        Markup('<div><span>{}</span><b>{}</b></div>').format(name, value)
        for name, value in pairs)
    return Markup('<div class="ev-kpis">{}</div>').format(cells)


def _block(title: str, body: Any, *, mono: bool = True) -> Markup:
    """A labelled block of text, escaped and length-capped.

    Capped because a payload here can be a 50k-token prompt or a whole
    retrieved corpus, and a pane that takes a second to paint is a pane nobody
    opens.
    """
    if body in (None, "", {}, []):
        return Markup("")
    text = body if isinstance(body, str) else json.dumps(
        body, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    clipped = len(text) > _MAX_TEXT_CHARS
    if clipped:
        text = text[:_MAX_TEXT_CHARS]
    note = Markup('<span class="ev-clip"> — first {} characters</span>').format(
        _MAX_TEXT_CHARS) if clipped else Markup("")
    cls = "ev-pre" if mono else "ev-text"
    return Markup('<div class="ev-block"><h5>{}{}</h5><pre class="{}">{}</pre></div>').format(
        title, note, cls, text)


def _generic_action(event: dict) -> Markup:
    """Any action at all: what was asked for, and what came back.

    The renderer that has to be right, because it is the one 30-odd actions
    use and the one a new action gets for free.
    """
    payload = event.get("payload") or {}
    observation = payload.get("observation") or {}
    text = observation.get("text") if isinstance(observation, dict) else None
    return Markup("").join([
        _block("reason", payload.get("reason"), mono=False),
        _block("arguments", payload.get("args")),
        _block("result", text or payload.get("observation_preview")),
        _block("error", payload.get("error")),
    ])


def _python_run(event: dict) -> Markup:
    """A program and its output, as a program and its output."""
    payload = event.get("payload") or {}
    observation = payload.get("observation") or {}
    args = payload.get("args") or {}
    text = observation.get("text") if isinstance(observation, dict) else None
    return Markup("").join([
        _block("code", args.get("code")),
        _block("output", text),
        _block("error", payload.get("error")),
    ])


def _memory_query(event: dict) -> Markup:
    """The recalled text first: it is what the rest of the turn reasons from."""
    payload = event.get("payload") or {}
    observation = payload.get("observation") or {}
    text = observation.get("text") if isinstance(observation, dict) else None
    data = observation.get("data") if isinstance(observation, dict) else None
    return Markup("").join([
        _block("query", (payload.get("args") or {}).get("query"), mono=False),
        _block("recalled", text),
        _block("retrieval", data),
    ])


#: Actions whose payload earned a renderer of its own.
_ACTION_RENDERERS = {
    "python_run": _python_run,
    "memory_query": _memory_query,
}


def _llm(event: dict) -> Markup:
    payload = event.get("payload") or {}
    rejected = payload.get("rejected_attempts") or []
    parts = [
        _block("system prompt", payload.get("system_prompt")),
        _block("user prompt", payload.get("user_prompt")),
        _block("response", payload.get("model_response")),
        _block("reasoning", payload.get("reasoning")),
        _block("log", payload.get("log")),
        _block("error", payload.get("error")),
    ]
    if rejected:
        parts.append(_block(
            f"rejected attempts ({len(rejected)})", rejected))
    return Markup("").join(parts)


def _embedding(event: dict) -> Markup:
    return _block("text", (event.get("payload") or {}).get("detail"),
                  mono=False)


def _control(event: dict) -> Markup:
    payload = event.get("payload") or {}
    return Markup("").join([
        _block("instruction", payload.get("reason"), mono=False),
        _block("payload", payload.get("args")),
    ])


def _unaccounted(_event: dict) -> Markup:
    return Markup(
        '<p class="ev-note">Nothing measured this stretch. It is the absence '
        'of a record, not a slow operation that was observed — the gap says '
        'where instrumenting would pay.</p>')


def _activity(_event: dict) -> Markup:
    return Markup(
        '<p class="ev-note">An action\'s own work, outside the calls it made. '
        'Nothing finer was recorded inside it.</p>')


_KIND_RENDERERS = {
    "llm": _llm,
    "embedding": _embedding,
    "action": _generic_action,
    "activity": _activity,
    "control": _control,
    "unaccounted": _unaccounted,
}


def render_event_detail(event: dict) -> str:
    """The detail pane for one event.

    Dispatches on kind, and for an action on the action name — falling back to
    the generic renderer, which is what lets an action nobody anticipated show
    up looking right.
    """
    kind = event.get("kind") or "action"
    label = event.get("label") or kind
    if kind == "action":
        body = _ACTION_RENDERERS.get(label, _generic_action)(event)
    else:
        body = _KIND_RENDERERS.get(kind, _generic_action)(event)
    return str(Markup(
        '<div class="ev-detail" data-kind="{}">'
        '<h4>{}</h4><div class="ev-caption">{}</div>{}{}</div>'
    ).format(kind, label, _KIND_CAPTION.get(kind, kind),
             _kpi_html(event), body))
