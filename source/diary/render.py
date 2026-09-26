"""Render diary items into one bounded observation (proposal §8).

The observation is a status line, the code-owned `diary_passages` fence
around the excerpts, and a continuation footer. Packing is exact: the
finished text, fence and footer included, never exceeds the budget. Items
are whole excerpts; one that does not fit is left for the cursor, never cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from diary.retrieval import DiaryItem, DiaryResult

# The newest event's own overhead (action, args, a 300-character reason, the
# 120 allowance) plus one compacted predecessor with the same overhead.
DIARY_SCRATCHPAD_RESERVE = 1500
COMPACT_OBSERVATION_MAX_CHARS = 400
PATH_LABEL_MAX = 120
_CURSOR_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def observation_budget() -> int:
    """DIARY_OBSERVATION_CHARS: derived from the assistant's scratchpad so a
    compacted search plus a whole read fit the deciding prompt together."""
    from agents.assistant import AssistantAgent

    return AssistantAgent.MAX_SCRATCHPAD_CHARS - DIARY_SCRATCHPAD_RESERVE


@dataclass
class Rendered:
    text: str
    compact: str
    rendered: list[DiaryItem]
    first_unrendered: int | None     # index into result.items, or None
    has_more: bool


def _clock(t: time) -> str:
    return f"{t.hour:02d}h{t.minute:02d}"


def date_label(item: DiaryItem) -> str:
    if item.superseded:
        return "superseded snapshot, no date labels"
    if item.date_local is None:
        label = "undated"
    else:
        label = item.date_local.isoformat()
        if item.date_basis == "filename":
            label += " (date from filename)"
    if item.clock_start is not None:
        label += " " + _clock(item.clock_start)
        if item.clock_end is not None:
            label += "–" + _clock(item.clock_end)
    if item.time_status == "ambiguous":
        label += " (DST-ambiguous local time)"
    elif item.time_status == "invalid_range":
        label += " (range as written; end before start)"
    if item.author_token:
        label += f" · author {item.author_token}"
    return label


def _path_label(path: str) -> str:
    if len(path) <= PATH_LABEL_MAX:
        return path
    return "…" + path[-(PATH_LABEL_MAX - 1):]


def _snapshot(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%MZ") if at else "unknown"


def _block(item: DiaryItem) -> str:
    text = item.text if item.text.endswith("\n") else item.text + "\n"
    matches = f" · {item.match_count} matches" if item.match_count > 1 else ""
    out = f"--- {_path_label(item.path)} · {date_label(item)}{matches} · {item.citation}\n{text}"
    if item.occurrence_dates or item.occurrence_more:
        dates = ", ".join(d.isoformat() for d in item.occurrence_dates)
        more = f" (+{item.occurrence_more} more)" if item.occurrence_more else ""
        out += f"(same text also on {dates}{more})\n"
    return out


def _status(result: DiaryResult, n: int, has_more: bool) -> str:
    mode = result.mode
    if n == 0 and not has_more:
        empty = {"literal": "no occurrences", "timeline": "no entries in that range",
                 "read": "nothing to read at that citation"}.get(mode, "no matching passages")
        line = f"diary_query {mode}: {empty}"
        if mode in ("search", "literal"):
            line += " (not proof that none exist)"
    else:
        line = f"diary_query {mode}: {n} excerpt{'s' if n != 1 else ''}"
        if has_more:
            line += ", more available"
    notes = []
    if result.degraded_routes:
        notes.append("degraded: " + ", ".join(result.degraded_routes))
    if result.metadata == "partial":
        notes.append("some files are quarantined, undated or have parse diagnostics")
    if notes:
        line += " (" + "; ".join(notes) + ")"
    return line + "."


def _footer(omitted: int, cursor: str) -> str:
    return (f'[{omitted} more not shown; continue with {{"mode": "continue", '
            f'"cursor": "{cursor}"}}]')


def _assemble(result: DiaryResult, blocks: list[str], n: int, omitted: int, has_more: bool) -> str:
    from memory.retrieval import fence_diary_passages

    status = _status(result, n, has_more)
    if not blocks:
        body = None
    else:
        body = "".join(blocks).rstrip("\n")
    text = status
    if body:
        text += "\n" + fence_diary_passages(body)
    if has_more:
        text += "\n" + _footer(omitted, _CURSOR_PLACEHOLDER)
    return text


def render_items(result: DiaryResult, budget: int) -> Rendered:
    """Pack whole items in order until the next would overflow `budget`."""
    items = result.items
    more_after = result.after_last is not None
    blocks: list[str] = []
    last_source = None
    k = 0
    text = _assemble(result, [], 0, 0, bool(items) or more_after)
    for k in range(len(items) + 1):
        if k == len(items):
            break
        item = items[k]
        block = ""
        if item.source_uuid != last_source:
            block += f"source {item.source_name} · snapshot {_snapshot(item.snapshot_at)}\n"
        block += _block(item)
        has_more = (k + 1 < len(items)) or more_after
        omitted = len(items) - (k + 1)
        candidate = _assemble(result, blocks + [block], k + 1, omitted, has_more)
        if len(candidate) > budget:
            break
        blocks.append(block)
        last_source = item.source_uuid
        text = candidate
    else:
        k = len(items)
    n = len(blocks)
    if n == 0 and items:
        # The first item alone does not fit: name it by citation and move the
        # continuation past it, so paging always makes progress.
        first = items[0]
        note = [f"--- excerpt too large for this observation: {first.citation}\n"]
        has_more = len(items) > 1 or more_after
        text = _assemble(result, note, 0, len(items) - 1, has_more)
        return Rendered(text=text, compact=compact_form(result.mode, []), rendered=[],
                        first_unrendered=1 if len(items) > 1 else None, has_more=has_more)
    has_more = n < len(items) or more_after
    text = _assemble(result, blocks, n, len(items) - n, has_more)
    rendered = items[:n]
    return Rendered(text=text, compact=compact_form(result.mode, rendered),
                    rendered=rendered, first_unrendered=n if n < len(items) else None,
                    has_more=has_more)


def with_cursor(text: str, cursor: str) -> str:
    return text.replace(_CURSOR_PLACEHOLDER, cursor)


def compact_form(mode: str, rendered: list[DiaryItem]) -> str:
    """The code-built stand-in kept when this observation is evicted from
    the scratchpad: mode, count, citations, and how to reopen them."""
    head = (f"diary_query {mode}: {len(rendered)} excerpt{'s' if len(rendered) != 1 else ''} "
            "shown earlier; text no longer in context. Reopen with "
            '{"mode": "read", "citation": ...}:')
    cites = [it.citation for it in rendered]
    out = head
    for c in cites:
        if len(out) + len(c) + 2 > COMPACT_OBSERVATION_MAX_CHARS:
            out += " …"
            break
        out += " " + c
    return out[:COMPACT_OBSERVATION_MAX_CHARS]


def item_json(item: DiaryItem) -> dict[str, Any]:
    return {"citation": item.citation, "source_uuid": str(item.source_uuid), "path": item.path,
            "passage_uuid": str(item.passage_uuid) if item.passage_uuid else None,
            "date_local": item.date_local.isoformat() if isinstance(item.date_local, date) else None,
            "time_status": item.time_status, "reasons": item.reasons}
