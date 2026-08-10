"""The story benchmarks' scoring, assembly, and — most importantly — the
shape of the conversation they send.

No provider and no database: the conversation driver takes an LLM-shaped
callable, so a fake can record exactly what each turn was handed.
"""

import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from llama_index.core.llms import MessageRole

from benchmarks import story
from benchmarks.story import (
    MAX_WORDS,
    count_number_occurrences,
    section_heading,
    section_problem,
    MIN_WORDS,
    STORY_TURNS,
    SectionOutcome,
    assemble_story,
    count_words,
    score_sections,
    tool_number_present,
)


class TestTopics:
    """One brief per trial, drawn from a wide list, so a single model sweep
    leaves a pile of different pieces rather than twelve near-identical ones."""

    def test_there_are_a_hundred(self):
        assert len(story.TOPICS) == 100

    def test_none_are_repeated(self):
        assert len(set(story.TOPICS)) == len(story.TOPICS)

    def test_none_are_blank_or_ragged(self):
        for t in story.TOPICS:
            assert t.strip() == t
            assert len(t) >= 10, t

    def test_they_are_briefs_rather_than_keywords(self):
        """A handful of one-worders are deliberate — "Frankenstein" is a
        complete instruction to any model. The bulk should still be a phrase
        with something to work with in it."""
        substantial = [t for t in story.TOPICS if len(t) >= 25]
        assert len(substantial) >= 90

    def test_the_briefs_the_operator_asked_for_are_present(self):
        must_mention = [
            "vote for me", "replaced by an AI", "Black Mirror", "Idiocracy",
            "Frankenstein", "Ex Machina", "ALIEN", "Asimov", "Among Us",
            "Metropolis",
        ]
        blob = " | ".join(story.TOPICS)
        for phrase in must_mention:
            assert phrase in blob, phrase

    def test_the_range_reaches_well_past_science_fiction(self):
        """A hundred variations on one theme would defeat the point."""
        blob = " | ".join(story.TOPICS).lower()
        for elsewhere in ("recipe", "obituary", "glacier", "lighthouse"):
            assert elsewhere in blob, elsewhere

    def test_a_trial_gets_one_topic(self, offline, monkeypatch):
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: RecordingLlm())
        result = story.BenchmarkStoryText(uuid4(), num_trials=1).run()
        assert result.trials[0].topic in story.TOPICS

    def test_trials_in_one_run_get_different_topics(self, offline, monkeypatch):
        """Drawn without replacement: three trials of the same benchmark
        should not spend all three on the same brief."""
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: RecordingLlm())
        result = story.BenchmarkStoryText(uuid4(), num_trials=3).run()
        topics = [t.topic for t in result.trials]
        assert len(set(topics)) == 3

    def test_asking_for_more_trials_than_topics_does_not_explode(
        self, offline, monkeypatch
    ):
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: RecordingLlm())
        picked = story.pick_topics(len(story.TOPICS) + 5)
        assert len(picked) == len(story.TOPICS) + 5

    def test_the_topic_reaches_the_model(self, offline, monkeypatch):
        llm = RecordingLlm()
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: llm)
        result = story.BenchmarkStoryText(uuid4(), num_trials=1).run()
        system = llm.calls[0][0].content
        assert result.trials[0].topic in system

    def test_the_topic_heads_the_copyable_story(self):
        out = assemble_story([section(text="prose")], topic="A djinn bound to a phone")
        assert "A djinn bound to a phone" in out.splitlines()[0]


