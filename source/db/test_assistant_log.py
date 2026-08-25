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


def _trigger(text="tell me about my siblings"):
    return {"id": 42, "uuid": "11111111-1111-1111-1111-111111111111",
            "sender_uuid": "22222222-2222-2222-2222-222222222222",
            "sender_name": "Operator", "text": text,
            "timestamp": "2026-08-24 00:46"}


def test_a_run_opens_with_a_start_event():
    """The request that set the run going is the first thing that happened, so
    it belongs on the stream rather than only in a card beside it."""
    step = _step("reply", at=5, ms=2000, code_driven=True)

    events = db.run_events(_run(finished=10), [step], trigger=_trigger())

    assert events[0]["kind"] == "start"
    assert events[0]["label"] == "start"
    assert events[0]["payload"]["text"] == "tell me about my siblings"
    assert events[0]["payload"]["sender_name"] == "Operator"


def test_the_start_event_has_no_duration():
    """It is the moment the run began, not a stretch of work — a bar would
    claim time nothing spent."""
    events = db.run_events(_run(finished=10),
                           [_step("reply", at=5, ms=2000, code_driven=True)],
                           trigger=_trigger())

    assert events[0]["duration_ms"] == 0


def test_the_start_event_sits_at_the_run_start():
    events = db.run_events(_run(started=0, finished=10),
                           [_step("reply", at=5, ms=2000, code_driven=True)],
                           trigger=_trigger())

    assert events[0]["start"] == _at(0)


def test_a_run_with_no_trigger_has_no_start_event():
    """A run seeded outside the chat flow has no message that began it, and an
    empty Start row would say one existed."""
    events = db.run_events(_run(finished=10),
                           [_step("reply", at=5, ms=2000, code_driven=True)])

    assert not [e for e in events if e["kind"] == "start"]


def test_the_start_event_is_not_a_model_call():
    """It costs nothing, so it must not reach the call count or the totals."""
    step = _step("reply", at=5, ms=2000, code_driven=True)
    run = _run(finished=10)

    calls = db.assistant_llm_calls([step], run=run)
    stats = db.assistant_run_stats([step], run=run)

    assert not [c for c in calls if c["kind"] == "start"]
    assert stats["calls"] == 1


def test_an_action_sits_where_it_settled_not_where_its_call_ended():
    """An action event is the record of what came back, and what came back is
    known when the action settles — after the phases it ran, not before them.
    Placed at the call's end it sorted above its own work."""
    # The phase starts a second AFTER the call ends, so a tie-break at the
    # same instant cannot be what orders these.
    step = _step("memory_query", at=0, ms=10000,
                 phases=[("claim retrieval", 11, 5.0)],
                 observation={"text": "facts"})
    step.settled_at = _at(16)

    events = db.run_events(_run(finished=20), [step])
    order = [e["label"] for e in events]

    assert order.index("memory_query") > order.index(
        "memory_query › claim retrieval")


def test_an_action_without_a_settled_time_still_places():
    """Legacy rows predate settled_at; they keep the call's end."""
    step = _step("memory_query", at=0, ms=10000, observation={"text": "f"})
    step.settled_at = None

    action = _first(db.run_events(_run(finished=20), [step]), "action")

    assert action["start"] == _at(10)


def test_a_memory_query_reports_what_it_recalled():
    """The counts the step's table shows — how much was found, how much was
    cut — belong on the event too, or the inspector is the poorer surface."""
    step = _step("memory_query", at=0, ms=1000,
                 observation={"text": "facts"})
    step.observation["data"].update(
        {"qa_static": 3, "qa_dynamic": 0, "memory": 0,
         "truncated": 1, "omitted": 0})

    action = _first(db.run_events(_run(finished=5), [step]), "action")

    assert action["kpis"]["qa_static"] == 3
    assert action["kpis"]["truncated"] == 1
    assert action["kpis"]["omitted"] == 0


def test_an_action_with_no_counts_reports_none():
    """Only memory_query carries them; every other action's line stays clean."""
    step = _step("python_run", at=0, ms=1000, observation={"text": "4"})

    action = _first(db.run_events(_run(finished=5), [step]), "action")

    assert "qa_static" not in action["kpis"]


