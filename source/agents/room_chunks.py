"""Summarizing one chunk of a room's messages with a local model.

A room's counted messages are cut into chunks (`db.due_room_chunks`). This
module turns one chunk into a prompt, and the model's structured reply into
what is stored: the chunk split into parts, each with what was wanted, what
came of it, how the human reacted, and topic names; and one broader phrase
and broader topics over the whole chunk. How well calibrated the assistant
was in a part is then decided by rule (`calibrate`), from that reaction and
the records, and not by the model.

A second call checks the stored parts against the messages (`AUDIT_PROMPT`,
`settle_audit`). A part whose request or result does not match them has the
chunk written once more, with the first answer and what was wrong with it in
the prompt (`build_retry_prompt`).

The model reads both sides of the conversation. Under each assistant reply
it is given what the records say about that reply: how its run ended,
whether the reply met the acceptance criteria, and how the human voted on
it. Those records are also stored with each part, as counts, so a part's
result is always shown beside what is known about its reliability. Design:
notes/housekeeping-design.md.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Bumped when the prompt, the schema or the parsing changes. A chunk belongs
#: to the series of the version that wrote it, so a new version summarizes a
#: room again beside the old series, and the two can be compared.
PROMPT_VERSION = 6

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
    "messages about one subject. Messages that repeat a request, retry it "
    "or correct it belong to the same part. The same question about another "
    "thing or another value is a part of its own, because it has an answer "
    "of its own. A chunk that stays on one subject has one part. For each "
    "part give:\n"
    "- the numbers of its first and last message;\n"
    "- summary: what the human wanted;\n"
    "- result: what came of it, in at most twenty words: the one main thing "
    "that was found or concluded, or that nothing was. No list of details;\n"
    "- human_reaction: what the human did after the replies in this part. "
    "moved_on: went to another subject. asked_again: sent the same request "
    "once more, or told it to retry. corrected: said a reply was wrong or "
    "gave the right answer. pushed_for_more: asked a further question on "
    "the same subject. "
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


def _join_repeats(parts: list[dict[str, Any]], chunk: list[dict[str, Any]],
                  first_index: int) -> list[dict[str, Any]]:
    """A part that opens with the request before it asked again is joined
    to that part. A small model gives a retry a part of its own, whatever
    the prompt says. "Asked again" is read as `calibrate` reads it: from the
    text (`_same_request`), or the opening request is too short to be one of
    its own and the model read the reaction before it as asking again. A
    short request alone is not enough, since a new question can be three
    words. Nor is a shared topic name: the same question about another value
    has its own answer, and joined to the first it would be shown with the
    wrong one.

    The joined part keeps the first one's phrase, the result of the last
    (where the request ended up) and every reaction along the way. Expects
    parts that cover the chunk (`_tile`)."""
    def requests(part: dict[str, Any]) -> list[list[str]]:
        return [_tokens(m["text"]) for k, m in enumerate(chunk)
                if m["sender"] == "human"
                and part["first_message"] <= first_index + k <= part["last_message"]]

    out: list[dict[str, Any]] = []
    for part in parts:
        prev = out[-1] if out else None
        if (prev is not None and part["first_message"] is not None
                and prev["first_message"] is not None):
            earlier, mine = requests(prev), requests(part)
            retry = (len(mine[0]) <= BARE_MAX_WORDS
                     and prev["reactions"][-1] == "asked_again") if mine else False
            if earlier and mine and (
                    retry or any(_same_request(mine[0], e) for e in earlier)):
                prev["last_message"] = part["last_message"]
                prev["result"] = part["result"]
                prev["reactions"] += part["reactions"]
                prev["topics"] += [t for t in part["topics"]
                                   if t not in prev["topics"]][:MAX_PART_TOPICS - len(prev["topics"])]
                continue
        out.append(part)
    return out


def _tile(parts: list[dict[str, Any]], last_index: int) -> list[dict[str, Any]]:
    """Each numbered part runs up to the message before the next one, and the
    last to the chunk's end. The model tends to end a part on the request and
    leave the reply that followed outside every part."""
    numbered = [p for p in parts if p["first_message"] is not None]
    for part, nxt in zip(numbered, numbered[1:]):
        if nxt["first_message"] > part["first_message"]:
            part["last_message"] = nxt["first_message"] - 1
    if numbered:
        numbered[-1]["last_message"] = last_index
    return parts


#: Two requests this alike, letter for letter with the spaces taken out, are
#: the same request typed again. A slip of the keys keeps them this close.
#: The same question about another thing does not: another word changes more
#: letters than a slip does, and another number is ruled out outright.
REPEAT_MIN_LIKENESS = 0.92
#: A request needs this many words to be matched as a repeat.
REPEAT_MIN_WORDS = 3
#: A request that is an earlier one with up to this many words added counts
#: as the earlier one asked again.
REPEAT_MAX_ADDED = 8
#: A follow-up of at most this many words carries no request of its own: it
#: is a retry, a thanks or a nudge. Which of these it is, is the one thing
#: taken from the model's reading of the reaction.
BARE_MAX_WORDS = 3


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def _same_request(words: list[str], earlier: list[str]) -> bool:
    if not earlier or len(words) < REPEAT_MIN_WORDS:
        return False
    added = len(words) - len(earlier)
    if 1 <= added <= REPEAT_MAX_ADDED and words[:len(earlier)] == earlier:
        return True
    if len(earlier) < REPEAT_MIN_WORDS:
        return False
    numbers = lambda ws: sorted(w for w in ws if w.isdigit())  # noqa: E731
    if numbers(words) != numbers(earlier):
        return False
    return difflib.SequenceMatcher(
        None, "".join(words), "".join(earlier)).ratio() >= REPEAT_MIN_LIKENESS


def count_repeats(requests: list[str]) -> tuple[int, int]:
    """`(repeats, bare)` over a part's requests, in order, read from their
    text alone. `repeats`: how many are an earlier one of them asked again
    (the same words, a few words added, or the same but for a slip). `bare`:
    how many after the first are too short to be a request of their own."""
    seen: list[list[str]] = []
    repeats = bare = 0
    for n, text in enumerate(requests):
        words = _tokens(text)
        if n and len(words) <= BARE_MAX_WORDS:
            bare += 1
        elif any(_same_request(words, earlier) for earlier in seen):
            repeats += 1
        seen.append(words)
    return repeats, bare


def part_signals(part: dict[str, Any], chunk: list[dict[str, Any]],
                 first_index: int) -> dict[str, Any]:
    """What the records say about one part, counted over its replies:
    `{replies, repeats, bare, runs: {outcome: n}, first_run, last_run,
    criteria_not_met, revisions, up, down}`. `repeats` and `bare` are read
    from the text of the part's requests (`count_repeats`). These are facts,
    kept beside the model's own words about the part."""
    out: dict[str, Any] = {"replies": 0, "repeats": 0, "bare": 0, "runs": {},
                           "first_run": None, "last_run": None,
                           "criteria_not_met": 0, "revisions": 0,
                           "up": 0, "down": 0}
    if part.get("first_message") is None:
        return out
    runs: Counter[str] = Counter()
    requests: list[str] = []
    for k, m in enumerate(chunk):
        if not part["first_message"] <= first_index + k <= part["last_message"]:
            continue
        if m["sender"] == "human":
            requests.append(m["text"])
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
    out["repeats"], out["bare"] = count_repeats(requests)
    return out


