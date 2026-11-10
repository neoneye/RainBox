"""Summarizing one chunk of a room's messages with a local model.

A room's counted messages are cut into chunks (`db.due_room_chunks`). This
module turns one chunk into a prompt, and the model's structured reply into
what is stored: the chunk split into parts, each with what was wanted, what
came of it, how the human reacted, and topic names; and one broader phrase
and broader topics over the whole chunk. How well calibrated the assistant
was in a part is then decided by rule (`calibrate`), from that reaction and
the records, and not by the model.

The model reads both sides of the conversation. Under each assistant reply
it is given what the records say about that reply: how its run ended,
whether the reply met the acceptance criteria, and how the human voted on
it. Those records are also stored with each part, as counts, so a part's
result is always shown beside what is known about its reliability. Design:
notes/housekeeping-design.md.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Bumped when the prompt, the schema or the parsing changes. A chunk belongs
#: to the series of the version that wrote it, so a new version summarizes a
#: room again beside the old series, and the two can be compared.
PROMPT_VERSION = 4

SUMMARY_MAX_CHARS = 120
RESULT_MAX_CHARS = 160
TOPIC_MAX_CHARS = 32
MAX_PARTS = 5
MAX_PART_TOPICS = 2
MAX_CHUNK_TOPICS = 3
#: A message's share of a prompt. Longer ones are middle-cut. A reply gets
#: less than a request: its opening and its close say what it delivered.
PROMPT_MESSAGE_MAX_CHARS = 1500
PROMPT_REPLY_MAX_CHARS = 600

Calibration = Literal["calibrated", "overconfident", "underconfident", "unclear"]
Reaction = Literal["moved_on", "asked_again", "corrected", "pushed_for_more",
                   "thanked", "not_shown"]


class ChunkPart(BaseModel):
    first_message: int = Field(description="Number of the part's first message.")
    last_message: int = Field(description="Number of the part's last message.")
    summary: str = Field(description="A short phrase: what was wanted in this part.")
    result: str = Field(description="A short phrase: what came of it.")
    human_reaction: Reaction = Field(
        description="What the human did after the replies in this part.")
    topics: list[str] = Field(description="Names for this part's subject.")


class ChunkSummary(BaseModel):
    """The parts come first, so the broader phrase is written with them in
    view: a breakdown read bottom-up. The human's reaction is a choice,
    because the model can read it off the messages; asked for it in its own
    words, it copied the prompt's. The model is not asked for the
    calibration: given the reaction and the records, it still called an
    answer that was repeated unchanged while the human kept asking
    "calibrated"."""

    parts: list[ChunkPart] = Field(description="The chunk's parts, in order.")
    summary: str = Field(description="A short phrase covering the whole chunk.")
    topics: list[str] = Field(description="Broader names that group the parts' subjects.")


# No sample answers and no sample topic names anywhere in this prompt: a small
# model copies them. An earlier version listed the topic names of earlier
# chunks for reuse, and the model then chose from that list whether or not a
# name fitted, and returned the list's placeholder word as a topic.
SYSTEM_PROMPT = (
    "You index one chunk of a chat between a human and an assistant, so that "
    "the conversation can be found again later and its weak spots can be "
    "seen. The chunk's messages are numbered. Under an assistant message "
    "there can be a line of signals. They are records, not opinions: how the "
    "assistant's run ended, whether its reply met the acceptance criteria it "
    "was checked against, and whether the human voted the reply up or down. "
    "A few messages before and after the chunk are given as context. They "
    "show where a subject began or continues, and they are not part of the "
    "chunk.\n\n"
    "First split the chunk into parts. A part is a run of consecutive "
    "messages about one subject. Messages that repeat a request, retry it, "
    "correct it or change only a value in it belong to the same part. A "
    "chunk that stays on one subject has one part. For each part give:\n"
    "- the numbers of its first and last message;\n"
    "- summary: what the human wanted;\n"
    "- result: what came of it, in at most twenty words: the one main thing "
    "that was found or concluded, or that nothing was. No list of details;\n"
    "- human_reaction: what the human did after the replies in this part. "
    "moved_on: went to another subject. asked_again: asked the same thing "
    "again or told it to retry. corrected: said a reply was wrong or gave "
    "the right answer. pushed_for_more: asked for more on the same subject. "
    "thanked: acknowledged it. not_shown: the chunk ends before any "
    "reaction;\n"
    "- one or two topic names.\n\n"
    "A reply voted up after a hard request is worth naming in the result. A "
    "reply voted down had something wrong with it: say what the human "
    "objected to when the messages show it.\n\n"
    "Then describe the whole chunk: a phrase that covers its parts and names "
    "their subjects, and one to three broader topic names that group the "
    "parts' subjects.\n\n"
    "Rules:\n"
    "- A phrase is at most fourteen words unless said otherwise. It does "
    "not begin by naming who spoke, and it does not announce itself.\n"
    "- Use the human's own words for the things they named. Never put a "
    "vague stand-in word where the human gave a name.\n"
    "- Take results from the messages only. Do not judge whether an answer "
    "is true from your own knowledge.\n"
    "- A topic name is lowercase words joined by hyphens. Every topic name "
    "comes from this chunk's own messages.\n"
    "- Write in the language the human writes in."
)


def signal_line(signals: dict[str, Any] | None) -> str:
    """What the records say about one assistant reply, as the prompt states
    it. Empty when nothing is recorded."""
    if not signals:
        return ""
    out = []
    run = signals.get("run")
    if run:
        out.append(f"run {run}")
    criteria = signals.get("criteria")
    if criteria == "met":
        revisions = signals.get("revisions") or 0
        out.append("criteria met" + (
            f" after {revisions} revision{'s' if revisions != 1 else ''}"
            if revisions else ""))
    elif criteria == "not met":
        out.append("criteria not met")
    vote = signals.get("vote")
    if vote:
        out.append("voted up by the human" if vote == "upvote"
                   else "voted down by the human")
        if signals.get("vote_comment"):
            out.append(f"the human's comment on the vote: {_cut(signals['vote_comment'], 200)}")
    return "; ".join(out)


def content_hash(messages: list[dict[str, Any]]) -> str:
    """Over the ids and texts of a chunk's messages, so an edit or a deletion
    since the summary was written can be seen."""
    h = hashlib.sha256()
    for m in messages:
        h.update(f"{m['id']}\x1f{m['text']}\x1e".encode())
    return h.hexdigest()


def _cut(text: str, limit: int = PROMPT_MESSAGE_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return f"{text[:half]} [..] {text[-half:]}"


def _lines(messages: list[dict[str, Any]], offset: int) -> str:
    """Numbered lines, middle-cut when long. An assistant message that has
    signals is followed by them on a line of its own."""
    out = []
    for k, m in enumerate(messages):
        if m["sender"] == "human":
            out.append(f"[m{offset + k} human] {_cut(m['text'])}")
            continue
        out.append(f"[m{offset + k} assistant] {_cut(m['text'], PROMPT_REPLY_MAX_CHARS)}")
        signals = signal_line(m.get("signals"))
        if signals:
            out.append(f"    signals for m{offset + k}: {signals}")
    return "\n".join(out) or "(none)"


def build_prompt(before: list[dict[str, Any]], chunk: list[dict[str, Any]],
                 after: list[dict[str, Any]], first_index: int) -> dict[str, str]:
    """`first_index` is the chunk's first message counted from the room's
    first, so the numbers the model sees and returns match the page."""
    last_index = first_index + len(chunk) - 1
    user = "\n\n".join([
        "Context before the chunk, not part of it:\n"
        + _lines(before, first_index - len(before)),
        f"The chunk, m{first_index} to m{last_index}:\n" + _lines(chunk, first_index),
        "Context after the chunk, not part of it:\n" + _lines(after, last_index + 1),
    ])
    return {"system": SYSTEM_PROMPT, "user": user}


def slug(name: str) -> str:
    """A topic name as it is stored: lowercase words joined by hyphens."""
    norm = unicodedata.normalize("NFKC", name).casefold()
    return "-".join(re.findall(r"[^\W_]+", norm))[:TOPIC_MAX_CHARS].rstrip("-")


def _topics(raw: list[str], limit: int, where: str,
            problems: list[str]) -> list[str]:
    out: list[str] = []
    for name in raw:
        s = slug(str(name))
        if s and s not in out:
            out.append(s)
    if not out:
        problems.append(f"{where}: no topics")
    elif len(out) > limit:
        problems.append(f"{where}: {len(out)} topics, kept {limit}")
    return out[:limit]


def _sentence(text: str, where: str, problems: list[str],
              limit: int = SUMMARY_MAX_CHARS) -> str:
    text = " ".join(str(text).split())
    if not text:
        problems.append(f"{where}: empty")
    elif len(text) > limit:
        problems.append(f"{where}: {len(text)} characters")
    return text


def _join_repeats(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neighbouring parts with the same first topic are one part. A small
    model splits a request that is repeated with another value into a part
    per repeat, whatever the prompt says. The joined part keeps the first
    one's phrase and says how many it stands for in `requests`."""
    out: list[dict[str, Any]] = []
    for part in parts:
        prev = out[-1] if out else None
        if (prev is not None and part["topics"] and prev["topics"]
                and part["topics"][0] == prev["topics"][0]
                and part["first_message"] is not None
                and prev["first_message"] is not None):
            prev["last_message"] = part["last_message"]
            prev["requests"] += 1
            # The later part is where the subject ended up, and every
            # reaction along the way is kept.
            prev["result"] = part["result"]
            prev["reactions"] += part["reactions"]
            prev["topics"] += [t for t in part["topics"]
                               if t not in prev["topics"]][:MAX_PART_TOPICS - len(prev["topics"])]
        else:
            out.append({**part, "requests": 1})
    return out