def _ref(events, kind, label=None):
    for e in events:
        if e["kind"] == kind and (label is None or e["label"] == label):
            return e["step_ref"]
    raise AssertionError(_kinds(events))


def test_a_step_s_call_opens_it_and_its_action_closes_it():
    """A reader looking at one row wants to know which step it belongs to. A
    step's call is the first thing in it and its action settles last, so those
    two rows say where the step begins and ends."""
    step = _step("memory_query", at=0, ms=11800,
                 phases=[("claim retrieval", 11.8, 5.0)],
                 observation={"text": "facts"})
    step.settled_at = _at(17)

    events = db.run_events(_run(finished=30),
                           [step, _step("reply", at=25, ms=1000)])

    assert _ref(events, "llm", "decide → memory_query") == "Step 1 start"
    assert _ref(events, "action", "memory_query") == "Step 1 end"


def test_work_inside_a_step_is_named_by_the_step_alone():
    """A phase or an embed is neither where the step began nor where it
    ended."""
    step = _step("memory_query", at=0, ms=10000,
                 phases=[("recall filter", 10, 4.0)],
                 embeds=[("where do i live", 10.1, 0.5)],
                 observation={"text": "facts"})
    step.settled_at = _at(14)

    events = db.run_events(_run(finished=20), [step])

    assert _ref(events, "activity") == "Step 1"
    assert _ref(events, "embedding") == "Step 1"


def test_a_step_that_is_only_a_call_is_not_split_into_two_ends():
    """A code-driven step has no action, so its one row IS the step. Calling
    it the start would promise an end that never comes.

    It says the loop issued it instead — which is what the number cannot: this
    row is not part of the ReAct sequence and consumed none of its budget."""
    events = db.run_events(_run(finished=10),
                           [_step("reply_audit", at=0, ms=2000,
                                  code_driven=True)])

    ref = _ref(events, "llm")
    assert not ref.endswith(" start") and not ref.endswith(" end")
    assert ref.startswith("Step 1 \u00b7 ")


def test_time_between_two_steps_names_both():
    """The gap the operator is hunting: nothing measured it, and which two
    steps it fell between is the whole question."""
    first = _step("memory_query", at=0, ms=1000, observation={"text": "f"})
    first.settled_at = _at(1)
    second = _step("reply", at=20, ms=1000, code_driven=True)

    events = db.run_events(_run(finished=25), [first, second])

    assert _ref(events, "unaccounted") == "Step 1 \u2192 Step 2"


def test_the_run_s_opening_sits_before_the_first_step():
    events = db.run_events(_run(finished=10),
                           [_step("reply", at=5, ms=2000, code_driven=True)],
                           trigger=_trigger())

    assert _ref(events, "start") == "before Step 1"


def test_work_after_the_last_step_says_so():
    """The summarizer runs once the loop is over; no step owns it."""
    step = _step("reply", at=0, ms=1000, code_driven=True)
    run = _run(finished=2)

    events = db.run_events(run, [step])
    trailing = [e for e in events if e["start"] and e["start"] > _at(1)]

    assert all(e["step_ref"] in ("Step 1", "after Step 1") for e in trailing), \
        [(e["label"], e["step_ref"]) for e in trailing]


def test_a_run_with_no_steps_claims_no_step():
    events = db.run_events(_run(finished=10), [], trigger=_trigger())

    assert all(e["step_ref"] == "" for e in events)


def test_a_phase_carries_what_it_found():
    """A phase row that can only say how long it took is a dead end: the
    reader is told 21 seconds went somewhere and sent to read another step's
    user prompt to find out where."""
    step = _step("memory_query", at=0, ms=1000,
                 phases=[("recall filter", 1, 21.0)],
                 observation={"text": "facts"})
    step.observation["data"]["recall_filter"] = {
        "mode": "llm",
        "candidates": [{"path": "human.x.location", "kept": True,
                        "score": 1000}]}

    phase = next(e for e in db.run_events(_run(finished=30), [step])
                 if e["kind"] == "activity")

    assert phase["payload"]["found"]["candidates"][0]["kept"] is True


