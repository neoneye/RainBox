"""Tests for evals.runner: scoring functions + run_eval_case +
run_eval_suite. The scoring tests are pure (no DB); the runner tests use
live Postgres and clean up via per-test name tags."""

from uuid import UUID, uuid4

import pytest

import db
from db import EvalCase, EvalResult, EvalRun

from evals.runner import (
    run_eval_case,
    run_eval_suite,
    score_chat_reply_case,
    score_memory_retrieval_case,
)


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def fresh_tag() -> str:
    return f"test-{uuid4().hex[:8]}"


def _cleanup(prefix: str) -> None:
    run_uuids = [
        r.uuid for r in db.db.session.query(EvalRun)
        .filter(EvalRun.name.like(f"{prefix}%")).all()
    ]
    if run_uuids:
        db.db.session.query(EvalResult).filter(
            EvalResult.eval_run_uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(EvalRun).filter(
            EvalRun.uuid.in_(run_uuids)
        ).delete(synchronize_session=False)
    db.db.session.query(EvalCase).filter(
        EvalCase.name.like(f"{prefix}%")
    ).delete(synchronize_session=False)
    db.db.session.query(db.MemoryClaim).filter(
        db.MemoryClaim.subject == prefix
    ).delete(synchronize_session=False)
    db.db.session.commit()


def _case(
    name: str, *,
    case_type: str = "chat_reply",
    input: dict | None = None,
    expected: dict | None = None,
    rubric: dict | None = None,
    status: str = "active",
) -> EvalCase:
    return db.create_eval_case(
        name=name, case_type=case_type, split="train",
        status=status,
        input=input or {},
        expected=expected or {},
        rubric=rubric or {},
    )


# --- scoring (pure unit tests, no DB writes for the scoring assertions) ---


def test_score_chat_reply_must_include_all_present():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={"must_include": ["hello", "world"]},
        rubric={}, status="active",
    )
    score, details = score_chat_reply_case(case, {"text": "hello cruel world"})
    assert score == 1.0
    assert details["must_include"]["matched"] == 2
    assert details["must_include"]["total"] == 2


def test_score_chat_reply_must_include_partial():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={"must_include": ["hello", "missing"]},
        rubric={}, status="active",
    )
    score, details = score_chat_reply_case(case, {"text": "hello world"})
    assert abs(score - 0.5) < 1e-9


def test_score_chat_reply_must_include_any_is_binary():
    """Every alternatives group must match; one satisfied override out of two
    must NOT earn fractional credit that averages past the threshold."""
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={},
        expected={"must_include": ["62", "22"],
                  "must_include_any": [["mile", " mi"],
                                       ["USD", "US dollar", "$", "dollar"]]},
        rubric={}, status="active",
    )
    score, details = score_chat_reply_case(
        case, {"text": "About 62 miles, costing 22 US dollars."})
    assert score == 1.0
    for text in ("62 mi and 22 EUR", "62 km and 22 USD"):
        score, details = score_chat_reply_case(case, {"text": text})
        assert details["must_include_any"]["matched"] == 1
        assert score < 0.7, text          # fails the default threshold


def test_score_chat_reply_word_bounds():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={"min_words": 3, "max_words": 5},
        rubric={}, status="active",
    )
    assert score_chat_reply_case(case, {"text": "one two three four"})[0] == 1.0
    assert score_chat_reply_case(case, {"text": "one two"})[0] == 0.0
    assert score_chat_reply_case(
        case, {"text": "one two three four five six"})[0] == 0.0


def test_score_chat_reply_must_not_include_violation_lowers_score():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={
            "must_include": ["hello"],
            "must_not_include": ["forbidden"],
        },
        rubric={}, status="active",
    )
    score, _details = score_chat_reply_case(
        case, {"text": "hello forbidden"},
    )
    # must_include passes (1.0); must_not_include fails (0.0); avg=0.5
    assert abs(score - 0.5) < 1e-9