def _tile(parts: list[dict[str, Any]], last_index: int) -> list[dict[str, Any]]:
    """Each numbered part runs up to the message before the next one, and the
    last to the chunk's end. The model sees no replies, so it tends to end a
    part on the request and leave the reply that followed outside every
    part."""
    numbered = [p for p in parts if p["first_message"] is not None]
    for part, nxt in zip(numbered, numbered[1:]):
        if nxt["first_message"] > part["first_message"]:
            part["last_message"] = nxt["first_message"] - 1
    if numbered:
        numbered[-1]["last_message"] = last_index
    return parts


def part_signals(part: dict[str, Any], chunk: list[dict[str, Any]],
                 first_index: int) -> dict[str, Any]:
    """What the records say about one part, counted over its replies:
    `{replies, runs: {outcome: n}, first_run, last_run, criteria_not_met,
    revisions, up, down}`. These are facts, kept beside the model's own
    words about the part."""
    out: dict[str, Any] = {"replies": 0, "runs": {}, "first_run": None,
                           "last_run": None, "criteria_not_met": 0,
                           "revisions": 0, "up": 0, "down": 0}
    if part.get("first_message") is None:
        return out
    runs: Counter[str] = Counter()
    for k, m in enumerate(chunk):
        if m["sender"] == "human" or not (
                part["first_message"] <= first_index + k <= part["last_message"]):
            continue
        out["replies"] += 1
        signals = m.get("signals") or {}
        if signals.get("run"):
            runs[signals["run"]] += 1
            out["first_run"] = out["first_run"] or signals["run"]
            out["last_run"] = signals["run"]
        out["criteria_not_met"] += int(signals.get("criteria") == "not met")
        out["revisions"] += signals.get("revisions") or 0
        out["up"] += int(signals.get("vote") == "upvote")
        out["down"] += int(signals.get("vote") == "downvote")
    out["runs"] = dict(runs)
    return out


