"""Summarizing one chunk of a room's messages with a local model.

A room's counted messages are cut into chunks (`db.due_room_chunks`). This
module turns one chunk into a prompt, and the model's reply into what is
stored: a phrase saying what was asked, in kind, and a few topic names.

The text describes requests, never answers. An answer in a summary is an old
answer posing as a fact, so the prompt forbids it. Design:
notes/housekeeping-design.md and notes/proposals/2026-11-09-room-digest.md.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

#: Bumped when the prompt or the parsing changes, so chunks written by
#: different versions can be told apart.
PROMPT_VERSION = 1

ASKED_MAX_CHARS = 120
TOPIC_MAX_CHARS = 24
MAX_TOPICS = 4
#: Topic names from earlier chunks offered for reuse.
KNOWN_TOPICS_LIMIT = 40
#: One message's share of a prompt. Longer ones are middle-cut.
PROMPT_MESSAGE_MAX_CHARS = 1500

SYSTEM_PROMPT = (
    "You index a chat log so that an assistant can later orient itself in a "
    "long conversation. You receive one chunk of a conversation between a "
    "human and an assistant, with a few messages of context before and after "
    "it. Describe only the chunk; the context is there so you can tell where "
    "a subject began or continues.\n\n"
    "Answer with exactly two lines and nothing else.\n"
    "The first line starts with \"asked:\" and says, in at most 120 "
    "characters, what kinds of things the human asked for in the chunk. "
    "Describe the kind of request, never its answer. Never state a value, a "
    "number, a date, a name or a fact that appears in an assistant reply, "
    "and never quote a reply.\n"
    "The second line starts with \"topics:\" and lists one to four topic "
    "names for the subjects of the chunk, separated by commas. A topic name "
    "is lowercase words joined by hyphens, at most 24 characters. When a "
    "subject is one of the known topic names, reuse that name exactly."
)


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
    who = {"human": "human", "agent": "assistant"}
    return "\n".join(
        f"[m{offset + k} {who.get(m['sender'], m['sender'])}] {_cut(m['text'])}"
        for k, m in enumerate(messages)) or "(none)"


def build_prompt(before: list[dict[str, Any]], chunk: list[dict[str, Any]],
                 after: list[dict[str, Any]], first_index: int,
                 known_topics: list[str]) -> dict[str, str]:
    """`first_index` is the chunk's first message counted from the room's
    first, so the labels the model sees match the page."""
    last_index = first_index + len(chunk) - 1
    user = "\n\n".join([
        "Known topic names: " + (", ".join(known_topics) if known_topics else "none yet"),
        "Context before the chunk, not to be described:\n"
        + _lines(before, first_index - len(before)),
        f"The chunk, m{first_index} to m{last_index}:\n" + _lines(chunk, first_index),
        "Context after the chunk, not to be described:\n" + _lines(after, last_index + 1),
    ])
    return {"system": SYSTEM_PROMPT, "user": user}


def slug(name: str) -> str:
    norm = unicodedata.normalize("NFKC", name).casefold()
    return "-".join(re.findall(r"[^\W_]+", norm))[:TOPIC_MAX_CHARS].rstrip("-")


def parse_reply(text: str) -> tuple[str | None, list[str], list[str]]:
    """`(asked, topics, problems)`. Lines are found by their label anywhere in
    the reply. Anything off-format is reported, never repaired silently."""
    problems: list[str] = []
    asked: str | None = None
    topics: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("*-# ").strip()
        low = line.lower()
        # A label written as markdown ("**asked:** …") leaves its closing
        # marks in front of the value.
        if low.startswith("asked:") and asked is None:
            asked = line[len("asked:"):].lstrip("*_ ").strip()
        elif low.startswith("topics:") and not topics:
            rest = line[len("topics:"):].lstrip("*_ ")
            for raw in (t for t in rest.split(",") if t.strip()):
                s = slug(raw)
                if s != raw.strip():
                    problems.append(f"topic {raw.strip()!r} normalized to {s!r}")
                if s and s not in topics:
                    topics.append(s)
    if asked is None:
        problems.append("no asked: line")
    elif len(asked) > ASKED_MAX_CHARS:
        problems.append(f"asked is {len(asked)} characters")
    if not topics:
        problems.append("no topics: line")
    elif len(topics) > MAX_TOPICS:
        problems.append(f"{len(topics)} topics")
    return asked, topics, problems


def require_asked(text: str) -> tuple[str | None, list[str], list[str]]:
    """The parser handed to the model call: a reply with no `asked:` line is
    rejected, which sends it back to the model once with the reason."""
    asked, topics, problems = parse_reply(text)
    if asked is None:
        raise ValueError(
            'The reply must have a line that starts with "asked:" and a line '
            'that starts with "topics:".')
    return asked, topics, problems