def test_score_chat_reply_requires_json_fails_on_invalid():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={"requires_json": True},
        rubric={}, status="active",
    )
    score, _details = score_chat_reply_case(case, {"text": "not valid json"})
    assert score == 0.0


def test_score_chat_reply_requires_json_passes_on_valid():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={"requires_json": True},
        rubric={}, status="active",
    )
    score, _details = score_chat_reply_case(case, {"text": '{"a": 1}'})
    assert score == 1.0


def test_score_memory_retrieval_expected_memory_present():
    expected_uuid = uuid4()
    case = EvalCase(
        name="x", case_type="memory_retrieval", split="train",
        input={}, expected={"expected_memories": [str(expected_uuid)]},
        rubric={}, status="active",
    )
    retrieved = [
        type("M", (), {"uuid": expected_uuid, "text": "some fact"})(),
    ]
    score, details = score_memory_retrieval_case(case, retrieved)
    assert score == 1.0
    assert details["expected_memories"]["matched"] == 1


def test_score_memory_retrieval_missing_expected_memory():
    expected_uuid = uuid4()
    case = EvalCase(
        name="x", case_type="memory_retrieval", split="train",
        input={}, expected={"expected_memories": [str(expected_uuid)]},
        rubric={"threshold": 0.7}, status="active",
    )
    retrieved = []   # nothing came back
    score, _details = score_memory_retrieval_case(case, retrieved)
    assert score == 0.0


def test_score_memory_retrieval_forbidden_memory_present_lowers_score():
    forbidden_uuid = uuid4()
    case = EvalCase(
        name="x", case_type="memory_retrieval", split="train",
        input={},
        expected={"forbidden_memories": [str(forbidden_uuid)]},
        rubric={}, status="active",
    )
    retrieved = [
        type("M", (), {"uuid": forbidden_uuid, "text": "leaked secret"})(),
    ]
    score, _details = score_memory_retrieval_case(case, retrieved)
    assert score == 0.0


# --- runner (live DB) ---


def test_run_eval_case_records_result_with_threshold(app_ctx, fresh_tag):
    try:
        case = _case(
            f"{fresh_tag}: chat reply",
            input={"actual_output": "hello world"},
            expected={"must_include": ["hello"]},
            rubric={"threshold": 0.7},
        )
        run = db.create_eval_run(name=f"{fresh_tag}: ad-hoc", agent_role="chat")
        result = run_eval_case(case, eval_run_uuid=run.uuid)
        assert result.eval_case_uuid == case.uuid
        assert result.score == 1.0
        assert result.passed is True
    finally:
        _cleanup(fresh_tag)


def test_run_eval_case_below_threshold_fails(app_ctx, fresh_tag):
    try:
        case = _case(
            f"{fresh_tag}: failing chat",
            input={"actual_output": "no hello here"},
            expected={"must_include": ["banana", "apple"]},
            rubric={"threshold": 0.7},
        )
        run = db.create_eval_run(name=f"{fresh_tag}: fail run", agent_role="chat")
        result = run_eval_case(case, eval_run_uuid=run.uuid)
        assert result.score == 0.0
        assert result.passed is False
    finally:
        _cleanup(fresh_tag)


def test_run_eval_case_memory_retrieval_uses_live_retrieval(app_ctx, fresh_tag):
    """End-to-end: a memory_retrieval case with input.query hits the real
    retrieve_memories implementation. Seed a memory_claim so the query
    has something to find."""
    try:
        claim = db.create_memory_claim(
            scope="global", kind="fact",
            text="the universal answer is forty two",
            confidence=1.0, status="active", sensitivity="public",
            subject=fresh_tag,
        )
        case = _case(
            f"{fresh_tag}: mem", case_type="memory_retrieval",
            input={"query": "universal answer"},
            expected={"expected_memories": [str(claim.uuid)]},
            rubric={"threshold": 0.7},
        )
        run = db.create_eval_run(name=f"{fresh_tag}: mem run", agent_role="chat")
        result = run_eval_case(case, eval_run_uuid=run.uuid)
        assert result.score == 1.0
        assert result.passed is True
    finally:
        _cleanup(fresh_tag)


