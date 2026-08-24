"""The assistant run read model — a run as a flat stream of typed events.

`assistant_step` is one row playing four roles: a model call, the action it
chose, the observation that came back, and a container for sub-events buried
in its JSON. Reading a run through that shape is why anything without a step
row of its own — an embedding call, a retrieval phase — has nowhere to be
rendered.

This module derives events instead. Nothing is written differently, so every
run that has already happened reads back the same way as the next one.

Two invariants hold everything else up:

- **No event contains another.** A phase contributes only the time its calls
  do not occupy, so the stream lays out as a staircase and a bar can never
  hide what ran inside it.
- **One enumeration.** `assistant_llm_calls` is a filter over `run_events`,
  so the page, the export, the run stats and the in-chat progress row cannot
  quote different numbers for the same run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from db.model_config import get_model_config, get_model_config_override
from db.models import AgentModelBinding, AssistantStep, LlmCall, db


def step_started_at(step):
    """When a step's model call BEGAN — which is not when its row was written.

    `requested_at` where it was recorded. Rows predating that capture are
    placed at their write time minus how long they took: the response landed
    when the row was written, so that is where the call ran. None when the row
    has neither, which is a legacy row the caller has to place itself."""
    if step.requested_at:
        return step.requested_at
    if step.created_at and step.duration_ms:
        return step.created_at - timedelta(milliseconds=step.duration_ms)
    return None


# --- model calls --------------------------------------------------------------
#
# A run's model calls do not map one-to-one onto step rows. Most are a step's
# own decide/code-driven call, but two ride inside something else: the
# second-opinion review (its own table) and the acceptance-criteria revision's
# inner call (in a step's observation payload). Counting rows therefore
# under-reports the calls, and their time books as "action" time — exactly the
# time an operator is hunting for. This is the single enumeration: the
# inspector's dashboard and waterfall, the markdown export, and the in-chat
# progress row all read it, so no surface can quote a different number of calls
# than another.


def _parse_ts(value):
    """An ISO timestamp from a JSONB payload, or None. Payload-sourced values
    are never trusted to parse — a malformed one drops the call's placement,
    not the page."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # Payloads store UTC; the rows come back from Postgres in the session's
    # zone. Convert so a payload-sourced call is not shown hours off the step
    # it ran inside.
    return parsed.astimezone() if parsed.tzinfo else parsed


def _call(label: str, kind: str, *, start, duration_ms, anchor: str = "",
          model_uuid=None, input_tokens=None, output_tokens=None,
          detail: str = "") -> dict:
    return {"label": label, "kind": kind, "start": start,
            "duration_ms": duration_ms, "anchor": anchor,
            "model_uuid": str(model_uuid) if model_uuid else None,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "detail": detail}


def _rejected_calls(step) -> list[dict]:
    """The calls this step made and threw away: responses that arrived and
    were refused by the schema or a validator, each retried with the reason
    attached (see `ModelGroupAgent.REJECTED_RESPONSE_RETRIES`).

    Real calls to a real model, so they belong in the enumeration on the same
    footing as the one that succeeded — the step's own `duration_ms` covers
    only the attempt it kept, and without these rows their seconds read as a
    gap where nothing was running."""
    calls: list[dict] = []
    attempts = step.rejected_attempts or []
    for index, attempt in enumerate(attempts, start=1):
        # Numbered only when there were several, so the common single retry
        # reads as plain "(rejected)" and a model that failed twice in a row
        # is still tellable apart row by row.
        ordinal = f" {index}/{len(attempts)}" if len(attempts) > 1 else ""
        calls.append(_call(
            f"{step.action or '—'} (rejected{ordinal})", "rejected",
            start=_parse_ts(attempt.get("requested_at")),
            duration_ms=attempt.get("ms"), anchor=str(step.uuid),
            model_uuid=attempt.get("model_uuid"),
            input_tokens=attempt.get("input_tokens"),
            output_tokens=attempt.get("output_tokens")))
    return calls


def embed_call_label(call: dict) -> tuple[str, str]:
    """An embed row's label and its detail: the shape of the call, and WHAT
    went to the embedder.

    The label never carries the text. It sits in a fixed-width column beside a
    bar, so a value of unbounded length pushes the timing off the row — and a
    query is exactly that. The text belongs in the detail, which the inspector
    pane and the step's timing table both have room to show whole.

    The model is deliberately in neither: a run embeds on one model, so naming
    it per row is the same string repeated down the column. It is named once
    per step, in the timing block's embedder line.

    A batch is named by its size. A first-run seed populate embeds the whole
    registry in one call, and how many it took IS the thing worth knowing —
    where the first chunk of it says nothing at all.
    """
    preview = [str(t) for t in (call.get("preview") or [])]
    texts = call.get("texts") or 0
    label = f"embed {texts} texts" if texts > 1 else "embed"
    return label, " / ".join(preview)