class TestToolPrompting:
    """The tool instruction has to survive contact with a small model.

    Observed live: the rule illustrated itself with "if the tool returns 4242,
    the section must contain 4242", and a model duly wrote 4242 into its
    section without calling the tool at all. An example in a prompt is
    something models copy, not something they generalise from.
    """

    def test_no_prompt_shows_a_number_the_tool_could_have_returned(self):
        """The precise hazard: a number in the prompt that looks like a tool
        result. A model copies it, never calls the tool, and the section then
        contains a plausible-looking number that means nothing. Ordered step
        markers and the word-count target are outside the tool's range and so
        cannot be mistaken for one."""
        prompts = [
            story._STATE_MACHINE,
            story._FINAL_CHECK,
            story._STORY_OUTPUT_RULES,
            story._STRUCT_OUTPUT_RULES,
            story.tool_user_message(0),
            story.tool_user_message(3),
            story.FIRST_USER_MESSAGE,
            story.NEXT_USER_MESSAGE,
            story.system_prompt_text_tool("A river changes course"),
            story.system_prompt_struct_tool("A river changes course"),
        ]
        for text in prompts:
            for found in re.findall(r"\d+", text):
                assert not (
                    story.RANDOM_NUMBER_MIN <= int(found) <= story.RANDOM_NUMBER_MAX
                ), f"{found} could be parroted as a tool result"

    def test_the_tool_range_cannot_collide_with_the_word_target(self):
        """If the tool could return the word-count number, a model parroting
        the target would score as a correct tool use by luck."""
        assert story.TARGET_WORDS < story.RANDOM_NUMBER_MIN
        assert not (
            story.RANDOM_NUMBER_MIN <= story.MAX_WORDS <= story.RANDOM_NUMBER_MAX
        )

    def test_the_rule_names_the_tool(self):
        assert "random_number" in story.system_prompt_text_tool("x")

    def test_the_two_states_are_mutually_exclusive_and_ordered(self):
        """Tool call and prose are cast as states rather than phases of one
        reply, which is how a FunctionAgent actually runs: the model is
        re-invoked after the tool returns, so "which state am I in" is a
        question it can answer from the messages in front of it."""
        for build in (story.system_prompt_text_tool, story.system_prompt_struct_tool):
            prompt = build("A man sues gravity")
            assert "STATE A" in prompt and "STATE B" in prompt
            assert prompt.index("STATE A") < prompt.index("STATE B")
            assert "Never write a story section while in State A" in prompt

    def test_the_brief_is_in_the_prompt(self):
        for build in (story.system_prompt_text_tool, story.system_prompt_struct_tool):
            assert "A man sues gravity" in build("A man sues gravity")

    def test_the_final_check_is_about_the_tool_call(self):
        """Which is the one thing scored."""
        for build in (story.system_prompt_text_tool, story.system_prompt_struct_tool):
            assert "FINAL MANDATORY CHECK" in build("x")
            assert "confirm silently that a `random_number` result appears" in build("x")

    def test_the_word_bounds_track_the_scoring_constants(self):
        """A prompt asking for a range the scorer doesn't accept would fail
        models for obeying it. Both the system prompt and the per-request
        block state the range, in different phrasings."""
        for text in (story.system_prompt_text_tool("x"),
                     story.system_prompt_struct_tool("x"),
                     story.tool_user_message(0)):
            assert str(MIN_WORDS) in text and str(MAX_WORDS) in text

    def test_the_structured_variant_still_asks_for_both_fields(self):
        prompt = story.system_prompt_struct_tool("x")
        assert "section_text" in prompt
        assert "section_reviewer" in prompt

    def test_a_non_tool_turn_stays_bare(self):
        bench = story.BenchmarkStoryText(uuid4(), num_trials=1)
        assert bench.user_message(0) == "Write first section"
        for turn in range(1, STORY_TURNS):
            assert bench.user_message(turn) == "Write next section"

    def test_a_tool_turn_opens_a_named_transaction(self):
        bench = story.BenchmarkStoryTextTool(uuid4(), num_trials=1)
        assert bench.user_message(2).startswith(
            "NEW SECTION REQUEST: section-charlie"
        )

    def test_every_request_id_is_distinct_within_a_trial(self):
        """Reusing an id would undercut "this is a new and independent
        transaction" — the model would see the same header twice."""
        bench = story.BenchmarkStoryTextTool(uuid4(), num_trials=1)
        ids = [story.request_id(t) for t in range(STORY_TURNS)]
        assert len(set(ids)) == STORY_TURNS

    def test_request_ids_carry_no_digits(self):
        """A numeric id would land in the conversation as digits and could be
        taken — by the model, or by the occurrence check — for a tool result."""
        for turn in range(STORY_TURNS):
            assert not re.search(r"\d", story.request_id(turn))

    def test_the_ids_do_not_run_out(self):
        assert story.request_id(200)

    def test_every_turn_after_the_first_is_identical(self):
        """An unchanging suffix is the friendliest possible shape for the
        cache, and keeps the only variable the history itself."""
        bench = story.BenchmarkStoryText(uuid4(), num_trials=1)
        later = {bench.user_message(t) for t in range(1, STORY_TURNS)}
        assert len(later) == 1

    def test_a_non_tool_benchmark_does_not_mention_the_tool(self):
        bench = story.BenchmarkStoryText(uuid4(), num_trials=1)
        for turn in range(STORY_TURNS):
            assert "random_number" not in bench.user_message(turn)

    def test_the_rule_still_forbids_inventing_a_number(self):
        prompt = story.system_prompt_text_tool("x").lower()
        assert "do not invent or predict the number" in prompt


