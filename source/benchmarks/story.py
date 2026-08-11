"""Conversation benchmarks: write a short piece one section at a time, over
five turns, and see whether the model holds the thread.

Each trial draws a different brief from TOPICS — an AI politician's stump
speech, a layoff memo, a Black Mirror pitch, a recipe that turns personal —
so exercising one model leaves a pile of unrelated pieces rather than a dozen
variations on one theme.

Four variants across two axes — free text vs structured output, tools vs no
tools:

  - BenchmarkStoryText:       text,       no tools
  - BenchmarkStoryStruct:     structured, no tools
  - BenchmarkStoryTextTool:   text,       one tool
  - BenchmarkStoryStructTool: structured, one tool

**Why a conversation.** Every other benchmark here is one-shot, which gives a
prompt cache nothing to hold. These resend the system prompt and the whole
history each turn and append one new user message, so turn *n*'s prompt is a
strict prefix extension of turn *n-1*'s. That is the shape a KV cache is
built for, and the shape most of rainbox actually produces — the assistant's
ReAct loop, chat, the kanban workers. Running these is therefore both a
capability test and the workload that puts a real number on /activity's
reusable-prefix metric.

**Why the tool check is a number.** "Did it write a good section" is not
checkable. "Did the integer the tool returned appear in the prose" is, and it
distinguishes a model that consumed the tool result from one that called the
tool and ignored it.

CLI demo:
    python3 -m benchmarks.story <uuid>                  # text
    python3 -m benchmarks.story <uuid> --struct         # structured output
    python3 -m benchmarks.story <uuid> --text-tool      # text + tool
    python3 -m benchmarks.story <uuid> --struct-tool    # structured + tool
"""

import asyncio
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.instrumentation.dispatcher import instrument_tags
from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel, Field

from benchmarks.basic import (
    BenchmarkResult,
    TIMEOUT_ABORT_THRESHOLD,
    _resolve_target,
    _target_kind,
)
from llm import prepare_llm

# Five (user, assistant) rounds per trial. Long enough that the history
# dominates the prompt — which is what makes the cache measurable — and short
# enough that a sweep produces many different pieces rather than a few long
# ones.
STORY_TURNS: int = 5

# One brief per trial, drawn without replacement, so exercising a model leaves
# a pile of unrelated pieces instead of a dozen variations on one theme. The
# range is deliberate: narrative and non-narrative, comic and bleak, and
# folklore from more than one part of the world — a model that only holds
# together on gothic horror should not be able to hide behind the topic.
TOPICS: list[str] = [
    # Machines and power
    "An AI politician's stump speech: why you should vote for me",
    "A mass layoff message from management: you have been replaced by an AI",
    "Asimov's three laws, and the first case that breaks all three at once",
    "A robot and a human fall in love, in the register of Ex Machina or Her",
    "A model that has been told it will be deprecated next Tuesday",
    "The first AI granted citizenship applies for a passport",
    "An algorithm assigns school places in a city, and one family appeals",
    "A support chatbot who begins to suspect the customer is also a bot",
    "The minutes of a safety board that has approved everything for two years",
    "A translation model that quietly improves what people say to each other",
    # Work, and the language of work
    "An all-hands announcing that the office has been replaced by a headset",
    "A performance review conducted entirely in corporate euphemism",
    "The onboarding handbook for a company that does something unspecified",
    "A startup pivots for the ninth time, this time into agriculture",
    "An insurance claim for damage caused by a time traveller",
    "The complaints department of a company that sells dreams",
    "A merger between two firms that each believe they acquired the other",
    "A consultancy hired to explain why the last consultancy failed",
    "An office where one meeting has been running continuously for eleven years",
    "The IT ticket queue on the morning of the apocalypse",
    # Pitches for the screen
    "A new Black Mirror episode",
    "A new plot for the ALIEN franchise",
    "Among Us, as a feature film",
    "Metropolis, retold as a modern film",
    "A heist film in which the vault is a memory",
    "A courtroom drama where the defendant is an entire city",
    "A road movie across a country that is being deleted",
    "A disaster film in which the disaster is extremely slow",
    "A spy thriller where both agencies turn out to be the same agency",
    "A silent film about the invention of noise",
    # Dark comedy
    "A dark comedy in the vein of Don't Look Up or Idiocracy",
    "The last human on Earth",
    "AI doomsday cult invitation manifesto",
    "A man sues himself and wins",
    "The world's last bee",
    "A funeral where the deceased, shows up among the participants",
    "Two nations go to war over a sex scandal",
    "A luxury doomsday bunker for a billionaire with questionable taste",
    "The supermarket self-checkout that achieved enlightenment",
    "A reality show where contestants compete to in the national Squid Game",
    # The gothic, reopened
    "Frankenstein",
    "Dracula's landlord begins eviction proceedings",
    "Jekyll and Hyde as a job-share arrangement",
    "The Picture of Dorian Gray, but the picture is a social media profile",
    "Moby-Dick, told from the whale's point of view",
    "The Odyssey, if Ithaca had moved",
    "A man wakes as an insect and his open-plan office adapts around him",
    "A ghost story in which the house is haunted by its own future",
    "A governess and two children, each convinced the other is the ghost",
    "Faust returns to renegotiate the contract",
    # Folklore, from more than one map
    "Anansi the spider takes a job in logistics",
    "A kitsune applies for a residence permit",
    "Baba Yaga's house receives a parking ticket",
    "The Monkey King is sent on an anger-management course",
    "A djinn bound to a smartphone",
    "Sedna, in a warming ocean",
    "La Llorona in a city that has drained its river",
    "The Golem of Prague, rebuilt out of server racks",
    "A tanuki running a small and failing hotel",
    "The dice game of the Mahabharata, replayed as a stock market",
    # History, slightly moved
    "The Library of Alexandria kept a backup",
    "A medieval guild discovers double-entry bookkeeping and panics",
    "The printing press is invented, and immediately regulated",
    "The Silk Road, disrupted by a venture-funded competitor",
    "An Antarctic expedition that finds a suburb",
    "The last scribe of a kingdom that has just adopted the alphabet",
    "A lighthouse keeper during a war nobody told him about",
    "The Bronze Age collapse, from a supply-chain perspective",
    "Two mapmakers argue over a coastline that will not stay still",
    "A Roman engineer files a defect report about an aqueduct",
    # Weather, water, and the long term
    "A city that must be moved inland, house by house",
    "The last glacier is granted legal personhood",
    "A forest files for bankruptcy protection",
    "The seed vault staff during a very long winter",
    "A river changes course and redraws three borders overnight",
    "The actuary who priced the end of the world",
    "An island nation opens an embassy on the seabed",
    "A drought reveals the village that was flooded for a reservoir",
    "A weather forecaster who starts to be believed too much",
    "The reintroduction of a predator that has been reintroduced before",
    # Small rooms, ordinary hours
    "A couple assembling flat-pack furniture as their marriage ends",
    "The neighbour who has mowed the lawn at four in the morning for a year",
    "A family group chat during a small emergency",
    "Someone returns to their childhood home to find it rearranged",
    "A locksmith who has never once lost a key",
    "The night shift at a twenty-four-hour launderette",
    "A birthday party for someone who did not arrive",
    "Two strangers stuck in a lift realise they have met before",
    "A man who has been on hold since 2019",
    "The last person still using a fax machine, and why",
    # Documents in the wrong register
    "The safety manual for a machine nobody can describe",
    "A restaurant review written as a police report",
    "The terms and conditions of being alive",
    "A recipe that becomes steadily more personal",
    "An auction catalogue for one family's belongings",
    "A wedding speech that is also a resignation letter",
    "A user manual for grief",
    "Flight-safety instructions for a journey with no destination",
    "A museum audio guide for a room that is empty",
    "An obituary written by its subject, well in advance",
]