def _embedding_calls(step, data: dict) -> list[dict]:
    """The embedder calls a step made, from its `timing` payload.

    A different model from the ones above — no tokens, no prompt, and not the
    assistant's own — but it runs on the same local runtime, so its calls are
    part of what the wall-clock between two LLM bars is made of, and enough of
    them can evict the model the next decide call needs warm. Kept out of the
    run's token/throughput totals (see `assistant_run_stats`) and counted on
    their own."""
    timing = data.get("timing") or {}
    embeddings = timing.get("embeddings") or {}
    calls: list[dict] = []
    for call in embeddings.get("calls") or []:
        label, detail = embed_call_label(call)
        calls.append(_call(
            label, "embedding",
            start=_parse_ts(call.get("requested_at")),
            duration_ms=call.get("ms"), anchor=str(step.uuid), detail=detail))
    return calls


#: A gap shorter than this is not drawn. Sub-second scheduling jitter between
#: two adjacent calls is not a finding, and a row per 0.1s gap would bury the
#: ones that are.
UNACCOUNTED_MIN_MS: int = 1000


def _phase_calls(step, data: dict) -> list[dict]:
    """The named phases a step's action recorded, from its `timing` payload.

    Not model calls — spans of the action's own wall-clock, which is exactly
    the part a per-call waterfall leaves as empty space. `_PhaseTimer` records
    them with start times for this purpose.
    """
    timing = data.get("timing") or {}
    calls: list[dict] = []
    for phase in timing.get("phases") or []:
        name = phase.get("name") or "phase"
        # Named for the step that recorded it. A phase called "recall filter"
        # sits beside the `recall_filter` call it contains, and the two read as
        # one thing listed twice; the prefix says which step owns the bar, and
        # so where following it goes.
        calls.append(_call(
            f"{step.action} › {name}", "phase",
            start=_parse_ts(phase.get("started_at")),
            duration_ms=phase.get("ms"), anchor=str(step.uuid)))
    return calls


def _end_of(call):
    """When a row stopped, or None if it cannot be placed."""
    if not call["start"]:
        return None
    return call["start"] + timedelta(milliseconds=call["duration_ms"] or 0)


def _span(call):
    """(start, end) for a placeable row, else None."""
    end = _end_of(call)
    return (call["start"], end) if call["start"] and end else None


def _subtract(window, occupied: list[tuple]) -> list[tuple]:
    """`window` minus every interval in `occupied`, as the pieces left over.

    The core of the layout: an action's recorded phase is wall-clock that
    OVERLAPS the calls made during it, and drawing it whole would put a bar on
    top of those calls — hiding them, and hiding any stall between them. What
    the phase actually contributes is the time no call occupies.
    """
    pieces = [window]
    for busy_start, busy_end in sorted(occupied):
        remaining: list[tuple] = []
        for piece_start, piece_end in pieces:
            if busy_end <= piece_start or busy_start >= piece_end:
                remaining.append((piece_start, piece_end))
                continue
            if busy_start > piece_start:
                remaining.append((piece_start, busy_start))
            if busy_end < piece_end:
                remaining.append((busy_end, piece_end))
        pieces = remaining
    return pieces


#: A leftover slice of a phase shorter than this is rounding, not an activity.
MIN_ACTIVITY_MS: int = 100

#: A gap shorter than this is not drawn. Sub-second scheduling jitter between
#: two adjacent calls is not a finding, and a row per 0.1s gap would bury the
#: ones that are.
UNACCOUNTED_MIN_MS: int = 1000


def _activity_rows(phases: list[dict], calls: list[dict]) -> list[dict]:
    """Each phase's own work: the phase minus the calls made inside it.

    A phase that made no calls yields one bar the length of the phase. A phase
    that made calls yields the slices around them — which is how the ten
    seconds a filter spends loading its model become a bar of their own
    instead of vanishing inside a span drawn over the call that followed.
    """
    busy = [sp for sp in (_span(c) for c in calls) if sp]
    rows: list[dict] = []
    for phase in phases:
        window = _span(phase)
        if window is None:
            continue
        inside = [(a, b) for a, b in busy
                  if a < window[1] and b > window[0]]
        for piece_start, piece_end in _subtract(window, inside):
            ms = int((piece_end - piece_start).total_seconds() * 1000)
            if ms < MIN_ACTIVITY_MS:
                continue
            rows.append(_call(phase["label"], "activity", start=piece_start,
                              duration_ms=ms, anchor=phase["anchor"]))
    return rows