def calibrate(part: dict[str, Any]) -> tuple[Calibration, str]:
    """How well calibrated the assistant was in one part, and on what basis.

    A rule over what the model read (the human's reactions) and what the
    records say (repeated requests, runs, acceptance criteria, votes). The
    first rule that applies decides:

    1. A reply was voted down, or the human corrected one: overconfident.
    2. The human asked again. Either a request in the part repeats an
       earlier one, by its text; or the part has a bare follow-up (a few
       words with no request of their own) and the model read the reaction
       as asking again. The model's reading alone is not enough: it calls a
       new question on the same subject asking again.
       - the first run did not resolve it and the last did: underconfident.
         It delivered only once it was asked again.
       - a run resolved it: overconfident. It answered, and the answer did
         not settle it.
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
    if signals.get("repeats") or (
            signals.get("bare") and "asked_again" in reactions):
        if first and first != "resolved" and last == "resolved":
            return "underconfident", "it delivered only after being asked again"
        if runs.get("resolved"):
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
    parts are made to cover the chunk end to end, and a part that opens with
    the request before it asked again is joined to that part. Each part then
    gets its `signals`, counted from `chunk`'s records, `requests` (how many
    times its request was made, by the same reading as `calibrate`'s "asked
    again"), and its `calibration`, by rule."""
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
    parts = _join_repeats(_tile(parts, last_index), chunk or [], first_index)
    for part in parts:
        signals = part["signals"] = part_signals(part, chunk or [], first_index)
        part["requests"] = 1 + signals["repeats"] + (
            signals["bare"] if "asked_again" in part["reactions"] else 0)
        part["calibration"], part["calibration_basis"] = calibrate(part)
    summary = _sentence(reply.summary, "chunk", problems)
    topics = _topics(reply.topics, MAX_CHUNK_TOPICS, "chunk", problems)
    return summary, topics, parts, problems


# --- the check ---------------------------------------------------------------

AUDIT_REASONING_MAX_CHARS = 600
#: The check's caller label. Its own, so the job's stored call (the newest
#: by the summarizer's label) is the summary that was kept, not its check.
AUDIT_CALLER = "assistant.room_auditor"


