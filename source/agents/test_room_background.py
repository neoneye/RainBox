"""The room's background for a prompt: which chunks, in what words, at what
size. Pure: hand-built rows, no database."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from agents import room_background as rb
from agents.room_chunks import content_hash

DAY0 = datetime(1999, 10, 4, 9, 0, tzinfo=UTC)


def _messages(n: int, per_day: int = 10) -> list[dict]:
    """Counted messages with ids 1..n, `per_day` to a day."""
    return [{"id": i, "text": f"message number {i}",
             "created_at": DAY0 + timedelta(days=(i - 1) // per_day, minutes=i)}
            for i in range(1, n + 1)]


def _part(wanted: str = "the opening hours of the harbour office",
          result: str = "it opens at nine", **more) -> dict:
    part = {"summary": wanted, "result": result, "requests": 1,
            "reactions": ["moved_on"], "signals": {"runs": {"resolved": 1}}}
    part.update(more)
    return part


def _chunks(messages: list[dict], count: int, size: int = 10, **more) -> list[dict]:
    """The first `count` chunks of `size` over `messages`, hashes matching."""
    out = []
    for seq in range(count):
        inside = messages[seq * size:(seq + 1) * size]
        chunk = {"uuid": uuid4(), "first_id": inside[0]["id"], "last_id": inside[-1]["id"],
                 "content_hash": content_hash(inside), "prompt_version": 19,
                 "summary": f"subject {seq}", "parts": [_part()]}
        chunk.update(more)
        out.append(chunk)
    return out


def _room(n: int, chunks: int | None = None):
    """A room of `n` messages, summarized as far as it can be, and the id
    its window starts at (the last 30), or 0 when all of it is window."""
    messages = _messages(n)
    made = _chunks(messages, (n - 2) // 10 if chunks is None else chunks)
    return messages, made, (messages[-30]["id"] if n > 30 else 0)


# --- which chunks ----------------------------------------------------------------

def test_a_room_inside_its_window_has_no_background():
    for n in (29, 30):
        messages, chunks, _window = _room(n)
        assert rb.build(chunks, messages, None, None) == rb.Background()


def test_the_chunk_that_holds_the_windows_first_message_is_shown():
    # 45 messages: the window starts at message 16, inside the second chunk.
    messages, chunks, window = _room(45)
    assert window == 16
    shown = rb.shown_chunks(chunks, window)
    assert [(c["first_id"], c["last_id"]) for c in shown] == [(1, 10), (11, 20)]
    # A chunk that begins where the window does is all window, and is left out.
    assert len(rb.shown_chunks(chunks, 11)) == 1


def test_a_long_room_shows_its_newest_sixteen_and_says_what_is_before():
    messages, chunks, window = _room(400)
    shown = rb.shown_chunks(chunks, window)
    # The window starts at message 371, inside no chunk: 361-370 is the newest.
    assert len(shown) == rb.EARLIER_CHUNKS
    assert (shown[0]["first_id"], shown[-1]["last_id"]) == (211, 370)
    older = rb.Older(210, "1999-10-04", "1999-10-24")
    out = rb.build(chunks, [m for m in messages if m["id"] >= 211], window, None, older=older)
    lines = out.earlier.split("\n")
    assert lines[0] == "Before this: 210 messages from 1999-10-04 to 1999-10-24."
    assert out.record["chunks"] == 16 and out.record["near"] == 4
    assert out.record["trimmed"] == 0
    assert out.record["earlier_chars"] == len(out.earlier) <= rb.EARLIER_MAX_CHARS


def test_a_room_with_messages_and_no_chunk_has_no_block():
    messages = _messages(80)
    assert rb.build([], messages, messages[-30]["id"], None) == rb.Background()


def test_the_overlap_of_a_series_does_not_change_the_lines():
    messages, chunks, window = _room(60)
    texts = set()
    for overlap in (0, 2, 5):
        series = [dict(c, overlap=overlap) for c in chunks]
        texts.add(rb.build(series, messages, window, None).earlier)
    assert len(texts) == 1


def test_the_series_that_reaches_furthest_is_read():
    # The current prompt's series is behind an older one.
    assert rb.pick_series([(10, 2, 19, 500), (10, 2, 20, 120)]) == (10, 2, 19)
    # Between equals, the newer prompt.
    assert rb.pick_series([(10, 2, 19, 500), (10, 2, 20, 500)]) == (10, 2, 20)
    assert rb.pick_series([]) is None


# --- how it went -----------------------------------------------------------------

def test_how_a_part_went_is_said_from_its_records():
    said = rb.how_it_went
    # No tool behind the reply: it is what the assistant said.
    assert said(_part()) == "answered then: it opens at nine."
    # A tool ran for it.
    assert said(_part(grounding="tool")) == "came of it: it opens at nine."
    assert said(_part(grounding="tool failed")) == "answered after its tool failed."
    assert said(_part(grounding="tool failed", signals={
        "tools": {"program": {"ran": 0, "failed": 1}}})) == "answered after its program failed."
    assert said(_part(grounding="output not used")) == (
        "the answer does not match what its program printed.")
    for rejected in (_part(signals={"down": 1, "runs": {"resolved": 1}}),
                     _part(reactions=["questioned"]),
                     _part(reactions=["corrected"], signals={"down": 1})):
        assert said(rejected) == "the answer was rejected."
    assert said(_part(signals={"runs": {"failed": 1}})) == "nothing was delivered."
    # Partway is not nothing: the line says what there was.
    assert said(_part(signals={"runs": {"partial": 1}})) == (
        "it was only partly delivered. what there was: it opens at nine.")
    assert not rb.settled(_part(signals={"runs": {"partial": 1}}))


def test_a_correction_is_not_a_rejection():
    part = _part(reactions=["corrected", "moved_on"], result="it opens at ten")
    assert rb.how_it_went(part) == "the user corrected the answer. after that: it opens at ten."
    assert rb.settled(part)
    messages, chunks, window = _room(90)
    chunks[0]["parts"] = [dict(part, summary="the opening hours")]
    far = rb.build(chunks, messages, window, None).earlier.split("\n")[0]
    assert far == "1999-10-04: the opening hours (corrected by the user)"
    # No result stored: the line says nothing more.
    assert rb.how_it_went(_part(result="")) == ""


def test_asking_again_is_counted_and_not_called_rejecting():
    part = _part(reactions=["asked_again", "moved_on"], requests=3, result="two")
    assert rb.how_it_went(part) == "asked 3 times. the last answer: two."
    assert rb.settled(part)
    # A repeat seen only after the part still counts as twice.
    assert rb.how_it_went(_part(reactions=["asked_again"])).startswith("asked 2 times.")


def test_a_stored_no_reaction_yet_is_a_plain_result():
    part = _part(reactions=["not_shown"], calibration="unclear",
                 calibration_basis="the room shows no reaction yet")
    assert rb.how_it_went(part) == "answered then: it opens at nine."


def test_a_result_is_withheld_after_the_rows_that_speak_against_it():
    diary = {"tools": {"diary query": {"ran": 1, "failed": 0}}, "runs": {"resolved": 1}}
    part = _part(grounding="tool", signals=diary)
    assert rb.how_it_went(part) == "came of it: it opens at nine."
    assert rb.how_it_went(part, diary_allowed=False) == "answered from the diary."
    # A part that read nothing from the diary keeps its result.
    assert rb.how_it_went(_part(), diary_allowed=False) == "answered then: it opens at nine."
    # No tools recorded for the part: nothing says it read the diary.
    assert rb.how_it_went(_part(), diary_allowed=False, tools_recorded=False) == "answered then."
    assert rb.how_it_went(_part(), before_invalidation=True) == (
        "answered then, before stored facts were declared stale.")
    # The failure is the news.
    rejected = _part(grounding="tool", signals=dict(diary, down=1))
    assert rb.how_it_went(rejected, diary_allowed=False) == "the answer was rejected."
    again = _part(reactions=["asked_again"], requests=2, signals=diary)
    assert rb.how_it_went(again, diary_allowed=False) == (
        "asked 2 times. answered from the diary.")


def test_wanted_result_and_subjects_are_cut_at_whitespace():
    long = "word " * 200
    line = rb.how_it_went(_part(result=long))
    assert len(line) <= len("answered then: ") + rb.RESULT_MAX_CHARS and line.endswith("word…")
    messages, chunks, window = _room(90)        # two far chunks, then four near
    chunks[0].update(summary=long, parts=[])     # no parts: its subjects
    chunks[2]["parts"] = [_part(wanted=long)]
    far, _far, _dates, near = rb.build(chunks, messages, window, None).earlier.split("\n")[:4]
    assert len(far) <= len("1999-10-04: ") + rb.SUBJECTS_MAX_CHARS and far.endswith("word…")
    assert near.startswith("- wanted: word word") and "word… answered then" in near


# --- the lines -------------------------------------------------------------------

def test_near_chunks_are_part_by_part_and_far_ones_a_line():
    messages, chunks, window = _room(90)       # 8 chunks; 6 begin before message 61
    chunks[0]["parts"] = [
        _part(wanted="the tide table", signals={"down": 1}),
        _part(wanted="the ferry times", reactions=["asked_again"], requests=3),
        _part(wanted="a settled thing")]
    out = rb.build(chunks, messages, window, None)
    lines = out.earlier.split("\n")
    assert lines[0] == ("1999-10-04: the tide table (not settled: the answer was rejected), "
                        "the ferry times (asked 3 times), a settled thing")
    assert lines[1] == "1999-10-05: the opening hours of the harbour office"
    assert lines[2:4] == [
        "1999-10-06:",
        "- wanted: the opening hours of the harbour office. answered then: it opens at nine."]
    assert out.record["near"] == 4 and out.record["chunks"] == 6
    assert "Before this" not in out.earlier


def test_what_the_user_stated_is_on_the_chunks_lines():
    messages, chunks, window = _room(90)
    said = ["Tasks run in child processes so a hung one can be stopped.",
            "The cloud is not trusted with the diary"]
    chunks[0]["statements"] = said                 # far
    chunks[2]["statements"] = said[:1]             # near
    lines = rb.build(chunks, messages, window, None).earlier.split("\n")
    assert lines[0] == ("1999-10-04: the opening hours of the harbour office; "
                        "the user said: Tasks run in child "
                        "processes so a hung one can be stopped; the user said: "
                        "The cloud is not trusted with the diary")
    assert lines[2:5] == [
        "1999-10-06:",
        "- the user said: Tasks run in child processes so a hung one can be stopped.",
        "- wanted: the opening hours of the harbour office. answered then: it opens at nine."]
    # From the prompt that has them copied, they are the user's own words.
    chunks[2]["prompt_version"] = rb.QUOTED_FROM
    assert ('- the user said: "Tasks run in child processes so a hung one can be stopped."'
            in rb.build(chunks, messages, window, None).earlier.split("\n"))
    # A chunk written before statements were kept has none, and no line.
    chunks[2]["statements"] = None
    assert "the user said" not in rb.build(chunks, messages, window, None).earlier.split("\n")[3]


def test_a_hole_and_a_lagging_tail_are_each_one_line():
    messages = _messages(120)
    chunks = _chunks(messages, 6)
    del chunks[2]                                # the third chunk's job failed for good
    window = messages[-30]["id"]                 # 91; chunks reach message 60
    lines = rb.build(chunks, messages, window, None).earlier.split("\n")
    assert "1999-10-06: 10 messages not summarized." in lines
    assert lines[-1] == "1999-10-10 to 1999-10-12: 30 messages not summarized."
    # The tail is there with every chunk in place too.
    whole = rb.build(_chunks(messages, 6), messages, window, None).earlier
    assert whole.split("\n")[-1] == lines[-1] and "10 messages not" not in whole


def test_a_changed_chunk_shows_its_dates_alone():
    messages, chunks, window = _room(45)
    messages[3]["text"] = "edited since"
    lines = rb.build(chunks, messages, window, None).earlier.split("\n")
    assert lines[0] == "1999-10-04: (changed since it was summarized)"
    # Every message of a chunk deleted: the chunk has no line.
    gone = [m for m in messages if m["id"] > 10]
    assert not rb.build(chunks, gone, window, None).earlier.startswith("1999-10-04")


def test_a_part_before_a_facts_invalidation_keeps_what_was_wanted_only():
    messages, chunks, window = _room(45)
    out = rb.build(chunks, messages, window, None, facts_invalidated_id=15)
    assert ("- wanted: the opening hours of the harbour office. answered then, "
            "before stored facts were declared stale.") in out.earlier
    assert "it opens at nine" in out.earlier        # the chunk after the marker
    assert out.record["results_withheld"] == 1


def test_parts_of_an_older_chunk_prompt_show_no_result_to_a_remote_model():
    messages, chunks, window = _room(45)
    chunks[0]["prompt_version"] = 12
    out = rb.build(chunks, messages, window, None, diary_allowed=False)
    first, second = out.earlier.split("\n")[1], out.earlier.split("\n")[3]
    assert first.endswith("harbour office. answered then.")
    assert second.endswith("answered then: it opens at nine.")


# --- size ------------------------------------------------------------------------

def test_wordy_near_chunks_leave_the_far_lines_in_the_block():
    messages, chunks, window = _room(200)
    wordy = [_part(wanted="a thing wanted", result="r" * 300) for _ in range(4)]
    for chunk in chunks:
        chunk["parts"] = wordy
        chunk["summary"] = "a subject"
    shown = rb.shown_chunks(chunks, window)
    out = rb.build(chunks, [m for m in messages if m["id"] >= shown[0]["first_id"]], window, None)
    assert len(out.earlier) <= rb.EARLIER_MAX_CHARS
    assert out.record["near"] < rb.NEAR_CHUNKS
    assert out.record["chunks"] == 16 and out.record["trimmed"] == 0


def test_trimming_drops_the_oldest_line_and_says_so():
    messages = _messages(130)
    chunks = _chunks(messages, 10, parts=[_part("s" * 150)])
    window = messages[-30]["id"]
    # A room of ten chunks has no such line until one is dropped.
    assert "Before this" not in rb.render_earlier(chunks, messages, window)[0]
    text, record = rb.render_earlier(chunks, messages, window, limit=1_700)
    assert len(text) <= 1_700 and record["trimmed"] == 1
    lines = text.split("\n")
    assert lines[0] == "Before this: 10 messages on 1999-10-04."
    assert lines[1].startswith("1999-10-05: ")
    text, record = rb.render_earlier(chunks, messages, window, limit=1_500)
    assert record["trimmed"] == 3
    assert text.startswith("Before this: 30 messages from 1999-10-04 to 1999-10-06.")
    # What was already before the chunks is counted with what was dropped.
    text, _record = rb.render_earlier(
        chunks, messages, window, limit=1_700,
        older=rb.Older(25, "1999-09-01", "1999-10-03"))
    assert text.startswith("Before this: 35 messages from 1999-09-01 to 1999-10-04.")


def test_the_same_rows_give_the_same_bytes():
    messages, chunks, window = _room(250)
    first = rb.build(chunks, messages, window, None)
    assert first == rb.build(chunks, messages, window, None)


def test_the_background_is_sent_only_where_the_prompt_has_room():
    # 20,000 + 5,000 + the reserve is 69,000 characters: 23,000 tokens at 3.
    assert rb.fits(20_000, 5_000, 23_000)
    assert not rb.fits(20_000, 5_000, 22_999)
    assert not rb.fits(60_000, 5_000, 32_768)


def test_what_the_user_stated_outlasts_its_chunk():
    messages = _messages(130)
    chunks = _chunks(messages, 10, parts=[_part("s" * 150)])
    window = messages[-30]["id"]
    chunks[0]["statements"] = ["Tasks run in child processes so a hung one can be stopped."]
    chunks[1]["statements"] = ["The assistant is small local models composed together."]
    text, record = rb.render_earlier(chunks, messages, window, limit=1_500)
    assert record["trimmed"] >= 2 and record["said_before"] == 2
    lines = text.split("\n")
    assert lines[0].startswith("Before this: ") and lines[0].endswith(" The user said in them:")
    assert lines[1:3] == [
        "- Tasks run in child processes so a hung one can be stopped.",
        "- The assistant is small local models composed together."]
    # The statements of the chunks before the shown ones come ahead of them,
    # and the newest are the ones kept.
    before = [f"A thing said long ago, number {n}." for n in range(rb.SAID_BEFORE)]
    text, record = rb.render_earlier(
        chunks, messages, window, limit=1_800, said_before=before,
        older=rb.Older(25, "1999-09-01", "1999-10-03"))
    lines = text.split("\n")
    assert record["said_before"] == rb.SAID_BEFORE
    assert lines[1] == "- A thing said long ago, number 2."
    assert lines[rb.SAID_BEFORE] == "- The assistant is small local models composed together."
    # Nothing was dropped and nothing lies before: no such lines.
    text, record = rb.render_earlier(chunks, messages, window)
    assert "said in them" not in text and record["said_before"] == 0


# --- topics ----------------------------------------------------------------------

_part_numbers = iter(range(1_000, 10_000))


def _row(name: str, parts: int, messages: int, chunk, last: str, about: str = "",
         members: list | None = None) -> dict:
    """A tallied topic. Its one member is a part of its own unless
    `members` gives the `(chunk, part)` pairs it counts."""
    pairs = members or [(chunk, next(_part_numbers))]
    return {"topic": SimpleNamespace(name=name, explanation=about), "parts": parts,
            "messages": messages, "last": last,
            "members": [SimpleNamespace(chunk_uuid=c, part_index=i) for c, i in pairs]}


def test_two_topics_over_the_same_parts_are_listed_once():
    shown = uuid4()
    five = [(shown, i) for i in range(5)]
    tallied = {"axes": {
        "task": [_row("working out a root", 5, 20, shown, "1999-10-20", members=five),
                 _row("counting letters", 2, 8, shown, "1999-10-21", members=five[:2]),
                 _row("planning the trip", 2, 8, shown, "1999-10-19")],
        "thing": [_row("the root of a number", 2, 8, shown, "1999-10-20", members=five[3:]),
                  _row("the word harbour", 2, 8, shown, "1999-10-21", members=five[:2])]}}
    names = {r["topic"].name for r in rb.pick_topics(tallied, {shown})}
    # A topic inside a larger one goes; of two that are the same parts, the
    # named thing stays.
    assert names == {"working out a root", "planning the trip"}
    tallied["axes"]["task"].pop(0)
    names = {r["topic"].name for r in rb.pick_topics(tallied, {shown})}
    assert names == {"the root of a number", "the word harbour", "planning the trip"}


def test_a_small_recent_topic_is_listed_ahead_of_a_larger_old_one():
    shown, old = uuid4(), uuid4()
    tallied = {"axes": {
        "task": [_row("looking up tides", 40, 300, old, "1999-06-02"),
                 _row("planning the trip", 2, 8, shown, "1999-10-20")],
        "thing": [_row("the ferry", 9, 60, old, "1999-07-11", about="the boat to the island")]}}
    rows = rb.pick_topics(tallied, {shown})
    # After the recent one, a named thing and a kind of task by turns.
    assert [r["topic"].name for r in rows] == [
        "planning the trip", "the ferry", "looking up tides"]
    assert rb.render_topics(rows).split("\n") == [
        "- planning the trip (2 times)",
        "- the ferry: the boat to the island (9 times, last summarized 1999-07-11)",
        "- looking up tides (40 times, last summarized 1999-06-02)"]


def test_topics_stop_at_their_number_and_their_size():
    shown, old = uuid4(), uuid4()
    tallied = {"axes": {
        "task": [_row(f"recent {i}", 2, 4, shown, f"1999-10-{10 + i}") for i in range(8)],
        "thing": [_row(f"old {i}", 3, 100 - i, old, "1999-06-02") for i in range(8)]}}
    rows = rb.pick_topics(tallied, {shown})
    names = [r["topic"].name for r in rows]
    assert len(rows) == rb.TOPICS_MAX
    # The five newest of the shown chunks, then the largest of the rest.
    assert names[:5] == [f"recent {i}" for i in (7, 6, 5, 4, 3)]
    assert names[5] == "old 0"
    assert len(rb.render_topics(rows, limit=120).split("\n")) < len(rows)
    assert rb.render_topics([]) == ""


def test_a_block_is_its_text_between_its_tags():
    assert rb.as_block("room_topics", "- a (2 times)") == (
        "<room_topics>\n- a (2 times)\n</room_topics>")
    assert rb.as_block("room_topics", "") == ""
