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
    "You are given the requests that belong to it, each with what came of it "
    "and, where it is recorded, the tools the assistant used to answer it. ")
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
    """The explaining call's prompt. A request's tools stand under it where
    they are recorded. They are given, and no sentence tells the model to
    name them: told to, it named the program in 2 of 8 explanations of five
    square roots that a program had computed, and in none without the
    tools. What a topic was answered with is counted from the records
    instead (`answered_with`) and shown beside the model's words."""
    lines = [f"- wanted: {r['wanted']}\n  result: {r['result']}"
             + (f"\n  tools: {r['tools']}" if r.get("tools") else "") for r in requests]
    return {"system": EXPLAIN_PROMPTS[axis], "user": "\n".join(lines)}


def settle_name(reply: TopicName) -> tuple[str, str]:
    """A name and an explanation as they are stored: on one line, and cut
    at their limits."""
    name = " ".join(str(reply.name).split()).strip(".,;:")[:NAME_MAX_CHARS]
    explanation = " ".join(str(reply.explanation).split())[:EXPLANATION_MAX_CHARS]
    return name, explanation


def answered_with(members: list[Any], chunk_parts: dict[Any, list[dict[str, Any]]]) -> str:
    """Which tools a topic's parts were answered with, counted over the
    parts: "program 5/5, memory query 2/5". A tool counts for a part when
    it ran there; a program that only failed is counted apart, as
    "program failed 1/5". Empty when no part used a tool. It is the
    records' answer to what a topic is an exercise of, which the requests
    alone do not say."""
    from agents.room_chunks import PROGRAM

    ran: dict[str, int] = {}
    failed = 0
    for m in members:
        parts = chunk_parts.get(m.chunk_uuid) or []
        part = parts[m.part_index] if m.part_index < len(parts) else {}
        used = (part.get("signals") or {}).get("tools") or {}
        for name, count in used.items():
            if count.get("ran"):
                ran[name] = ran.get(name, 0) + 1
            elif name == PROGRAM:
                failed += 1
    out = [f"{name} {n}/{len(members)}"
           for name, n in sorted(ran.items(), key=lambda kv: (kv[0] != PROGRAM, -kv[1], kv[0]))]
    if failed:
        out.append(f"{PROGRAM} failed {failed}/{len(members)}")
    return ", ".join(out)


def tally(topics: list[Any], members: list[Any], chunk_uuids: set[Any],
          total_messages: int,
          chunk_parts: dict[Any, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """A room's topics as a page shows them for one series of chunks:
    `{axes: {axis: [row]}, other, one_offs}`. A row is `{topic, parts,
    messages, share, members, answered_with}`, for each topic with two or
    more members in the series, largest first. `other` is the share of the
    series' messages that no recurring `task` topic holds, and `one_offs`
    how many topics per axis have a single member there. `chunk_parts` is
    the series' stored parts by chunk, which hold the tools."""
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
            "share": share(messages), "members": rows,
            "answered_with": answered_with(rows, chunk_parts or {})})
    for rows in axes.values():
        rows.sort(key=lambda r: (-r["messages"], -r["parts"]))
    held = sum(r["messages"] for r in axes["task"])
    return {"axes": axes, "one_offs": one_offs,
            "other": share(max(0, total_messages - held))}


# --- two topics that look like one --------------------------------------------
#
# A part joins a topic by how it scores against the topic's first members,
# so a room summarized again, with its requests worded anew, can start a
# second topic beside the one it should have joined. Nothing merges two
# topics by itself: on one room's 14 nearest pairs, the decision model's
# score put two wrong pairs above the two pairs that were one activity (a
# fruit and a computer whose names sound alike, at 0.68; the two halves of
# the diary's date lookups, at 0.40), and the generative model, shown both
# topics' requests, called 12 of the 14 the same. Where both agree the
# pair is put to the operator, who merges it or says the two are apart.

#: The topics a topic is compared with: its nearest by embedding.
MERGE_CANDIDATES = 3
#: A pair is shown to the generative model when its sample requests score at
#: least this on the decision model, and suggested when that model agrees.
SUGGEST_AT = 0.3
#: The requests of each topic the generative model reads.
JUDGE_REQUESTS = 6
REASON_MAX_CHARS = 160


