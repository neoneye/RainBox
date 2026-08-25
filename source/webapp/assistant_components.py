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

from agents.assistant import CAPABILITIES
from markupsafe import Markup

#: What each action is for, keyed by action name. Every pane says what
#: the call was doing: a reader should not have to know from the name
#: alone what `response_language_classifier` does.
ACTION_DESCRIPTIONS = {
    n.value: (c.summary or c.description) for n, c in CAPABILITIES.items()
}
# Code-driven trace rows are not model-selectable capabilities, so they do not
# belong in CAPABILITIES. Give them the same compact timeline description via
# this companion registry.
ACTION_DESCRIPTIONS.update({
    "response_language_classifier": (
        "determine which language(s) the reply should use"
    ),
    "reply_audit": "check the finished reply before it is sent",
    "request_summary": (
        "describe a request too long to fit in the prompt whole"
    ),
    "in flight": (
        "the model call happening right now, as it streams back"
    ),
    "second opinion": (
        "review a gated action independently before it is allowed to run"
    ),
    "run_summarizer": (
        "condense the finished run into the digest at the top of this page"
    ),
    "summary queued": (
        "the finished run waiting for the summarizer to come off the queue"
    ),
})
# Consulted first for a `code_driven` row. `acceptance_criteria` is the one
# action that is both: the catalog summary describes the revision the model can
# request, which is not what the loop's own call does.
CODE_DRIVEN_DESCRIPTIONS = {
    "acceptance_criteria": "establish what a good reply must satisfy",
    "recall_filter": "score what memory_query recalled for relevance",
}


#: The glyph the list and the gantt put in front of a row. Kept here so the
#: two surfaces cannot disagree about what a kind looks like.
EVENT_GLYPH: dict[str, str] = {
    "start": "▶",
    "llm": "◆",
    "embedding": "◇",
    "action": "▸",
    "activity": "▤",
    "control": "●",
    "unaccounted": "·",
    "skipped": "⊘",
}

#: What each kind's pane says it is, under the label.
_KIND_CAPTION: dict[str, str] = {
    "start": "the request that began the run",
    "llm": "model call",
    "embedding": "embedding call",
    "action": "action",
    "activity": "action's own work",
    "control": "operator",
    "unaccounted": "unmeasured",
    "skipped": "a call the loop could not make",
}

def _fmt_ms(value: Any) -> str | None:
    if value is None:
        return None
    return f"{value / 1000:.1f}s"


def _fmt_int(value: Any) -> str | None:
    """Plain digits, no thousands separator — the operator's number format."""
    if value is None:
        return None
    return str(value)


#: KPI key → (short key, how the value reads, hover). The text carries its own
#: label — "in 6180" rather than a column headed "in" — so the line reads the
#: way the step meta line always has, and a bare number never floats free.
_KPI_FIELDS: list[tuple[str, str, Any, str]] = [
    ("input_tokens", "in", lambda v: f"in {v}",
     "Input tokens: the size of the prompt sent to the model"),
    ("output_tokens", "out", lambda v: f"out {v}",
     "Output tokens: the amount of text the model generated"),
    ("cached_tokens", "cached", lambda v: f"cached {v}",
     "Prompt tokens the runtime served from cache instead of prefilling"),
    ("prefill_ms", "prefill", lambda v: f"prefill {v / 1000:.1f}s",
     "Prefill: time reading the prompt before the first output token"),
    ("decode_ms", "decode", lambda v: f"decode {v / 1000:.1f}s",
     "Decode: time spent generating the response"),
    # What a retrieval found and what it had to cut. The hovers are the ones
    # the step's own counts table carries, so the two surfaces explain a
    # number the same way.
    ("qa_static", "qa_static", lambda v: f"QA static {v}",
     "number of QA static items"),
    ("qa_dynamic", "qa_dynamic", lambda v: f"QA dynamic {v}",
     "number of QA dynamic items"),
    ("memory", "memory", lambda v: f"memory {v}",
     "number of memory items"),
    ("truncated", "truncated", lambda v: f"truncated {v}",
     "number of facts whose middle was dropped to fit the 1200-char rendered "
     "per-fact cap, both ends kept (tagged truncate1200); read one in full "
     "via memory_query with its uuid"),
    ("omitted", "omitted", lambda v: f"omitted {v}",
     "number of lower-ranked facts not admitted because they no longer fit "
     "the 11000-char fact payload — the legend and the retained lines, not "
     "the whole observation; narrow the query or fetch a fact by its uuid"),
    ("chars", "chars", lambda v: f"{v} chars",
     "Characters sent to the embedder"),
    ("texts", "texts", lambda v: f"{v} texts",
     "How many texts were embedded"),
]


