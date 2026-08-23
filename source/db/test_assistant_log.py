"""The run read model: a run as a flat stream of typed events.

Fakes rather than DB rows — this is derivation over attributes, and the
persistence of a step is tested elsewhere.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import db

T0 = datetime(2026, 8, 24, 0, 46, 5, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _step(action, *, at, ms, phases=None, embeds=None, observation=None,
          code_driven=False, phase="observed", args=None):
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
    obs = dict(observation or {})
    obs.setdefault("data", data)
    return SimpleNamespace(
        uuid=uuid4(), action=action, phase=phase, code_driven=code_driven,
        requested_at=_at(at), created_at=_at(at + ms / 1000),
        duration_ms=int(ms), system_prompt="sys", user_prompt="usr",
        model_response="{}", reasoning=None, log=None, error=None,
        args=args or {}, reason="because", observation=obs,
        observation_preview=(obs.get("text") or "")[:200],
        model_uuid=None, model_group_uuid=None,
        input_tokens=10, output_tokens=5, rejected_attempts=[],
        step_index=0, settled_at=None,
    )


def _run(started=0.0, finished=None):
    return SimpleNamespace(uuid=uuid4(), started_at=_at(started),
                           finished_at=_at(finished) if finished else None)


def _kinds(events):
    return [(e["kind"], e["label"]) for e in events]


def _first(events, kind):
    return next(e for e in events if e["kind"] == kind)


def test_one_step_becomes_a_call_and_an_action():
    """The split the whole rework rests on. A step row is a model call AND the
    action it chose; rendering them as one row is why neither has a home."""
    step = _step("memory_query", at=0, ms=11800,
                 observation={"text": "facts"})

    events = db.run_events(_run(finished=30), [step])

    assert ("llm", "decide → memory_query") in _kinds(events)
    assert ("action", "memory_query") in _kinds(events)


def test_a_code_driven_call_is_not_labelled_a_decision():
    """The loop issued it; presenting it as a model decision is a fiction the
    step row's own docstring warns about."""
    step = _step("reply_audit", at=0, ms=4900, code_driven=True)

    labels = [e["label"] for e in db.run_events(_run(finished=10), [step])
              if e["kind"] == "llm"]

    assert labels == ["reply_audit"]


def test_an_action_carries_its_args_and_observation():
    step = _step("python_run", at=0, ms=1000, args={"code": "print(2+2)"},
                 observation={"text": "4"})

    action = _first(db.run_events(_run(finished=5), [step]), "action")

    assert action["payload"]["args"] == {"code": "print(2+2)"}
    assert action["payload"]["observation"]["text"] == "4"


def test_a_step_with_no_action_yields_only_a_call():
    step = _step(None, at=0, ms=2000)

    kinds = [e["kind"] for e in db.run_events(_run(finished=5), [step])]

    assert "action" not in kinds
    assert "llm" in kinds


def test_every_kind_appears_for_a_rich_run():
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4)],
                 embeds=[("what do I like", 12, 0.5)],
                 observation={"text": "facts"})
    control = _step("stop", at=40, ms=0, phase="control")

    kinds = {e["kind"] for e in db.run_events(_run(finished=60),
                                              [step, control])}

    assert {"llm", "action", "activity", "embedding",
            "control", "unaccounted"} <= kinds, kinds


def test_no_event_contains_another():
    """The staircase property. It moved modules; it did not become optional.

    An action event spans its own phases, so it must not be counted as
    occupying them — otherwise it would swallow every phase it wraps.
    """
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("recall filter", 11.8, 22.8)],
                 observation={"text": "facts"})
    inner = _step("recall_filter", at=21.8, ms=12700, code_driven=True)

    events = db.run_events(_run(finished=34.5), [step, inner])

    spans = [(e["start"], e["start"] + timedelta(milliseconds=e["duration_ms"]))
             for e in events
             if e["start"] and e["duration_ms"] and e["kind"] != "action"]
    for i, (a0, a1) in enumerate(spans):
        for j, (b0, b1) in enumerate(spans):
            assert i == j or not (a0 <= b0 and b1 <= a1), _kinds(events)


def test_events_are_ordered_by_when_they_happened():
    late = _step("reply", at=30, ms=2000)
    early = _step("acceptance_criteria", at=0, ms=2000, code_driven=True)

    events = db.run_events(_run(finished=40), [late, early])
    placed = [e["start"] for e in events if e["start"]]

    assert placed == sorted(placed)


def test_the_llm_projection_is_a_filter_over_the_events():
    """Two enumerations of one run that could disagree is the bug this module
    exists against."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4)],
                 observation={"text": "facts"})
    run = _run(finished=30)

    calls = db.assistant_llm_calls([step], run=run)
    events = db.run_events(run, [step])

    assert [c["label"] for c in calls] == [
        e["label"] for e in events if e["kind"] not in ("action", "control")]


def test_run_stats_ignores_everything_that_is_not_a_call():
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 10.4)],
                 observation={"text": "facts"})

    stats = db.assistant_run_stats([step], run=_run(finished=40))

    assert stats["calls"] == 1
    assert stats["duration_ms"] == 11800
    assert stats["input_tokens"] == 10