def test_a_phase_that_recorded_its_own_findings_carries_those():
    """The general channel: a phase records what it produced beside its
    timing, and the row shows it without anyone naming that phase here."""
    step = _step("memory_query", at=0, ms=1000,
                 phases=[("seed KB load", 1, 0.3)],
                 observation={"text": "facts"})
    step.observation["data"]["timing"]["phases"][0]["found"] = {"entries": 412}

    phase = next(e for e in db.run_events(_run(finished=5), [step])
                 if e["kind"] == "activity")

    assert phase["payload"]["found"] == {"entries": 412}


def test_a_phase_with_nothing_recorded_carries_nothing():
    step = _step("python_run", at=0, ms=1000,
                 phases=[("execute", 1, 2.0)], observation={"text": "4"})

    phase = next(e for e in db.run_events(_run(finished=5), [step])
                 if e["kind"] == "activity")

    assert not phase["payload"].get("found")


# --- slice 0: what the step sections hold and the stream did not -------------


def _review(step_uuid, **over):
    row = SimpleNamespace(
        uuid=uuid4(), step_uuid=step_uuid, requested_at=_at(5),
        duration_ms=900, model_uuid=None, input_tokens=40, output_tokens=8,
        verdict="approved", skip_reason=None, error=None, problems=[],
        system_prompt="review sys", user_prompt="review usr",
        reasoning=None, response='{"approved": true}', action="python_run")
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _intent(step_uuid, **over):
    row = SimpleNamespace(
        uuid=uuid4(), step_uuid=step_uuid, capability_name="memory_write",
        state="proposed", payload={"text": "x"}, preview_text="remember x",
        result={})
    for k, v in over.items():
        setattr(row, k, v)
    return row


def test_a_skipped_step_is_still_on_the_stream():
    """The loop could not make this call. It cost nothing, which is why it has
    no bar — but a run where a call was skipped and a run where it was never
    scheduled are different runs, and the stream has to be able to say so."""
    step = _step("recall_filter", at=0, ms=0, phase="skipped",
                 code_driven=True)

    events = db.run_events(_run(finished=10), [step])

    assert ("skipped", "recall_filter") in _kinds(events)


def test_a_skipped_step_costs_the_run_nothing():
    """It is not a model call. Counted as one it would inflate the run's call
    count with a call that was never made."""
    step = _step("recall_filter", at=0, ms=0, phase="skipped",
                 code_driven=True)
    run = _run(finished=10)

    stats = db.assistant_run_stats([step], run=run)

    assert stats["calls"] == 0
    assert not [c for c in db.assistant_llm_calls([step], run=run)
                if c["kind"] == "skipped"]


def test_the_review_event_carries_its_verdict():
    """The review was already a row on the stream carrying nothing but its
    cost, so the one thing it is read for — whether it approved — was only in
    the step section."""
    step = _step("python_run", at=0, ms=1000, observation={"text": "4"})
    review = _review(step.uuid, verdict="rejected",
                     problems=[{"category": "safety", "text": "writes a file"}])

    event = next(e for e in db.run_events(_run(finished=10), [step], [review])
                 if e["variant"] == "review")

    assert event["kpis"]["verdict"] == "rejected"
    assert event["payload"]["problems"][0]["text"] == "writes a file"
    assert event["payload"]["system_prompt"] == "review sys"
    assert event["payload"]["model_response"] == '{"approved": true}'


def test_a_review_that_never_ran_says_why():
    """Skipped and errored both let the action run, and neither is an
    approval. A run that went wrong because the gate never ran is a different
    bug from one the gate approved."""
    step = _step("python_run", at=0, ms=1000, observation={"text": "4"})
    review = _review(step.uuid, verdict="skipped",
                     skip_reason="no model group bound")

    event = next(e for e in db.run_events(_run(finished=10), [step], [review])
                 if e["variant"] == "review")

    assert event["payload"]["skip_reason"] == "no model group bound"


def test_an_action_carries_the_writes_it_proposed():
    """The pane that says what an action did is where what it wants to write
    belongs — and where the button to approve it will go."""
    step = _step("memory_write", at=0, ms=1000, observation={"text": "ok"})
    intent = _intent(step.uuid)

    action = _first(db.run_events(_run(finished=10), [step],
                                  intents=[intent]), "action")

    assert action["payload"]["intents"][0]["capability_name"] == "memory_write"
    assert action["payload"]["intents"][0]["state"] == "proposed"