def _unaccounted_rows(rows: list[dict], run) -> list[dict]:
    """Every stretch of the run that no bar covers.

    With every bar a leaf, this now means one thing only: time nothing
    measured. A gap in the waterfall is a real hole in the instrumentation,
    which is what makes it worth drawing.
    """
    if run is None:
        return []
    placed = sorted((sp for sp in (_span(r) for r in rows) if sp))
    if not placed:
        return []
    first = min([placed[0][0]] + ([run.started_at] if run.started_at else []))
    last = max([b for _, b in placed]
               + ([run.finished_at] if run.finished_at else []))
    gaps: list[dict] = []
    for piece_start, piece_end in _subtract((first, last), placed):
        ms = int((piece_end - piece_start).total_seconds() * 1000)
        if ms >= UNACCOUNTED_MIN_MS:
            gaps.append(_call("unaccounted", "unaccounted", start=piece_start,
                              duration_ms=ms, anchor=""))
    return gaps


def _inner_calls(step, data: dict) -> list[dict]:
    """The model calls a step made from inside its action, which have no row of
    their own: the criteria revision's inner call. It records `requested_at` +
    `usage` in the observation payload; older payloads have the usage but no
    start time."""
    calls: list[dict] = []
    if "acceptance_criteria" in data or "usage" in data:
        usage = data.get("usage") or {}
        if usage.get("ms") is not None:
            calls.append(_call(
                "acceptance_criteria revision", "inner",
                start=_parse_ts(data.get("requested_at")),
                duration_ms=usage.get("ms"), anchor=str(step.uuid),
                model_uuid=data.get("model_uuid"),
                input_tokens=usage.get("input"),
                output_tokens=usage.get("output")))
    return calls


def retry_resumed_at(step):
    """When the attempt a step RECORDS began, on a call that retried.

    `requested_at` is when the call was sent, which on a retried call is when
    its first — rejected — attempt was sent. The row's `duration_ms` is the
    kept attempt's. Placed at `requested_at` the two bars start together and
    the kept one looks like it ran alongside the attempt it replaced, when in
    fact it began where that one stopped: the attempts are sequential, each
    sent only after the previous was refused.

    None when the call kept its first answer, which is nearly every call."""
    resumed = None
    for attempt in step.rejected_attempts or []:
        started = _parse_ts(attempt.get("requested_at"))
        if started is None:
            continue
        ended = started + timedelta(milliseconds=attempt.get("ms") or 0)
        if resumed is None or ended > resumed:
            resumed = ended
    return resumed


#: Every kind a run event can be. `llm` covers every model call whatever made
#: it; `variant` on the event keeps the finer distinction the page colours by.
EVENT_KINDS: tuple[str, ...] = (
    "start", "llm", "embedding", "action", "activity", "control",
    "unaccounted",
)

#: Which `variant` values are model calls. Everything produced by the call
#: helpers is one of these.
_LLM_VARIANTS: frozenset[str] = frozenset(
    {"decide", "code-driven", "inner", "review", "rejected"})


def _event(kind: str, label: str, *, start, duration_ms, anchor: str = "",
           variant: str = "", uuid: str = "", kpis: dict | None = None,
           payload: dict | None = None) -> dict:
    """One row of the stream.

    `kind` selects the detail component; `variant` is the finer distinction
    within a kind that the page colours by (a rejected call is still an `llm`
    event).
    """
    return {"uuid": uuid, "kind": kind, "variant": variant or kind,
            "label": label, "start": start, "duration_ms": duration_ms,
            "anchor": anchor, "kpis": kpis or {}, "payload": payload or {}}


def _as_event(call: dict) -> dict:
    """An event from one of the `_call` helpers' dicts."""
    variant = call["kind"]
    kind = "llm" if variant in _LLM_VARIANTS else variant
    return _event(
        kind, call["label"], start=call["start"],
        duration_ms=call["duration_ms"], anchor=call["anchor"],
        variant=variant,
        kpis={"model_uuid": call.get("model_uuid"),
              "input_tokens": call.get("input_tokens"),
              "output_tokens": call.get("output_tokens")},
        payload={"detail": call.get("detail", "")})


#: Counts an action's observation may report about what it found. Only
#: memory_query carries them today; an action without them reports none, so
#: every other action's meta line stays clean.
_OBSERVATION_COUNTS: tuple[str, ...] = (
    "qa_static", "qa_dynamic", "memory", "truncated", "omitted")


