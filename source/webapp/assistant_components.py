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
    # The in-flight row's two. A bar that grows says the call is still going;
    # these say whether anything is still coming back, and how long it has
    # before the loop gives up on it.
    ("streamed", "streamed", lambda v: f"{v} chars back",
     "How much the model has sent back so far. A count that keeps climbing "
     "while the text says nothing new is the model repeating itself; a count "
     "that has stopped moving is a stall, not a slow answer"),
    ("timeout", "timeout", lambda v: f"of {v}",
     "How long this call may take before the loop abandons it and records a "
     "failed step"),
    ("attempt", "attempt", lambda v: f"attempt {v}",
     "Which try this is — the ones before it were answered and refused"),
    ("texts", "texts", lambda v: f"{v} texts",
     "How many texts were embedded"),
]


def _kpi(label: str, text: str, title: str, *, href: str = "",
         html: str = "", live: bool = False) -> dict:
    """One field of the meta line. `html` overrides the shown text where a
    link reads better short — "model ↗" on the page, the name on hover.
    `live` marks a value the page keeps advancing between refreshes; only the
    in-flight call has one, and only its elapsed time."""
    return {"label": label, "text": text, "title": title,
            "href": href, "html": html or text, "live": live}


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
        running = event.get("variant") == "live"
        fields.append(_kpi(
            "took", f"took {duration / 1000:.1f}s",
            "How long this has been running so far — still climbing"
            if running else "Duration: how long this took",
            live=running))
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
        Markup('<span class="ev-kpi"{} title="{}">{}</span>').format(
            Markup(' data-live-elapsed') if f.get("live") else Markup(""),
            f["title"],
            Markup('<a href="{}">{}</a>').format(f["href"], f["html"])
            if f["href"] else f["html"])
        for f in fields)
    return Markup('<div class="ev-kpis">{}</div>').format(cells)


# --- blocks -------------------------------------------------------------------
#
# A component decides WHAT a pane says; a serializer decides how one surface
# says it. They were the same function once, which is why the markdown export
# had to rebuild every pane from the step rows instead of reading the events
# the page reads. Splitting them is what lets both surfaces read one stream.


def _block(title: str, body: Any, *, collapsed: bool = False,
           key: str = "") -> dict | None:
    """A labelled block of text, or None when there is nothing to show.

    None rather than an empty block: an absent field says "not recorded",
    which is true, where an empty box says the field was empty.

    `collapsed` asks a surface to fold it — for the prompts above all, where a
    50k-token payload open by default buries every number above it. `key` is
    the `data-k` the live refresh reopens by, the same mechanism every other
    collapsed block on the page uses. Markdown honours neither: a document has
    no fold and no refresh.

    Never truncated. This is the pane that exists to inspect a prompt, and the
    prompt-cache work turns on reading the exact bytes a call sent — a clipped
    tail hides the divergence being hunted. Nothing is bought by clipping
    either: a collapsed block costs no paint until it is opened, and what a
    step recorded was already bounded when it was captured.
    """
    if body in (None, "", {}, []):
        return None
    return {"title": title, "body": body, "collapsed": collapsed,
            "key": key or title, "note": False, "json": _is_json_text(body)}


def _is_json_text(body: Any) -> bool:
    """Whether this body is a JSON document that ARRIVED as text.

    Only a string qualifies. A dict or list was never text in the first place
    — it is serialized for display and comes out indented already, so there is
    no second reading of it to offer. What this finds is the structured call's
    own answer: a whole object on one line, exactly as the provider sent it,
    which is unreadable and is also the only record of what was actually
    received.

    A bare scalar is valid JSON and is excluded: `"ok"` and `12` read the same
    either way, and offering to reformat them says there is something to see.
    """
    if not isinstance(body, str):
        return False
    try:
        return isinstance(json.loads(body), (dict, list))
    except (ValueError, TypeError):
        return False


def _note(text: str) -> dict:
    """A pane's prose, for a kind whose substance is that nothing was
    recorded. Not a block: there is no field being labelled."""
    return {"title": "", "body": text, "collapsed": False, "key": "",
            "note": True, "json": False}


def _blocks(*items: dict | None) -> list[dict]:
    """The blocks that have something in them, in the order given."""
    return [item for item in items if item is not None]


def _block_text(block: dict) -> str:
    """A block's body as the text a surface shows: strings as they are, and
    everything else as indented JSON."""
    body = block["body"]
    return body if isinstance(body, str) else json.dumps(
        body, indent=2, sort_keys=True, default=str, ensure_ascii=False)