def test_run_eval_suite_passes_memory_retrieval_limit_to_retrieve_memories(
    app_ctx, fresh_tag, monkeypatch,
):
    """If the candidate config supplies memory_retrieval_limit, the
    runner must pass it to retrieve_memories. Verified by spying on
    the function the evals.runner actually calls."""
    received_kwargs: dict = {}

    import evals.runner as er

    # evals.runner may import retrieve_memories at module level or call
    # it via memory_retrieval.retrieve_memories. Patch both, harmless
    # if one is unused.
    import memory.retrieval as mr
    real_mr = mr.retrieve_memories

    def spy(query, **kw):
        received_kwargs.update(kw)
        return real_mr(query, **kw)

    monkeypatch.setattr(mr, "retrieve_memories", spy)
    if hasattr(er, "retrieve_memories"):
        monkeypatch.setattr(er, "retrieve_memories", spy)

    try:
        case = _case(
            f"{fresh_tag}: limit-check",
            case_type="memory_retrieval",
            input={"query": "hello", "agent_uuid": str(uuid4())},
            expected={"expected_memories": []},
        )
        run = er.run_eval_suite(
            case_uuids=[case.uuid],
            name=f"{fresh_tag}: spy",
            config={"memory_retrieval_limit": 9},
        )
        assert received_kwargs.get("limit") == 9, received_kwargs
        # Default include_secret is False; not exercised by this test
        # but worth asserting so a future regression that drops it is
        # caught.
        assert received_kwargs.get("include_secret") is False, received_kwargs
        assert (run.config or {}).get("memory_retrieval_limit") == 9
    finally:
        _cleanup(fresh_tag)


def test_run_eval_suite_active_records_run_and_results(app_ctx, fresh_tag):
    try:
        good = _case(
            f"{fresh_tag}: passing",
            input={"actual_output": "hello"},
            expected={"must_include": ["hello"]},
        )
        bad = _case(
            f"{fresh_tag}: failing",
            input={"actual_output": "no match"},
            expected={"must_include": ["never"]},
        )
        run = run_eval_suite(
            case_uuids=[good.uuid, bad.uuid],
            name=f"{fresh_tag}: suite",
        )
        assert run.finished_at is not None
        results = db.list_eval_results_for_run(run.uuid)
        assert len(results) == 2
        by_case = {r.eval_case_uuid: r for r in results}
        assert by_case[good.uuid].passed is True
        assert by_case[bad.uuid].passed is False
        summary = run.summary
        assert summary["cases"] == 2
        assert summary["passed"] == 1
        assert "mean_score" in summary
    finally:
        _cleanup(fresh_tag)


def test_score_chat_reply_expected_memory_substring_in_output():
    """The expected_memories criterion for a chat_reply case treats each
    expected entry as a substring of the actual_output text — not as a
    set-membership lookup against the whole output."""
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={"expected_memories": ["forty two"]},
        rubric={}, status="active",
    )
    score, _details = score_chat_reply_case(
        case, {"text": "the answer is forty two"},
    )
    assert score == 1.0


def test_score_chat_reply_with_no_criteria_records_warnings():
    case = EvalCase(
        name="x", case_type="chat_reply", split="train",
        input={}, expected={}, rubric={}, status="active",
    )
    score, details = score_chat_reply_case(case, {"text": "anything"})
    assert score == 1.0
    assert details.get("warnings"), \
        f"warnings flag missing; got {details!r}"


import subprocess