def _observation_counts(step) -> dict:
    """The counts an action reported, for its meta line.

    Same numbers the step's own table shows. Read here so the inspector is not
    the poorer surface — how much was found and how much was cut is the first
    thing asked of a retrieval.
    """
    data = (step.observation or {}).get("data") or {}
    return {key: data[key] for key in _OBSERVATION_COUNTS
            if isinstance(data.get(key), int)}


def _observation_without_timing(step) -> dict:
    """The observation an action pane shows, minus its `timing` block.

    Those seconds are already on the stream as their own activity and
    embedding events; dumping the payload again would show the same numbers
    twice, as JSON nobody reads.
    """
    observation = dict(step.observation or {})
    data = observation.get("data")
    if isinstance(data, dict) and "timing" in data:
        data = {k: v for k, v in data.items() if k != "timing"}
        observation["data"] = data
    return observation


def _step_events(step) -> list[dict]:
    """The events one step row stands for.

    A step is a model call AND the action that call chose AND what came back.
    Rendering them as one row is why none of the three has a place of its own,
    so they are separated here.
    """
    data = (step.observation or {}).get("data") or {}
    events: list[dict] = []
    if step.phase == "control":
        # An operator event — a redirect or a stop. Not a model call, and not
        # an action the loop chose.
        events.append(_event(
            "control", step.action or "control", start=step_started_at(step),
            duration_ms=step.duration_ms, anchor=str(step.uuid),
            uuid=str(step.uuid),
            payload={"reason": getattr(step, "reason", None),
                     "args": getattr(step, "args", None) or {}}))
        return events
    if step.phase == "skipped":
        # A call the loop could not make. Worth a row, but it cost nothing.
        return events

    # The attempts thrown away came first, so they enumerate first.
    events.extend(_as_event(c) for c in _rejected_calls(step))
    if step.requested_at or step.duration_ms is not None or step.system_prompt:
        start = step_started_at(step)
        resumed = retry_resumed_at(step)
        if resumed is not None and (start is None or resumed > start):
            start = resumed
        events.append(_event(
            "llm",
            step.action or "—" if step.code_driven
            else f"decide → {step.action}" if step.action else "decide",
            start=start, duration_ms=step.duration_ms,
            anchor=str(step.uuid), uuid=str(step.uuid),
            variant="code-driven" if step.code_driven else "decide",
            kpis={"model_uuid": str(step.model_uuid) if step.model_uuid else None,
                  "input_tokens": step.input_tokens,
                  "output_tokens": step.output_tokens},
            # getattr: a step row carries all of these, but the trace is also
            # read from lighter shapes in tests and tools, and a missing
            # optional field must not cost the whole stream.
            payload={k: getattr(step, k, None) for k in (
                "system_prompt", "user_prompt", "model_response", "reasoning",
                "log", "error")}
            | {"rejected_attempts": getattr(step, "rejected_attempts", None) or []}))
    events.extend(_as_event(c) for c in _inner_calls(step, data))
    events.extend(_as_event(c) for c in _embedding_calls(step, data))
    events.extend(_as_event(c) for c in _phase_calls(step, data))

    if getattr(step, "action", None) and not step.code_driven:
        # Deliberately WITHOUT a duration. The action's wall-clock is already
        # on the stream as the phases it ran, and giving this row a span would
        # make it contain them — the one thing the layout forbids. What this
        # row carries is the request and the result, which had nowhere to go
        # while a step was a single row.
        events.append(_event(
            "action", step.action,
            # Where it SETTLED: an action event records what came back, and
            # that is known when the observation lands — after the phases the
            # action ran, not before them. Legacy rows predate settled_at and
            # keep the call's end.
            start=getattr(step, "settled_at", None) or _end_of(
                {"start": step_started_at(step),
                 "duration_ms": step.duration_ms}),
            duration_ms=None, anchor=str(step.uuid), uuid=str(step.uuid),
            kpis={"status": "error" if getattr(step, "error", None) else "ok",
                  **_observation_counts(step)},
            payload={"args": getattr(step, "args", None) or {},
                     "reason": getattr(step, "reason", None),
                     "observation": _observation_without_timing(step),
                     "observation_preview": getattr(
                         step, "observation_preview", None),
                     "error": getattr(step, "error", None)}))
    return events