class TestTranscript:
    """The JSON artifact: what was asked, what came back, what the tool did.

    The markdown is for reading the piece; this is for working out why a trial
    failed. It carries the system prompt once and then each turn's exchange,
    rather than replaying the whole growing history five times.
    """

    def _trial(self, monkeypatch, cls=None):
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: RecordingLlm())
        bench = (cls or story.BenchmarkStoryText)(uuid4(), num_trials=1)
        return bench.run().trials[0]

    def test_it_is_json_serialisable(self, offline, monkeypatch):
        import json

        json.dumps(self._trial(monkeypatch).transcript)

    def test_it_names_the_benchmark_model_and_brief(self, offline, monkeypatch):
        t = self._trial(monkeypatch)
        assert t.transcript["benchmark"] == "story_text"
        assert t.transcript["model"] == "fake-model"
        assert t.transcript["topic"] == t.topic

    def test_the_system_prompt_appears_once(self, offline, monkeypatch):
        """Verbatim, because a prompt you have to reconstruct is a prompt you
        will reconstruct wrongly."""
        t = self._trial(monkeypatch)
        assert t.transcript["system_prompt"].startswith("You are a writer")
        assert t.topic in t.transcript["system_prompt"]

    def test_every_turn_carries_its_request_and_response(self, offline, monkeypatch):
        t = self._trial(monkeypatch)
        turns = t.transcript["turns"]
        assert len(turns) == STORY_TURNS
        for i, turn in enumerate(turns, start=1):
            assert turn["section"] == i
            assert turn["user"] == (
                "Write first section" if i == 1 else "Write next section"
            )
            assert turn["assistant"]

    def test_a_turn_reports_its_own_verdict(self, offline, monkeypatch):
        turn = self._trial(monkeypatch).transcript["turns"][0]
        assert turn["correct"] is True
        assert turn["problem"] is None
        assert turn["words"] > 0

    def test_the_trial_verdict_is_there_too(self, offline, monkeypatch):
        t = self._trial(monkeypatch)
        assert t.transcript["correct"] is True
        assert t.transcript["reason"] is None
        assert t.transcript["error"] is None

    def test_tool_activity_is_recorded_per_turn(self):
        """The whole point for the tool benchmarks: how many times it ran,
        what it returned, and whether the answer was used."""
        s = section(text="word " * 200 + " 42", numbers=[42], calls=1)
        turn = story.transcript_turn(1, "ask", s, require_tool=True)
        assert turn["tool_calls"] == 1
        assert turn["tool_numbers"] == [42]
        assert turn["number_occurrences"] == 1

    def test_a_skipped_tool_call_is_visible_as_zero(self):
        s = section(text="word " * 200, numbers=[], calls=0)
        turn = story.transcript_turn(1, "ask", s, require_tool=True)
        assert turn["tool_calls"] == 0
        assert turn["tool_numbers"] == []

    def test_the_reviewer_field_rides_along_for_structured_runs(self):
        s = section(text="word " * 200, reviewer="derivative tripe")
        turn = story.transcript_turn(1, "ask", s, require_reviewer=True)
        assert turn["reviewer"] == "derivative tripe"


class TestCountWords:
    def test_plain_prose(self):
        assert count_words("the door opened slowly") == 4

    def test_runs_of_whitespace_are_one_break(self):
        assert count_words("a  b\n\nc\td") == 4

    def test_empty_text_has_no_words(self):
        assert count_words("") == 0
        assert count_words("   \n ") == 0


class TestCountNumberOccurrences:
    """How many times the tool's number appears, not merely whether it does —
    "found once" and "found four times" are different stories about what the
    model did with the tool result."""

    def test_counts_each_appearance(self):
        assert count_number_occurrences("42 and 42 and 42", 42) == 3

    def test_absent_is_zero(self):
        assert count_number_occurrences("nothing here", 42) == 0

    def test_a_longer_number_containing_it_does_not_count(self):
        assert count_number_occurrences("chamber 14242 was empty", 4242) == 0

    def test_a_thousands_separator_counts_once_not_twice(self):
        """4,242 must not match both the grouped and the plain form."""
        assert count_number_occurrences("the year 4,242 arrived", 4242) == 1