def test_cli_runs_a_single_case_and_exits_successfully(app_ctx, fresh_tag):
    """Run `python -m evals.runner --case <uuid>` in a subprocess. The CLI
    must print a compact summary and exit 0."""
    try:
        case = _case(
            f"{fresh_tag}: cli case",
            input={"actual_output": "hello"},
            expected={"must_include": ["hello"]},
        )
        # Commit so the subprocess (a separate Postgres connection) sees the
        # row. The fixtures already commit through their helpers; an extra
        # commit here is a no-op but defensive.
        db.db.session.commit()
        result = subprocess.run(
            [
                "venv/bin/python", "-m", "evals.runner",
                "--case", str(case.uuid),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"CLI exited {result.returncode}; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        # The summary block should mention the run, the case count, and at
        # least one of the score lines.
        out = result.stdout
        assert "Cases:" in out
        assert "Passed:" in out
    finally:
        _cleanup(fresh_tag)


def test_cli_failure_line_shows_case_name(app_ctx, fresh_tag):
    """The CLI's Failures: block should label rows by case name, not uuid."""
    try:
        case = _case(
            f"{fresh_tag}: distinctive case name",
            input={"actual_output": "no match"},
            expected={"must_include": ["never seen"]},
        )
        db.db.session.commit()
        result = subprocess.run(
            [
                "venv/bin/python", "-m", "evals.runner",
                "--case", str(case.uuid),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "Failures:" in out
        assert "distinctive case name" in out, (
            f"expected case name in failure line; got {out!r}"
        )
        assert "must_include" in out
    finally:
        _cleanup(fresh_tag)


def test_memory_include_private_is_unsupported(app_ctx, fresh_tag, monkeypatch):
    """Passing `memory_include_private=True` must NOT set
    include_secret=True. The misnamed key is now explicitly unsupported."""
    received_kwargs: dict = {}
    import memory.retrieval as mr
    import evals.runner as er
    real = mr.retrieve_memories

    def spy(query, **kw):
        received_kwargs.update(kw)
        return real(query, **kw)

    monkeypatch.setattr(mr, "retrieve_memories", spy)
    if hasattr(er, "retrieve_memories"):
        monkeypatch.setattr(er, "retrieve_memories", spy)

    try:
        case = db.create_eval_case(
            name=f"{fresh_tag}: private-knob",
            case_type="memory_retrieval",
            split="train",
            status="active",
            input={"query": "x", "agent_uuid": str(uuid4())},
            expected={"expected_memories": []},
        )
        run = er.run_eval_suite(
            case_uuids=[case.uuid],
            name=f"{fresh_tag}: private",
            config={"memory_include_private": True},
        )
        assert received_kwargs.get("include_secret") is False, received_kwargs
        unsupported = (run.config or {}).get("unsupported_config_keys") or []
        assert "memory_include_private" in unsupported, run.config
    finally:
        _cleanup(fresh_tag)


def test_memory_include_secret_is_supported(app_ctx, fresh_tag, monkeypatch):
    """`memory_include_secret=True` is the explicitly-named supported
    knob; it must flow to retrieve_memories(include_secret=True)."""
    received_kwargs: dict = {}
    import memory.retrieval as mr
    import evals.runner as er
    real = mr.retrieve_memories

    def spy(query, **kw):
        received_kwargs.update(kw)
        return real(query, **kw)

    monkeypatch.setattr(mr, "retrieve_memories", spy)
    if hasattr(er, "retrieve_memories"):
        monkeypatch.setattr(er, "retrieve_memories", spy)

    try:
        case = db.create_eval_case(
            name=f"{fresh_tag}: secret-knob",
            case_type="memory_retrieval",
            split="train",
            status="active",
            input={"query": "x", "agent_uuid": str(uuid4())},
            expected={"expected_memories": []},
        )
        er.run_eval_suite(
            case_uuids=[case.uuid],
            name=f"{fresh_tag}: secret",
            config={"memory_include_secret": True},
        )
        assert received_kwargs.get("include_secret") is True, received_kwargs
    finally:
        _cleanup(fresh_tag)