def test_a_write_with_no_step_belongs_to_the_run():
    """It has no step to hang off, and a write the operator has to approve is
    the last thing that may go missing."""
    step = _step("reply", at=0, ms=1000, code_driven=True)
    intent = _intent(None)

    events = db.run_events(_run(finished=10), [step], intents=[intent],
                           trigger=_trigger())

    assert _first(events, "start")["payload"]["intents"][0]["uuid"] == str(
        intent.uuid)


def test_an_action_with_no_writes_carries_none():
    step = _step("memory_query", at=0, ms=1000, observation={"text": "f"})

    action = _first(db.run_events(_run(finished=10), [step]), "action")

    assert action["payload"].get("intents") in (None, [])


def test_a_call_in_flight_gets_a_row_while_it_runs():
    """The one thing on the page with no record yet: the model is answering
    right now. Watching a run means watching this, so it belongs on the stream
    rather than in a card below it."""
    run = _run(finished=None)
    run.status = "running"

    events = db.run_events(
        run, [_step("acceptance_criteria", at=0, ms=2000, code_driven=True)],
        active={"step_index": 1, "model_name": "gemma4:e4b",
                "partial_response": '{"action": "re',
                "partial_reasoning": "thinking", "error": None})

    live = next(e for e in events if e["variant"] == "live")
    assert live["kpis"]["model"] == "gemma4:e4b"
    assert live["payload"]["model_response"] == '{"action": "re'


def test_the_live_row_is_last_and_carries_no_duration():
    """It has not finished, so any duration would be a guess, and a bar would
    claim time that is still being spent."""
    run = _run(finished=None)
    run.status = "running"

    events = db.run_events(run, [_step("reply", at=0, ms=2000,
                                       code_driven=True)],
                           active={"model_name": "m", "partial_response": "x"})

    assert events[-1]["variant"] == "live"
    assert events[-1]["duration_ms"] is None


def test_a_call_still_in_flight_is_not_counted_as_a_finished_one():
    """Its tokens are not known and its row is not written; counting it would
    put a call in the totals that may yet be retried or fail."""
    run = _run(finished=None)
    run.status = "running"
    steps = [_step("reply", at=0, ms=2000, code_driven=True)]

    stats = db.assistant_run_stats(steps, run=run)

    assert stats["calls"] == 1


def test_an_idle_run_has_no_live_row():
    events = db.run_events(_run(finished=10),
                           [_step("reply", at=0, ms=2000, code_driven=True)])

    assert not [e for e in events if e["variant"] == "live"]


def test_a_step_that_recorded_only_its_model_still_has_a_call_row():
    """A recorded model IS evidence a call was made. Legacy rows carry the
    model and nothing else about the call, and without a row for it the model
    the step ran on has nowhere to be shown at all."""
    step = _step("memory_query", at=0, ms=0, observation={"text": "f"})
    step.requested_at = None
    step.duration_ms = None
    step.system_prompt = None
    step.model_uuid = "9999"

    events = db.run_events(_run(finished=10), [step])

    call = _first(events, "llm")
    assert call["kpis"]["model_uuid"] == "9999"


def test_a_review_recorded_inside_the_observation_gets_a_row_too():
    """The gate wrote its result into the step's observation before it had a
    table of its own, and most of the reviews that exist are that shape. Left
    unread they are raw JSON inside the action's result — which is exactly the
    dead end the step sections were removed for."""
    step = _step("python_run", at=0, ms=1000, observation={"text": "4"})
    step.observation["data"]["second_opinion"] = {
        "approved": False,
        "problems": [{"category": "safety", "text": "writes a file"}],
        "response": '{"approved": false}',
        "system_prompt": "review sys", "user_prompt": "review usr",
        "reasoning": "it writes", "model_uuid": "9999"}

    events = db.run_events(_run(finished=10), [step])

    review = next(e for e in events if e["variant"] == "review")
    assert review["kpis"]["verdict"] == "rejected"
    assert review["payload"]["problems"][0]["text"] == "writes a file"
    assert review["payload"]["system_prompt"] == "review sys"