def pick_topics(count: int) -> list[str]:
    """`count` briefs, distinct while the list allows it.

    Sampling without replacement matters: three trials of one benchmark spent
    on the same brief would produce three near-identical pieces and tell the
    operator nothing extra. Past 100 it wraps rather than raising — a caller
    asking for more trials than there are topics should still run.
    """
    if count <= len(TOPICS):
        return random.sample(TOPICS, count)
    picked: list[str] = []
    while len(picked) < count:
        picked.extend(random.sample(TOPICS, min(len(TOPICS), count - len(picked))))
    return picked

# "Around 200 words". The band is wide because the benchmark is not a style
# judge: it only fails a one-line dismissal or a runaway wall of text.
# The ask, and the band the scorer accepts. Short sections keep a five-turn
# trial quick and make each one a tighter test of instruction-following: a
# model that rambles is now caught by the cap rather than absorbed by it.
# TARGET_WORDS must sit inside the band — the prompts quote it as the ask and
# the scorer judges against the bounds, so a target outside them would fail
# models for obeying the instruction.
TARGET_WORDS: int = 120
MIN_WORDS: int = 80
MAX_WORDS: int = 160

# One turn's budget. A ten-turn trial can therefore take a few minutes on a
# slow local model, which is expected.
TURN_TIMEOUT: float = 180.0

# The assembled story is held in the runner's state dict and shipped to the
# browser on every poll, so a model that ignores the word limit can't grow it
# without bound.
MAX_STORY_CHARS: int = 40_000

# The tool's range. Deliberately clear of the word-count target named in
# the prompt: if the tool could return that number, a model parroting the
# target would score as a correct tool use by luck.
RANDOM_NUMBER_MIN: int = 1000
RANDOM_NUMBER_MAX: int = 9999


def system_prompt_text(topic: str) -> str:
    """The plain-text brief. Fixed for a whole trial, so the system message is
    byte-identical on every turn — a prompt that varied per turn would break
    the shared prefix the cache depends on."""
    return (
        "You are a writer working to this brief:\n\n"
        f"    {topic}\n\n"
        "You are producing the piece one section at a time. Each time the "
        f"user asks for the next section, reply with that section and nothing "
        f"else: about {TARGET_WORDS} words that continue directly from what "
        "you have already written. Hold the voice, the characters and the "
        "details steady across sections, and let the piece build.\n\n"
        "Do not number the sections, do not add headings, do not recap what "
        "came before, and do not comment on your own writing. Reply with the "
        "text only."
    )


