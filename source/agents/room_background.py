"""A room's background for the personal assistant's prompt: what the room
keeps coming back to, and what happened in the stretch before the last 30
messages.

Two blocks of plain lines, both read off rows the housekeeping role has
already written (`room_chunk`, `room_topic`): nothing here calls a model or
the database. `build` is the whole of it; `db.load_room_background` reads the
rows and calls it, for a turn and for the Housekeeping page alike.

Design: notes/proposals/2026-11-21-chat-compaction.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents.room_chunks import PROGRAM, content_hash, part_grounding, restates_request
from agents.room_topics import AXES

#: The chunks before the window that `room_earlier` shows, and how many of
#: the newest are shown part by part.
EARLIER_CHUNKS = 16
NEAR_CHUNKS = 4
#: Topics with a part in the shown chunks that are listed first, and the
#: rows of `room_topics` in all.
TOPICS_RECENT = 5
TOPICS_MAX = 10
EARLIER_MAX_CHARS = 4_000
#: What the near chunks may take of `room_earlier` between them.
NEAR_MAX_CHARS = 2_400
TOPICS_MAX_CHARS = 1_200
WANTED_MAX_CHARS = 160
RESULT_MAX_CHARS = 240
SUBJECTS_MAX_CHARS = 200
STATED_MAX_CHARS = 200
#: What the user stated in the chunks the block no longer shows: how many
#: of the newest are kept under "Before this", and their characters.
SAID_BEFORE = 6
SAID_BEFORE_MAX_CHARS = 900
#: What a turn's prompt needs beside the request, the window and the static
#: head: the instructions, the steps and the reply.
PROMPT_RESERVE_CHARS = 16_000
#: Low on purpose: markup and text outside English run to more tokens per
#: character than English prose.
CHARS_PER_TOKEN = 3
#: The chunk prompt from which a part records the tools its replies used.
TOOLS_RECORDED_FROM = 19
#: A part whose runs got partway: unsettled, and what there was is shown.
PARTLY = "it was only partly delivered"
#: The diary's read, as a part's `signals.tools` names it.
DIARY_TOOL = "diary query"


@dataclass(frozen=True)
class Older:
    """The counted messages before the oldest chunk the loader read."""
    count: int
    date_from: str
    date_to: str


@dataclass(frozen=True)
class Background:
    """What a turn is given. `topics` and `earlier` are the text inside
    their blocks, `""` when there is nothing to show."""
    topics: str = ""
    earlier: str = ""
    record: dict[str, Any] = field(default_factory=dict)


def as_block(tag: str, text: str) -> str:
    """A block as a reader sees it in a prompt."""
    return f"<{tag}>\n{text}\n</{tag}>" if text else ""


def pick_series(series: list[tuple[int, int, int, int]]) -> tuple[int, int, int] | None:
    """The series a turn reads, of `(chunk_size, overlap, prompt_version,
    newest_last_message_id)`: the one that reaches furthest into the room,
    and between equals the newest prompt. A prompt change starts a series
    from the room's first message, and the room keeps the background it has
    until the new one has caught up."""
    if not series:
        return None
    best = max(series, key=lambda s: (s[3], s[2]))
    return best[0], best[1], best[2]


def shown_chunks(chunks: list[dict[str, Any]], window_first_id: int) -> list[dict[str, Any]]:
    """The chunks `room_earlier` shows, oldest first: the newest
    `EARLIER_CHUNKS` that begin before the window does. The newest of them
    can hold the window's first message, so no message is under neither."""
    before = sorted((c for c in chunks if c["first_id"] < window_first_id),
                    key=lambda c: c["first_id"])
    return before[-EARLIER_CHUNKS:]


def fits(base_chars: int, background_chars: int, context_window: int) -> bool:
    """Whether a prompt that already holds `base_chars` has room for the
    background, in a model of `context_window` tokens."""
    need = base_chars + background_chars + PROMPT_RESERVE_CHARS
    return need / CHARS_PER_TOKEN <= context_window


# --- words ---------------------------------------------------------------------

def _cut(text: Any, limit: int) -> str:
    """One line of at most `limit` characters, cut back to a whitespace
    when there is one in its last fifth."""
    line = " ".join(str(text or "").split())
    if len(line) <= limit:
        return line
    head = line[:limit - 1]
    space = head.rfind(" ")
    if space >= limit * 4 // 5:
        head = head[:space]
    return head.rstrip(" ,;:.") + "…"


def _sentence(text: str) -> str:
    return text if text.endswith((".", "…", "!", "?")) else text + "."


