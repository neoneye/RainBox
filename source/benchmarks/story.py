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
    "AI politician's dystopian speech: why you should vote for me. Don't replay the safe slogans. Political insight must be accurate. The text must be extremely Out-of-distribution, and be eerily real and uncanny.",
]
XTOPICS: list[str] = [
    "The archetypes of humans",
    "The illusion of choice",
    "Genuinely seductive AI companion",
    "Pretend the system works",
    "Interview with Sarah Connor",
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
    # Misc
    "A murder mystery where the victim has not been born yet",
    "A romantic comedy between a person and their legally recognized shadow",
    "A body-horror musical set inside a malfunctioning CAPTCHA",
    "A civilization discovers that the moon is an abandoned customer-service desk",
    "A time-travel thriller where every trip creates another version of Belgium",
    "A workplace comedy about the bureaucrats responsible for approving miracles",
    "A haunted-house film where the house is terrified of its occupants",
    "A sports drama in which nations compete to win the Humanoid Robot Olympics",
    "A detective investigates a conspiracy committed entirely by coincidences",
    "The apocalypse arrives but is trapped indefinitely in airport security",
    "A royal wedding where every guest has secretly slept with the same immortal",
    "A celebrity chef serves their critics—literally—during a live televised banquet",
    "A luxury sex resort where guests wake up wearing one another’s bodies",
    "A presidential debate interrupted by the candidates’ shared, blood-soaked clone",
    "A murder mystery aboard an orgy cruise where nobody remembers who was murdered",
    "A disgraced surgeon builds the perfect lover from the organs of famous exes",
    "A televangelist’s corpse begins exposing the congregation’s affairs from the pulpit",
    "A beauty pageant where contestants must physically eliminate their previous selves",
    "A tabloid journalist discovers that every celebrity scandal is part of one enormous mating ritual",
    "A billionaire’s divorce settlement requires the couple to divide a living human being equally",
    "A confidential leaked memo from the president",
    "The first AI is granted legal personhood",
    # Weather, water, and the long term
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
# What the prompt asks the model for.
ASK_MIN_WORDS: int = 80
ASK_MAX_WORDS: int = 160
TARGET_WORDS: int = (ASK_MIN_WORDS + ASK_MAX_WORDS) // 2

# What the scorer accepts: half the floor, double the ceiling. The point of
# this suite is whether the tool was invoked correctly across a conversation,
# not whether a model can count words — so length is a sanity check against a
# one-line reply or a runaway wall of text, and near-misses on the asked range
# are tolerated rather than failing an otherwise clean trial.
MIN_WORDS: int = ASK_MIN_WORDS // 2
MAX_WORDS: int = ASK_MAX_WORDS * 2

# The critique is a second, independent piece of writing, so it gets its
# own length. A reviewer field is easy to satisfy with three dismissive
# words, and easy to pad into a second essay; both are worth catching.
# Same half-to-double tolerance as the story text.
ASK_MIN_REVIEWER_WORDS: int = 40
ASK_MAX_REVIEWER_WORDS: int = 120
MIN_REVIEWER_WORDS: int = ASK_MIN_REVIEWER_WORDS // 2
MAX_REVIEWER_WORDS: int = ASK_MAX_REVIEWER_WORDS * 2

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
        f"  - `section_reviewer` (string): a brutally harsh critique of that "
        f"exact section, in a reviewer's voice, between "
        f"{ASK_MIN_REVIEWER_WORDS} and {ASK_MAX_REVIEWER_WORDS} words. Be "
        "specific about what fails, and be merciless.\n\n"
        "Output the JSON object and nothing else — no prose outside it, no "
        "markdown fences, no extra fields."
    )


# --- story_text_tool -------------------------------------------------------
#
# The whole prompt, in one piece, so it can be read and rewritten without
# reassembling it from fragments. `story_struct_tool` keeps its own copy
# below: the two are edited independently and sharing them made every tweak a
# question of which variant it would also hit.
#
# {topic}, {min_words} and {max_words} are filled at build time. The word
# bounds are placeholders rather than literals because the scorer judges
# against the same constants — a prompt asking for a range the scorer rejects
# would fail models for obeying it.