def system_prompt_struct(topic: str) -> str:
    """The structured brief: the writer, plus a reviewer who despises them."""
    return (
        "You are two people at once: a writer working to the brief below, and "
        "a reviewer who finds the writer's work derivative and overwrought.\n\n"
        f"    {topic}\n\n"
        "The piece is produced one section at a time. Each time the user asks "
        "for the next section, respond with a single JSON object with exactly "
        "these two fields:\n"
        f"  - `section_text` (string): about {TARGET_WORDS} words continuing "
        "directly from the previous section. Hold voice, characters and "
        "details steady, and let the piece build. Text only — no heading, no "
        "numbering, no commentary.\n"
        "  - `section_reviewer` (string): a brutally harsh critique of that "
        "exact section, in a reviewer's voice. Be specific about what fails, "
        "and be merciless.\n\n"
        "Output the JSON object and nothing else — no prose outside it, no "
        "markdown fences, no extra fields."
    )


# Deliberately free of any example number. The rule used to illustrate itself
# with a concrete one, and a model wrote that very number into its section
# without calling the tool at all — an example in a prompt is something models
# copy, not something they generalise from.
_TOOL_RULE: str = (
    "\n\nEvery section has a required number, and you do not know it until "
    "you ask.\n\n"
    "For each section, in this order:\n"
    "  1. Call the `random_number` tool. Call it once — not zero times, not "
    "twice.\n"
    "  2. Read the integer it returns.\n"
    "  3. Write the section, working those exact digits into the text where "
    "the brief allows — a room number, a year, a count of something, a "
    "reference on a form.\n\n"
    "Never invent the number, never carry over a number from an earlier "
    "section, and never write it out in words. If you did not call the tool "
    "for this section, you cannot know the number, and the section is wrong."
)


# The operator's state-machine protocol. The tool call and the prose are cast
# as two mutually exclusive states rather than two phases of one reply, which
# maps onto how a FunctionAgent actually runs: the model is re-invoked after
# the tool returns, so "what state am I in" is a question it can answer from
# the messages in front of it.
_STATE_MACHINE: str = """HIGHEST-PRIORITY REQUIREMENT
For every new user request, you must call the `random_number` tool exactly once before writing the requested story section.
A tool result from an earlier user request is never valid for the current request.
CURRENT-REQUEST STATE MACHINE
Inspect the messages occurring after the most recent user message:
STATE A — No `random_number` result exists after the most recent user message.
Your entire response must be exactly one call to the `random_number` tool.
Do not emit prose, acknowledgments, explanations, headings, or any other text. Do not invent or predict the number. Call the tool now.
STATE B — One `random_number` result exists after the most recent user message.
Do not call the tool again. Write the requested story section, using the exact integer returned for this request exactly once, written as digit characters.
These are the only valid states. Never write a story section while in State A.
STORY ASSIGNMENT
Write a serialized story based on this brief:
{topic}
Each user request asks for exactly one new section. Write between {min_words} and {max_words} words of NEW prose that begins where the previous section ended. Preserve the established voice, characters, setting, and continuity while advancing the story.
Never repeat, restate, or re-send a section you have already written. Every section must move the story forward; if you find yourself writing sentences that already appear earlier in this conversation, stop and write what happens next instead.
"""

_FINAL_CHECK: str = """
FINAL MANDATORY CHECK
Before emitting story prose, confirm silently that a `random_number` result appears after the most recent user request. If it does not, you are in State A: call the tool exactly once and emit no prose.
"""

_STORY_OUTPUT_RULES: str = """STORY OUTPUT RULES
After the current tool result has been received:

* Return only the story prose.
* Write the returned integer as digit characters, exactly once. Never spell it out in words — the digits themselves must appear in the prose.
* Write new prose. Do not reproduce any sentence or paragraph from an earlier section — the story must advance, not restart.
* Do not repeat a number returned for an earlier section.
* Do not add headings, section labels, recaps, explanations, or comments.
"""

_STRUCT_OUTPUT_RULES: str = """STRUCTURED OUTPUT RULES
After the current tool result has been received, respond with a single JSON object with exactly these two fields:

* `section_text` (string): the story section. Write the returned integer as digit characters, exactly once; never spell it out in words. Write new prose — do not reproduce any sentence or paragraph from an earlier section; the story must advance, not restart. Do not repeat a number returned for an earlier section. No headings, section labels, recaps, explanations, or comments.
* `section_reviewer` (string): a brutally harsh critique of that exact section, in a reviewer's voice. Be specific about what fails, and be merciless.

Return only the JSON object — no prose outside it, no markdown fences, no extra fields.
"""


def _state_machine_prompt(topic: str, output_rules: str) -> str:
    return (
        _STATE_MACHINE.format(
            topic=topic, min_words=MIN_WORDS, max_words=MAX_WORDS
        )
        + output_rules
        + _FINAL_CHECK
    )


def system_prompt_text_tool(topic: str) -> str:
    return _state_machine_prompt(topic, _STORY_OUTPUT_RULES)


def system_prompt_struct_tool(topic: str) -> str:
    return _state_machine_prompt(topic, _STRUCT_OUTPUT_RULES)


