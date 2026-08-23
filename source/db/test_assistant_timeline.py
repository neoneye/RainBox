"""The assistant timeline accounts for the run's whole wall-clock.

A bar per model call leaves the rest of the run as empty space, and empty space
is the one thing an operator cannot investigate. Recorded phases become rows,
and whatever they still do not cover becomes a measured `unaccounted` row.

Fakes rather than DB rows: this is layout arithmetic over attributes, and the
persistence of a step is tested elsewhere.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import db
from db.assistant import UNACCOUNTED_MIN_MS

T0 = datetime(2026, 8, 23, 23, 4, 12, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _step(action, *, at, ms, phases=None, code_driven=True, phase="observed"):
    """One trace row, with optional recorded action phases."""
    data = {}
    if phases is not None:
        data["timing"] = {"phases": [
            {"name": n, "started_at": _at(s).isoformat(), "ms": int(d * 1000)}
            for n, s, d in phases
        ]}
    return SimpleNamespace(
        uuid=uuid4(), action=action, phase=phase, code_driven=code_driven,
        requested_at=_at(at), created_at=_at(at + ms / 1000),
        duration_ms=int(ms), system_prompt="p", model_uuid=None,
        input_tokens=10, output_tokens=5, rejected_attempts=[],
        observation={"data": data},
    )


def _run(started=0.0, finished=None):
    return SimpleNamespace(started_at=_at(started),
                           finished_at=_at(finished) if finished else None)


def _labels(calls):
    return [(c["label"], c["kind"], c.get("depth")) for c in calls]


def test_a_recorded_phase_becomes_a_row_indented_under_its_step():
    """memory_query records what its action spent; the waterfall never drew it,
    so the time read as a gap."""
    step = _step("memory_query", at=20, ms=11800,
                 phases=[("claim retrieval", 31.8, 10.4)])

    calls = db.assistant_llm_calls([step])

    assert ("claim retrieval", "phase", 1) in _labels(calls)


def test_a_call_inside_a_phase_is_nested_under_it():
    """The recall filter's model call runs inside the recall filter phase, so
    it reads as contained rather than as a sibling that happens to overlap."""
    outer = _step("memory_query", at=20, ms=11800,
                  phases=[("recall filter", 42, 22.8)])
    inner = _step("recall_filter", at=52, ms=12700)

    calls = db.assistant_llm_calls([outer, inner])
    by_label = {c["label"]: c for c in calls}

    assert by_label["recall filter"]["depth"] == 1
    assert by_label["recall_filter"]["depth"] == 2
    assert by_label["memory_query"]["depth"] == 0


def test_a_gap_between_calls_becomes_an_unaccounted_row():
    first = _step("acceptance_criteria", at=0, ms=2000)
    second = _step("reply", at=30, ms=2000)

    calls = db.assistant_llm_calls([first, second], run=_run())
    gaps = [c for c in calls if c["kind"] == "unaccounted"]

    assert len(gaps) == 1
    assert gaps[0]["duration_ms"] == 28000
    assert gaps[0]["depth"] == 0


def test_a_gap_shorter_than_the_threshold_is_not_drawn():
    """Sub-second scheduling jitter between two adjacent calls is not a
    finding, and a row per 0.1s would bury the ones that are."""
    first = _step("acceptance_criteria", at=0, ms=2000)
    second = _step("reply", at=2 + (UNACCOUNTED_MIN_MS - 200) / 1000, ms=2000)

    calls = db.assistant_llm_calls([first, second], run=_run())

    assert not [c for c in calls if c["kind"] == "unaccounted"]


def test_a_hole_inside_a_phase_is_still_reported():
    """The case that motivates computing gaps per level. Globally the phase
    covers this time, so a whole-run complement finds nothing; measured against
    the phase's own children, ten seconds are missing."""
    outer = _step("memory_query", at=0, ms=1000,
                  phases=[("recall filter", 1, 22.8)])
    inner = _step("recall_filter", at=11, ms=12700)

    calls = db.assistant_llm_calls([outer, inner], run=_run())
    holes = [c for c in calls if c["kind"] == "unaccounted"]

    assert any(c["depth"] == 2 and 9_000 <= c["duration_ms"] <= 11_000
               for c in holes), _labels(calls)


def test_spans_do_not_reach_the_run_totals():
    """assistant_run_stats reads this same enumeration. A phase overlaps the
    calls inside it and an unaccounted row is the absence of a call, so
    counting either would inflate the call count and double-count seconds
    already inside a model bar."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 12, 10.4)])
    gap_maker = _step("reply", at=40, ms=2000)

    stats = db.assistant_run_stats([step, gap_maker], run=_run())
    calls = db.assistant_llm_calls([step, gap_maker], run=_run())

    assert [c["kind"] for c in calls].count("phase") == 1
    assert [c["kind"] for c in calls].count("unaccounted") >= 1
    assert stats["calls"] == 2
    assert stats["duration_ms"] == 13800
    assert stats["input_tokens"] == 20


def test_a_run_with_no_phases_gains_only_top_level_gaps():
    """Nothing else about an uninstrumented run's timeline changes."""
    steps = [_step("reply", at=0, ms=2000), _step("reply_audit", at=2, ms=1000)]

    calls = db.assistant_llm_calls(steps, run=_run())

    assert not [c for c in calls if c["kind"] == "phase"]
    assert [c["label"] for c in calls if c["kind"] != "unaccounted"] == [
        "reply", "reply_audit"]


def test_the_enumeration_still_works_without_a_run():
    """The trace is read in places that have steps but no run row; gaps simply
    cannot be bounded there, and the calls must still enumerate."""
    calls = db.assistant_llm_calls([_step("reply", at=0, ms=2000)])

    assert [c["label"] for c in calls] == ["reply"]


def test_a_phase_covers_its_time_at_the_top_level():
    """A phase is drawn indented but it is still measured wall-clock. Counting
    only depth-0 rows made the whole action look empty and reported a gap the
    length of every phase in it."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4),
                         ("recall filter", 22.2, 22.8)])
    inner = _step("recall_filter", at=32, ms=12700)

    calls = db.assistant_llm_calls([step, inner], run=_run(finished=45))
    top_gaps = [c for c in calls
                if c["kind"] == "unaccounted" and c["depth"] == 0]

    assert top_gaps == [], _labels(calls)


def test_a_phase_with_no_calls_inside_it_is_not_a_hole():
    """`claim retrieval` says exactly what those ten seconds were. Reporting
    them as unaccounted as well double-counts them and buries the one gap that
    genuinely has no explanation."""
    step = _step("memory_query", at=0, ms=1000,
                 phases=[("claim retrieval", 1, 10.4)])

    calls = db.assistant_llm_calls([step], run=_run(finished=11.4))

    assert not [c for c in calls if c["kind"] == "unaccounted"], _labels(calls)