#: The raw / pretty switch a JSON block carries. Two buttons rather than one
#: that toggles: a lone button has to be read to know which way it will go,
#: and which view you are LOOKING at is the thing worth showing.
#:
#: The choice is one preference, not one per block — it is how this operator
#: reads JSON, and the page restores it from localStorage on every load. So
#: every switch on the page moves together; a block whose text is already
#: indented simply does not change.
_VIEW_SWITCH: Markup = Markup(
    '<span class="ev-view">'
    '<button type="button" data-view="raw" title="Exactly the bytes that were '
    'recorded">raw</button>'
    '<button type="button" data-view="pretty" title="The same JSON, indented. '
    'Field order is the model\'s own, never sorted — the order a decision '
    'was written in is part of reading it">pretty</button></span>')


def _block_html(block: dict | None) -> Markup:
    """One block as the page draws it. Everything is escaped: a body is model
    and tool output, and a title can be an action name that arrived as data.

    A JSON block ships its RAW text and a switch. The reformatting happens in
    the browser, so the page carries one copy of a response rather than two,
    and what arrives is what was recorded — the reader opts into the prettier
    reading rather than being handed it and having to trust it.
    """
    if block is None:
        return Markup("")
    if block["note"]:
        return Markup('<p class="ev-note">{}</p>').format(block["body"])
    text = _block_text(block)
    switch = _VIEW_SWITCH if block.get("json") else Markup("")
    pre = Markup('<pre class="ev-pre"{}>{}</pre>').format(
        Markup(' data-json') if block.get("json") else Markup(""), text)
    if block["collapsed"]:
        return Markup(
            '<details class="prompt ev-block" data-k="{}">'
            '<summary>{} ({} chars){}</summary>{}</details>'
        ).format(block["key"], block["title"], len(text), switch, pre)
    return Markup(
        '<div class="ev-block"><h5>{}{}</h5>{}</div>'
    ).format(block["title"], switch, pre)


def _blocks_html(blocks: list[dict]) -> Markup:
    return Markup("").join(_block_html(b) for b in blocks)


# --- components ---------------------------------------------------------------


def _result_text(payload: dict):
    """What an action returned, as prose.

    The observation's own text where the step recorded one, and the capped
    preview otherwise. The fallback is not cosmetic: a step settled before the
    full observation was captured has the preview and nothing else, and a
    bespoke renderer that read only the observation showed such a step no
    result at all — the one thing every action pane must carry.
    """
    observation = payload.get("observation") or {}
    text = observation.get("text") if isinstance(observation, dict) else None
    return text or payload.get("observation_preview")


def _generic_action(event: dict) -> list[dict]:
    """Any action at all: what was asked for, and what came back.

    The renderer that has to be right, because it is the one 30-odd actions
    use and the one a new action gets for free.
    """
    payload = event.get("payload") or {}
    return _blocks(
        _block("reason", payload.get("reason")),
        _block("arguments", payload.get("args")),
        _block("result", _result_text(payload)),
        _block("error", payload.get("error")),
    )


def _python_run(event: dict) -> list[dict]:
    """A program and its output, as a program and its output."""
    payload = event.get("payload") or {}
    args = payload.get("args") or {}
    return _blocks(
        _block("code", args.get("code")),
        _block("output", _result_text(payload)),
        _block("error", payload.get("error")),
    )


def _memory_query(event: dict) -> list[dict]:
    """The recalled text first: it is what the rest of the turn reasons from."""
    payload = event.get("payload") or {}
    observation = payload.get("observation") or {}
    data = observation.get("data") if isinstance(observation, dict) else None
    return _blocks(
        _block("query", (payload.get("args") or {}).get("query")),
        _block("recalled", _result_text(payload)),
        _block("retrieval", data),
    )


#: Actions whose payload earned a renderer of its own.
_ACTION_RENDERERS = {
    "python_run": _python_run,
    "memory_query": _memory_query,
}


def _llm(event: dict) -> list[dict]:
    payload = event.get("payload") or {}
    rejected = payload.get("rejected_attempts") or []
    key = event.get("uuid") or event.get("label") or "ev"
    return _blocks(
        # First: what the call was working from — the profile in force, the
        # switch states. It frames everything below it, and it is short, so
        # putting it after the prompts left it past tens of thousands of
        # characters.
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
        _block(f"rejected attempts ({len(rejected)})", rejected or None),
        # What was appended to a retried call's prompt: its own refused answer
        # and why it was refused. That is what makes the second attempt a
        # different call from the first, and the first carries none of it.
        _block("added turns", payload.get("feedback"),
               collapsed=True, key=f"{key}-feedback"),
    )


