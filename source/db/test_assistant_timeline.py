"""The assistant timeline is a waterfall of leaf activities.

Every bar is one thing that spent time: an LLM call, an embedding call, or a
measured stretch of an action's own work. No bar contains another, because a
container hides whatever sits inside it — which is exactly the gap this page
exists to expose. One activity ends where the next begins, and a gap means
something really is unmeasured.

Fakes rather than DB rows: this is interval arithmetic over attributes, and the
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


def _step(action, *, at, ms, phases=None, embeds=None, code_driven=True,
          phase="observed"):
    """One trace row, with optional recorded action phases and embed calls."""
    data: dict = {}
    timing: dict = {}
    if phases is not None:
        timing["phases"] = [
            {"name": n, "started_at": _at(s).isoformat(), "ms": int(d * 1000)}
            for n, s, d in phases
        ]
    if embeds is not None:
        timing["embeddings"] = {"calls": [
            {"text": t, "requested_at": _at(s).isoformat(), "ms": int(d * 1000)}
            for t, s, d in embeds
        ]}
    if timing:
        data["timing"] = timing
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


def _rows(calls):
    return [(c["label"], c["kind"], round((c["duration_ms"] or 0) / 1000, 1))
            for c in calls]


def _covers_continuously(calls) -> bool:
    """True if the rows tile their own span with no hole over the threshold."""
    placed = sorted(((c["start"], c["start"]
                      + timedelta(milliseconds=c["duration_ms"] or 0))
                     for c in calls if c["start"]), key=lambda p: p[0])
    cursor = placed[0][0]
    for start, end in placed:
        if (start - cursor).total_seconds() * 1000 > UNACCOUNTED_MIN_MS:
            return False
        cursor = max(cursor, end)
    return True


def test_no_row_contains_another():
    """The property the whole layout rests on. A bar spanning other bars hides
    them, and what it hid here was a ten-second stall."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("recall filter", 11.8, 22.8)])
    inner = _step("recall_filter", at=21.8, ms=12700)

    calls = db.assistant_llm_calls([step, inner], run=_run(finished=34.5))

    spans = [(c["start"],
              c["start"] + timedelta(milliseconds=c["duration_ms"] or 0))
             for c in calls if c["start"] and c["duration_ms"]]
    for i, (a_start, a_end) in enumerate(spans):
        for j, (b_start, b_end) in enumerate(spans):
            if i == j:
                continue
            assert not (a_start <= b_start and b_end <= a_end), _rows(calls)


def test_a_phase_contributes_only_the_time_no_call_occupies():
    """`recall filter` ran 22.8s and its model call took 12.7s of that. The
    bar is the remaining 10.1s of the phase's own work — not a 22.8s span
    drawn over the call."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("recall filter", 11.8, 22.8)])
    inner = _step("recall_filter", at=21.8, ms=12700)

    calls = db.assistant_llm_calls([step, inner], run=_run(finished=34.5))

    assert ("recall filter", "activity", 10.0) in _rows(calls)
    assert not [c for c in calls if (c["duration_ms"] or 0) == 22800]


def test_a_phase_with_no_calls_inside_it_is_one_whole_bar():
    """Nothing to subtract, so the phase IS the activity."""
    step = _step("memory_query", at=0, ms=1000,
                 phases=[("claim retrieval", 1, 10.4)])

    calls = db.assistant_llm_calls([step], run=_run(finished=11.4))

    assert ("claim retrieval", "activity", 10.4) in _rows(calls)


def test_an_embedding_call_is_its_own_bar_inside_a_phase():
    """Embedding is a real model on the same runtime; it gets a bar, and the
    phase around it shrinks by exactly that much."""
    step = _step("memory_query", at=0, ms=1000,
                 phases=[("claim retrieval", 1, 10.0)],
                 embeds=[("what languages do I know", 3, 2.0)])

    calls = db.assistant_llm_calls([step], run=_run(finished=11))
    rows = _rows(calls)

    assert any(k == "embedding" and s == 2.0 for _, k, s in rows), rows
    # 10s of phase minus the 2s embed, as two segments around it.
    activity = [s for _, k, s in rows if k == "activity"]
    assert sum(activity) == 8.0, rows


def test_the_timeline_is_continuous_when_everything_is_measured():
    """One activity finishes where the next starts. This is the shape the page
    is supposed to have, and a break in it is the signal."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4),
                         ("recall filter", 22.2, 22.8)])
    inner = _step("recall_filter", at=32, ms=12700)

    calls = db.assistant_llm_calls([step, inner], run=_run(finished=45))

    assert _covers_continuously(calls), _rows(calls)
    assert not [c for c in calls if c["kind"] == "unaccounted"], _rows(calls)


def test_time_nothing_measured_is_still_reported():
    """A stretch no call and no phase covers is what an unaccounted bar is
    for — and now it is the only thing one can mean."""
    first = _step("acceptance_criteria", at=0, ms=2000)
    second = _step("reply", at=30, ms=2000)

    calls = db.assistant_llm_calls([first, second], run=_run())
    gaps = [c for c in calls if c["kind"] == "unaccounted"]

    assert len(gaps) == 1
    assert gaps[0]["duration_ms"] == 28000


def test_a_gap_shorter_than_the_threshold_is_not_drawn():
    """Sub-second jitter between adjacent calls is not a finding, and a row per
    0.1s would bury the ones that are."""
    first = _step("acceptance_criteria", at=0, ms=2000)
    second = _step("reply", at=2 + (UNACCOUNTED_MIN_MS - 200) / 1000, ms=2000)

    calls = db.assistant_llm_calls([first, second], run=_run())

    assert not [c for c in calls if c["kind"] == "unaccounted"]


def test_spans_do_not_reach_the_run_totals():
    """assistant_run_stats reads this same enumeration. An activity bar is an
    action's own work and an unaccounted bar is the absence of a call, so
    counting either would inflate the call count and its seconds."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4)])
    gap_maker = _step("reply", at=40, ms=2000)

    calls = db.assistant_llm_calls([step, gap_maker], run=_run())
    stats = db.assistant_run_stats([step, gap_maker], run=_run())

    assert [c["kind"] for c in calls].count("activity") == 1
    assert [c["kind"] for c in calls].count("unaccounted") >= 1
    assert stats["calls"] == 2
    assert stats["duration_ms"] == 13800
    assert stats["input_tokens"] == 20


def test_a_run_with_no_phases_is_unchanged_apart_from_gaps():
    steps = [_step("reply", at=0, ms=2000), _step("reply_audit", at=2, ms=1000)]

    calls = db.assistant_llm_calls(steps, run=_run())

    assert not [c for c in calls if c["kind"] == "activity"]
    assert [c["label"] for c in calls if c["kind"] != "unaccounted"] == [
        "reply", "reply_audit"]


def test_the_enumeration_still_works_without_a_run():
    """The trace is read where there are steps but no run row; gaps cannot be
    bounded there, and the calls must still enumerate."""
    calls = db.assistant_llm_calls([_step("reply", at=0, ms=2000)])

    assert [c["label"] for c in calls] == ["reply"]
