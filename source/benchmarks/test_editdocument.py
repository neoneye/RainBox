"""Tests for benchmarks.editdocument.

Deterministic / fast / no LM Studio. Verifies the seeded test cases are
self-consistent (the expected output really is reachable via valid
patches), and that the runner records correct/failure outcomes given
stubbed agents.

    python -m pytest benchmarks/test_editdocument.py -v
"""

from benchmarks.editdocument import (
    EDIT_DOCUMENT_TESTS,
    BenchmarkEditDocument,
    BenchmarkEditDocumentResult,
    EditDocumentTest,
)
from patch_apply import apply_patches


# Subset of EDIT_DOCUMENT_TESTS used by the runner tests below: keeps the
# stub-driven tests stable when new test cases are added to the seeded
# list. The names here are the ones `_correct_plan_for` knows how to
# build a known-good plan for.
_KNOWN_NAMES = ("append_task", "remove_task", "check_task")
_KNOWN_TESTS = [t for t in EDIT_DOCUMENT_TESTS if t.name in _KNOWN_NAMES]


def test_edit_document_tests_have_unique_names():
    names = [t.name for t in EDIT_DOCUMENT_TESTS]
    assert len(names) == len(set(names))


def test_edit_document_tests_are_frozen_dataclass():
    # Catches accidental field mutation later in the program. EditDocumentTest
    # should be frozen=True.
    import pytest
    t = EDIT_DOCUMENT_TESTS[0]
    with pytest.raises((AttributeError, Exception)):
        t.name = "mutated"  # pyright: ignore[reportAttributeAccessIssue]


def test_original_three_test_names_present():
    # Subset check (not equality): EDIT_DOCUMENT_TESTS may grow beyond the
    # original three over time. The original three are the ones with
    # known-good reachability via hand-crafted patches below.
    names = {t.name for t in EDIT_DOCUMENT_TESTS}
    assert {"append_task", "remove_task", "check_task"}.issubset(names)


def test_each_seeded_test_has_non_empty_required_fields():
    for t in EDIT_DOCUMENT_TESTS:
        assert isinstance(t, EditDocumentTest)
        assert t.name
        assert t.description
        # `document` is allowed to be empty (generate-from-scratch case),
        # but none of the seeded tests use that — assert non-empty for now
        # as a guard against accidental clearing.
        assert t.document
        assert t.instructions
        assert t.expected


def test_each_seeded_test_expected_is_reachable_via_known_patch():
    """For each seeded test, construct the right patch by hand and apply it;
    `applied` must equal `expected`. Catches typos in the seeded `expected`
    strings — i.e. confirms the expected output really is reachable via a
    valid replace_lines patch on the seeded document."""
    by_name = {t.name: t for t in EDIT_DOCUMENT_TESTS}

    # append_task: 6-line doc; append after last line.
    append = by_name["append_task"]
    applied = apply_patches(append.document, [{
        "op": "replace_lines",
        "start_line": 7, "end_line": 6,
        "replacement": "- [ ] move furniture",
        "intent": "append",
    }])
    assert applied == append.expected, "append_task expected mismatch"

    # remove_task: delete the buy-shoelaces line (line 4, 1-based).
    remove = by_name["remove_task"]
    applied = apply_patches(remove.document, [{
        "op": "replace_lines",
        "start_line": 4, "end_line": 4,
        "replacement": "",
        "intent": "delete",
    }])
    assert applied == remove.expected, "remove_task expected mismatch"

    # check_task: replace the buy-shoelaces line with the x-marked variant.
    check = by_name["check_task"]
    applied = apply_patches(check.document, [{
        "op": "replace_lines",
        "start_line": 4, "end_line": 4,
        "replacement": "- [x] buy shoelaces",
        "intent": "check",
    }])
    assert applied == check.expected, "check_task expected mismatch"


import pytest

import db
from agent_config import EDIT_DOCUMENT_V1_UUID
from agent_edit_document_v1 import EditDocumentAgentV1, EditPlan, Patch


@pytest.fixture
def app_ctx():
    """Push a Flask app context — the agent's _structured_call path needs
    one because it touches db.session via resolved_model_kwargs."""
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


def _patch_resolve(monkeypatch):
    """The runner calls db.resolved_model_kwargs(target_uuid) before
    instantiating the agent; the stubbed tests pass EDIT_DOCUMENT_V1_UUID
    (an agent uuid, not a model uuid) as the target, so stub the resolver
    to return a fixed tuple."""
    monkeypatch.setattr(
        "benchmarks.editdocument.db.resolved_model_kwargs",
        lambda _uuid: ("lm_studio", "stub-model", {}),
    )