def calibrate(part: dict[str, Any]) -> tuple[Calibration, str]:
    """How well calibrated the assistant was in one part, and on what basis.

    A rule over what the model read (the human's reactions, how many times
    the request was made) and what the records say (runs, acceptance
    criteria, votes). The first rule that applies decides:

    1. A reply was voted down, or the human corrected one: overconfident.
    2. The human asked again, or the request was repeated:
       - the first run did not resolve it and the last did: underconfident.
         It delivered only once it was asked again.
       - a run resolved it, or a reply did not meet its criteria:
         overconfident. It answered, and the answer did not settle it.
       - otherwise unclear: it never delivered, which is a failure and not a
         matter of confidence.
    3. A reply was voted up: calibrated.
    4. No reaction is shown: unclear.
    5. The human moved on, thanked or went deeper, and the last run resolved
       the request: calibrated. If it did not: unclear.
    """
    signals = part.get("signals") or {}
    reactions = set(part.get("reactions") or [])
    runs = signals.get("runs") or {}
    first, last = signals.get("first_run"), signals.get("last_run")
    if signals.get("down"):
        return "overconfident", "a reply was voted down"
    if "corrected" in reactions:
        return "overconfident", "the human corrected a reply"
    if "asked_again" in reactions or (part.get("requests") or 1) > 1:
        if first and first != "resolved" and last == "resolved":
            return "underconfident", "it delivered only after being asked again"
        if runs.get("resolved") or signals.get("criteria_not_met"):
            return "overconfident", "it answered, and the human kept asking"
        return "unclear", "asked again, and nothing was delivered"
    if signals.get("up"):
        return "calibrated", "a reply was voted up"
    if not reactions or reactions == {"not_shown"}:
        return "unclear", "the chunk shows no reaction"
    if last == "resolved" or not runs:
        return "calibrated", "the human went on without contesting it"
    return "unclear", f"the human went on, and the last run was {last}"


