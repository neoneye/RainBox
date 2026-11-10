"""Summarizing one chunk of a room's messages with a local model.

A room's counted messages are cut into chunks (`db.due_room_chunks`). This
module turns one chunk into a prompt, and the model's structured reply into
what is stored: the chunk split into parts, each with a phrase and topic
names, and one broader phrase and broader topics over the whole chunk.

The text says what was asked and what it was about, never what the assistant
answered: the prompt holds the human's messages only. Design:
notes/housekeeping-design.md and notes/proposals/2026-11-09-room-digest.md.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field

#: Bumped when the prompt, the schema or the parsing changes. A chunk belongs
#: to the series of the version that wrote it, so a new version summarizes a
#: room again beside the old series, and the two can be compared.
PROMPT_VERSION = 2

SUMMARY_MAX_CHARS = 120
TOPIC_MAX_CHARS = 32
MAX_PARTS = 5
MAX_PART_TOPICS = 2
MAX_CHUNK_TOPICS = 3
#: One message's share of a prompt. Longer ones are middle-cut.
PROMPT_MESSAGE_MAX_CHARS = 1500


class ChunkPart(BaseModel):
    first_message: int = Field(description="Number of the part's first message.")
    last_message: int = Field(description="Number of the part's last message.")
    summary: str = Field(description="A short phrase: what was wanted in this part.")
    topics: list[str] = Field(description="Names for this part's subject.")


class ChunkSummary(BaseModel):
    """The parts come first, so the broader phrase is written with them in
    view: a breakdown read bottom-up."""

    parts: list[ChunkPart] = Field(description="The chunk's parts, in order.")
    summary: str = Field(description="A short phrase covering the whole chunk.")
    topics: list[str] = Field(description="Broader names that group the parts' subjects.")


# No sample answers and no sample topic names anywhere in this prompt: a small
# model copies them. An earlier version listed the topic names of earlier
# chunks for reuse, and the model then chose from that list whether or not a
# name fitted, and returned the list's placeholder word as a topic.
SYSTEM_PROMPT = (
    "You index one chunk of a chat between a human and an assistant, so that "
    "the conversation can be found again later. The chunk's messages are "
    "numbered. Only the human's messages are shown in full. Where the "
    "assistant replied, the reply is left out. A few messages before and "
    "after the chunk are given as context. They show where a subject began "
    "or continues, and they are not part of the chunk.\n\n"
    "First split the chunk into parts. A part is a run of consecutive "
    "messages about one subject. Messages that repeat a request, retry it, "
    "correct it or change only a value in it belong to the same part. A "
    "chunk that stays on one subject has one part. For each part give the "
    "numbers of its first and last message, a phrase for what was wanted, "
    "and one or two topic names.\n\n"
    "Then describe the whole chunk: a phrase that covers its parts and names "
    "their subjects, and one to three broader topic names that group the "
    "parts' subjects.\n\n"
    "Rules:\n"
    "- A phrase names what was wanted, in at most twelve words. It is the "
    "request itself in short form. It mentions neither the human nor the "
    "assistant, and it does not begin by announcing itself.\n"
    "- Use the human's own words for the things they named. Never put a "
    "vague stand-in word where the human gave a name.\n"
    "- A topic name is lowercase words joined by hyphens. Every topic name "
    "comes from this chunk's own messages.\n"
    "- Write in the language the human writes in."
)

#: What stands in a prompt where the assistant replied. The model never sees
#: a reply, so no summary can repeat one: an old answer in a summary would
#: reach a later turn posing as a fact.
REPLY_PLACEHOLDER = "(reply left out)"


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
    """Numbered lines. A human message is shown, middle-cut when long. Any
    other message keeps its number and its place, and none of its text."""
    out = []
    for k, m in enumerate(messages):
        if m["sender"] == "human":
            out.append(f"[m{offset + k} human] {_cut(m['text'])}")
        else:
            out.append(f"[m{offset + k} assistant] {REPLY_PLACEHOLDER}")
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


def _sentence(text: str, where: str, problems: list[str]) -> str:
    text = " ".join(str(text).split())
    if not text:
        problems.append(f"{where}: empty summary")
    elif len(text) > SUMMARY_MAX_CHARS:
        problems.append(f"{where}: summary is {len(text)} characters")
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


def settle(reply: ChunkSummary, first_index: int,
           last_index: int) -> tuple[str, list[str], list[dict[str, Any]], list[str]]:
    """What is stored from a reply: `(summary, topics, parts, problems)`.

    Topic names are normalized, and a part's message numbers are checked
    against the chunk. Whatever was off is listed in `problems` and shown
    with the chunk. A part whose numbers lie outside the chunk keeps its text
    and loses its numbers. Two things are settled by rule and not reported:
    repeats are joined, and parts are made to cover the chunk end to end."""
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
            "topics": _topics(part.topics, MAX_PART_TOPICS, where, problems),
        })
    if len(reply.parts) > MAX_PARTS:
        problems.append(f"{len(reply.parts)} parts, kept {MAX_PARTS}")
    if not parts:
        problems.append("no parts")
    parts = _tile(_join_repeats(parts), last_index)
    summary = _sentence(reply.summary, "chunk", problems)
    topics = _topics(reply.topics, MAX_CHUNK_TOPICS, "chunk", problems)
    return summary, topics, parts, problems