def test_an_inline_review_is_not_repeated_inside_the_action():
    """It has a row of its own now, and the same review printed twice on one
    screen reads as two reviews."""
    step = _step("python_run", at=0, ms=1000, observation={"text": "4"})
    step.observation["data"]["second_opinion"] = {"approved": True,
                                                  "problems": []}
    step.observation["data"]["duration_seconds"] = 0.01

    action = _first(db.run_events(_run(finished=10), [step]), "action")

    assert "second_opinion" not in action["payload"]["observation"]["data"]
    assert action["payload"]["observation"]["data"]["duration_seconds"] == 0.01


def test_a_phases_findings_are_not_repeated_inside_the_action():
    """The recall filter's payload is every candidate's document and score,
    and its phase row renders it split into what was sent and what came back.
    The action's pane was printing all of it again underneath."""
    step = _step("memory_query", at=0, ms=1000, observation={"text": "facts"})
    step.observation["data"]["qa_static"] = 2
    step.observation["data"]["recall_filter"] = {
        "mode": "reranker",
        "candidates": [{"qa_id": "qa-1", "document": "a document body"}]}
    step.observation["data"]["timing"] = {
        "phases": [{"name": "recall filter", "ms": 380,
                    "started_at": _at(0.1).isoformat()}]}

    action = _first(db.run_events(_run(finished=10), [step]), "action")

    data = action["payload"]["observation"]["data"]
    assert "recall_filter" not in data
    assert data["qa_static"] == 2       # the rest of what it recorded stays


def test_a_run_with_no_phase_row_keeps_its_findings_in_the_action():
    """No phase means no row to send the reader to, so dropping the payload
    would lose it rather than move it — which is what every run recorded
    before the phase existed depends on."""
    step = _step("memory_query", at=0, ms=1000, observation={"text": "facts"})
    step.observation["data"]["recall_filter"] = {"mode": "llm",
                                                 "reasoning": "an old note"}

    action = _first(db.run_events(_run(finished=10), [step]), "action")

    assert action["payload"]["observation"]["data"]["recall_filter"] == {
        "mode": "llm", "reasoning": "an old note"}


def test_a_pointer_to_a_review_row_does_not_become_a_second_row():
    """Newer runs record only the review's uuid there; the row it points at is
    already on the stream from the reviews table."""
    step = _step("python_run", at=0, ms=1000, observation={"text": "4"})
    step.observation["data"]["second_opinion"] = {"review_uuid": "abcd"}
    review = SimpleNamespace(
        uuid=uuid4(), step_uuid=step.uuid, requested_at=_at(0.5),
        duration_ms=100, model_uuid=None, input_tokens=1, output_tokens=1,
        verdict="approved", skip_reason=None, error=None, problems=[],
        system_prompt="s", user_prompt="u", reasoning=None, response="{}")

    events = db.run_events(_run(finished=10), [step], [review])

    assert len([e for e in events if e["variant"] == "review"]) == 1


def test_a_rejected_attempt_carries_what_it_answered():
    """It is an invocation like any other: it was sent, it thought, it
    answered, and the answer was refused. A row that showed only its seconds
    would teach that it is less of a call than the one that replaced it — and
    the refused answer is the whole reason anyone opens it."""
    step = _step("reply", at=10, ms=2000, code_driven=True)
    step.rejected_attempts = [{
        "requested_at": _at(0).isoformat(), "ms": 9000,
        "input_tokens": 940, "output_tokens": 68,
        "reasoning": "thinking about it",
        "response": '{"action": null}',
        "error": "RejectedResponse: not a valid decision",
        "feedback": [{"role": "user", "content": "<rejected_response>"}]}]

    events = db.run_events(_run(finished=20), [step])

    attempt = next(e for e in events if e["variant"] == "rejected")
    assert attempt["payload"]["model_response"] == '{"action": null}'
    assert attempt["payload"]["reasoning"] == "thinking about it"
    assert "not a valid decision" in attempt["payload"]["error"]
    assert attempt["payload"]["feedback"][0]["content"] == "<rejected_response>"