def _live(event: dict) -> list[dict]:
    """The call happening right now: what it was asked, and what has come back
    so far.

    The only pane on the page with no record behind it. The row lands when the
    call returns, so until then the streamed checkpoint is the sole evidence
    there is — but the REQUEST half of it is complete before the call goes
    out, so this pane reads exactly like the step row that will replace it:
    same blocks, same order. There is no reason for the one row an operator is
    actively watching to be the one row that cannot say what it sent.

    It says which it is, because everything else in the inspector is a
    finished fact and this one is still moving — and because a pane holding a
    prompt and nothing else has to distinguish "the model answered nothing"
    from "the model has not answered yet".
    """
    payload = event.get("payload") or {}
    answering = bool(payload.get("model_response") or payload.get("reasoning"))
    return [_note(
        "This call is still running. What it has sent back so far is below,"
        " and grows as more arrives." if answering else
        "This call is still running. What it was asked is below; nothing has"
        " come back from the model yet.")] + _llm(event)


def _embedding(event: dict) -> list[dict]:
    return _blocks(_block("text", (event.get("payload") or {}).get("detail")))


def _control(event: dict) -> list[dict]:
    payload = event.get("payload") or {}
    return _blocks(
        _block("instruction", payload.get("reason")),
        _block("payload", payload.get("args")),
    )


def _start(event: dict) -> list[dict]:
    """The question the run was given.

    The run already had a "Started by" card beside the trace. On the stream it
    sits where it belongs — first — so reading the log top to bottom starts
    with the question rather than with whatever the loop did about it.

    Where it came FROM — the person who asked and the message in the room — is
    a pair of links, so it belongs to the surfaces rather than here.
    """
    return _blocks(_block("request", (event.get("payload") or {}).get("text")))


def _unaccounted(_event: dict) -> list[dict]:
    return [_note(
        "Nothing measured this stretch. It is the absence of a record, not a "
        "slow operation that was observed — the gap says where instrumenting "
        "would pay.")]


def _activity(event: dict) -> list[dict]:
    """A stretch of an action's own work — and what it produced, where the
    action recorded that.

    The findings are the point. A phase pane that could only say how long it
    took sent the reader to another step's user prompt to learn what the phase
    had actually retrieved, which is the trip this page exists to remove.
    """
    found = (event.get("payload") or {}).get("found")
    if found in (None, {}, []):
        return [_note("An action's own work, outside the calls it made. "
                      "Nothing finer was recorded inside it.")]
    return _blocks(_block("found", found))


def _skipped(event: dict) -> list[dict]:
    """A call that was never made.

    Not a failure and not a success: nothing ran. A pane that looked like
    either would say something untrue about the run.
    """
    payload = event.get("payload") or {}
    return [_note("This call was never made — nothing ran, and nothing "
                  "failed.")] + _blocks(
        _block("reason", payload.get("reason")),
        _block("error", payload.get("error")),
    )


def _review(event: dict) -> list[dict]:
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
    # Present on approvals too: "approved with problems" is the
    # right-answer-wrong-reasons signal, and folding it behind the verdict
    # would lose exactly the reviews worth reading.
    #
    # The sentences, not the objects. A problem carries a category as well,
    # and printing it beside the finding puts a tag where the reader is
    # looking for what was actually wrong.
    problem_text = "\n".join(
        f"- {p.get('text', '')}" if isinstance(p, dict) else f"- {p}"
        for p in problems) if problems else None
    return _blocks(
        _block("verdict", (event.get("kpis") or {}).get("verdict")),
        _block("group", payload.get("group_from")),
        _block("reason", payload.get("skip_reason")),
        _block("error", payload.get("error")),
        _block(f"problems ({len(problems)})", problem_text),
    ) + _llm(event)


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


def event_blocks(event: dict) -> list[dict]:
    """What one event's detail pane says, before any surface renders it.

    Dispatches on kind, and for an action on the action name — falling back to
    the generic renderer, which is what lets an action nobody anticipated show
    up looking right. One dispatch, so the page and the export cannot give one
    event two different readings.
    """
    kind = event.get("kind") or "action"
    label = event.get("label") or kind
    if kind == "action":
        # How the action ended is an outcome, not a cost, so it reads as a
        # labelled block rather than a figure on the meta line. Prepended here
        # so a bespoke renderer cannot be the one that drops it.
        status = (event.get("kpis") or {}).get("status")
        observation = (event.get("payload") or {}).get("observation") or {}
        data = observation.get("data") if isinstance(observation, dict) else None
        return (
            _blocks(_block("status", status))
            + _ACTION_RENDERERS.get(label, _generic_action)(event)
            # Appended here rather than inside a renderer so a bespoke one
            # cannot be the one that drops it — the same reason the status
            # block is prepended. The counts a retrieval reports are on the
            # meta line above; anything else an action records has this row as
            # its only home.
            + _blocks(_block("data", data, collapsed=True,
                             key=f"{event.get('uuid') or 'ev'}-data")))
    # A variant earns a renderer on the same terms an action does: its payload
    # genuinely differs from a plain model call's.
    if event.get("variant") == "review":
        return _review(event)
    if event.get("variant") == "live":
        return _live(event)
    return _KIND_RENDERERS.get(kind, _generic_action)(event)


