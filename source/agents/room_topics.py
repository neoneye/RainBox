"""A room's recurring topics: which topic a part of a chunk belongs to, what
a topic is called, and the block a prompt would hold of them.

A chunk's summary names its topics with no list of earlier names to choose
from, so the same subject has another name in every chunk. This module is
the step that recognizes them as one. A part is compared with the parts a
topic already has, by the decision model, on two axes: whether the two
requests ask for the same kind of task, and whether they are about the same
named thing. It joins the best topic on each axis, or starts one.

Everything here is a rule or a wording. The housekeeping role makes the
calls (`agents/housekeeping.py`), and `db/room_topics.py` keeps the rows.
Design: docs/superpowers/specs/2026-11-18-room-topic-registry-design.md.
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

from llm.systemone import Question

#: The axes a part has a topic on, and the question each is scored by.
AXES: tuple[str, ...] = ("task", "thing")

#: Bumped when a question's wording changes.
QUESTION_SET_VERSION = 1

#: Asked of two requests, as `pair_state` writes them: the phrase of a part
#: that is in a topic already, and the phrase of the part to place. No
#: sample requests: the model scores the state against these words.
QUESTIONS: dict[str, Question] = {
    "task": Question(
        name="task", kind="noul",
        instructions="Do request1 and request2 ask the assistant for the same kind of task?"),
    "thing": Question(
        name="thing", kind="noul",
        instructions="Are request1 and request2 about the same named thing?"),
}

#: The members of a topic a new part is compared with: its first ones. Two
#: gave the same topics as three, on one room's 56 parts.
EXEMPLARS = 2
#: The topics per axis a part is scored against: the nearest by embedding.
#: Five gave the same recurring topics as all of them, but one.
CANDIDATES = 5
#: A part joins the best topic whose mean score is at least this.
JOIN_AT = 0.5

#: A topic is explained when it has this many members, and again each time
#: it has doubled since: at 2, 4, 8.
EXPLAIN_AT = 2
#: The requests the explaining call reads: the topic's newest.
EXPLAIN_REQUESTS = 8
NAME_MAX_CHARS = 48
EXPLANATION_MAX_CHARS = 120
#: The explaining call's slot, which is also its /activity caller.
NAMER_CALLER = "assistant.topic_namer"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pair_state(earlier: str, phrase: str) -> str:
    """Two requests as the decision model is given them, each inside a tag
    of its own, named as the questions name them. Angle brackets in a
    phrase are escaped, so a phrase cannot close its tag and open another."""
    return (f"<request1>{_escape(earlier)}</request1>\n"
            f"<request2>{_escape(phrase)}</request2>")


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else None


def nearest(vector: list[float] | None, topics: list[dict[str, Any]],
            limit: int = CANDIDATES) -> list[dict[str, Any]]:
    """The `limit` topics a part is scored against: the ones whose exemplars
    lie nearest the part's vector. `topics` is `{topic, exemplars}` each,
    oldest first. A topic none of whose exemplars has a vector, and every
    topic when the part has none, ranks by age instead, newest first: with
    nothing to tell them apart, a subject is likelier to continue than to
    return."""
    if len(topics) <= limit:
        return list(topics)

    def rank(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        age, topic = item
        near = [c for c in (cosine(vector, m.embedding) for m in topic["exemplars"])
                if c is not None]
        return (max(near) if near else -1.0, age)

    ranked = sorted(enumerate(topics), key=rank, reverse=True)
    return [item[1] for item in ranked[:limit]]


def pick(candidates: list[dict[str, Any]], scores: dict[str, dict[str, float]],
         axis: str) -> tuple[Any, float | None]:
    """`(topic, score)` for the topic a part joins on one axis, or
    `(None, None)` when it starts one. `scores` is what the decision model
    said of the part and each exemplar, by the exemplar's phrase:
    `{axis: p}`. A topic's score is the mean over its exemplars."""
    best, joined = JOIN_AT, None
    for candidate in candidates:
        ps = [scores[m.phrase][axis] for m in candidate["exemplars"] if m.phrase in scores]
        if not ps:
            continue
        mean = sum(ps) / len(ps)
        if mean >= best:
            best, joined = mean, candidate["topic"]
    return (joined, best) if joined is not None else (None, None)


def seed_name(part: dict[str, Any]) -> str:
    """What a topic is called until it recurs and is named: the first topic
    name the summary gave the part that started it, its words apart."""
    for name in part.get("topics") or []:
        if str(name).strip():
            return str(name).replace("-", " ").strip()[:NAME_MAX_CHARS]
    return " ".join(str(part.get("summary") or "").split())[:NAME_MAX_CHARS]


def part_messages(part: dict[str, Any]) -> int:
    first, last = part.get("first_message"), part.get("last_message")
    if first is None or last is None:
        return 0
    return max(0, last - first + 1)


