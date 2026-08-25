"""Row identity in the /assistant log.

A live run refreshes every few seconds and the page swaps its whole pane. What
the reader had selected has to survive that, which means a row needs a name
that does not move — an index does, because an event can be inserted ahead of
the one being read.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from webapp.assistant_log_view import log_view

T0 = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _step(action, *, at, ms, phases=None, code_driven=True):
    data: dict = {}
    if phases is not None:
        data["timing"] = {"phases": [
            {"name": n, "started_at": _at(s).isoformat(), "ms": int(d * 1000)}
            for n, s, d in phases]}
    return SimpleNamespace(
        uuid=uuid4(), action=action, phase="observed",
        code_driven=code_driven, requested_at=_at(at),
        created_at=_at(at + ms / 1000), duration_ms=int(ms),
        system_prompt="sys", user_prompt="usr", model_response="{}",
        reasoning=None, log=None, error=None, args={}, reason="",
        observation={"data": data}, observation_preview="", model_uuid=None,
        model_group_uuid=None, input_tokens=10, output_tokens=5,
        rejected_attempts=[], step_index=0, settled_at=None)


def _run(finished=None):
    return SimpleNamespace(uuid=uuid4(), started_at=_at(0), room_uuid=None,
                           finished_at=_at(finished) if finished else None,
                           summary=None)


def _keys(view):
    return [e["key"] for e in view["events"]]


def test_every_row_carries_a_key():
    view = log_view(_run(30), [_step("reply", at=0, ms=2000)])

    assert all(e.get("key") for e in view["events"])


def test_two_rows_never_share_a_key():
    """A step is a call AND the action it chose, and they sit on the same step
    uuid — so the uuid alone cannot name a row."""
    view = log_view(_run(40), [
        _step("memory_query", at=0, ms=2000, code_driven=False,
              phases=[("claim retrieval", 2, 3.0)]),
        _step("reply", at=20, ms=2000)])

    assert len(set(_keys(view))) == len(view["events"])


def test_a_row_keeps_its_key_when_an_event_lands_before_it():
    """The reason a position cannot be the name. A run grows while it is being
    read, and an event with an earlier start slides every row after it down.
    """
    late = _step("reply", at=30, ms=2000)
    before = log_view(_run(40), [late])
    key_of_late = [e["key"] for e in before["events"]
                   if e["label"] == "reply"][0]
    row_of_late = [e["row_id"] for e in before["events"]
                   if e["label"] == "reply"][0]

    after = log_view(_run(40), [_step("acceptance_criteria", at=0, ms=2000),
                                late])
    now = [e for e in after["events"] if e["label"] == "reply"][0]

    assert now["key"] == key_of_late
    assert now["row_id"] != row_of_late, "the index was expected to move"


def test_a_key_keeps_the_sign_of_a_timezone_offset():
    """A scrubbed "+" turns +02:00 into -02:00, which reads as a different
    moment to anyone looking at the markup."""
    view = log_view(_run(30), [_step("reply", at=0, ms=1000)])

    assert any("+" in k or "Z" in k or "-00:00" in k for k in _keys(view))


def test_a_key_is_usable_as_an_html_attribute():
    """It travels in the markup, so it must not carry a quote or an angle
    bracket out of a label that arrived as data."""
    view = log_view(_run(30), [_step('<img src="x">', at=0, ms=1000)])

    for key in _keys(view):
        assert not set(key) & set('"\'<>&')


# --- anchors -----------------------------------------------------------------
#
# `#step-<uuid>` is a published URL: db.assistant_step_path() mints it and six
# places outside this page link to it, including chat messages and cron rows
# that are years old by now. One step uuid is several rows, so it needs a
# documented primary row rather than whichever one happens to sort first.


def _primary(view, step):
    return [e for e in view["events"] if e.get("primary_for") == str(step.uuid)]


def test_a_step_with_an_action_is_anchored_to_the_action():
    """Every external link means "the step that did this", and the action is
    the row carrying the args, the result and the writes it proposed."""
    step = _step("memory_query", at=0, ms=2000, code_driven=False)

    view = log_view(_run(30), [step])

    assert [e["kind"] for e in _primary(view, step)] == ["action"]


def test_a_step_that_is_only_a_call_is_anchored_to_the_call():
    step = _step("reply_audit", at=0, ms=2000, code_driven=True)

    view = log_view(_run(30), [step])

    assert [e["kind"] for e in _primary(view, step)] == ["llm"]


def test_a_step_is_anchored_to_exactly_one_row():
    """Two rows claiming one uuid is a link that lands somewhere different
    depending on which the lookup reaches first."""
    step = _step("memory_query", at=0, ms=2000, code_driven=False,
                 phases=[("claim retrieval", 2, 3.0)])

    view = log_view(_run(30), [step])

    assert len(_primary(view, step)) == 1


def test_a_row_belonging_to_no_step_anchors_nothing():
    view = log_view(_run(30), [_step("reply", at=0, ms=1000)])

    assert [e for e in view["events"] if e.get("primary_for")], "no anchors"
    assert all(e.get("primary_for") in (None, "")
               for e in view["events"] if not e["anchor"])


def test_a_retried_step_is_anchored_to_the_call_it_kept():
    """Its rejected attempts are llm rows on the same step and they ran first.
    A published link landing on an answer the run threw away would be worse
    than one that landed nowhere."""
    step = _step("reply_audit", at=10, ms=2000, code_driven=True)
    step.rejected_attempts = [{"requested_at": _at(0).isoformat(), "ms": 9000,
                               "input_tokens": 5, "output_tokens": 1}]

    view = log_view(_run(30), [step])
    primary = [e for e in view["events"] if e.get("primary_for")]

    assert len(primary) == 1
    assert primary[0]["variant"] == "code-driven"