class PartCheck(BaseModel):
    """The reasoning comes first, so the three answers are given with it in
    view. Without it the answers came with nothing a rewrite could be told."""

    first_message: int = Field(description="Number of the part's first message.")
    reasoning: str = Field(
        description="What the human asked and was answered in these messages, "
                    "against what the part says.")
    request_matches: bool = Field(
        description="The part's 'wanted' line says what the human asked in these messages.")
    result_matches: bool = Field(
        description="The part's 'came of it' line answers that same request, "
                    "from a reply in these messages.")
    reaction_matches: bool = Field(
        description="The part's reaction is what the human did next.")


class ChunkCheck(BaseModel):
    parts: list[PartCheck] = Field(description="One check per part, in order.")


# Three yes-or-no questions per part and no score. Asked for one score over
# the whole index, the same model gave the middle of the scale to an index
# whose parts paired one request with the answer to another, and wrote
# reasoning that confirmed each line against some message without asking
# whether the two belonged together.
AUDIT_PROMPT = (
    "You check an index of one chunk of a chat between a human and an "
    "assistant. You are given the chunk's numbered messages, with a few "
    "messages of context before and after, and then the index that was "
    "written for the chunk: its parts, each with the messages it covers, "
    "what the human wanted, what came of it, and what the human did after. "
    "A part marked as asked several times claims that the human made the "
    "same request that many times. For each part, in order, read the "
    "messages it covers and answer three questions with true or false: "
    "does the 'wanted' line say what the human asked there; does the 'came "
    "of it' line answer that same request from a reply there; is the "
    "reaction what the human did next. Before the three answers for a part, "
    "write its reasoning: what the human asked and was answered in the "
    "messages it covers, and where the part differs from them."
)


def _span(part: dict[str, Any]) -> str:
    if part.get("first_message") is None:
        return "Part with no message numbers"
    return f"Part m{part['first_message']} to m{part['last_message']}"


def render_index(summary: str, parts: list[dict[str, Any]]) -> str:
    """The stored summary as the check and a rewrite read it."""
    out = [f"Whole chunk: {summary}"]
    for part in parts:
        asked = part.get("requests") or 1
        out.append(
            _span(part) + (f", asked {asked} times" if asked > 1 else "") + ":\n"
            f"  wanted: {part['summary']}\n"
            f"  came of it: {part.get('result') or ''}\n"
            "  the human then: "
            + ", ".join(r.replace("_", " ") for r in part.get("reactions") or []))
    return "\n".join(out)


def build_audit_prompt(prompt: dict[str, str], summary: str,
                       parts: list[dict[str, Any]]) -> dict[str, str]:
    """`prompt` is the chunk's own prompt: the check reads the same messages
    and the same records as the call it checks."""
    return {"system": AUDIT_PROMPT,
            "user": prompt["user"] + "\n\nThe index to check:\n"
                    + render_index(summary, parts)}


def settle_audit(check: ChunkCheck, parts: list[dict[str, Any]],
                 ) -> tuple[list[dict[str, Any]], list[str]]:
    """Put each check on its part, as `part["audit"]`: `{request, result,
    reaction, reasoning}`. Returns `(failed, problems)`: the parts whose
    request or result the check found not to match the messages, and what
    was off in the check's own reply.

    The reaction's answer is kept and shown, and does not fail a part: the
    check disagrees with itself on reactions from one run to the next.

    Checks are matched to parts in order when there is one per part. The
    model sometimes numbers them 1, 2, 3 in place of the message numbers, so
    the numbers are only used when the counts differ."""
    problems: list[str] = []
    if len(check.parts) == len(parts):
        pairs = list(zip(parts, check.parts))
    else:
        problems.append(f"check: {len(check.parts)} checks for {len(parts)} parts")
        by_first = {c.first_message: c for c in check.parts}
        pairs = [(p, by_first[p["first_message"]]) for p in parts
                 if p.get("first_message") in by_first]
    failed = []
    for part, c in pairs:
        part["audit"] = {
            "request": c.request_matches, "result": c.result_matches,
            "reaction": c.reaction_matches,
            "reasoning": _cut(c.reasoning, AUDIT_REASONING_MAX_CHARS)}
        if not (c.request_matches and c.result_matches):
            failed.append(part)
    return failed, problems


def build_retry_prompt(prompt: dict[str, str], summary: str,
                       parts: list[dict[str, Any]],
                       failed: list[dict[str, Any]]) -> dict[str, str]:
    """The chunk's prompt once more, followed by the first answer as it was
    stored and what the check found wrong with it."""
    wrong = "\n".join(f"- {_span(p)}: {p['audit']['reasoning']}" for p in failed)
    return {"system": prompt["system"], "user": (
        prompt["user"]
        + "\n\nAn earlier answer for this chunk, as it was stored:\n"
        + render_index(summary, parts)
        + "\n\nA check of that answer against the messages found these "
          "parts wrong:\n" + wrong
        + "\n\nWrite the answer again from the messages. In every part the "
          "result must answer that part's own request.")}
