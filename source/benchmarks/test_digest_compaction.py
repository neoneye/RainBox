"""Offline tests for benchmarks/digest_compaction.py: counting, edges, records,
prompts and parsing. No database and no model."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from benchmarks import digest_compaction as dc

T0 = datetime(1999, 3, 31, 9, 0)


def msg(i: int, sender: str, text: str, **kw) -> dc.Message:
    return dc.Message(id=i + 1, uuid=f"u{i}", sender=sender, text=text,
                      created_at=T0 + timedelta(minutes=i), **kw)


def room(n: int) -> list[dc.Message]:
    """n alternating messages; every reply links to its own run."""
    return [msg(i, "human", f"question number {i} about the weather today")
            if i % 2 == 0 else msg(i, "agent", f"reply {i}", run_uuid=f"r{i}")
            for i in range(n)]


def test_counted_drops_progress_and_streaming_rows_and_orders_by_id():
    rows = [msg(2, "agent", "b"), msg(0, "human", "a"),
            msg(1, "agent", "thinking", kind="progress"),
            msg(3, "agent", "half a repl", streaming=True)]
    assert [m.text for m in dc.counted(rows)] == ["a", "b"]


@pytest.mark.parametrize("n,expected", [
    (11, []),                    # 10 messages, no after-margin yet
    (12, [(0, 9)]),
    (21, [(0, 9)]),
    (22, [(0, 9), (10, 19)]),
    (142, [(k * 10, k * 10 + 9) for k in range(14)]),
])
def test_chunk_ranges_count_from_zero_and_need_the_after_margin(n, expected):
    assert dc.chunk_ranges(n) == expected


def test_appending_never_changes_an_earlier_chunk():
    before = dc.chunk_ranges(57)
    after = dc.chunk_ranges(83)
    assert after[:len(before)] == before


@pytest.mark.parametrize("n,start", [(10, 0), (30, 0), (39, 0), (40, 10),
                                     (49, 10), (142, 110)])
def test_window_starts_on_an_edge_and_holds_30_to_39(n, start):
    assert dc.window_start(n) == start
    assert n <= 30 or 30 <= n - start <= 39


def test_exact_repeats_keep_case_and_skip_short_messages():
    long = "count the letters in this sentence please"
    msgs = [msg(0, "human", long), msg(1, "agent", "x"),
            msg(2, "human", "again"), msg(3, "agent", "y"),
            msg(4, "human", "again"), msg(5, "agent", "z"),
            msg(6, "human", long.upper()), msg(7, "agent", "w"),
            msg(8, "human", long.replace(" ", "\r\n", 1).replace("\r\n", "\n")),
            msg(9, "agent", "v"),
            msg(10, "human", long)]
    exact, _ = dc.repeats(msgs)
    assert exact == [[10, 0]]


def test_line_endings_are_the_only_normalization():
    a = "first line of a longer request\nsecond line"
    b = "first line of a longer request\r\nsecond line"
    exact, _ = dc.repeats([msg(0, "human", a), msg(1, "human", b)])
    assert exact == [[1, 0]]


def test_extended_repeat_is_a_token_prefix_with_one_to_eight_added():
    base = "count the letters in the word"
    msgs = [msg(0, "human", base),
            msg(1, "human", base + ".. use python for counting"),
            msg(2, "human", base + " " + " ".join(["more"] * 9))]
    _, extended = dc.repeats(msgs)
    assert extended == [[1, 0]]


def test_run_counts_by_outcome_once_per_run():
    msgs = [msg(0, "human", "q"), msg(1, "agent", "a", run_uuid="r1"),
            msg(2, "agent", "a2", run_uuid="r1"), msg(3, "agent", "b", run_uuid="r2"),
            msg(4, "agent", "c", run_uuid="r3"), msg(5, "agent", "d", run_uuid="gone")]
    runs = {"r1": dc.Run("r1", "finished", "resolved"),
            "r2": dc.Run("r2", "finished", "partial"),
            "r3": dc.Run("r3", "stopped", None)}
    assert dc.run_counts(msgs, runs) == {"resolved": 1, "partial": 1,
                                         "status stopped": 1, "unknown run": 1}


def test_outcome_text_orders_outcomes_and_counts_repeats():
    assert dc.outcome_text({"failed": 1, "resolved": 3}, 2) == \
        "3 resolved, 1 failed, 2 exact repeats"
    assert dc.outcome_text({}, 0) == "no runs"


def test_level2_sums_ten_level1_chunks_and_lists_topics_in_order():
    msgs = room(142)
    runs = {f"r{i}": dc.Run(f"r{i}", "finished", "resolved") for i in range(142)}
    level1 = dc.build_level1(msgs, runs)
    for k, c in enumerate(level1):
        c.topics = ["weather"] if k % 2 == 0 else ["weather", f"t{k}"]
    level2 = dc.build_level2(level1)
    assert len(level2) == 1
    l2 = level2[0]
    assert (l2.first, l2.last, l2.exchanges) == (0, 99, 50)
    assert l2.runs == {"resolved": 50}
    assert l2.topics == ["weather", "t1", "t3", "t5", "t7", "t9"]


def test_prompt_marks_margins_as_context_and_passes_known_topics():
    msgs = room(30)
    chunk = dc.build_level1(msgs, {})[1]          # m10-m19
    prompt = dc.build_prompt(msgs, chunk, ["weather", "units"])
    user = prompt["user"]
    assert "Known topic names: weather, units" in user
    before, rest = user.split("The chunk, m10 to m19:")
    assert "[m8 human]" in before and "[m9 assistant]" in before
    chunk_part, after = rest.split("Context after the chunk")
    assert "[m10 human]" in chunk_part and "[m19 assistant]" in chunk_part
    assert "[m20 human]" in after and "[m21 assistant]" in after
    assert "[m22" not in user


def test_first_chunk_has_no_context_before():
    msgs = room(12)
    prompt = dc.build_prompt(msgs, dc.build_level1(msgs, {})[0], [])
    assert "Known topic names: none yet" in prompt["user"]
    assert "not to be described:\n(none)" in prompt["user"]


def test_long_messages_are_middle_cut_in_prompts():
    text = "a" * 3000 + "END"
    cut = dc._cut(text)
    assert len(cut) <= dc.PROMPT_MESSAGE_MAX_CHARS and cut.endswith("END")


def test_parse_reply_reads_the_two_lines_and_reports_problems():
    asked, topics, problems = dc.parse_reply(
        "asked: conversions between units\ntopics: Unit Conversion, weather")
    assert asked == "conversions between units"
    assert topics == ["unit-conversion", "weather"]
    assert problems == ["topic 'Unit Conversion' normalized to 'unit-conversion'"]

    asked, topics, problems = dc.parse_reply("**asked:** " + "x" * 130)
    assert topics == [] and "no topics: line" in problems
    assert any("characters" in p for p in problems)


def test_generate_bursts_with_cooldown_and_feeds_topics_forward():
    msgs = room(142)
    level1 = dc.build_level1(msgs, {})
    sleeps: list[float] = []
    prompts: list[str] = []

    def chat(url, model, prompt):
        prompts.append(prompt["user"])
        return f"asked: something\ntopics: topic-{len(prompts)}", {"wall_ms": 1}

    dc.generate(msgs, level1, "http://localhost:11434", "m", dry_run=False,
                burst=10, cooldown=60, sleep=sleeps.append, chat=chat, log=lambda s: None)
    assert sleeps == [60]                          # 14 calls: 10, pause, 4
    assert "Known topic names: none yet" in prompts[0]
    assert "Known topic names: topic-1, topic-2" in prompts[2]
    assert level1[13].topics == ["topic-14"]


def test_dry_run_builds_prompts_and_calls_nothing():
    msgs = room(30)
    level1 = dc.build_level1(msgs, {})

    def chat(*a):
        raise AssertionError("no model call in a dry run")

    dc.generate(msgs, level1, "u", "m", dry_run=True, burst=10, cooldown=60,
                sleep=lambda s: None, chat=chat, log=lambda s: None)
    assert all(c.prompt and c.reply is None for c in level1)


def test_a_hundred_stays_at_level1_until_the_window_is_a_hundred_past_it():
    msgs = room(142)
    level1 = dc.build_level1(msgs, {})
    w, block = dc.earlier_block(142, level1, dc.build_level2(level1))
    assert w == 110
    assert 'level="2"' not in block
    assert block.count('<chunk level="1"') == 11        # m0-m109

    msgs = room(242)
    level1 = dc.build_level1(msgs, {})
    w, block = dc.earlier_block(242, level1, dc.build_level2(level1))
    assert w == 210
    assert block.count('<chunk level="2"') == 1 and 'level="2" messages="0-99"' in block
    assert block.count('<chunk level="1"') == 11        # m100-m209
    assert 'from="1999-03-31" to="1999-03-31"' in block


@pytest.mark.parametrize("w, start", [(0, 0), (90, 0), (100, 0), (190, 0),
                                      (200, 100), (290, 100), (300, 200)])
def test_level1_keeps_100_to_190_messages_behind_the_window(w, start):
    assert dc.level1_start(w) == start


def test_excerpt_is_cut_at_a_word_edge_and_marked():
    assert dc.excerpt("  how tall\n is the tower ") == "how tall is the tower"
    long = "please tell me " + "about the history of the old harbour " * 4
    cut = dc.excerpt(long)
    assert cut.endswith("…") and len(cut) <= dc.EXCERPT_MAX_CODEPOINTS + 1
    assert long.startswith(cut[:-1] + " ")              # cut between words
    assert dc.excerpt("x" * 200) == "x" * 80 + "…"      # no space to cut at


def test_threads_open_on_a_new_request_and_absorb_follow_ups_and_repeats():
    q1 = "how many days are there until the first of may"
    q2 = "which boards exist on the kanban page right now"
    msgs = [msg(0, "human", q1), msg(1, "agent", "a", run_uuid="r1"),
            msg(2, "human", "again"), msg(3, "agent", "b", run_uuid="r3"),
            msg(4, "human", q1), msg(5, "agent", "c", run_uuid="r5"),
            msg(6, "human", q1 + " counted in weeks"), msg(7, "agent", "d"),
            msg(8, "human", q2), msg(9, "agent", "e", run_uuid="r9"),
            msg(10, "human", "thanks"), msg(11, "agent", "f")]
    exact, extended = dc.repeats(msgs)
    openers = dc.thread_openers(msgs, exact, extended)
    assert openers == [0] * 8 + [8] * 4
    runs = {"r1": dc.Run("r1", "finished", "resolved"),
            "r3": dc.Run("r3", "failed", "failed"),
            "r9": dc.Run("r9", "finished", "resolved")}
    a, b = dc.build_threads(msgs, 0, 9, openers, runs, exact)
    assert (a.first, a.last, a.exchanges, a.exact_repeats) == (0, 7, 4, 1)
    assert a.runs == {"resolved": 1, "failed": 1, "unknown run": 1}
    assert a.excerpt == q1 and not a.continues
    assert (b.first, b.last, b.exchanges, b.excerpt) == (8, 9, 1, q2)
    # The next chunk begins inside the second thread and keeps its opening words.
    (c,) = dc.build_threads(msgs, 10, 11, openers, runs, exact)
    assert c.continues and c.excerpt == q2


def test_a_repeat_of_an_older_thread_opens_a_new_thread():
    q1 = "how many days are there until the first of may"
    q2 = "which boards exist on the kanban page right now"
    msgs = [msg(0, "human", q1), msg(1, "agent", "a"),
            msg(2, "human", q2), msg(3, "agent", "b"),
            msg(4, "human", q1), msg(5, "agent", "c")]
    exact, extended = dc.repeats(msgs)
    assert dc.thread_openers(msgs, exact, extended) == [0, 0, 2, 2, 4, 4]


def test_appending_never_changes_an_earlier_thread():
    msgs = room(40)
    short = dc.thread_openers(msgs[:20], *dc.repeats(msgs[:20]))
    assert dc.thread_openers(msgs, *dc.repeats(msgs))[:20] == short


def test_level1_chunk_renders_its_threads_with_ranges_and_opening_words():
    msgs = room(42)
    chunk = dc.build_level1(msgs, {})[0]
    xml = dc.render_chunk_xml(chunk)
    assert xml.count("<topic ") == 5
    assert ('<topic slug="?" messages="0-1" exchanges="1" outcome="1 unknown run" '
            'opened_with="question number 0 about the weather today"/>') in xml


def test_refuses_output_inside_the_repo_and_remote_models(tmp_path):
    with pytest.raises(SystemExit):
        dc.check_out(Path(dc.__file__).parent / "out")
    dc.check_out(tmp_path)
    with pytest.raises(SystemExit):
        dc.check_local("http://example.com:11434", "gemma4:e4b-mlx")
    with pytest.raises(SystemExit):
        dc.check_local(dc.DEFAULT_URL, "some-model:cloud")