class TestSectionProblem:
    """Every section is judged on its own, so the artifact can say which one
    went wrong and why — rather than reporting only the first failure."""

    def test_a_good_tool_section_has_no_problem(self):
        s = section(text="word " * 200 + " 42", numbers=[42], calls=1)
        assert section_problem(s, require_tool=True) is None

    def test_calling_the_tool_and_ignoring_the_answer_fails(self):
        """Both halves are required: the call, and the result reaching the
        page. A call whose answer is discarded proves nothing about tool use."""
        ignored_the_answer = section(text="word " * 200, numbers=[42], calls=1)
        assert section_problem(ignored_the_answer, require_tool=True) is not None

    def test_reusing_an_earlier_number_is_fine(self):
        s = section(text="word " * 200 + " 42", numbers=[42], calls=1)
        assert section_problem(s, require_tool=True) is None

    def test_stray_numerals_are_fine(self):
        s = section(text="word " * 200 + " 42 and 1999 and 7", numbers=[42], calls=1)
        assert section_problem(s, require_tool=True) is None

    def test_never_calling_the_tool_is_named(self):
        s = section(text="word " * 200, numbers=[], calls=0)
        problem = section_problem(s, require_tool=True)
        assert problem is not None
        assert "not called" in problem

    def test_calling_the_tool_twice_is_named(self):
        """The brief says exactly once. Twice means the model is looping, and
        the artifact should say so rather than silently passing."""
        s = section(text="word " * 200 + " 42 77", numbers=[42, 77], calls=2)
        problem = section_problem(s, require_tool=True)
        assert problem is not None
        assert "2 times" in problem

    def test_the_word_count_still_applies_to_tool_sections(self):
        s = section(text="too short", numbers=[42], calls=1)
        assert section_problem(s, require_tool=True) is not None

    def test_a_short_section_is_named_with_its_count(self):
        problem = section_problem(section(text="too short"))
        assert problem is not None
        assert "2 words" in problem

    def test_a_missing_reviewer_is_named(self):
        problem = section_problem(section(reviewer=" "), require_reviewer=True)
        assert problem is not None
        assert "reviewer" in problem.lower()


class TestToolNumberPresent:
    def test_the_number_is_found_in_the_prose(self):
        assert tool_number_present("the clock struck 4242 times", 4242) is True

    def test_an_absent_number_is_not_invented(self):
        assert tool_number_present("the clock struck midnight", 4242) is False

    def test_a_longer_number_containing_it_does_not_count(self):
        """14242 is not 4242 — a substring match would let a model pass by
        emitting any number that happens to contain the digits."""
        assert tool_number_present("chamber 14242 was empty", 4242) is False

    def test_punctuation_around_the_number_is_fine(self):
        assert tool_number_present("room 4242, empty.", 4242) is True

    def test_a_thousands_separator_still_counts(self):
        """Models write 4,242 as readily as 4242, and it is the same number."""
        assert tool_number_present("the year 4,242 arrived", 4242) is True


def section(text="word " * 200, reviewer="dreadful", numbers=None, calls=None):
    numbers = [] if numbers is None else numbers
    return SectionOutcome(
        text=text.strip(),
        reviewer=reviewer,
        tool_numbers=list(numbers),
        tool_calls=len(numbers) if calls is None else calls,
    )