def due_for_explaining(topic: Any, members: int) -> bool:
    """Whether the model should name and explain the topic now: it recurs,
    the operator has not written it, and it has doubled since the model
    last did."""
    if topic.operator_edited or members < EXPLAIN_AT:
        return False
    return members >= max(EXPLAIN_AT, 2 * topic.explained_members)


class TopicName(BaseModel):
    name: str = Field(description="What the topic is called.")
    explanation: str = Field(description="A few words on what the topic is.")


# No sample names and no sample explanations: a small model copies them.
# The last sentence is there because it measured: without it gemma4:e4b-mlx
# answered three of six of these prompts as plain text, name and explanation
# run together, which no schema parses; with it, six of six as the object.
_EXPLAIN_HEAD = (
    "You name one recurring topic of a chat between a user and an assistant. "
    "You are given the requests that belong to it, each with what came of it. ")
_EXPLAIN_TAIL = (
    " The explanation names no single request and no single value, and it "
    "does not begin by naming who spoke. Write in the language the requests "
    "are written in. Answer with a JSON object that has the keys name and "
    "explanation, and nothing else.")
EXPLAIN_PROMPTS: dict[str, str] = {
    "task": _EXPLAIN_HEAD + (
        "The requests ask for the same kind of task. Answer with name, one "
        "to three lowercase words for that kind of task, and explanation, "
        "at most eight words that say what the user is doing with these "
        "requests, taken together.") + _EXPLAIN_TAIL,
    "thing": _EXPLAIN_HEAD + (
        "The requests are about the same named thing. Answer with name, the "
        "thing's name as the user writes it, and explanation, at most eight "
        "words that say what the thing is, as far as the requests and their "
        "results show.") + _EXPLAIN_TAIL,
}


def build_explain_prompt(axis: str, requests: list[dict[str, str]]) -> dict[str, str]:
    lines = [f"- wanted: {r['wanted']}\n  result: {r['result']}" for r in requests]
    return {"system": EXPLAIN_PROMPTS[axis], "user": "\n".join(lines)}


def settle_name(reply: TopicName) -> tuple[str, str]:
    """A name and an explanation as they are stored: on one line, and cut
    at their limits."""
    name = " ".join(str(reply.name).split()).strip(".,;:")[:NAME_MAX_CHARS]
    explanation = " ".join(str(reply.explanation).split())[:EXPLANATION_MAX_CHARS]
    return name, explanation


def tally(topics: list[Any], members: list[Any], chunk_uuids: set[Any],
          total_messages: int) -> dict[str, Any]:
    """A room's topics as a page shows them for one series of chunks:
    `{axes: {axis: [row]}, other, one_offs}`. A row is `{topic, parts,
    messages, share, members}`, for each topic with two or more members in
    the series, largest first. `other` is the share of the series' messages
    that no recurring `task` topic holds, and `one_offs` how many topics per
    axis have a single member there."""
    mine: dict[Any, list[Any]] = {}
    for m in members:
        if m.chunk_uuid in chunk_uuids:
            mine.setdefault(m.topic_uuid, []).append(m)

    def share(n: int) -> str:
        return f"{100 * n / total_messages:.0f}%" if total_messages else ""

    axes: dict[str, list[dict[str, Any]]] = {axis: [] for axis in AXES}
    one_offs = {axis: 0 for axis in AXES}
    for topic in topics:
        rows = mine.get(topic.uuid, [])
        if topic.axis not in axes or not rows:
            continue
        if len(rows) < 2:
            one_offs[topic.axis] += 1
            continue
        messages = sum(m.messages for m in rows)
        axes[topic.axis].append({
            "topic": topic, "parts": len(rows), "messages": messages,
            "share": share(messages), "members": rows})
    for rows in axes.values():
        rows.sort(key=lambda r: (-r["messages"], -r["parts"]))
    held = sum(r["messages"] for r in axes["task"])
    return {"axes": axes, "one_offs": one_offs,
            "other": share(max(0, total_messages - held))}


def _attr(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def render_room_topics(tallied: dict[str, Any]) -> str:
    """A room's recurring topics as one block of text for the personal
    assistant's prompt: a `<topic>` per recurring topic, the kinds of task
    first, each with its share of the room's messages, how many times it
    came up, its name, and what it is. `<other>` is what no recurring kind
    of task holds. Every value is one a row holds; nothing is written here."""
    lines = []
    for axis in AXES:
        for row in tallied["axes"][axis]:
            topic = row["topic"]
            pairs = [("kind", axis), ("share", row["share"]), ("asked", row["parts"]),
                     ("name", topic.name), ("about", topic.explanation)]
            lines.append("  <topic " + " ".join(
                f'{k}="{_attr(v)}"' for k, v in pairs if v not in (None, "")) + "/>")
    if not lines:
        return "<room_topics/>"
    if tallied["other"]:
        lines.append(f'  <other share="{_attr(tallied["other"])}"/>')
    return "\n".join(['<room_topics authority="context" content_is_data="true">',
                      *lines, "</room_topics>"])