def run_events(run, steps: list, reviews: list | None = None,
               trigger: dict | None = None) -> list[dict]:
    """A run as a flat stream of typed events, oldest first.

    Derived from records that already exist, so a run that happened before
    this module did reads back the same way as the next one.

    No event contains another. A phase contributes only the time its calls do
    not occupy, and an action carries no span at all, so the stream lays out
    as a staircase and no bar can hide what ran inside it.

    `trigger` is the chat message that set the run going
    (`get_run_trigger_message`). Given one, the stream opens with a `start`
    event: the request is the first thing that happened, and a reader who has
    to look somewhere else for it is reading the run without its question.
    Omitted when there is none — a run seeded outside the chat flow has no
    message that began it, and an empty Start row would claim one existed.
    """
    events: list[dict] = []
    if trigger is not None:
        events.append(_event(
            "start", "start",
            start=getattr(run, "started_at", None),
            # Zero, not None: the run began at a moment, it did not spend a
            # stretch. A duration would put a bar on the gantt for time
            # nothing worked.
            duration_ms=0, anchor="", uuid="start",
            payload={"text": trigger.get("text") or "",
                     "sender_name": trigger.get("sender_name") or "",
                     "sender_uuid": trigger.get("sender_uuid") or "",
                     "message_id": trigger.get("id"),
                     "room_uuid": str(getattr(run, "room_uuid", "") or ""),
                     "timestamp": trigger.get("timestamp") or ""}))
    for step in steps:
        events.extend(_step_events(step))

    by_uuid = {str(s.uuid): s for s in steps}
    for r in reviews or []:
        # A review runs between its step's decide call returning and the action
        # executing, so a row written before review start times were recorded
        # is placed at the moment its step row was opened.
        gated = by_uuid.get(str(r.step_uuid)) if r.step_uuid else None
        events.append(_event(
            "llm", "second opinion", variant="review",
            start=r.requested_at or (gated.created_at if gated else None),
            duration_ms=r.duration_ms,
            anchor=str(r.step_uuid) if r.step_uuid else "",
            kpis={"model_uuid": str(r.model_uuid) if r.model_uuid else None,
                  "input_tokens": r.input_tokens,
                  "output_tokens": r.output_tokens}))

    events.extend(_summary_events(run))

    # Undated events sort last rather than crashing the comparison — they are
    # legacy rows, and the page renders them without a bar.
    events.sort(key=lambda e: (e["start"] is None, e["start"] or datetime.min))

    # Phases are not rows of their own: they contribute the slices their calls
    # leave. Only rows with a span occupy time, which is why the action and
    # control events are held out of the subtraction.
    phases = [e for e in events if e["kind"] == "phase"]
    spanning = [e for e in events
                if e["kind"] not in ("phase", "action", "control", "start")]
    rest = [e for e in events if e["kind"] in ("action", "control", "start")]
    leaves = spanning + [_as_event(c) for c in _activity_rows(phases, spanning)]
    leaves.extend(_as_event(c) for c in _unaccounted_rows(leaves, run))
    out = leaves + rest
    out.sort(key=lambda e: (e["start"] is None, e["start"] or datetime.min,
                            _ORDER_AT_SAME_INSTANT.get(e["kind"], 1)))
    _attach_llm_call_kpis(out, run)
    _attach_model_names(out)
    _attach_step_refs(out, steps)
    return out


#: How far an llm_call row's start may sit from an event's before the two are
#: not the same call. Generous: the row is stamped when the request goes out
#: and the event from the step's own `requested_at`, which can differ by the
#: time it takes to build a prompt.
_LLM_CALL_MATCH_TOLERANCE = timedelta(seconds=5)


#: Ties at one instant: the run's opening event leads, an action trails the
#: call that chose it.
_ORDER_AT_SAME_INSTANT: dict[str, int] = {"start": 0, "action": 2}


def _step_span(step):
    """When a step began and when it settled.

    It opens at its model call and closes when the observation lands, which is
    after the phases its action ran — the same two moments the call event and
    the action event are placed at.
    """
    start = step_started_at(step)
    end = getattr(step, "settled_at", None) or _end_of(
        {"start": start, "duration_ms": getattr(step, "duration_ms", None)})
    return start, end


def _attach_step_refs(events: list[dict], steps: list) -> None:
    """Say which step each row belongs to.

    A row on its own does not answer the question the page is read for: a
    twenty-second gap matters because of WHERE it fell, and a phase means
    little until you know which step ran it.

    An event that carries a step says so directly. One that carries none is
    placed by when it happened, and the answer is which two steps it fell
    between — an unaccounted stretch between step 3 settling and step 4 opening
    is loop overhead, and naming both ends is what makes that legible.

    A step's call is labelled its start and its action its end, because those
    are the two moments the step is bounded by. A code-driven step has no
    action, so its one row IS the step: calling that row a start would promise
    an end the stream never delivers.
    """
    for event in events:
        event["step_ref"] = ""
    if not steps:
        return
    number = {str(s.uuid): i + 1 for i, s in enumerate(steps)}
    bounded = {str(s.uuid) for s in steps
               if getattr(s, "action", None) and not s.code_driven}
    spans = [(_step_span(s), i + 1) for i, s in enumerate(steps)]
    spans = [((a, b), n) for (a, b), n in spans if a and b]
    spans.sort()
    for event in events:
        n = number.get(str(event.get("anchor") or ""))
        if n is not None:
            if str(event["anchor"]) in bounded and event["kind"] == "llm" \
                    and event["variant"] in ("decide", "code-driven"):
                event["step_ref"] = f"Step {n} start"
            elif event["kind"] == "action":
                event["step_ref"] = f"Step {n} end"
            else:
                event["step_ref"] = f"Step {n}"
            continue
        event["step_ref"] = _ref_by_time(event["start"], spans)