# --- write intents ------------------------------------------------------------
#
#: Where a write intent is acted on. The endpoints already exist and are
#: tested on their own (webapp/chat_api.py); this is the only place the
#: operator reaches them for a run.
_INTENT_ACTION = "/chat/api/assistant/write-intents/{}/{}"


def event_intents(event: dict) -> list[dict]:
    """The writes a row proposed. On an action, the ones it proposed; on the
    run's opening row, the ones that belong to no step."""
    return (event.get("payload") or {}).get("intents") or []


def _intent_controls(intent: dict) -> Markup:
    """The decision an intent is still waiting on, if any.

    A proposed write is the only thing on this page that BLOCKS a run: until
    it is confirmed or rejected the assistant is waiting. Undo is offered only
    where the write recorded how to reverse itself — a button that cannot do
    what it says is worse than no button.

    The page only. A document cannot be clicked, so the export states the
    decision an intent is waiting on rather than offering it.
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


def _intents_html(event: dict) -> Markup:
    """The writes a row proposed, each with whatever decision it still owes."""
    intents = event_intents(event)
    if not intents:
        return Markup("")
    rows = [
        Markup('<div class="intent {}"><span class="cap">{}</span>'
               '<span class="badge b-{}">{}{}</span>{}{}<div class="acts">{}'
               '</div></div>').format(
            intent.get("state") or "", intent.get("capability_name") or "",
            intent.get("state") or "",
            "↩ " if intent.get("state") == "undone" else "",
            intent.get("state") or "",
            Markup('<div class="muted">{}</div>').format(
                intent["preview_text"]) if intent.get("preview_text")
            else Markup(""),
            _block_html(_block("payload", intent.get("payload"))),
            _intent_controls(intent))
        for intent in intents
    ]
    # Not through _block: that wraps its body in a <pre>, which is right for
    # text exactly as it was sent and wrong for buttons — they would render
    # monospace and the long-block clamp would try to fold them away.
    return Markup(
        '<div class="ev-block"><h5>writes ({})</h5>{}</div>'
    ).format(len(rows), Markup("").join(rows))


def _start_links(event: dict) -> Markup:
    """Both ways back to where a run came from: the person who asked, and the
    message in the room. Links, so the page only."""
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
    if not links:
        return Markup("")
    return Markup('<p class="ev-links">{}</p>').format(
        Markup(" · ").join(links))


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


# --- surfaces -----------------------------------------------------------------


def render_event_detail(event: dict) -> str:
    """The detail pane for one event, as the page draws it.

    The blocks come from `event_blocks`; what is added here is what only a page
    can carry — the links out of the opening row, and the buttons a proposed
    write is waiting on.
    """
    kind = event.get("kind") or "action"
    body = (_start_links(event) if kind == "start" else Markup("")) \
        + _blocks_html(event_blocks(event)) \
        + (_intents_html(event) if kind in ("start", "action") else Markup(""))
    # The label and the description ride as data rather than as markup: the
    # card header is what names the thing being inspected, and printing them
    # here too would say it twice on one screen.
    return str(Markup(
        '<div class="ev-detail" data-kind="{}" data-label="{}" data-desc="{}"'
        ' data-step="{}">{}{}</div>'
    ).format(kind, event.get("label") or kind, event_description(event),
             event.get("step_ref") or "", _kpi_html(event), body))


def fence(text: str, lang: str = "") -> str:
    """A fenced code block whose fence is long enough to survive backticks in
    `text` (CommonMark: the closing fence must be at least as long as any run of
    backticks inside)."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def event_markdown(event: dict) -> list[str]:
    """One event as the export writes it: heading, meta line, then its blocks.

    The same blocks the page draws, from the same dispatch. That is the whole
    point of the split — the export used to rebuild every pane from the step
    rows, which is how two readings of one run came to exist.
    """
    label = " ".join((event.get("label") or "").split())
    description = event_description(event)
    out = [f"### {label}" + (f" — {description}" if description else ""), ""]
    meta = [f["text"] for f in event_kpis(event)]
    if step_ref := event.get("step_ref"):
        meta.insert(0, step_ref)
    if meta:
        out += [" · ".join(meta), ""]
    for block in event_blocks(event):
        if block["note"]:
            out += [block["body"], ""]
            continue
        out += [f"**{block['title']}**", "", fence(_block_text(block)), ""]
    for intent in event_intents(event):
        out += [f"**write — {intent.get('capability_name') or 'write'} "
                f"({intent.get('state') or '—'})**", ""]
        if intent.get("preview_text"):
            out += [intent["preview_text"], ""]
        if intent.get("payload"):
            out += [fence(json.dumps(intent["payload"], indent=2,
                                      sort_keys=True, default=str,
                                      ensure_ascii=False)), ""]
    return out