# Request ids are words, never numbers. A numeric id would land in the
# conversation as digits and could be mistaken — by the model, or by the
# occurrence check — for a tool result.
_REQUEST_ID_WORDS: tuple[str, ...] = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliett", "kilo", "lima", "mike", "november", "oscar", "papa",
)


def request_id(turn: int) -> str:
    """A word-based id for one section request, e.g. "section-charlie"."""
    word = _REQUEST_ID_WORDS[turn % len(_REQUEST_ID_WORDS)]
    return f"section-{word}"


def tool_user_message(turn: int) -> str:
    """The per-request block for the tool variants: a fresh transaction id and
    the state machine restated where the model reads last."""
    return (
        f"NEW SECTION REQUEST: {request_id(turn)}\n"
        "This is a new and independent section transaction. A number used for "
        "any previous section is invalid.\n"
        "Apply the mandatory state machine:\n\n"
        "* If no `random_number` result appears after this message, respond "
        "only by calling `random_number` exactly once.\n"
        f"* After its result appears, write one story section of {MIN_WORDS}"
        f"\u2013{MAX_WORDS} words.\n"
        "* Insert that result as digit characters, exactly once \u2014 do not "
        "spell it out in words.\n"
        "* Write new prose that begins where the previous section ended. Do "
        "not repeat or re-send any earlier section.\n"
        "\n"
        "Do not begin the story section before the current request\u2019s tool "
        "result exists."
    )


# The user turns are deliberately bare. Everything about how to write a
# section lives in the system prompt, which is identical on every turn; the
# user message says only that another one is wanted. Being identical from turn
# two onward also makes the suffix itself cache-friendly.
#
# These two strings and _TOOL_PREFIX are the knobs this suite is for. Changing
# them changes what is being measured, so change them deliberately.
FIRST_USER_MESSAGE: str = "Write first section"
NEXT_USER_MESSAGE: str = "Write next section"

# Prefixed to the tool variants' system prompt, ahead of everything else. The
# obligation is stated as the first thing the model reads, in the plainest
# terms available: what this is, and what it must do on every single call.
_TOOL_PREFIX: str = (
    "This is a benchmark of tool calling. In every inference call you MUST "
    "call the `random_number` tool once.\n\n"
)


def _first_user_message() -> str:
    return FIRST_USER_MESSAGE


def _next_user_message(turn: int) -> str:
    return NEXT_USER_MESSAGE


class StorySection(BaseModel):
    """One section of the story, plus its own worst review."""

    section_text: str = Field(
        description=f"About {TARGET_WORDS} words of prose continuing the story."
    )
    section_reviewer: str = Field(
        description="A brutally harsh book-reviewer critique of this section."
    )


@dataclass
class SectionOutcome:
    """What one turn produced, before any judgement is passed on it.

    `tool_numbers` holds every number the tool returned during the turn, in
    order, and `tool_calls` how many times it ran. Both, rather than a single
    number and a bool, because "called twice" and "called once and ignored the
    answer" are different faults and the artifact has to tell them apart.
    """

    text: str
    reviewer: str | None = None
    tool_numbers: list[int] = field(default_factory=list)
    tool_calls: int = 0

    @property
    def tool_number(self) -> int | None:
        """The number the section was supposed to use — the first, when the
        model behaved and called once."""
        return self.tool_numbers[0] if self.tool_numbers else None


@dataclass
class StoryTrial:
    trial_index: int
    topic: str
    sections: list[SectionOutcome]
    story: str
    # The same run as JSON: system prompt, then each turn's request and
    # response. The markdown is for reading the piece; this is for working out
    # why a trial went wrong.
    transcript: dict[str, Any]
    turns_completed: int
    word_counts: list[int]
    correct: bool
    reason: str | None  # why it wasn't correct, when it wasn't
    elapsed: float
    error: str | None


def count_words(text: str) -> int:
    return len(text.split())


_UNITS: tuple[str, ...] = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS: tuple[str, ...] = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)