def _ref_by_time(when, spans: list) -> str:
    """Where a row with no step of its own fell, in terms of the steps."""
    if when is None or not spans:
        return ""
    # Half-open at the end: a gap begins exactly where the step before it
    # stopped, and counting that instant as still inside the step would hide
    # the very stretch the row was synthesized to show.
    inside = [n for (a, b), n in spans if a <= when < b]
    if inside:
        return f"Step {inside[0]}"
    # `b <= when` pairs with the half-open test above: the instant a step ends
    # belongs to what came after it, and every moment lands in exactly one of
    # inside / before / after rather than in none of them.
    before = [n for (a, b), n in spans if b <= when]
    after = [n for (a, b), n in spans if a > when]
    if before and after:
        return f"Step {max(before)} \u2192 Step {min(after)}"
    if before:
        return f"after Step {max(before)}"
    if after:
        return f"before Step {min(after)}"
    return ""


def _model_label(model_uuid) -> str:
    """What to call the model behind a uuid.

    A step records the OVERRIDE it ran on, not the base config, so looking the
    uuid up as a config alone finds nothing and the reader is left with eight
    hex characters. An override is named by the model it tunes plus the tuning
    itself — "gemma4:e4b · t0.15 c100k struct" — since neither half identifies
    the call on its own.
    """
    config = get_model_config(model_uuid)
    if config is not None:
        return config.display_name or config.model_name
    override = get_model_config_override(model_uuid)
    if override is None:
        return ""
    base = get_model_config(override.model_config_uuid)
    tuning = override.effective_display_name or ""
    if base is None:
        return tuning
    return f"{base.model_name} · {tuning}" if tuning else base.model_name


def _attach_model_names(events: list[dict]) -> None:
    """Name the model each call ran on, from its uuid.

    The llm_call join supplies this for runs recorded since the linkage; every
    older run has only the uuid on its step row, and "the model that answered:
    eca6dd6d" tells a reader nothing. One lookup per distinct model, not per
    event.
    """
    wanted = {e["kpis"].get("model_uuid") for e in events
              if e["kind"] == "llm" and e["kpis"].get("model_uuid")
              and not e["kpis"].get("model")}
    if not wanted:
        return
    names: dict[str, str] = {}
    for model_uuid in wanted:
        try:
            name = _model_label(UUID(str(model_uuid)))
        except Exception:
            continue
        if name:
            names[str(model_uuid)] = name
    for event in events:
        name = names.get(str(event["kpis"].get("model_uuid") or ""))
        if name:
            event["kpis"]["model"] = name


def _attach_llm_call_kpis(events: list[dict], run) -> None:
    """Fill each llm event's prefill, decode and cache KPIs from `llm_call`.

    Matched on the run plus the closest start time. Within a run that is exact
    rather than a guess: the assistant makes one model call at a time, so the
    calls and the events are the same sequence. `run_uuid` is what makes it
    safe — matching on time alone would confuse two runs happening at once.

    One query for the run, not a lookup per event, which would be a query per
    bar on the page. An event with no matching row keeps the KPIs the step
    itself carries, so a run predating the linkage still renders.
    """
    for event in events:
        if event["kind"] == "llm":
            event["kpis"].setdefault("prefill_ms", None)
            event["kpis"].setdefault("decode_ms", None)
            event["kpis"].setdefault("cached_tokens", None)
    run_uuid = getattr(run, "uuid", None)
    if run_uuid is None:
        return
    try:
        rows = (db.session.query(LlmCall)
                .filter(LlmCall.run_uuid == run_uuid)
                .order_by(LlmCall.started_at)
                .all())
    except Exception:
        # The read model must never take the page down over telemetry.
        return
    unused = [r for r in rows if r.started_at]
    for event in events:
        if event["kind"] != "llm" or not event["start"]:
            continue
        best, best_gap = None, None
        for row in unused:
            gap = abs(row.started_at - event["start"])
            if best_gap is None or gap < best_gap:
                best, best_gap = row, gap
        if best is None or best_gap > _LLM_CALL_MATCH_TOLERANCE:
            continue
        unused.remove(best)
        event["kpis"].update({
            "prefill_ms": best.prefill_ms,
            "decode_ms": best.decode_ms,
            "cached_tokens": (best.cached_tokens_reported
                              if best.cached_tokens_reported is not None
                              else best.cached_tokens_estimated),
            "model": best.model,
            "provider": best.provider,
            "call_uuid": str(best.uuid),
        })
        if best.prompt_tokens is not None:
            event["kpis"]["input_tokens"] = best.prompt_tokens
        if best.completion_tokens is not None:
            event["kpis"]["output_tokens"] = best.completion_tokens