def _unsettled(part: dict[str, Any]) -> str | None:
    """Why a part was not settled, as the words its line ends with, or None
    when the records do not speak against it. The first that applies."""
    signals = part.get("signals") or {}
    reactions = set(part.get("reactions") or [])
    grounding = part.get("grounding") or part_grounding(signals)
    if grounding == "tool failed":
        program = (signals.get("tools") or {}).get(PROGRAM)
        return ("answered after its program failed" if program
                else "answered after its tool failed")
    if grounding == "output not used":
        return "the answer does not match what its program printed"
    if signals.get("down") or "questioned" in reactions:
        return "the answer was rejected"
    runs = signals.get("runs") or {}
    if runs and not runs.get("resolved"):
        return PARTLY if runs.get("partial") else "nothing was delivered"
    return None


def settled(part: dict[str, Any]) -> bool:
    return _unsettled(part) is None


def _corrected(part: dict[str, Any]) -> bool:
    """Whether the user corrected a reply in the part. That is not a
    rejection: the exchange went on from the correction, and where it ended
    is the part's result."""
    return "corrected" in (part.get("reactions") or [])


def _asked(part: dict[str, Any]) -> int:
    """How many times the part's request was made, when it was asked
    again; 0 when it was not."""
    if "asked_again" not in (part.get("reactions") or []):
        return 0
    return max(int(part.get("requests") or 1), 2)


def _withheld(part: dict[str, Any], *, before_invalidation: bool,
              diary_allowed: bool, tools_recorded: bool) -> str | None:
    """What stands in place of a result that is not shown, or None when it
    is shown."""
    if before_invalidation:
        return "answered then, before stored facts were declared stale"
    if diary_allowed:
        return None
    if not tools_recorded:
        return "answered then"
    tools = (part.get("signals") or {}).get("tools") or {}
    return "answered from the diary" if DIARY_TOOL in tools else None


def how_it_went(part: dict[str, Any], *, before_invalidation: bool = False,
                diary_allowed: bool = True, tools_recorded: bool = True) -> str:
    """How a part went, in words, from its records. A part the records
    speak against says so and gives no result, but for one that was partly
    delivered, which says what there was. A reply the user corrected is
    said to be corrected, with where the exchange ended. A result is `came
    of it` only where a tool ran for it; otherwise it is what was answered
    then."""
    why = _unsettled(part)
    if why and why != PARTLY:
        return _sentence(why)
    asked = _asked(part)
    corrected = _corrected(part)
    lead = (_sentence(why) + " " if why
            else "the user corrected the answer. " if corrected
            else f"asked {asked} times. " if asked else "")
    held = _withheld(part, before_invalidation=before_invalidation,
                     diary_allowed=diary_allowed, tools_recorded=tools_recorded)
    if held:
        return lead + _sentence(held)
    result = _cut(part.get("result"), RESULT_MAX_CHARS)
    if not result:
        return lead.rstrip()
    signals = part.get("signals") or {}
    grounding = part.get("grounding") or part_grounding(signals)
    label = ("what there was" if why else "after that" if corrected
             else "the last answer" if asked
             else "came of it" if grounding == "tool" else "answered then")
    return f"{lead}{label}: {_sentence(result)}"


# --- room_earlier ----------------------------------------------------------------

def _date(at: Any) -> str:
    return at.strftime("%Y-%m-%d") if isinstance(at, datetime) else str(at or "")[:10]


def _dates(messages: list[dict[str, Any]]) -> str:
    first, last = _date(messages[0]["created_at"]), _date(messages[-1]["created_at"])
    return first if first == last else f"{first} to {last}"


def _said(chunk: dict[str, Any]) -> list[str]:
    """What the user stated in the chunk, each cut to a line. One that
    repeats a request of the chunk is left out, as it is when a chunk is
    stored: the part's own line says it."""
    parts = chunk.get("parts")
    return [s for s in (_cut(t, STATED_MAX_CHARS) for t in chunk.get("statements") or []
                        if not restates_request(str(t or ""), parts)) if s]


def _far_line(dates: str, chunk: dict[str, Any]) -> str:
    out = [_cut(chunk.get("summary"), SUBJECTS_MAX_CHARS)]
    out += [f"the user said: {_sentence(s)}".rstrip(".") for s in _said(chunk)]
    for part in chunk.get("parts") or []:
        wanted = _cut(part.get("summary"), WANTED_MAX_CHARS)
        if not wanted:
            continue
        why = _unsettled(part)
        if why:
            out.append(f"not settled: {wanted} ({why})")
        elif _corrected(part):
            out.append(f"corrected by the user: {wanted}")
        elif _asked(part):
            out.append(f"asked {_asked(part)} times: {wanted}")
    return f"{dates}: " + "; ".join(o for o in out if o)