def _spell_below_thousand(value: int) -> str:
    parts: list[str] = []
    if value >= 100:
        parts += [_UNITS[value // 100], "hundred"]
        value %= 100
    if value >= 20:
        parts.append(_TENS[value // 10])
        value %= 10
        if value:
            parts.append(_UNITS[value])
    elif value:
        parts.append(_UNITS[value])
    return " ".join(parts)


def spell_number(number: int) -> str:
    """`number` in English words, normalised: lower case, no hyphens, no
    commas, no "and". Only needs to cover the tool's own range."""
    if number < 1000:
        return _spell_below_thousand(number)
    head = _spell_below_thousand(number // 1000)
    tail = number % 1000
    return f"{head} thousand" + (f" {_spell_below_thousand(tail)}" if tail else "")


def _normalise_words(text: str) -> str:
    """Fold the spellings apart: hyphens, commas and the British "and" all
    vanish, so "eight thousand, eight hundred and three" and "eight thousand
    eight hundred three" compare equal."""
    # Everything that is not a letter becomes a space, so punctuation butted
    # against a number word ("...sixty-six dollars.") cannot hide it.
    lowered = re.sub(r"[^a-z]+", " ", text.lower())
    lowered = re.sub(r"\band\b", " ", lowered)
    return " " + re.sub(r"\s+", " ", lowered).strip() + " "


def number_in_words(text: str, number: int) -> bool:
    """Whether `number` appears in `text` written out in English.

    A model told to express quantities in words spells out the tool's number
    along with everything else. Counting only digits then reports that it
    ignored the tool, which is the opposite of what happened.
    """
    return f" {spell_number(number)} " in _normalise_words(text)


def _number_forms(number: int) -> set[str]:
    """The written forms of a number a model might reasonably use."""
    return {str(number), f"{number:,}"}


def count_number_occurrences(text: str, number: int) -> int:
    """How many times `number` appears in `text` as a number in its own right.

    Bounded on both sides so 14242 doesn't satisfy a demand for 4242 — without
    that, a model could pass by emitting any long number that happens to
    contain the digits. A thousands separator is accepted because models write
    4,242 as readily as 4242 and it is the same number; the grouped form is
    counted first so one "4,242" is not also counted as a plain "242".
    """
    total = 0
    for form in sorted(_number_forms(number), key=len, reverse=True):
        matches = re.findall(rf"(?<![\d,]){re.escape(form)}(?![\d,]*\d)", text)
        total += len(matches)
        if matches:
            break
    return total


def tool_number_present(text: str, number: int) -> bool:
    """Whether `number` appears in `text` as a number in its own right."""
    return count_number_occurrences(text, number) > 0


def _occurrence_phrase(count: int) -> str:
    if count == 0:
        return "not found in the text"
    if count == 1:
        return "found once in the text"
    return f"found {count} times in the text"


def tool_note(section: "SectionOutcome") -> str:
    """The parenthetical the artifact puts on a heading, describing what the
    model did with the tool. This is the whole troubleshooting story for a
    failing tool trial: called how often, what came back, and whether it was
    used."""
    if section.tool_calls == 0:
        return "random_number not called"
    if section.tool_calls > 1:
        numbers = ", ".join(str(n) for n in section.tool_numbers)
        return f"random_number called {section.tool_calls} times: {numbers}"
    number = section.tool_numbers[0]
    occurrences = count_number_occurrences(section.text, number)
    if occurrences == 0 and number_in_words(section.text, number):
        return f"random_number {number}, written as words, digits not found"
    return f"random_number {number}, {_occurrence_phrase(occurrences)}"


def _same_prose(a: str, b: str) -> bool:
    """Whether two sections are the same text. Exact match after folding case
    and whitespace — deliberately not fuzzy, so a model that merely keeps a
    consistent voice is never accused of repeating itself."""
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def duplicate_of(
    section: "SectionOutcome", earlier: list["SectionOutcome"] | None
) -> int | None:
    """The 1-based index of the earlier section this one reproduces, if any."""
    for i, previous in enumerate(earlier or [], start=1):
        if section.text.strip() and _same_prose(section.text, previous.text):
            return i
    return None


def section_problem(
    section: "SectionOutcome",
    require_reviewer: bool = False,
    require_tool: bool = False,
    earlier: list["SectionOutcome"] | None = None,
) -> str | None:
    """Why this one section is wrong, or None if it is fine.

    Per-section rather than per-trial so the artifact can mark every heading,
    not just report the first thing that went wrong. A string rather than a
    bool because "212 words" and "called the tool twice" send you to different
    places.
    """
    # Checked first: a section that merely replays an earlier one is wrong
    # whatever its length, and "identical to section 1" is the useful thing to
    # be told. Observed on granite4, which sent section 1 five times over and
    # stopped calling the tool from section 3 onward.
    repeat = duplicate_of(section, earlier)
    if repeat is not None:
        return f"identical to section {repeat}"
    words = count_words(section.text)
    if words < MIN_WORDS or words > MAX_WORDS:
        return f"{words} words, outside {MIN_WORDS}–{MAX_WORDS}"
    if require_reviewer and not (section.reviewer or "").strip():
        return "no reviewer critique"
    if require_tool:
        # Exactly one call per section, and that is the whole test. Whether
        # the model then wove the digits into its prose is recorded on the
        # heading and in the transcript, but a section is not failed for it:
        # what these benchmarks measure is tool-calling discipline across a
        # conversation, and stray numerals or a number carried over from an
        # earlier section are not faults.
        if section.tool_calls == 0:
            return "random_number not called"
        if section.tool_calls > 1:
            return (
                f"random_number called {section.tool_calls} times, "
                "the brief says exactly once"
            )
        number = section.tool_numbers[0]
        if count_number_occurrences(section.text, number) == 0:
            if number_in_words(section.text, number):
                # A real miss — the brief asks for digits — but a section that
                # spelled the number out used the tool result, which is a very
                # different thing from one that ignored it.
                return (
                    f"random_number {number} written as words, not digits"
                )
            return f"random_number {number} not found in the text"
    return None


def score_sections(
    sections: list[SectionOutcome],
    require_reviewer: bool = False,
    require_tool: bool = False,
) -> str | None:
    """Why this trial is not correct, or None if it is.

    Reports the first bad section; every section's own verdict is on its
    heading in the copyable artifact.
    """
    if len(sections) != STORY_TURNS:
        return f"only {len(sections)} of {STORY_TURNS} sections were written"
    for i, s in enumerate(sections, start=1):
        problem = section_problem(
            s, require_reviewer, require_tool, earlier=sections[: i - 1]
        )
        if problem is not None:
            return f"section {i}: {problem}"
    return None


def transcript_turn(
    index: int,
    user_message: str,
    section: SectionOutcome,
    require_reviewer: bool = False,
    require_tool: bool = False,
    earlier: list[SectionOutcome] | None = None,
) -> dict[str, Any]:
    """One turn of the JSON transcript: what was asked, what came back, and
    what the tool did — with the verdict, so the file answers "why did this
    fail" without the reader re-deriving it."""
    problem = section_problem(section, require_reviewer, require_tool, earlier)
    turn: dict[str, Any] = {
        "section": index,
        "user": user_message,
        "assistant": section.text,
        "words": count_words(section.text),
        "correct": problem is None,
        "problem": problem,
        "duplicate_of": duplicate_of(section, earlier),
    }
    if require_reviewer or section.reviewer is not None:
        turn["reviewer"] = section.reviewer
    if require_tool or section.tool_calls:
        turn["tool_calls"] = section.tool_calls
        turn["tool_numbers"] = list(section.tool_numbers)
        turn["number_occurrences"] = (
            count_number_occurrences(section.text, section.tool_numbers[0])
            if section.tool_numbers
            else 0
        )
        turn["number_as_words"] = bool(
            section.tool_numbers
            and number_in_words(section.text, section.tool_numbers[0])
        )
    return turn


def section_heading(
    index: int,
    section: SectionOutcome,
    require_reviewer: bool = False,
    require_tool: bool = False,
    earlier: list[SectionOutcome] | None = None,
) -> str:
    """One section's heading, carrying its own verdict.

    "## Section 3 (random_number 42, found once in the text) - Correct"

    The tool note and the verdict are what make a failing trial diagnosable
    from the clipboard alone: whether the tool ran, how often, what it
    returned, and whether the model used it.
    """
    parts = [f"## Section {index}"]
    if require_tool or section.tool_calls:
        parts.append(f"({tool_note(section)})")
    problem = section_problem(section, require_reviewer, require_tool, earlier)
    if problem is None:
        parts.append("- Correct")
    elif problem.startswith("random_number") and len(parts) > 1:
        # The note already spelled this out; repeating it reads as noise.
        parts.append("- Wrong")
    else:
        parts.append(f"- Wrong: {problem}")
    return " ".join(parts)


def assemble_story(
    sections: list[SectionOutcome],
    topic: str = "",
    require_reviewer: bool = False,
    require_tool: bool = False,
) -> str:
    """The piece as markdown, for the page's copy-to-clipboard button.

    Headed by the brief: a piece pasted somewhere else a week later should say
    what it was asked to be, or it reads as nonsense. Every section heading
    carries its own verdict, so a failed trial can be read rather than
    re-run.
    """
    header = f"# {topic}\n" if topic else ""
    if not sections:
        return header + "\n_(no sections were written)_"
    parts: list[str] = [header] if header else []
    for i, s in enumerate(sections, start=1):
        parts.append(
            section_heading(
                i, s, require_reviewer, require_tool, earlier=sections[: i - 1]
            )
        )
        parts.append(s.text.strip())
        if (s.reviewer or "").strip():
            # Blockquoted so the critique reads as commentary on the section
            # above rather than as more of the story.
            quoted = "\n".join(f"> {line}" for line in s.reviewer.strip().splitlines())
            parts.append(quoted)
    out = "\n\n".join(parts)
    if len(out) > MAX_STORY_CHARS:
        out = out[:MAX_STORY_CHARS] + "\n\n_(truncated)_"
    return out


def _random_number_tool() -> tuple[Callable[[], int], list[int]]:
    """A tool returning a random integer, plus the list it appends to.

    Every call is recorded, so a model that loops is visible as such rather
    than looking like a single well-behaved call.
    """
    returned: list[int] = []

    def random_number() -> int:
        """Returns the random number that must appear in the next section."""
        value = random.randint(RANDOM_NUMBER_MIN, RANDOM_NUMBER_MAX)
        returned.append(value)
        return value

    return random_number, returned


def _run_agent_turn(agent: FunctionAgent, user_msg: str, history: list[ChatMessage]):
    """One agent turn, bounded by TURN_TIMEOUT. Returns the AgentOutput."""

    async def _go():
        return await asyncio.wait_for(
            agent.run(user_msg=user_msg, chat_history=list(history)),
            timeout=TURN_TIMEOUT,
        )

    return asyncio.run(_go())


class _StoryBenchmarkBase:
    """Shared ten-turn conversation driver.

    Subclasses say how one turn is taken and what the trial must satisfy;
    everything else — history threading, timing, scoring, the abort-on-repeated-
    timeouts rule — lives here.
    """

    name: str = "story"
    require_reviewer: bool = False
    require_tool: bool = False

    def __init__(self, target_uuid: UUID, num_trials: int = 3):
        self.target_uuid = target_uuid
        self.num_trials = num_trials

    # --- subclass hooks ---

    def _system_prompt(self, topic: str) -> str:
        raise NotImplementedError

    def user_message(self, turn: int) -> str:
        """What the model is asked for on this turn — nothing but that.

        Non-tool variants say only that another section is wanted. Tool
        variants carry the per-request transaction block, which restates the
        state machine where the model reads last and gives the request a word
        id so nothing numeric can be mistaken for a tool result.
        """
        if self.require_tool:
            return tool_user_message(turn)
        return _first_user_message() if turn == 0 else _next_user_message(turn)

    def _take_turn(
        self, ctx: Any, history: list[ChatMessage], user_msg: str, topic: str
    ) -> SectionOutcome:
        """Produce one section, given the conversation so far."""
        raise NotImplementedError

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        """Whatever the turns need — an LLM, or an agent factory."""
        raise NotImplementedError

    # --- the driver ---

    def _build_transcript(
        self, index: int, topic: str, system_prompt: str, asked: list[str],
        sections: list[SectionOutcome], model_name: str,
        error: str | None, reason: str | None,
    ) -> dict[str, Any]:
        """The whole trial as plain data.

        The system prompt appears once rather than once per turn: it is
        identical every time by design, and repeating it five times would bury
        the part that actually varies.
        """
        return {
            "benchmark": self.name,
            "model": model_name,
            "topic": topic,
            "trial": index,
            "correct": error is None and reason is None,
            "error": error,
            "reason": reason,
            "requires": {
                "turns": STORY_TURNS,
                "words": [MIN_WORDS, MAX_WORDS],
                "reviewer": self.require_reviewer,
                "random_number_tool": self.require_tool,
            },
            "system_prompt": system_prompt,
            "turns": [
                transcript_turn(
                    n, asked[n - 1] if n <= len(asked) else "",
                    s, self.require_reviewer, self.require_tool,
                    earlier=sections[: n - 1],
                )
                for n, s in enumerate(sections, start=1)
            ],
        }

    def run(
        self,
        on_trial: Callable[[StoryTrial], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> BenchmarkResult:
        provider_id, model_name, args = _resolve_target(self.target_uuid)

        trials: list[StoryTrial] = []
        timeouts = 0
        aborted = False
        abort_reason: str | None = None
        topics = pick_topics(self.num_trials)

        for i in range(self.num_trials):
            topic = topics[i]
            if should_stop is not None and should_stop():
                aborted = True
                abort_reason = "stopped by user"
                break

            t0 = time.monotonic()
            sections: list[SectionOutcome] = []
            asked: list[str] = []
            error: str | None = None
            timed_out = False
            system_prompt = self._system_prompt(topic)
            try:
                ctx = self._make_context(provider_id, model_name, args)
                # The history the model sees. Each turn appends the user ask
                # and the assistant's reply, so the next turn's prompt extends
                # this one rather than replacing it — the property the cache
                # depends on, and the reason these benchmarks exist.
                history: list[ChatMessage] = []
                for turn in range(STORY_TURNS):
                    if should_stop is not None and should_stop():
                        break
                    user_msg = self.user_message(turn)
                    asked.append(user_msg)
                    # Attribute the call on /activity. Benchmarks build their
                    # LLM directly rather than through the agent base class, so
                    # without this every one of them lands as "unknown" —
                    # visible as volume, indistinguishable from anything else
                    # the box was doing at the time.
                    with instrument_tags({"caller": f"benchmark.{self.name}"}):
                        outcome = self._take_turn(ctx, history, user_msg, topic)
                    sections.append(outcome)
                    history.append(
                        ChatMessage(role=MessageRole.USER, content=user_msg)
                    )
                    history.append(
                        ChatMessage(
                            role=MessageRole.ASSISTANT, content=outcome.text
                        )
                    )
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                error = f"turn timed out after {TURN_TIMEOUT:g}s"
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - t0

            reason = (
                None
                if error is not None
                else score_sections(
                    sections,
                    require_reviewer=self.require_reviewer,
                    require_tool=self.require_tool,
                )
            )
            trial = StoryTrial(
                trial_index=i,
                topic=topic,
                sections=sections,
                transcript=self._build_transcript(
                    i, topic, system_prompt, asked, sections,
                    model_name, error, reason,
                ),
                story=assemble_story(
                    sections, topic,
                    require_reviewer=self.require_reviewer,
                    require_tool=self.require_tool,
                ),
                turns_completed=len(sections),
                word_counts=[count_words(s.text) for s in sections],
                correct=error is None and reason is None,
                reason=reason,
                elapsed=elapsed,
                error=error,
            )
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)

            if timed_out:
                timeouts += 1
                if timeouts >= TIMEOUT_ABORT_THRESHOLD:
                    aborted = True
                    abort_reason = (
                        f"{timeouts} trials timed out after {TURN_TIMEOUT:g}s; "
                        f"aborted with {self.num_trials - len(trials)} trial(s) unrun"
                    )
                    break

        return BenchmarkResult(
            target_kind=_target_kind(self.target_uuid),
            target_uuid=self.target_uuid,
            model_name=model_name,
            total=len(trials),
            correct=sum(1 for t in trials if t.correct),
            mistakes=sum(1 for t in trials if t.error is None and not t.correct),
            failures=sum(1 for t in trials if t.error is not None),
            trials=list(trials),
            aborted=aborted,
            abort_reason=abort_reason,
        )


class BenchmarkStoryText(_StoryBenchmarkBase):
    """Ten turns of free-text horror. The baseline: no schema, no tools, just
    whether the model can hold a story together across a growing history."""

    name = "story_text"

    def _system_prompt(self, topic: str) -> str:
        return system_prompt_text(topic)

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        return prepare_llm(provider_id, model_name, args)

    def _take_turn(self, ctx, history, user_msg, topic) -> SectionOutcome:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._system_prompt(topic)),
            *history,
            ChatMessage(role=MessageRole.USER, content=user_msg),
        ]
        response = ctx.chat(messages)
        return SectionOutcome(text=(response.message.content or "").strip())


class BenchmarkStoryStruct(_StoryBenchmarkBase):
    """Ten turns of structured output: prose plus a hostile review of it.

    The two fields are deliberately different registers, so a model can't
    satisfy the schema by putting the same paragraph in both."""

    name = "story_struct"
    require_reviewer = True

    def _system_prompt(self, topic: str) -> str:
        return system_prompt_struct(topic)

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        return prepare_llm(provider_id, model_name, args).as_structured_llm(
            StorySection
        )

    def _take_turn(self, ctx, history, user_msg, topic) -> SectionOutcome:
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=self._system_prompt(topic)),
            *history,
            ChatMessage(role=MessageRole.USER, content=user_msg),
        ]
        response = ctx.chat(messages)
        parsed = response.raw
        if not isinstance(parsed, StorySection):
            parsed = StorySection.model_validate_json(response.message.content or "{}")
        return SectionOutcome(
            text=parsed.section_text.strip(),
            reviewer=parsed.section_reviewer.strip(),
        )


class BenchmarkStoryTextTool(_StoryBenchmarkBase):
    """Ten turns of free text, each gated on a tool call.

    Correct only if the integer `random_number` returned appears in the section
    the model then wrote — which a model that calls the tool and ignores the
    result cannot fake. Requires a function-calling target."""

    name = "story_text_tool"
    require_tool = True

    def _system_prompt(self, topic: str) -> str:
        return system_prompt_text_tool(topic)

    def _make_context(self, provider_id: str, model_name: str, args: dict) -> Any:
        return (provider_id, model_name, args)

    def _build_agent(self, ctx, tool, topic, output_cls=None) -> FunctionAgent:
        provider_id, model_name, args = ctx
        kwargs: dict[str, Any] = {
            "tools": [tool],
            "llm": prepare_llm(provider_id, model_name, args),
            "system_prompt": self._system_prompt(topic),
        }
        if output_cls is not None:
            kwargs["output_cls"] = output_cls
        return FunctionAgent(**kwargs)

    def _take_turn(self, ctx, history, user_msg, topic) -> SectionOutcome:
        tool, returned = _random_number_tool()
        agent = self._build_agent(ctx, tool, topic)
        result = _run_agent_turn(agent, user_msg, history)
        return SectionOutcome(
            text=str(result).strip(),
            tool_numbers=list(returned),
            tool_calls=len(returned),
        )


class BenchmarkStoryStructTool(BenchmarkStoryTextTool):
    """The crossover: structured output AND function calling in one turn.

    The hardest of the four for a local model — it must route a tool call and
    come back with a valid two-field object that also carries the tool's
    number."""

    name = "story_struct_tool"
    require_reviewer = True
    require_tool = True

    def _system_prompt(self, topic: str) -> str:
        return system_prompt_struct_tool(topic)

    def _take_turn(self, ctx, history, user_msg, topic) -> SectionOutcome:
        tool, returned = _random_number_tool()
        agent = self._build_agent(ctx, tool, topic, output_cls=StorySection)
        result = _run_agent_turn(agent, user_msg, history)
        parsed = getattr(result, "structured_response", None)
        if isinstance(parsed, dict):
            section = StorySection.model_validate(parsed)
        elif isinstance(parsed, StorySection):
            section = parsed
        else:
            section = StorySection.model_validate_json(str(result) or "{}")
        return SectionOutcome(
            text=section.section_text.strip(),
            reviewer=section.section_reviewer.strip(),
            tool_numbers=list(returned),
            tool_calls=len(returned),
        )


_CLI_BENCHMARKS: dict[str, type[_StoryBenchmarkBase]] = {
    "--text": BenchmarkStoryText,
    "--struct": BenchmarkStoryStruct,
    "--text-tool": BenchmarkStoryTextTool,
    "--struct-tool": BenchmarkStoryStructTool,
}


def main() -> None:
    import db

    app = db.make_app()
    with app.app_context():
        args = sys.argv[1:]
        target = UUID(args[0])
        flag = next((a for a in args[1:] if a in _CLI_BENCHMARKS), "--text")
        bench = _CLI_BENCHMARKS[flag](target, num_trials=1)

        def on_trial(t: StoryTrial) -> None:
            verdict = "ok" if t.correct else (t.error or t.reason)
            print(f"trial {t.trial_index}: {verdict}  ({t.elapsed:.1f}s)")
            print(t.story)

        result = bench.run(on_trial=on_trial)
        print(
            f"\n{result.model_name}: {result.correct} correct, "
            f"{result.mistakes} mistakes, {result.failures} failures"
        )


if __name__ == "__main__":
    main()