def _kpi(label: str, text: str, title: str, *, href: str = "",
         html: str = "") -> dict:
    """One field of the meta line. `html` overrides the shown text where a
    link reads better short — "model ↗" on the page, the name on hover."""
    return {"label": label, "text": text, "title": title,
            "href": href, "html": html or text}


def event_kpis(event: dict) -> list[dict]:
    """The meta-line fields a pane shows, in a fixed order.

    A field with nothing recorded is omitted rather than rendered as "None" —
    an absent field says "not measured", which is true, where the word None
    reads like a value.
    """
    kpis = event.get("kpis") or {}
    fields: list[dict] = []
    model_uuid = kpis.get("model_uuid")
    group_uuid = kpis.get("model_group_uuid")
    name = kpis.get("model") or (str(model_uuid)[:8] if model_uuid else "")
    if model_uuid:
        fields.append(_kpi(
            "model", name or str(model_uuid)[:8],
            f"The model that answered: {name or model_uuid}",
            href=f"/model?id={model_uuid}", html="model ↗"))
    elif group_uuid:
        # No config recorded, but the group that picked one is. Which model
        # answers is settled by the binding, so that is the page to reach —
        # and the name the provider answered on still says what ran.
        fields.append(_kpi(
            "model", name or str(group_uuid)[:8],
            f"The model group this call resolved"
            + (f", which answered on {name}" if name else ""),
            href=f"/modelgroup?id={group_uuid}", html="model ↗"))
    elif name:
        fields.append(_kpi("model", name, f"The model that answered: {name}"))

    for key, label, fmt, title in _KPI_FIELDS:
        value = kpis.get(key)
        if value is None or value == "":
            continue
        fields.append(_kpi(label, str(fmt(value)), title))

    duration = event.get("duration_ms")
    tokens = (kpis.get("input_tokens") or 0) + (kpis.get("output_tokens") or 0)
    if duration and tokens:
        fields.append(_kpi(
            "tok/s", f"{tokens * 1000 / duration:.0f} tok/s",
            "Throughput: total tokens (input + output) per second"))
    if duration is not None:
        fields.append(_kpi(
            "took", f"took {duration / 1000:.1f}s",
            "Duration: how long this took"))
    start = event.get("start")
    if start is not None:
        fields.append(_kpi(
            "at", start.strftime("%H:%M:%S"), "When this began"))
    return fields


def _kpi_html(event: dict) -> Markup:
    fields = event_kpis(event)
    if not fields:
        return Markup("")
    cells = Markup("").join(
        Markup('<span class="ev-kpi" title="{}">{}</span>').format(
            f["title"],
            Markup('<a href="{}">{}</a>').format(f["href"], f["html"])
            if f["href"] else f["html"])
        for f in fields)
    return Markup('<div class="ev-kpis">{}</div>').format(cells)


