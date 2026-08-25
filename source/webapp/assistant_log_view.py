"""The /assistant log: a run's events laid out as a gantt over a split view.

One event stream, rendered twice. The gantt is for spotting an anomaly by its
width; the list is for reading down what happened; both are the same events, so
a kind added to `db.assistant_log` appears in both without either learning
about it.

Placement is the same arithmetic the waterfall has always used: an offset and a
width as percentages of the run's span. An event with no duration — an action,
whose wall-clock is already on the stream as the phases it ran — gets a row and
no bar.
"""

from __future__ import annotations

from datetime import timedelta

import db
from webapp.assistant_components import (
    EVENT_GLYPH,
    event_description,
    render_event_detail,
)

#: A bar this narrow would be invisible; a sub-second call against a
#: two-minute run still has to be clickable.
_MIN_BAR_PCT: float = 0.6


def _end_of(event: dict):
    if not event["start"]:
        return None
    return event["start"] + timedelta(milliseconds=event["duration_ms"] or 0)


#: Characters a key may carry into an HTML attribute. A label can be an action
#: name that arrived as data, so everything else is dropped rather than
#: escaped — a key is matched, never read.
_KEY_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:+")


def _row_key(event: dict) -> str:
    """A name for a row that does not move when the run grows.

    Position cannot be it. A live run gains events while it is being read, and
    one with an earlier start slides every row below it down — so a reader
    inspecting a step would be handed a different one on the next refresh.

    What identifies a row is what it IS: its kind, the step or call it belongs
    to, what it is called, and when it began. All four are needed. A step is a
    model call AND the action that call chose, and both carry the step's uuid;
    two embeds in one step share a label and differ only by their start.
    """
    start = event.get("start")
    parts = (event.get("kind") or "",
             str(event.get("uuid") or event.get("anchor") or ""),
             event.get("label") or "",
             start.isoformat() if start else "")
    return "".join(c if c in _KEY_SAFE else "-" for c in ":".join(parts))


#: Which row a `#step-<uuid>` link resolves to, best first. That format is
#: minted by `db.assistant_step_path` and linked from six places outside this
#: page — chat proposal cards, cron provenance, the second-opinion view, the
#: uuid lookup — so it has to keep landing somewhere, and one step uuid is
#: several rows. The action wins because every one of those links means "the
#: step that did this", and the action is the row carrying the arguments, the
#: result and the writes it proposed.
#: Matched on (kind, variant); an empty variant matches any. The kept call is
#: named by variant because a retried step's thrown-away attempts are `llm`
#: rows on the same step and they ran first — a published link landing on an
#: answer the run discarded is worse than one landing nowhere.
_PRIMARY_ROW_ORDER: tuple[tuple[str, str], ...] = (
    ("action", ""), ("skipped", ""), ("llm", "decide"), ("llm", "code-driven"),
    ("llm", ""),
)


def _mark_primary_rows(rows: list[dict]) -> None:
    """Name the one row each step's published link resolves to.

    Exactly one, or a link lands somewhere different depending on which row
    the lookup reaches first.
    """
    by_step: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("anchor"):
            by_step.setdefault(str(row["anchor"]), []).append(row)
    for step_uuid, owned in by_step.items():
        for kind, variant in _PRIMARY_ROW_ORDER:
            match = [r for r in owned if r["kind"] == kind
                     and (not variant or r["variant"] == variant)]
            if match:
                match[0]["primary_for"] = step_uuid
                break


def log_view(run, steps: list, reviews: list | None = None,
             trigger: dict | None = None, intents: list | None = None,
             active: dict | None = None) -> dict:
    """`{"events": [...], "span_seconds": float}` for the page.

    Each event gains what the two surfaces need to draw it: `offset_pct` and
    `width_pct` (None when it has no span), `seconds`, `glyph`, `row_id`,
    `key` (its identity across a live refresh, see `_row_key`), and
    the rendered `detail_html` its component produced. The detail is built
    here, once per event, so selecting a row is a client-side swap rather than
    a round trip.

    `trigger` is the chat message that began the run; given one, the stream
    opens with the request it carried. `intents` are the run's write intents,
    which ride on the action that proposed each of them. `active` is the model
    call in flight, which has no record behind it yet.
    """
    events = db.run_events(run, steps, reviews, trigger=trigger,
                           intents=intents, active=active)
    if not events:
        return {"events": [], "span_seconds": 0.0}

    starts = [e["start"] for e in events if e["start"]]
    if not starts:
        return {"events": [], "span_seconds": 0.0}
    run_started = getattr(run, "started_at", None)
    run_finished = getattr(run, "finished_at", None)
    first = min(starts + ([run_started] if run_started else []))
    ends = [e for e in (_end_of(x) for x in events) if e]
    last = max(ends + ([run_finished] if run_finished else []))
    span = (last - first).total_seconds()

    rows = []
    for index, event in enumerate(events):
        row = dict(event)
        row["row_id"] = f"ev-{index}"
        # What survives a live refresh. `row_id` addresses the pane in the
        # document it was rendered into; `key` addresses the same event in the
        # next one.
        row["key"] = _row_key(event)
        row["glyph"] = EVENT_GLYPH.get(event["kind"], "·")
        if event["start"] and span > 0:
            offset = (event["start"] - first).total_seconds()
            row["offset_pct"] = round(offset / span * 100, 3)
            if event["duration_ms"]:
                width = event["duration_ms"] / 1000
                row["width_pct"] = round(
                    max(width / span * 100, _MIN_BAR_PCT), 3)
            else:
                # An action carries no span by design; it is a record of what
                # was asked and what came back, not a stretch of wall-clock.
                row["width_pct"] = None
        else:
            row["offset_pct"] = None
            row["width_pct"] = None
        row["seconds"] = (f"{event['duration_ms'] / 1000:.1f}s"
                          if event["duration_ms"] is not None else "—")
        row["clock"] = (event["start"].strftime("%H:%M:%S")
                        if event["start"] else "—")
        # The header names what is being inspected, so the row carries the
        # description the header shows for whichever event is selected.
        row["description"] = event_description(event)
        row["detail_html"] = render_event_detail(event)
        row["primary_for"] = ""
        rows.append(row)
    _mark_primary_rows(rows)
    return {"events": rows, "span_seconds": span}
