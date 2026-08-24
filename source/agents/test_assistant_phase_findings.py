"""What a phase produced, recorded beside how long it took.

A phase row on /assistant could only ever say "21 seconds went here". The
thing the reader wanted — which candidates the filter considered and which it
kept — was reachable only by opening another step's user prompt.
"""

from agents.assistant import _PhaseTimer


def _phase(timer, name):
    return next(p for p in timer.phases if p["name"] == name)


def test_a_phase_records_what_it_found():
    timer = _PhaseTimer()

    with timer.phase("seed KB load") as phase:
        phase.found({"entries": 412})

    assert _phase(timer, "seed KB load")["found"] == {"entries": 412}


def test_a_phase_that_records_nothing_carries_no_key():
    """Most phases have nothing to report, and a null would put an empty
    "found" block on every one of their rows."""
    timer = _PhaseTimer()

    with timer.phase("execute"):
        pass

    assert "found" not in _phase(timer, "execute")


def test_a_phase_that_raised_keeps_what_it_had_found():
    """A phase that failed is exactly when what it managed to collect matters,
    and its timing is already recorded in `finally` for the same reason."""
    timer = _PhaseTimer()

    try:
        with timer.phase("recall filter") as phase:
            phase.found({"considered": 14})
            raise RuntimeError("filter died")
    except RuntimeError:
        pass

    assert _phase(timer, "recall filter")["found"] == {"considered": 14}


def test_the_last_recording_wins():
    """A phase that narrows what it found as it goes reports where it ended."""
    timer = _PhaseTimer()

    with timer.phase("claim retrieval") as phase:
        phase.found({"claims": 9})
        phase.found({"claims": 3})

    assert _phase(timer, "claim retrieval")["found"] == {"claims": 3}


def test_a_retrieved_claim_reports_enough_to_recognise_it():
    """Score and reason are what the reader judges the match on; the text says
    which fact it was."""
    from types import SimpleNamespace
    from uuid import uuid4

    from agents.assistant import _CLAIM_FOUND_CHARS, _claim_found

    uuid = uuid4()
    found = _claim_found(SimpleNamespace(
        uuid=uuid, text="L" * (_CLAIM_FOUND_CHARS + 50), kind="fact",
        reason="semantic", score=0.8123456))

    assert found["uuid"] == str(uuid)
    assert found["score"] == 0.8123
    assert found["reason"] == "semantic"
    # Shortened: the whole text is on the action's own row a click away.
    assert len(found["text"]) == _CLAIM_FOUND_CHARS


def test_a_claim_missing_its_optional_fields_still_reports():
    """The trace is read from lighter shapes in tests and tools, and a missing
    field must not cost the phase its whole row."""
    from types import SimpleNamespace

    from agents.assistant import _claim_found

    assert _claim_found(SimpleNamespace())["score"] == 0.0
