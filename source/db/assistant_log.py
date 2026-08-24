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
from db.models import AssistantStep, LlmCall, db


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


#: How much of an embedded text the waterfall's name column carries. The
#: column is 14rem wide; past this the row ellipsises either way, and the
#: tooltip has the rest.
EMBED_LABEL_CHARS: int = 40


def embed_call_label(call: dict) -> tuple[str, str]:
    """An embed row's label and its tooltip detail: WHAT went to the embedder.

    The model is deliberately not in either — a run embeds on one model, so
    naming it per row is the same string repeated down the column; it is named
    once per step, in the timing block's embedder line. The text IS worth
    repeating, because it is the thing that differs: without it a column of
    identical `embed` rows could equally be one query embedded repeatedly,
    several different queries, or one call drawn more than once.

    A batched call — a first-run seed populate embeds the whole registry — is
    named by its size instead: its first chunk says nothing about the call."""
    preview = [str(t) for t in (call.get("preview") or [])]
    texts = call.get("texts") or 0
    if texts > 1:
        label = f"embed {texts} texts"
    elif preview:
        head = preview[0][:EMBED_LABEL_CHARS]
        label = f'embed "{head}{"…" if len(preview[0]) > EMBED_LABEL_CHARS else ""}"'
    else:
        # A payload written before the text was captured, or a call that
        # embedded nothing. Still a call, still worth its row.
        label = "embed"
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
            start=_end_of({"start": step_started_at(step),
                           "duration_ms": step.duration_ms}),
            duration_ms=None, anchor=str(step.uuid), uuid=str(step.uuid),
            kpis={"status": "error" if getattr(step, "error", None) else "ok"},
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
    return out


#: How far an llm_call row's start may sit from an event's before the two are
#: not the same call. Generous: the row is stamped when the request goes out
#: and the event from the step's own `requested_at`, which can differ by the
#: time it takes to build a prompt.
_LLM_CALL_MATCH_TOLERANCE = timedelta(seconds=5)


#: Ties at one instant: the run's opening event leads, an action trails the
#: call that chose it.
_ORDER_AT_SAME_INSTANT: dict[str, int] = {"start": 0, "action": 2}


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


def assistant_llm_calls(steps: list, reviews: list | None = None,
                        run=None) -> list[dict]:
    """The run's timeline rows: everything but the action and control records.

    A filter over `run_events`, so the page, the export, the run stats and the
    in-chat progress row cannot quote different numbers for one run. Two
    enumerations that could disagree is the bug this module exists against.
    """
    return [e for e in run_events(run, steps, reviews)
            if e["kind"] not in ("action", "control", "start")]


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