class TestScoreSections:
    def test_a_full_set_of_good_sections_passes(self):
        assert score_sections([section() for _ in range(STORY_TURNS)]) is None

    def test_a_short_conversation_fails(self):
        """A model that stopped answering partway did not write the piece,
        however good the sections it managed were."""
        short = STORY_TURNS - 2
        why = score_sections([section() for _ in range(short)])
        assert why is not None
        assert str(short) in why and str(STORY_TURNS) in why

    def test_a_section_under_the_floor_fails(self):
        sections = [section() for _ in range(STORY_TURNS)]
        sections[3] = section(text="too short")
        why = score_sections(sections)
        assert why is not None
        assert "4" in why  # reported 1-based

    def test_a_runaway_section_fails(self):
        sections = [section() for _ in range(STORY_TURNS)]
        sections[0] = section(text="word " * (MAX_WORDS + 50))
        assert score_sections(sections) is not None

    def test_the_word_band_is_inclusive_at_both_ends(self):
        assert score_sections([section(text="w " * MIN_WORDS)
                               for _ in range(STORY_TURNS)]) is None
        assert score_sections([section(text="w " * MAX_WORDS)
                               for _ in range(STORY_TURNS)]) is None

    def test_a_missing_reviewer_fails_when_one_is_required(self):
        sections = [section() for _ in range(STORY_TURNS)]
        sections[3] = section(reviewer="   ")
        assert score_sections(sections, require_reviewer=True) is not None

    def test_the_reviewer_is_ignored_when_not_required(self):
        sections = [section(reviewer=None) for _ in range(STORY_TURNS)]
        assert score_sections(sections, require_reviewer=False) is None

    def test_an_uncalled_tool_fails(self):
        sections = [section(text='word ' * 200 + ' 7', numbers=[7])
                    for _ in range(STORY_TURNS)]
        sections[2] = section(numbers=[])
        assert score_sections(sections, require_tool=True) is not None

    def test_a_missing_tool_call_fails_the_trial(self):
        sections = [section(text="word " * 200 + " 99", numbers=[99])
                    for _ in range(STORY_TURNS)]
        sections[4] = section(text="word " * 200, numbers=[], calls=0)
        why = score_sections(sections, require_tool=True)
        assert why is not None
        assert "5" in why and "not called" in why

    def test_tools_are_ignored_when_not_required(self):
        assert score_sections([section(numbers=[])
                               for _ in range(STORY_TURNS)],
                              require_tool=False) is None


class RecordingLlm:
    """Stands in for a prepared LLM, keeping every message list it was sent."""

    def __init__(self, reply: str = "word " * 200):
        self.reply = reply.strip()
        self.calls: list[list] = []

    def chat(self, messages):
        self.calls.append(list(messages))
        return SimpleNamespace(
            message=SimpleNamespace(content=self.reply), raw=None
        )


@pytest.fixture
def offline(monkeypatch):
    """Detach the benchmarks from the model registry."""
    monkeypatch.setattr(
        story, "_resolve_target", lambda _u: ("ollama", "fake-model", {})
    )
    monkeypatch.setattr(story, "_target_kind", lambda _u: "override")


class TestConversationShape:
    """The property every one of these benchmarks exists to create: each turn
    resends the whole history and appends one message, so the prompt is a
    strict prefix extension of the previous turn's. If this breaks, the
    benchmarks still pass their own scoring but stop exercising the cache —
    so it is asserted directly rather than inferred from a hit rate."""

    def _run_once(self, offline, monkeypatch):
        llm = RecordingLlm()
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: llm)
        bench = story.BenchmarkStoryText(uuid4(), num_trials=1)
        result = bench.run()
        return llm, result

    def test_every_turn_is_sent(self, offline, monkeypatch):
        llm, _ = self._run_once(offline, monkeypatch)
        assert len(llm.calls) == STORY_TURNS

    def test_the_system_prompt_leads_every_turn(self, offline, monkeypatch):
        llm, _ = self._run_once(offline, monkeypatch)
        first = llm.calls[0][0].content
        for messages in llm.calls:
            assert messages[0].role == MessageRole.SYSTEM
            # Identical across turns — a system prompt that varied per turn
            # would break the shared prefix the cache depends on.
            assert messages[0].content == first

    def test_each_turn_grows_by_exactly_one_exchange(self, offline, monkeypatch):
        llm, _ = self._run_once(offline, monkeypatch)
        lengths = [len(m) for m in llm.calls]
        # system + user, then +2 (the previous user and assistant) each turn.
        assert lengths == [2 + 2 * i for i in range(STORY_TURNS)]

    def test_each_turn_is_a_strict_prefix_extension_of_the_last(
        self, offline, monkeypatch
    ):
        llm, _ = self._run_once(offline, monkeypatch)
        for earlier, later in zip(llm.calls, llm.calls[1:]):
            # Everything the previous turn sent, minus its trailing user ask,
            # must reappear unchanged at the head of this one.
            shared = earlier[:-1]
            assert [(m.role, m.content) for m in later[: len(shared)]] == [
                (m.role, m.content) for m in shared
            ]

    def test_the_assistants_own_words_come_back_in_the_history(
        self, offline, monkeypatch
    ):
        llm, _ = self._run_once(offline, monkeypatch)
        second_turn = llm.calls[1]
        assert second_turn[2].role == MessageRole.ASSISTANT
        assert second_turn[2].content == llm.reply

    def test_a_clean_run_scores_correct(self, offline, monkeypatch):
        _llm, result = self._run_once(offline, monkeypatch)
        assert result.correct == 1
        assert result.failures == 0

    def test_a_raising_model_is_a_failure_not_a_mistake(self, offline, monkeypatch):
        class Exploding:
            def chat(self, _messages):
                raise RuntimeError("provider said no")

        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: Exploding())
        result = story.BenchmarkStoryText(uuid4(), num_trials=1).run()
        assert result.failures == 1
        assert result.correct == 0

    def test_a_terse_model_is_a_mistake_not_a_failure(self, offline, monkeypatch):
        monkeypatch.setattr(
            story, "prepare_llm", lambda *_a, **_k: RecordingLlm(reply="no.")
        )
        result = story.BenchmarkStoryText(uuid4(), num_trials=1).run()
        assert result.mistakes == 1
        assert result.trials[0].reason is not None

    def test_stopping_ends_the_run_without_finishing_the_story(
        self, offline, monkeypatch
    ):
        monkeypatch.setattr(story, "prepare_llm", lambda *_a, **_k: RecordingLlm())
        bench = story.BenchmarkStoryText(uuid4(), num_trials=3)
        result = bench.run(should_stop=lambda: True)
        assert result.aborted is True
        assert result.total == 0

    def test_the_trial_carries_the_assembled_story(self, offline, monkeypatch):
        _llm, result = self._run_once(offline, monkeypatch)
        assert f"## Section {STORY_TURNS}" in result.trials[0].story