def _build_agent_with_stub(monkeypatch, plan_factory):
    """Return an EditDocumentAgentV1 whose _structured_call invokes
    plan_factory(test_index) → EditPlan. Tracks calls so each test can
    return a different plan."""
    state = {"i": 0}
    agent = EditDocumentAgentV1(
        agent_uuid=EDIT_DOCUMENT_V1_UUID, name="edit_document_v1", send=lambda _m: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []

    def stub(user_prompt, validator=None):
        plan = plan_factory(state["i"])
        state["i"] += 1
        if validator is not None:
            validator(plan)
        return plan
    monkeypatch.setattr(agent, "_structured_call", stub)
    return agent


def _correct_plan_for(test: EditDocumentTest) -> EditPlan:
    """Return a known-good EditPlan that turns test.document into test.expected.
    Used to verify that BenchmarkEditDocument records correct=True when the
    agent produces a passing edit."""
    if test.name == "append_task":
        return EditPlan(patches=[Patch(
            op="replace_lines", start_line=7, end_line=6,
            replacement="- [ ] move furniture", intent="append",
        )])
    if test.name == "remove_task":
        return EditPlan(patches=[Patch(
            op="replace_lines", start_line=4, end_line=4,
            replacement="", intent="delete",
        )])
    if test.name == "check_task":
        return EditPlan(patches=[Patch(
            op="replace_lines", start_line=4, end_line=4,
            replacement="- [x] buy shoelaces", intent="check",
        )])
    raise AssertionError(f"unknown test {test.name}")


def test_benchmark_run_records_correct_for_passing_stub(app_ctx, monkeypatch):
    _patch_resolve(monkeypatch)
    agent = _build_agent_with_stub(
        monkeypatch,
        lambda i: _correct_plan_for(_KNOWN_TESTS[i]),
    )
    # The benchmark instantiates the agent itself; for the stub path we
    # have to override that. Monkeypatch the constructor lookup to return
    # our pre-stubbed agent for any call.
    monkeypatch.setattr(
        "benchmarks.editdocument._instantiate_agent",
        lambda agent_cls, agent_uuid, name: agent,
    )
    bench = BenchmarkEditDocument(
        target_uuid=EDIT_DOCUMENT_V1_UUID,  # acts as the pinned model uuid in the stub
        agent_class=EditDocumentAgentV1,
        agent_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_name="edit_document_v1",
        tests=_KNOWN_TESTS,
    )
    result = bench.run()
    assert isinstance(result, BenchmarkEditDocumentResult)
    assert result.total == 3
    assert result.correct == 3
    assert result.failures == 0
    assert result.mistakes == 0
    assert [t.test_name for t in result.trials] == ["append_task", "remove_task", "check_task"]
    for t in result.trials:
        assert t.correct is True
        assert t.error is None
        assert t.applied is not None


def test_benchmark_run_records_failure_for_raising_stub(app_ctx, monkeypatch):
    _patch_resolve(monkeypatch)
    def raising_stub(user_prompt, validator=None):
        raise RuntimeError("simulated model failure")
    agent = EditDocumentAgentV1(
        agent_uuid=EDIT_DOCUMENT_V1_UUID, name="edit_document_v1", send=lambda _m: None
    )
    agent.model_group_uuid = None
    agent.candidate_model_uuids = []
    monkeypatch.setattr(agent, "_structured_call", raising_stub)
    monkeypatch.setattr(
        "benchmarks.editdocument._instantiate_agent",
        lambda agent_cls, agent_uuid, name: agent,
    )
    bench = BenchmarkEditDocument(
        target_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_class=EditDocumentAgentV1,
        agent_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_name="edit_document_v1",
        tests=_KNOWN_TESTS,
    )
    result = bench.run()
    assert result.total == 3
    assert result.correct == 0
    assert result.failures == 3
    assert all(t.error and "simulated model failure" in t.error for t in result.trials)


def test_benchmark_run_records_mistake_when_patches_dont_match_expected(app_ctx, monkeypatch):
    _patch_resolve(monkeypatch)
    # Stub returns a plan that's structurally valid but doesn't reach the
    # expected output (replaces wrong line).
    def bad_plan(_i):
        return EditPlan(patches=[Patch(
            op="replace_lines", start_line=1, end_line=1,
            replacement="WRONG", intent="off-target",
        )])
    agent = _build_agent_with_stub(monkeypatch, bad_plan)
    monkeypatch.setattr(
        "benchmarks.editdocument._instantiate_agent",
        lambda agent_cls, agent_uuid, name: agent,
    )
    bench = BenchmarkEditDocument(
        target_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_class=EditDocumentAgentV1,
        agent_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_name="edit_document_v1",
        tests=_KNOWN_TESTS,
    )
    result = bench.run()
    assert result.correct == 0
    assert result.mistakes == 3
    assert result.failures == 0


def test_benchmark_run_invokes_on_trial_callback(app_ctx, monkeypatch):
    _patch_resolve(monkeypatch)
    agent = _build_agent_with_stub(
        monkeypatch,
        lambda i: _correct_plan_for(_KNOWN_TESTS[i]),
    )
    monkeypatch.setattr(
        "benchmarks.editdocument._instantiate_agent",
        lambda agent_cls, agent_uuid, name: agent,
    )
    seen: list[str] = []
    bench = BenchmarkEditDocument(
        target_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_class=EditDocumentAgentV1,
        agent_uuid=EDIT_DOCUMENT_V1_UUID,
        agent_name="edit_document_v1",
        tests=_KNOWN_TESTS,
    )
    bench.run(on_trial=lambda t: seen.append(t.test_name))
    assert seen == ["append_task", "remove_task", "check_task"]