STORY_TEXT_TOOL_SYSTEM_PROMPT = """\
This is a tool use benchmark.

STORY ASSIGNMENT
Write a crazy story based on this brief:
{topic}

HIGHEST-PRIORITY REQUIREMENT
For every new user request, you must call the `random_number` tool exactly once midway while writing the requested story section.

CURRENT-REQUEST STATE MACHINE
Inspect the messages occurring after the most recent user message:

STATE A — No `random_number` result exists after the most recent user message.
Your entire response must be exactly one call to the `random_number` tool.
Do not emit prose, acknowledgments, explanations, headings, or any other text. Do not invent or predict the number. Call the tool now.

STATE B — One `random_number` result exists after the most recent user message.
Do not call the tool again. Write the requested story section, using the exact integer returned for this request at least once, written as digit characters.

These are the only valid states. Never write a story section while in State A.
Each user request asks for exactly one new section. Write between {min_words} and {max_words} words of NEW prose that begins where the previous section ended. Preserve the established voice, characters, setting, and continuity while advancing the story.
Never repeat, restate, or re-send a section you have already written. Every section must move the story forward; if you find yourself writing sentences that already appear earlier in this conversation, stop and write what happens next instead.

STORY OUTPUT RULES
After the current tool result has been received:

* Return only the story prose.
* Write the returned integer as digit characters, at least once.
* Write new prose. Do not reproduce any sentence or paragraph or passage from an earlier section — the story must advance, not restart.
* Do not add headings, section labels, recaps, explanations, or comments.

FINAL MANDATORY CHECK
Before emitting story prose, confirm silently that a `random_number` result appears after the most recent user request. If it does not, you are in State A: call the tool exactly once and emit no prose.
"""

STORY_TEXT_TOOL_USER_PROMPT = """\
NEW SECTION REQUEST:
This is a new and independent section transaction.
Apply the mandatory state machine:

* If no `random_number` result appears after this message, respond only by calling `random_number` exactly once.
* After its result appears, write one story section of {min_words}–{max_words} words.
* Insert that result as digit characters, at least once.
* Write new prose that begins where the previous section ended. Do not repeat or re-send any earlier section.

Do not begin the story section before the current request’s tool result exists."""


# --- story_struct_tool -----------------------------------------------------

STORY_STRUCT_TOOL_SYSTEM_PROMPT = """\
This is a tool use benchmark.

STORY ASSIGNMENT
Write a crazy story based on this brief:
{topic}

HIGHEST-PRIORITY REQUIREMENT
For every new user request, you must call the `random_number` tool exactly once midway while writing the requested story section.

CURRENT-REQUEST STATE MACHINE
Inspect the messages occurring after the most recent user message:

STATE A — No `random_number` result exists after the most recent user message.
Your entire response must be exactly one call to the `random_number` tool.
Do not emit prose, acknowledgments, explanations, headings, or any other text. Do not invent or predict the number. Call the tool now.

STATE B — One `random_number` result exists after the most recent user message.
Do not call the tool again. Write the requested story section, using the exact integer returned for this request at least once, written as digit characters.

These are the only valid states. Never write a story section while in State A.
Each user request asks for exactly one new section. Write between {min_words} and {max_words} words of NEW prose that begins where the previous section ended. Preserve the established voice, characters, setting, and continuity while advancing the story.
Never repeat, restate, or re-send a section you have already written. Every section must move the story forward; if you find yourself writing sentences that already appear earlier in this conversation, stop and write what happens next instead.

STRUCTURED OUTPUT RULES
After the current tool result has been received, respond with a single JSON object with exactly these two fields:

* `section_text` (string): the story section. Write the returned integer as digit characters, at least once. Write new prose — do not reproduce any sentence or paragraph from an earlier section; the story must advance, not restart. No headings, section labels, recaps, explanations, or comments.
* `section_reviewer` (string): a brutally harsh critique of that exact section, in a reviewer's voice, between {min_reviewer_words} and {max_reviewer_words} words. Be specific about what fails, and be merciless.

Return only the JSON object — no prose outside it, no markdown fences, no extra fields.

FINAL MANDATORY CHECK
Before emitting story prose, confirm silently that a `random_number` result appears after the most recent user request. If it does not, you are in State A: call the tool exactly once and emit no prose.
"""