def _near_lines(dates: str, chunk: dict[str, Any], **rules: Any) -> tuple[str, int]:
    """A near chunk's lines, and how many results its rules withheld."""
    lines, held = [f"{dates}:"], 0
    lines += [f"- the user said: {_sentence(s)}" for s in _said(chunk)]
    for part in chunk.get("parts") or []:
        wanted = _cut(part.get("summary"), WANTED_MAX_CHARS)
        if not wanted:
            continue
        went = how_it_went(part, **rules)
        if settled(part) and part.get("result") and _withheld(part, **rules):
            held += 1
        lines.append(f"- wanted: {_sentence(wanted)} {went}".rstrip())
    return "\n".join(lines), held


def _gap(messages: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(messages)
    return {"kind": "gap", "messages": messages,
            "text": f"{_dates(messages)}: {n} message{'' if n == 1 else 's'} not summarized."}


def render_earlier(chunks: list[dict[str, Any]], counted: list[dict[str, Any]],
                   window_first_id: int, *, older: Older | None = None,
                   facts_invalidated_id: int | None = None,
                   diary_allowed: bool = True,
                   said_before: list[str] | None = None,
                   limit: int = EARLIER_MAX_CHARS) -> tuple[str, dict[str, Any]]:
    """The text of `room_earlier`, and what was done to make it: `{chunks,
    near, trimmed, results_withheld, said_before}`.

    What the user stated outlasts what they asked for: the statements of a
    chunk that gave way to the limit, and `said_before` (those of the chunks
    before the shown ones, oldest first), stay under "Before this", the
    newest `SAID_BEFORE` of them.

    `chunks` are the shown chunks, oldest first, each `{uuid, first_id,
    last_id, content_hash, summary, parts, statements, prompt_version}`;
    `counted` the
    room's counted messages from the oldest of them on, `{id, text,
    created_at}`; `older` what lies before those."""
    record = {"chunks": 0, "near": 0, "trimmed": 0, "results_withheld": 0,
              "said_before": 0}
    if not chunks:
        return "", record
    entries: list[dict[str, Any]] = []
    after = chunks[0]["first_id"] - 1
    for chunk in chunks:
        between = [m for m in counted if after < m["id"] < chunk["first_id"]]
        if between:
            entries.append(_gap(between))
        after = chunk["last_id"]
        inside = [m for m in counted if chunk["first_id"] <= m["id"] <= chunk["last_id"]]
        if not inside:
            continue
        changed = content_hash(inside) != chunk.get("content_hash")
        entries.append({"kind": "changed" if changed else "chunk", "chunk": chunk,
                        "messages": inside, "dates": _dates(inside)})
    tail = [m for m in counted if after < m["id"] < window_first_id]
    if tail:
        entries.append(_gap(tail))

    near = [e for e in entries if e["kind"] == "chunk"][-NEAR_CHUNKS:]
    for entry in entries:
        if entry["kind"] == "gap":
            continue
        if entry["kind"] == "changed":
            entry["text"] = f"{entry['dates']}: (changed since it was summarized)"
            continue
        chunk = entry["chunk"]
        entry["far"] = _far_line(entry["dates"], chunk)
        if any(entry is n for n in near):
            entry["near"], entry["held"] = _near_lines(
                entry["dates"], chunk,
                before_invalidation=(facts_invalidated_id is not None
                                     and chunk["last_id"] < facts_invalidated_id),
                diary_allowed=diary_allowed,
                tools_recorded=int(chunk.get("prompt_version") or 0) >= TOOLS_RECORDED_FROM)

    # The near chunks give way to the far lines, the oldest first.
    for entry in near:
        if sum(len(e["near"]) for e in near if "near" in e) <= NEAR_MAX_CHARS:
            break
        del entry["near"]
    for entry in entries:
        if entry["kind"] == "chunk":
            entry["text"] = entry.get("near") or entry["far"]

    def before_line(dropped: list[dict[str, Any]]) -> str:
        messages = [m for e in dropped for m in e["messages"]]
        count = (older.count if older else 0) + len(messages)
        if not count:
            return ""
        first = older.date_from if older and older.count else _date(messages[0]["created_at"])
        last = (_date(messages[-1]["created_at"]) if messages
                else older.date_to if older else "")
        span = f"on {first}" if first == last else f"from {first} to {last}"
        line = f"Before this: {count} message{'' if count == 1 else 's'} {span}."
        said = said_then(dropped)
        if said:
            line += " The user said in them:\n" + "\n".join(f"- {_sentence(s)}" for s in said)
        return line

    def said_then(dropped: list[dict[str, Any]]) -> list[str]:
        said = [s for s in (_cut(t, STATED_MAX_CHARS) for t in said_before or []) if s]
        said += [s for e in dropped if e["kind"] == "chunk" for s in _said(e["chunk"])]
        said = list(dict.fromkeys(said))[-SAID_BEFORE:]
        while said and sum(len(s) + 3 for s in said) > SAID_BEFORE_MAX_CHARS:
            said.pop(0)
        return said

    def text_of(dropped: list[dict[str, Any]], kept: list[dict[str, Any]]) -> str:
        return "\n".join(filter(None, [before_line(dropped), *(e["text"] for e in kept)]))

    dropped: list[dict[str, Any]] = []
    while len(entries) > 1 and len(text_of(dropped, entries)) > limit:
        dropped.append(entries.pop(0))
    record.update(
        chunks=sum(e["kind"] != "gap" for e in entries),
        near=sum("near" in e and e["text"] is e["near"] for e in entries),
        trimmed=len(dropped),
        said_before=len(said_then(dropped)) if before_line(dropped) else 0,
        results_withheld=sum(e.get("held", 0) for e in entries if e.get("near")
                             and e["text"] is e["near"]))
    return text_of(dropped, entries), record


# --- room_topics -----------------------------------------------------------------

def _without_doubles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows without the ones that count parts another row already
    counts. A kind of task and a named thing are often one subject seen
    twice ("calculate square root", "sqrt"), and the topics of one axis
    overlap where the registry has not joined them. A row whose parts are
    all in a larger row's goes; of two with the same parts, the first."""
    keys = [{(m.chunk_uuid, m.part_index) for m in row["members"]} for row in rows]
    out = []
    for i, row in enumerate(rows):
        doubled = any(j != i and keys[i] <= keys[j]
                      and (len(keys[i]) < len(keys[j]) or j < i)
                      for j in range(len(rows)))
        if not doubled:
            out.append(row)
    return out


def pick_topics(tallied: dict[str, Any], shown_uuids: set[Any]) -> list[dict[str, Any]]:
    """The rows of `room_topics`: the topics with a part in the shown
    chunks first, newest first, at most `TOPICS_RECENT`; then the largest
    of the rest, up to `TOPICS_MAX` in all. Each row gains `recent`.
    Ranked by size alone, a room's first months would hold every row."""
    # Named things first: of two rows over the same parts, the name stays.
    rows = [dict(row) for axis in reversed(AXES) for row in tallied["axes"].get(axis, [])]
    rows = _without_doubles(rows)
    for row in rows:
        row["recent"] = any(m.chunk_uuid in shown_uuids for m in row["members"])
    recent = sorted((r for r in rows if r["recent"]),
                    key=lambda r: (r.get("last") or "", r["messages"]),
                    reverse=True)[:TOPICS_RECENT]
    listed = {id(r) for r in recent}
    rest = sorted((r for r in rows if id(r) not in listed),
                  key=lambda r: (-r["messages"], -r["parts"]))
    return (recent + rest)[:TOPICS_MAX]


def render_topics(rows: list[dict[str, Any]], *, limit: int = TOPICS_MAX_CHARS) -> str:
    """The text of `room_topics`: a line per topic with its name, what it
    is, and how many times it came up. A date only where it says the topic
    is old: a topic of the shown chunks may be in the window right now."""
    lines = []
    for row in rows:
        topic = row["topic"]
        name = " ".join(str(topic.name or "").split())
        about = " ".join(str(topic.explanation or "").split())
        head = f"{name}: {about}" if about and about.casefold() != name.casefold() else name
        n = row["parts"]
        tail = f"{n} time{'' if n == 1 else 's'}"
        if not row.get("recent") and row.get("last"):
            tail += f", last summarized {row['last']}"
        lines.append(f"- {head} ({tail})")
    while lines and len("\n".join(lines)) > limit:
        lines.pop()
    return "\n".join(lines)


# --- both ------------------------------------------------------------------------

def build(chunks: list[dict[str, Any]], counted: list[dict[str, Any]],
          window_first_id: int | None, tallied: dict[str, Any] | None, *,
          older: Older | None = None, facts_invalidated_id: int | None = None,
          diary_allowed: bool = True,
          said_before: list[str] | None = None) -> Background:
    """A room's background, from the chunks of one series and the tally of
    its topics. Nothing when the room has no chunk before its window: a
    block that only said so would be noise."""
    if window_first_id is None:
        return Background()
    shown = shown_chunks(chunks, window_first_id)
    if not shown:
        return Background()
    first = shown[0]["first_id"]
    earlier, record = render_earlier(
        shown, [m for m in counted if m["id"] >= first], window_first_id,
        older=older, facts_invalidated_id=facts_invalidated_id,
        diary_allowed=diary_allowed, said_before=said_before)
    topics = ""
    if tallied:
        topics = render_topics(pick_topics(tallied, {c["uuid"] for c in shown}))
    record.update(earlier_chars=len(earlier), topics_chars=len(topics))
    return Background(topics=topics, earlier=earlier, record=record)