class TestCallerAttribution:
    """Benchmarks call prepare_llm directly, so they miss the instrument_tags
    wrapping in agents/base.py and land on /activity as "unknown" — visible as
    volume, but indistinguishable from everything else the box was doing."""

    def test_each_turn_is_tagged_with_its_benchmark(self, offline, monkeypatch):
        from llama_index.core.instrumentation.dispatcher import (
            active_instrument_tags,
        )

        seen: list[dict] = []

        class TagSpy:
            """Reads the tags the dispatcher attaches while a call is in
            flight, which is what the activity recorder later reads."""

            def __init__(self, llm):
                self.llm = llm

            def chat(self, messages):
                seen.append(dict(active_instrument_tags.get() or {}))
                return self.llm.chat(messages)

        monkeypatch.setattr(
            story, "prepare_llm", lambda *_a, **_k: TagSpy(RecordingLlm())
        )
        story.BenchmarkStoryText(uuid4(), num_trials=1).run()
        assert seen
        assert all(t.get("caller") == "benchmark.story_text" for t in seen)

    def test_each_benchmark_reports_its_own_name(self, offline, monkeypatch):
        names = {
            cls.name
            for cls in (
                story.BenchmarkStoryText,
                story.BenchmarkStoryStruct,
                story.BenchmarkStoryTextTool,
                story.BenchmarkStoryStructTool,
            )
        }
        assert names == {
            "story_text", "story_struct", "story_text_tool", "story_struct_tool"
        }


class TestSectionHeading:
    """The heading is the troubleshooting surface for a failed tool trial."""

    def test_the_shape_the_operator_asked_for(self):
        s = section(text="word " * 200 + " 42", numbers=[42], calls=1)
        assert section_heading(3, s, require_tool=True) == (
            "## Section 3 (random_number 42, found once in the text) - Correct"
        )

    def test_an_unused_number_is_reported_and_fails(self):
        s = section(text="word " * 200, numbers=[77], calls=1)
        heading = section_heading(2, s, require_tool=True)
        assert heading == (
            "## Section 2 (random_number 77, not found in the text) - Wrong"
        )

    def test_a_non_tool_fault_is_spelled_out(self):
        """The note can't explain a word-count problem, so the verdict must."""
        s = section(text="too short", numbers=[42], calls=1)
        assert "Wrong: 2 words" in section_heading(1, s, require_tool=True)

    def test_a_repeated_call_is_reported_with_every_number(self):
        s = section(text="word " * 200, numbers=[11, 22], calls=2)
        heading = section_heading(1, s, require_tool=True)
        assert "called 2 times: 11, 22" in heading

    def test_a_non_tool_benchmark_gets_a_plain_heading(self):
        assert section_heading(1, section()) == "## Section 1 - Correct"


