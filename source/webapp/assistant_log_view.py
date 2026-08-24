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


def log_view(run, steps: list, reviews: list | None = None,
             trigger: dict | None = None) -> dict:
    """`{"events": [...], "span_seconds": float}` for the page.

    Each event gains what the two surfaces need to draw it: `offset_pct` and
    `width_pct` (None when it has no span), `seconds`, `glyph`, `row_id`, and
    the rendered `detail_html` its component produced. The detail is built
    here, once per event, so selecting a row is a client-side swap rather than
    a round trip.

    `trigger` is the chat message that began the run; given one, the stream
    opens with the request it carried.
    """
    events = db.run_events(run, steps, reviews, trigger=trigger)
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
        rows.append(row)
    return {"events": rows, "span_seconds": span}