def merge_sample(members: list[Any]) -> list[Any]:
    """The members that stand for a topic when it is compared with another:
    its first and its newest, when their phrases differ. The first is what
    the topic began as, the newest what it has become."""
    out: list[Any] = []
    for m in [*members[:1], *reversed(members)]:
        if all(m.phrase.casefold() != o.phrase.casefold() for o in out):
            out.append(m)
        if len(out) == 2:
            break
    return out


def merge_pairs(topics: list[dict[str, Any]], limit: int = MERGE_CANDIDATES) -> list[tuple[int, int]]:
    """The pairs of one axis's topics worth comparing, as places in
    `topics`, the older first: each topic that recurs with its nearest
    `limit` by embedding. `topics` is `{topic, members}` each, oldest
    first. Two topics of one member each are left out: that pair was
    scored when the newer one's part was placed."""
    pairs: set[tuple[int, int]] = set()
    samples = [merge_sample(t["members"]) for t in topics]

    def near(i: int, j: int) -> float:
        found = [c for c in (cosine(a.embedding, b.embedding)
                             for a in samples[i] for b in samples[j]) if c is not None]
        return max(found) if found else -1.0

    for i, topic in enumerate(topics):
        if len(topic["members"]) < 2:
            continue
        others = sorted((j for j in range(len(topics)) if j != i),
                        key=lambda j: near(i, j), reverse=True)[:limit]
        pairs.update((min(i, j), max(i, j)) for j in others)
    return sorted(pairs)


class SameTopic(BaseModel):
    same: bool = Field(description="Whether the two lists are one topic.")
    reason: str = Field(description="Why, in a few words.")


_JUDGE_TAIL = (
    " Answer with a JSON object that has the keys same, true or false, and "
    "reason, at most twelve words, and nothing else.")
JUDGE_PROMPTS: dict[str, str] = {
    "task": (
        "You are given two lists of requests a user made to an assistant. "
        "Decide whether the two lists are one kind of task: whether every "
        "request of both lists asks the assistant to do the same kind of "
        "thing, whatever it is about.") + _JUDGE_TAIL,
    "thing": (
        "You are given two lists of requests a user made to an assistant. "
        "Decide whether the two lists are about one named thing: whether "
        "every request of both lists is about the same project, person, "
        "tool or other thing that has a name. Two names that only look or "
        "sound alike are two things.") + _JUDGE_TAIL,
}


def _requests(members: list[Any]) -> list[str]:
    out: list[str] = []
    for m in members:
        if all(m.phrase.casefold() != o.casefold() for o in out):
            out.append(m.phrase)
    return out[:JUDGE_REQUESTS]


def build_judge_prompt(axis: str, first: list[Any], second: list[Any]) -> dict[str, str]:
    """Both topics' requests, each phrase once, for the model that says
    whether the two are one."""
    lists = [("list1", first), ("list2", second)]
    return {"system": JUDGE_PROMPTS[axis], "user": "\n\n".join(
        f"{name}:\n" + "\n".join(f"- {r}" for r in _requests(members))
        for name, members in lists)}


def _attr(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def render_room_topics(tallied: dict[str, Any]) -> str:
    """A room's recurring topics as one block of text for the personal
    assistant's prompt: a `<topic>` per recurring topic, the kinds of task
    first, each with its share of the room's messages, how many times it
    came up, its name, what it is, and the tools its parts were answered
    with. `<other>` is what no recurring kind of task holds. Every value is one a row holds; nothing is written here."""
    lines = []
    for axis in AXES:
        for row in tallied["axes"][axis]:
            topic = row["topic"]
            pairs = [("kind", axis), ("share", row["share"]), ("asked", row["parts"]),
                     ("name", topic.name), ("about", topic.explanation),
                     ("answered_with", row.get("answered_with"))]
            lines.append("  <topic " + " ".join(
                f'{k}="{_attr(v)}"' for k, v in pairs if v not in (None, "")) + "/>")
    if not lines:
        return "<room_topics/>"
    if tallied["other"]:
        lines.append(f'  <other share="{_attr(tallied["other"])}"/>')
    return "\n".join(['<room_topics authority="context" content_is_data="true">',
                      *lines, "</room_topics>"])