def _block(title: str, body: Any, *, collapsed: bool = False,
           key: str = "") -> Markup:
    """A labelled block of text, escaped.

    `collapsed` renders a shut `<details>` — for the prompts above all, where a
    50k-token payload open by default buries every number above it. `key` is
    the `data-k` the live refresh reopens by, the same mechanism every other
    collapsed block on the page uses.

    One typeface for every block, the same as the trigger card's: what a block
    holds is text exactly as it was sent or returned, and a second font would
    have said a request and a response are different kinds of thing.

    Never truncated. This is the pane that exists to inspect a prompt, and the
    prompt-cache work turns on reading the exact bytes a call sent — a clipped
    tail hides the divergence being hunted. Nothing is bought by clipping
    either: a collapsed block costs no paint until it is opened, and what a
    step recorded was already bounded when it was captured.
    """
    if body in (None, "", {}, []):
        return Markup("")
    text = body if isinstance(body, str) else json.dumps(
        body, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    if collapsed:
        return Markup(
            '<details class="prompt ev-block" data-k="{}">'
            '<summary>{} ({} chars)</summary>'
            '<pre class="ev-pre">{}</pre></details>'
        ).format(key or title, title, len(text), text)
    return Markup(
        '<div class="ev-block"><h5>{}</h5><pre class="ev-pre">{}</pre></div>'
    ).format(title, text)


def _generic_action(event: dict) -> Markup:
    """Any action at all: what was asked for, and what came back.

    The renderer that has to be right, because it is the one 30-odd actions
    use and the one a new action gets for free.
    """
    payload = event.get("payload") or {}
    observation = payload.get("observation") or {}
    text = observation.get("text") if isinstance(observation, dict) else None
    return Markup("").join([
        _block("reason", payload.get("reason")),
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
        _block("query", (payload.get("args") or {}).get("query")),
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
    key = event.get("uuid") or event.get("label") or "ev"
    parts = [
        # First: what the call was working from — the profile in force, the
        # switch states. It frames everything below it, and it is short, so
        # putting it after the prompts left it past tens of thousands of
        # characters. The step sections read in this order too.
        _block("log", payload.get("log"), collapsed=True, key=f"{key}-log"),
        # The prompts shut by default: they are the largest thing here and the
        # least often the answer. The response is what the reader came for.
        _block("system prompt", payload.get("system_prompt"),
               collapsed=True, key=f"{key}-system"),
        _block("user prompt", payload.get("user_prompt"),
               collapsed=True, key=f"{key}-user"),
        _block("response", payload.get("model_response")),
        _block("reasoning", payload.get("reasoning"),
               collapsed=True, key=f"{key}-reasoning"),
        _block("error", payload.get("error")),
    ]
    if rejected:
        parts.append(_block(
            f"rejected attempts ({len(rejected)})", rejected))
    return Markup("").join(parts)


def _embedding(event: dict) -> Markup:
    return _block("text", (event.get("payload") or {}).get("detail"))


def _control(event: dict) -> Markup:
    payload = event.get("payload") or {}
    return Markup("").join([
        _block("instruction", payload.get("reason")),
        _block("payload", payload.get("args")),
    ])


def _start(event: dict) -> Markup:
    """The question the run was given, and both ways back to where it came
    from: the person who asked, and the message in the room.

    The run already had a "Started by" card beside the trace. On the stream it
    sits where it belongs — first — so reading the log top to bottom starts
    with the question rather than with whatever the loop did about it.
    """
    payload = event.get("payload") or {}
    links = []
    if payload.get("sender_uuid"):
        links.append(Markup(
            'Started by <a href="/user?id={}">{} ↗</a>').format(
                payload["sender_uuid"], payload.get("sender_name") or "user"))
    elif payload.get("sender_name"):
        links.append(Markup("Started by {}").format(payload["sender_name"]))
    if payload.get("room_uuid"):
        target = Markup('/chat?id={}').format(payload["room_uuid"])
        if payload.get("message_id") is not None:
            target = Markup('/chat?id={}&msg={}').format(
                payload["room_uuid"], payload["message_id"])
        links.append(Markup('<a href="{}">chat ↗</a>').format(target))
    header = Markup('<p class="ev-links">{}</p>').format(
        Markup(" · ").join(links)) if links else Markup("")
    # A write with no step of its own belongs to the run, and this is the
    # run's row. It is still a decision the assistant is waiting on, so it
    # cannot be the one that has nowhere to appear.
    return header + _block("request", payload.get("text")) + _intents(event)


def _unaccounted(_event: dict) -> Markup:
    return Markup(
        '<p class="ev-note">Nothing measured this stretch. It is the absence '
        'of a record, not a slow operation that was observed — the gap says '
        'where instrumenting would pay.</p>')


def _activity(event: dict) -> Markup:
    """A stretch of an action's own work — and what it produced, where the
    action recorded that.

    The findings are the point. A phase pane that could only say how long it
    took sent the reader to another step's user prompt to learn what the phase
    had actually retrieved, which is the trip this page exists to remove.
    """
    found = (event.get("payload") or {}).get("found")
    if found in (None, {}, []):
        return Markup(
            '<p class="ev-note">An action\'s own work, outside the calls it '
            'made. Nothing finer was recorded inside it.</p>')
    return _block("found", found)


#: Where a write intent is acted on. The endpoints already exist and are
#: tested on their own (webapp/chat_api.py); this is the only place the
#: operator reaches them for a run.
_INTENT_ACTION = "/chat/api/assistant/write-intents/{}/{}"


def _intent_controls(intent: dict) -> Markup:
    """The decision an intent is still waiting on, if any.

    A proposed write is the only thing on this page that BLOCKS a run: until
    it is confirmed or rejected the assistant is waiting. Undo is offered only
    where the write recorded how to reverse itself — a button that cannot do
    what it says is worse than no button.
    """
    state = intent.get("state") or ""
    uuid = intent.get("uuid") or ""
    name = intent.get("capability_name") or "write"
    if state == "proposed":
        return Markup(
            '<button class="primary" onclick="ppAct(\'{}\')">Confirm</button>'
            '<button class="danger" onclick="ppConfirmAct(\'{}\', '
            '\'Reject this {} write?\')">Reject</button>'
        ).format(_INTENT_ACTION.format(uuid, "confirm"),
                 _INTENT_ACTION.format(uuid, "reject"), name)
    if state == "completed" and (intent.get("result") or {}).get("undo"):
        return Markup(
            '<button onclick="ppConfirmAct(\'{}\', \'Undo this {} write? '
            'This reverts the change.\')">Undo</button>'
        ).format(_INTENT_ACTION.format(uuid, "undo"), name)
    return Markup("")


def _intents(event: dict) -> Markup:
    """The writes a row proposed, each with whatever decision it still owes."""
    intents = (event.get("payload") or {}).get("intents") or []
    if not intents:
        return Markup("")
    rows = [
        Markup('<div class="intent {}"><span class="cap">{}</span>'
               '<span class="badge b-{}">{}{}</span>{}{}<div class="acts">{}'
               '</div></div>').format(
            intent.get("state") or "", intent.get("capability_name") or "",
            intent.get("state") or "",
            "\u21a9 " if intent.get("state") == "undone" else "",
            intent.get("state") or "",
            Markup('<div class="muted">{}</div>').format(
                intent["preview_text"]) if intent.get("preview_text")
            else Markup(""),
            _block("payload", intent.get("payload")),
            _intent_controls(intent))
        for intent in intents
    ]
    # Not through _block: that wraps its body in a <pre>, which is right for
    # text exactly as it was sent and wrong for buttons — they would render
    # monospace and the long-block clamp would try to fold them away.
    return Markup(
        '<div class="ev-block"><h5>writes ({})</h5>{}</div>'
    ).format(len(rows), Markup("").join(rows))


def _skipped(event: dict) -> Markup:
    """A call that was never made.

    Not a failure and not a success: nothing ran. A pane that looked like
    either would say something untrue about the run.
    """
    payload = event.get("payload") or {}
    return (Markup('<p class="ev-note">This call was never made \u2014 nothing '
                   'ran, and nothing failed.</p>')
            + _block("reason", payload.get("reason"))
            + _block("error", payload.get("error")))


def _review(event: dict) -> Markup:
    """The pre-execution gate on an action, led by its verdict.

    The verdict is what the row is read for, and it is an outcome rather than
    a cost — so it reads as a block like an action's status, not as a figure
    on the meta line beside the token counts.

    `skipped` and `error` are verdicts too. Both let the action run and
    neither is an approval, so the reason one of them happened is the pane's
    substance: a run that went wrong because the gate never ran is a different
    bug from one the gate approved.
    """
    payload = event.get("payload") or {}
    problems = payload.get("problems") or []
    parts = [
        _block("verdict", (event.get("kpis") or {}).get("verdict")),
        _block("reason", payload.get("skip_reason")),
        _block("error", payload.get("error")),
    ]
    if problems:
        # Present on approvals too: "approved with problems" is the
        # right-answer-wrong-reasons signal, and folding it behind the verdict
        # would lose exactly the reviews worth reading.
        parts.append(_block(f"problems ({len(problems)})", problems))
    parts.append(_llm(event))
    return Markup("").join(parts)


_KIND_RENDERERS = {
    "start": _start,
    "llm": _llm,
    "embedding": _embedding,
    "action": _generic_action,
    "activity": _activity,
    "control": _control,
    "unaccounted": _unaccounted,
    "skipped": _skipped,
}


def event_description(event: dict) -> str:
    """What this event was for, in one line.

    Looked up from the label in the words the action catalog uses: a decide
    call is described by the action it chose, and a loop-issued call by its own
    entry where the two differ (see CODE_DRIVEN_DESCRIPTIONS). A kind with no
    catalog entry falls back to what that kind is, so no event is ever nameless
    in the header.
    """
    label = event.get("label") or ""
    action = label.split("→")[-1].strip() if "→" in label else label
    return (CODE_DRIVEN_DESCRIPTIONS.get(action)
            or ACTION_DESCRIPTIONS.get(action)
            or _KIND_CAPTION.get(event.get("kind") or "", ""))


def render_event_detail(event: dict) -> str:
    """The detail pane for one event.

    Dispatches on kind, and for an action on the action name — falling back to
    the generic renderer, which is what lets an action nobody anticipated show
    up looking right.
    """
    kind = event.get("kind") or "action"
    label = event.get("label") or kind
    if kind == "action":
        # How the action ended is an outcome, not a cost, so it reads as a
        # labelled block rather than a figure on the meta line. Prepended here
        # so a bespoke renderer cannot be the one that drops it.
        status = (event.get("kpis") or {}).get("status")
        body = (_block("status", status)
                + _ACTION_RENDERERS.get(label, _generic_action)(event)
                + _intents(event))
    elif event.get("variant") == "review":
        # A variant earns a renderer on the same terms an action does: its
        # payload genuinely differs from a plain model call's.
        body = _review(event)
    else:
        body = _KIND_RENDERERS.get(kind, _generic_action)(event)
    # The label and the description ride as data rather than as markup: the
    # card header is what names the thing being inspected, and printing them
    # here too would say it twice on one screen.
    return str(Markup(
        '<div class="ev-detail" data-kind="{}" data-label="{}" data-desc="{}"'
        ' data-step="{}">{}{}</div>'
    ).format(kind, label, event_description(event),
             event.get("step_ref") or "", _kpi_html(event), body))