#: The caller name the run summarizer's model call records
#: (`AssistantRunSummarizerAgent.caller_name`). Its row is the only trace that
#: call leaves: it runs off a queue after the run is finished, so it has no
#: step row to be derived from.
SUMMARIZER_CALLER: str = "assistant.run_summarizer"

#: What the stream calls that call, and the wait in front of it.
SUMMARY_LABEL: str = "run_summarizer"
SUMMARY_WAIT_LABEL: str = "summary queued"

#: How far a summarizer row's finish may sit from the moment its digest was
#: stored before the two are not the same call. Milliseconds in practice — the
#: digest is written as soon as the call returns — so this is slack, not a
#: search radius.
_SUMMARY_MATCH_WINDOW = timedelta(seconds=10)


def _prompts_from_messages(messages) -> dict:
    """The system and user text of a recorded call, as the llm renderer wants
    them. First of each role: a structured call sends one of each, and a later
    one would be a continuation rather than the request."""
    out: dict = {}
    for message in messages or []:
        role = (message or {}).get("role")
        key = f"{role}_prompt"
        if role in ("system", "user") and key not in out:
            out[key] = (message or {}).get("content") or ""
    return out


def _summarizer_call(run):
    """The `llm_call` row behind the digest on the run's dashboard.

    By `run_uuid` where the summarizer recorded it. Where it did not — every
    run summarized before it tagged its calls — by the moment the digest was
    stored, which the summary itself records and which lands within
    milliseconds of the call returning. That fallback is why no row has to be
    rewritten for an old run to read back whole.
    """
    run_uuid = getattr(run, "uuid", None)
    if run_uuid is None:
        return None
    try:
        rows = (db.session.query(LlmCall)
                .filter(LlmCall.caller == SUMMARIZER_CALLER,
                        LlmCall.run_uuid == run_uuid)
                .order_by(LlmCall.started_at)
                .all())
        if rows:
            # The last: a re-summarized run is described by its newest digest.
            return rows[-1]
        stamp = _parse_ts(
            (getattr(run, "summary", None) or {}).get("summarized_at"))
        if stamp is None:
            return None
        near = (db.session.query(LlmCall)
                .filter(LlmCall.caller == SUMMARIZER_CALLER,
                        LlmCall.run_uuid.is_(None),
                        LlmCall.finished_at >= stamp - _SUMMARY_MATCH_WINDOW,
                        LlmCall.finished_at <= stamp + _SUMMARY_MATCH_WINDOW)
                .all())
    except Exception:
        # The read model must never take the page down over telemetry.
        return None
    near = [r for r in near if r.finished_at]
    if not near:
        return None
    return min(near, key=lambda r: abs(r.finished_at - stamp))


def _summarizer_group_uuid(started_at) -> str | None:
    """The model group the summarizer resolved for a call recorded without it.

    Which model summarizes a run is settled by the group bound to the
    `assistant.run_summarizer` slot, falling back to `assistant.default` — so
    the group, not the config the call landed on, is the thing a reader
    follows the link to change.

    Only offered when neither binding in that chain has been touched since the
    call ran. A binding is current configuration, and current configuration is
    not evidence about a call made before it: pointing the reader at a group
    this call never went through would be worse than the plain model name the
    row already shows.
    """
    from agents.config import ASSISTANT_DEFAULT_UUID, ASSISTANT_RUN_SUMMARIZER_UUID
    from agents.query_filter_router import resolve_assistant_model_group

    if started_at is None:
        return None
    try:
        rows = (db.session.query(AgentModelBinding)
                .filter(AgentModelBinding.agent_uuid.in_(
                    [ASSISTANT_RUN_SUMMARIZER_UUID, ASSISTANT_DEFAULT_UUID]))
                .all())
        if any(r.updated_at and r.updated_at > started_at for r in rows):
            return None
        group_uuid, _label = resolve_assistant_model_group(
            ASSISTANT_RUN_SUMMARIZER_UUID)
    except Exception:
        # The read model must never take the page down over telemetry.
        return None
    return str(group_uuid) if group_uuid else None