class TestAssembleStory:
    def test_each_section_gets_a_numbered_heading(self):
        out = assemble_story([section(text="one"), section(text="two")])
        assert "## Section 1" in out
        assert "## Section 2" in out
        assert "one" in out and "two" in out

    def test_the_reviewer_is_quoted_beneath_its_section(self):
        out = assemble_story([section(text="prose", reviewer="derivative tripe")])
        assert "> derivative tripe" in out
        assert out.index("prose") < out.index("derivative tripe")

    def test_a_section_without_a_reviewer_has_no_empty_quote(self):
        out = assemble_story([section(text="prose", reviewer=None)])
        assert ">" not in out

    def test_the_random_number_is_noted_in_the_heading(self):
        out = assemble_story([section(text="prose", numbers=[4242])])
        assert "random_number 4242" in out

    def test_no_sections_says_so_rather_than_producing_a_blank(self):
        """The copy button must never hand over an empty clipboard with no
        explanation."""
        assert assemble_story([]) .strip() != ""

    def test_the_result_is_capped(self):
        from benchmarks.story import MAX_STORY_CHARS

        out = assemble_story([section(text="w " * 20000) for _ in range(10)])
        assert len(out) <= MAX_STORY_CHARS + 200  # cap plus the truncation note
        assert "truncated" in out


class TestNumberInWords:
    """A model told to "express quantities in words" spells out the tool's
    number too. Observed on gemma4:e4b: every section used the returned
    integer faithfully, written out, and a digits-only check called all five
    a failure to use the tool at all — the opposite of what happened.
    """

    def test_the_plain_form(self):
        assert story.number_in_words("...sixty-six dollars", 66) is True

    def test_a_four_digit_number_as_gemma_writes_it(self):
        text = "allocating exactly seven thousand five hundred sixty-six dollars."
        assert story.number_in_words(text, 7566) is True

    def test_a_zero_in_the_tens_place(self):
        assert story.number_in_words("seven thousand six hundred eight", 7608) is True

    def test_a_zero_in_the_hundreds_place(self):
        assert story.number_in_words("eight thousand eight", 8008) is True

    def test_the_british_and(self):
        assert story.number_in_words(
            "eight thousand eight hundred and three", 8803
        ) is True

    def test_a_comma_between_groups(self):
        assert story.number_in_words(
            "eight thousand, five hundred twenty-four", 8524
        ) is True

    def test_a_different_number_is_not_a_match(self):
        assert story.number_in_words(
            "seven thousand five hundred sixty-five", 7566
        ) is False

    def test_prose_with_no_number_at_all(self):
        assert story.number_in_words("the room was silent", 7566) is False

    def test_case_is_ignored(self):
        assert story.number_in_words("Seven Thousand Five Hundred Sixty-Six", 7566) is True


class TestNumberIsUsed:
    """The criterion the operator wants back: the returned number has to turn
    up in the section, in digits."""

    def test_digits_present_passes(self):
        s = section(text="word " * 200 + " 4242", numbers=[4242], calls=1)
        assert section_problem(s, require_tool=True) is None

    def test_no_trace_of_the_number_fails(self):
        s = section(text="word " * 200, numbers=[4242], calls=1)
        problem = section_problem(s, require_tool=True)
        assert problem is not None
        assert "4242" in problem

    def test_words_instead_of_digits_fails_but_says_so(self):
        """The prompt asks for digits, so this is a real miss — but a section
        that spelled the number out is a different animal from one that
        ignored the tool, and the message has to distinguish them."""
        s = section(
            text="word " * 200 + " four thousand two hundred forty-two",
            numbers=[4242], calls=1,
        )
        problem = section_problem(s, require_tool=True)
        assert problem is not None
        assert "words" in problem.lower()

    def test_the_heading_reports_the_word_form(self):
        s = section(
            text="word " * 200 + " four thousand two hundred forty-two",
            numbers=[4242], calls=1,
        )
        assert "as words" in section_heading(1, s, require_tool=True)

    def test_the_transcript_records_the_word_form(self):
        s = section(
            text="four thousand two hundred forty-two", numbers=[4242], calls=1
        )
        turn = story.transcript_turn(1, "ask", s, require_tool=True)
        assert turn["number_as_words"] is True
        assert turn["number_occurrences"] == 0