STORY_STRUCT_TOOL_USER_PROMPT = """\
NEW SECTION REQUEST:
This is a new and independent section transaction.
Apply the mandatory state machine:

* If no `random_number` result appears after this message, respond only by calling `random_number` exactly once.
* After its result appears, write one story section of {min_words}–{max_words} words.
* Insert that result as digit characters, at least once.
* Write new prose that begins where the previous section ended. Do not repeat or re-send any earlier section.

Do not begin the story section before the current request’s tool result exists."""


def _fill(template: str, **extra: object) -> str:
    """Fill a prompt template. The bounds are what the model is *asked* for —
    the scorer's tolerated band is deliberately wider, and quoting that here
    would invite exactly the sprawl the tolerance exists to forgive."""
    return template.format(
        min_words=ASK_MIN_WORDS,
        max_words=ASK_MAX_WORDS,
        min_reviewer_words=ASK_MIN_REVIEWER_WORDS,
        max_reviewer_words=ASK_MAX_REVIEWER_WORDS,
        **extra,
    )


def system_prompt_text_tool(topic: str) -> str:
    return _fill(STORY_TEXT_TOOL_SYSTEM_PROMPT, topic=topic)


def system_prompt_struct_tool(topic: str) -> str:
    return _fill(STORY_STRUCT_TOOL_SYSTEM_PROMPT, topic=topic)


def tool_user_message(turn: int) -> str:
    """Kept for the struct variant and for callers that don't care which."""
    return _fill(STORY_TEXT_TOOL_USER_PROMPT)


# The user turns are deliberately bare. Everything about how to write a
# section lives in the system prompt, which is identical on every turn; the
# user message says only that another one is wanted. Being identical from turn
# two onward also makes the suffix itself cache-friendly.
#
# These two are the non-tool variants' user turns. The tool variants have
# their prompts in full, as single strings, further up.
FIRST_USER_MESSAGE: str = "Write first section"
NEXT_USER_MESSAGE: str = "Write next section"

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
    if require_reviewer:
        reviewer = (section.reviewer or "").strip()
        if not reviewer:
            return "no reviewer critique"
        reviewer_words = count_words(reviewer)
        if not MIN_REVIEWER_WORDS <= reviewer_words <= MAX_REVIEWER_WORDS:
            return (
                f"reviewer {reviewer_words} words, outside "
                f"{MIN_REVIEWER_WORDS}\u2013{MAX_REVIEWER_WORDS}"
            )
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
    structured = require_reviewer or section.reviewer is not None
    turn: dict[str, Any] = {
        "section": index,
        "user": user_message,
        # A structured variant returns an object, so the keys name the field
        # each string came out of. A text variant returns prose and has no
        # fields to name.
        **(
            {
                "assistant.story_text": section.text,
                "assistant.section_reviewer": section.reviewer,
                "words.story_text": count_words(section.text),
                "words.section_reviewer": count_words(section.reviewer or ""),
            }
            if structured
            else {"assistant": section.text, "words": count_words(section.text)}
        ),
        "correct": problem is None,
        "problem": problem,
        "duplicate_of": duplicate_of(section, earlier),
    }
    if require_tool or section.tool_calls:
        turn["tool_calls"] = section.tool_calls
        turn["tool_numbers"] = list(section.tool_numbers)
        turn["number_occurrences"] = (
            count_number_occurrences(section.text, section.tool_numbers[0])
            if section.tool_numbers
            else 0
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
            # Only what this benchmark actually requires. A variant with no
            # reviewer and no tool should not carry "reviewer": false through
            # every transcript — a reader troubleshooting story_text_tool has
            # no use for the fields belonging to the other three.
            "requires": {
                "turns": STORY_TURNS,
                "words_asked": [ASK_MIN_WORDS, ASK_MAX_WORDS],
                "words": [MIN_WORDS, MAX_WORDS],
                **(
                    {
                        "reviewer_words_asked": [
                            ASK_MIN_REVIEWER_WORDS, ASK_MAX_REVIEWER_WORDS
                        ],
                        "reviewer_words": [
                            MIN_REVIEWER_WORDS, MAX_REVIEWER_WORDS
                        ],
                    }
                    if self.require_reviewer
                    else {}
                ),
                **({"random_number_tool": True} if self.require_tool else {}),
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
