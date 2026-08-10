"""The story benchmarks' scoring, assembly, and — most importantly — the
shape of the conversation they send.

No provider and no database: the conversation driver takes an LLM-shaped
callable, so a fake can record exactly what each turn was handed.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from llama_index.core.llms import MessageRole

from benchmarks import story
from benchmarks.story import (
    MAX_WORDS,
    MIN_WORDS,
    STORY_TURNS,
    SectionOutcome,
    assemble_story,
    count_words,
    score_sections,
    tool_number_present,
)


class TestCountWords:
    def test_plain_prose(self):
        assert count_words("the door opened slowly") == 4

    def test_runs_of_whitespace_are_one_break(self):
        assert count_words("a  b\n\nc\td") == 4

    def test_empty_text_has_no_words(self):
        assert count_words("") == 0
        assert count_words("   \n ") == 0


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


def section(text="word " * 200, reviewer="dreadful", number=None, called=True):
    return SectionOutcome(
        text=text.strip(), reviewer=reviewer, tool_number=number, tool_called=called
    )


class TestScoreSections:
    def test_ten_good_sections_pass(self):
        assert score_sections([section() for _ in range(STORY_TURNS)]) is None

    def test_a_short_conversation_fails(self):
        """A model that stopped answering at turn six did not write the
        story, however good the six sections were."""
        why = score_sections([section() for _ in range(6)])
        assert why is not None
        assert "6" in why and str(STORY_TURNS) in why

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
        sections[7] = section(reviewer="   ")
        assert score_sections(sections, require_reviewer=True) is not None

    def test_the_reviewer_is_ignored_when_not_required(self):
        sections = [section(reviewer=None) for _ in range(STORY_TURNS)]
        assert score_sections(sections, require_reviewer=False) is None

    def test_an_uncalled_tool_fails(self):
        sections = [section(number=7) for _ in range(STORY_TURNS)]
        sections[2] = section(number=None, called=False)
        assert score_sections(sections, require_tool=True) is not None

    def test_a_tool_number_missing_from_the_prose_fails(self):
        """Calling the tool and ignoring what it returned is the failure this
        benchmark exists to catch."""
        sections = [section(text="word " * 200 + " 99", number=99)
                    for _ in range(STORY_TURNS)]
        sections[5] = section(text="word " * 200, number=99)
        why = score_sections(sections, require_tool=True)
        assert why is not None
        assert "99" in why

    def test_tools_are_ignored_when_not_required(self):
        assert score_sections([section(number=None, called=False)
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
        for messages in llm.calls:
            assert messages[0].role == MessageRole.SYSTEM
            assert messages[0].content == story.SYSTEM_PROMPT_TEXT

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
        assert "## Section 10" in result.trials[0].story


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

    def test_the_omen_number_is_noted_when_there_was_one(self):
        out = assemble_story([section(text="prose", number=4242)])
        assert "4242" in out

    def test_no_sections_says_so_rather_than_producing_a_blank(self):
        """The copy button must never hand over an empty clipboard with no
        explanation."""
        assert assemble_story([]) .strip() != ""

    def test_the_result_is_capped(self):
        from benchmarks.story import MAX_STORY_CHARS

        out = assemble_story([section(text="w " * 20000) for _ in range(10)])
        assert len(out) <= MAX_STORY_CHARS + 200  # cap plus the truncation note
        assert "truncated" in out