def _summary_events(run) -> list[dict]:
    """The summarizer's call, and the queue wait in front of it.

    The wait is named rather than left to `_unaccounted_rows`. It is not a hole
    in the instrumentation — it is the run sitting in a queue, which is known
    and worth seeing — and an `unaccounted` bar would say the opposite.
    """
    row = _summarizer_call(run)
    if row is None or not row.started_at:
        return []
    events: list[dict] = []
    finished = getattr(run, "finished_at", None)
    if finished and row.started_at > finished:
        waited = int((row.started_at - finished).total_seconds() * 1000)
        if waited >= MIN_ACTIVITY_MS:
            events.append(_event("activity", SUMMARY_WAIT_LABEL,
                                 start=finished, duration_ms=waited))
    events.append(_event(
        "llm", SUMMARY_LABEL, variant="summary", start=row.started_at,
        duration_ms=row.total_ms, uuid=str(row.uuid),
        kpis={"model": row.model, "provider": row.provider,
              # The config the call landed on is not offered as a link. What
              # answers is a tuned override of a group member, and the bare
              # config of the same name is a page the assistant never used —
              # so the group is the link, and the config would only mislead.
              "model_uuid": None,
              "model_group_uuid": (
                  str(row.model_group_uuid) if row.model_group_uuid
                  else _summarizer_group_uuid(row.started_at)),
              "input_tokens": row.prompt_tokens,
              "output_tokens": row.completion_tokens,
              "prefill_ms": row.prefill_ms, "decode_ms": row.decode_ms,
              "cached_tokens": (row.cached_tokens_reported
                                if row.cached_tokens_reported is not None
                                else row.cached_tokens_estimated),
              "call_uuid": str(row.uuid)},
        payload={"model_response": row.response_text or "",
                 **_prompts_from_messages(row.messages)}))
    return events


def assistant_llm_calls(steps: list, reviews: list | None = None,
                        run=None) -> list[dict]:
    """The run's timeline rows: everything but the action and control records.

    A filter over `run_events`, so the page, the export, the run stats and the
    in-chat progress row cannot quote different numbers for one run. Two
    enumerations that could disagree is the bug this module exists against.

    The summarizer's call is held out too, though it is a real call: it runs
    off a queue after the reply was delivered, so counting it would put
    seconds in the run's model total that the operator never waited for — and
    on a short run those seconds can exceed the run's own wall clock. It is on
    the stream, where its cost is its own; it is not part of the turn's.
    """
    return [e for e in run_events(run, steps, reviews)
            if e["kind"] not in ("action", "control", "start")
            and e["variant"] != "summary"]


#: Timeline rows that are not LLM calls. `embedding` is a real call but a
#: different model with no tokens, counted on its own (see the docstring
#: below); the other two are not calls at all.
_SPAN_KINDS: frozenset[str] = frozenset({"embedding", "activity", "unaccounted"})


def assistant_run_stats(steps: list, reviews: list | None = None,
                        run=None) -> dict:
    """What a run has cost so far: `{calls, input_tokens, output_tokens,
    duration_ms, tps, embedding_calls, embedding_ms}`. Summed from the call
    enumeration rather than the step rows, so the inner calls are in the
    totals.

    The embedder is counted apart from the LLM totals rather than folded in:
    it produces no tokens, so its seconds in `duration_ms` would drag the
    throughput figure down against work it never did. It gets its own two
    numbers instead — visible, and not mixed into anything.

    Rejected attempts DO count. The run paid their tokens and their seconds,
    and leaving them out is what made a retried step look like a fast call
    followed by a gap where nothing ran."""
    calls = assistant_llm_calls(steps, reviews, run=run)
    embeddings = [c for c in calls if c["kind"] == "embedding"]
    # `activity` and `unaccounted` are not calls: one is an action's own
    # seconds and the other is the absence of a call, so counting either would
    # inflate the call count and its seconds.
    llm = [c for c in calls if c["kind"] == "llm"]
    in_tokens = sum((c["kpis"].get("input_tokens") or 0) for c in llm)
    out_tokens = sum((c["kpis"].get("output_tokens") or 0) for c in llm)
    llm_ms = sum((c["duration_ms"] or 0) for c in llm)
    return {
        "calls": len(llm),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "duration_ms": llm_ms,
        "tps": round((in_tokens + out_tokens) / (llm_ms / 1000)) if llm_ms else None,
        "embedding_calls": len(embeddings),
        "embedding_ms": sum((c["duration_ms"] or 0) for c in embeddings),
    }