def settle(reply: ChunkSummary, first_index: int, last_index: int,
           chunk: list[dict[str, Any]] | None = None,
           ) -> tuple[str, list[str], list[dict[str, Any]], list[str]]:
    """What is stored from a reply: `(summary, topics, parts, problems)`.

    Topic names are normalized, and a part's message numbers are checked
    against the chunk. Whatever was off is listed in `problems` and shown
    with the chunk. A part whose numbers lie outside the chunk keeps its text
    and loses its numbers. Two things are settled by rule and not reported:
    repeats are joined, and parts are made to cover the chunk end to end.
    Each part then gets its `signals`, counted from `chunk`'s records, and
    its `calibration`, by rule."""
    problems: list[str] = []
    parts: list[dict[str, Any]] = []
    for n, part in enumerate(reply.parts[:MAX_PARTS], start=1):
        where = f"part {n}"
        first, last = part.first_message, part.last_message
        if not (first_index <= first <= last <= last_index):
            problems.append(
                f"{where}: messages {first}-{last} are not inside the chunk")
            first = last = None
        parts.append({
            "first_message": first, "last_message": last,
            "summary": _sentence(part.summary, where, problems),
            "result": _sentence(part.result, f"{where} result", problems,
                                RESULT_MAX_CHARS),
            "reactions": [part.human_reaction],
            "topics": _topics(part.topics, MAX_PART_TOPICS, where, problems),
        })
    if len(reply.parts) > MAX_PARTS:
        problems.append(f"{len(reply.parts)} parts, kept {MAX_PARTS}")
    if not parts:
        problems.append("no parts")
    parts = _tile(_join_repeats(parts), last_index)
    for part in parts:
        part["signals"] = part_signals(part, chunk or [], first_index)
        part["calibration"], part["calibration_basis"] = calibrate(part)
    summary = _sentence(reply.summary, "chunk", problems)
    topics = _topics(reply.topics, MAX_CHUNK_TOPICS, "chunk", problems)
    return summary, topics, parts, problems
