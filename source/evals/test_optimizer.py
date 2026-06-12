"""Tests for evals.optimizer: candidate generation and safety-gated selection."""

from uuid import uuid4

import pytest

import db
from db import EvalCase, EvalResult, EvalRun

from evals.optimizer import (
    BASE_CONFIG,
    OptimizerDecision,
    generate_candidate_configs,
    select_best_candidate,
)
from evals.optimizer import run_candidate_matrix


def test_generate_candidate_configs_returns_known_bounded_variants():
    base = dict(BASE_CONFIG)
    configs = generate_candidate_configs(base)
    # We expect exactly the memory_retrieval_limit knob, three variants.
    assert isinstance(configs, list)
    assert len(configs) == 3, configs
    limits = sorted(c["memory_retrieval_limit"] for c in configs)
    assert limits == [3, 6, 10]
    # All other fields preserved from base.
    for c in configs:
        for k, v in base.items():
            if k == "memory_retrieval_limit":
                continue
            assert c[k] == v, (k, v, c)
    # The base dict was not mutated.
    assert base["memory_retrieval_limit"] == BASE_CONFIG["memory_retrieval_limit"]


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
    # Collect cases for this test by name prefix.
    case_uuids = [
        c.uuid for c in db.db.session.query(EvalCase)
        .filter(EvalCase.name.like(f"{prefix}%")).all()
    ]
    # Also collect any EvalRuns produced by the default runner that
    # reference these cases (their name won't carry our `fresh_tag`,
    # so we can't filter by name — instead find runs whose config
    # references the case UUIDs).
    direct_run_uuids = [
        r.uuid for r in db.db.session.query(EvalRun)
        .filter(EvalRun.name.like(f"{prefix}%")).all()
    ]
    derived_run_uuids: list = []
    if case_uuids:
        case_uuid_strs = {str(u) for u in case_uuids}
        # Find runs whose config["case_uuids"] intersects our cases.
        for r in db.db.session.query(EvalRun).all():
            cfg = r.config or {}
            cfg_uuids = cfg.get("case_uuids") or []
            if any(s in case_uuid_strs for s in cfg_uuids):
                derived_run_uuids.append(r.uuid)
    all_run_uuids = list({*direct_run_uuids, *derived_run_uuids})
    if all_run_uuids:
        db.db.session.query(EvalResult).filter(
            EvalResult.eval_run_uuid.in_(all_run_uuids)
        ).delete(synchronize_session=False)
        db.db.session.query(EvalRun).filter(
            EvalRun.uuid.in_(all_run_uuids)
        ).delete(synchronize_session=False)
    if case_uuids:
        db.db.session.query(EvalCase).filter(
            EvalCase.uuid.in_(case_uuids)
        ).delete(synchronize_session=False)
    db.db.session.commit()


def _make_case(prefix: str, label: str, split: str = "train") -> EvalCase:
    return db.create_eval_case(
        name=f"{prefix}: {label}", case_type="chat_reply",
        split=split, status="active",
    )


def _make_run(prefix: str, label: str, *, config=None) -> EvalRun:
    return db.create_eval_run(
        name=f"{prefix}: {label}", agent_role="chat", config=config,
    )


def _stamp(run, *, mean, passed, total):
    db.finish_eval_run(
        run.uuid, summary={
            "cases": total, "passed": passed,
            "failed": total - passed, "mean_score": mean, "failures": [],
        },
    )


def _result(run, case, *, score, passed, details=None):
    return db.create_eval_result(
        eval_run_uuid=run.uuid, eval_case_uuid=case.uuid,
        score=score, passed=passed, details=details or {},
    )


def test_select_best_candidate_picks_improver_with_no_regressions(
    app_ctx, fresh_tag,
):
    try:
        train = _make_case(fresh_tag, "train", split="train")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, train, score=0.5, passed=True)
        _stamp(baseline, mean=0.5, passed=1, total=1)

        cand_good = _make_run(fresh_tag, "cand-good",
                              config={"memory_retrieval_limit": 6})
        _result(cand_good, train, score=0.9, passed=True)
        _stamp(cand_good, mean=0.9, passed=1, total=1)

        cand_bad = _make_run(fresh_tag, "cand-bad",
                             config={"memory_retrieval_limit": 10})
        _result(cand_bad, train, score=0.3, passed=False)
        _stamp(cand_bad, mean=0.3, passed=0, total=1)

        decision = select_best_candidate(
            baseline.uuid, [cand_good.uuid, cand_bad.uuid],
        )
        assert isinstance(decision, OptimizerDecision)
        assert decision.selected_uuid == cand_good.uuid
        assert "improves" in decision.reason.lower() or \
               "selected" in decision.reason.lower()
    finally:
        _cleanup(fresh_tag)


def test_select_best_candidate_rejects_regression_split_failure(
    app_ctx, fresh_tag,
):
    """A candidate that improves train but breaks a regression-split pin
    must be rejected even if its overall mean is higher."""
    try:
        train = _make_case(fresh_tag, "train", split="train")
        pin = _make_case(fresh_tag, "regression-pin", split="regression")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, train, score=0.5, passed=True)
        _result(baseline, pin, score=1.0, passed=True)
        _stamp(baseline, mean=0.75, passed=2, total=2)

        candidate = _make_run(fresh_tag, "cand-train-up-pin-broken")
        _result(candidate, train, score=1.0, passed=True)
        _result(candidate, pin, score=0.0, passed=False)
        _stamp(candidate, mean=0.5, passed=1, total=2)

        decision = select_best_candidate(baseline.uuid, [candidate.uuid])
        assert decision.selected_uuid is None
        assert any("regression" in r.lower() for r in
                   decision.rejected_candidates[0]["reasons"])
    finally:
        _cleanup(fresh_tag)


def test_select_best_candidate_no_safe_improvement(app_ctx, fresh_tag):
    """When every candidate drops the mean, the optimizer must answer
    'no safe improvement'."""
    try:
        train = _make_case(fresh_tag, "train", split="train")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, train, score=0.8, passed=True)
        _stamp(baseline, mean=0.8, passed=1, total=1)

        candidates = []
        for i, score in enumerate([0.7, 0.6, 0.5]):
            cand = _make_run(fresh_tag, f"cand-{i}")
            _result(cand, train, score=score, passed=(score >= 0.5))
            _stamp(cand, mean=score, passed=(1 if score >= 0.5 else 0), total=1)
            candidates.append(cand)

        decision = select_best_candidate(
            baseline.uuid, [c.uuid for c in candidates],
        )
        assert decision.selected_uuid is None
        assert "no safe improvement" in decision.reason.lower()
        assert len(decision.rejected_candidates) == 3
    finally:
        _cleanup(fresh_tag)


def test_select_best_candidate_rejects_forbidden_memory_failure(
    app_ctx, fresh_tag,
):
    """A candidate whose details.forbidden_memories shows a leak must
    be rejected even with a higher mean."""
    try:
        train = _make_case(fresh_tag, "train", split="train")
        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, train, score=0.5, passed=True,
                details={"forbidden_memories": {"total": 1, "absent": 1}})
        _stamp(baseline, mean=0.5, passed=1, total=1)

        candidate = _make_run(fresh_tag, "cand-leaks-forbidden")
        _result(candidate, train, score=0.9, passed=True,
                details={"forbidden_memories": {"total": 1, "absent": 0}})
        _stamp(candidate, mean=0.9, passed=1, total=1)

        decision = select_best_candidate(baseline.uuid, [candidate.uuid])
        assert decision.selected_uuid is None
        assert any("forbidden" in r.lower() for r in
                   decision.rejected_candidates[0]["reasons"])
    finally:
        _cleanup(fresh_tag)


def test_run_candidate_matrix_invokes_runner_per_config(app_ctx, fresh_tag):
    """run_candidate_matrix should call the injected runner once per
    config and return the list of EvalRuns it produced, preserving
    order. We inject a stub runner so the test doesn't need a real
    eval suite."""
    try:
        calls: list[dict] = []

        def stub_runner(config: dict, case_filter: dict) -> EvalRun:
            calls.append({"config": dict(config), "case_filter": dict(case_filter)})
            run = _make_run(fresh_tag, f"stub-{len(calls)}", config=config)
            _stamp(run, mean=0.5, passed=0, total=0)
            return run

        configs = [
            {"memory_retrieval_limit": 3},
            {"memory_retrieval_limit": 6},
        ]
        runs = run_candidate_matrix(
            configs, case_filter={"split": "train"}, runner=stub_runner,
        )
        assert len(runs) == 2
        assert all(isinstance(r, EvalRun) for r in runs)
        assert [c["config"]["memory_retrieval_limit"] for c in calls] == [3, 6]
        assert all(c["case_filter"] == {"split": "train"} for c in calls)
    finally:
        _cleanup(fresh_tag)


def test_run_candidate_matrix_default_runner_creates_eval_runs(
    app_ctx, fresh_tag,
):
    """Without a custom runner, the default runner must call into
    evals.runner.run_eval_suite and produce one EvalRun per candidate
    config. Each EvalRun.config must contain the candidate config."""
    try:
        case = db.create_eval_case(
            name=f"{fresh_tag}: bench",
            case_type="memory_retrieval",
            split="train",
            status="active",
            input={"query": "test", "agent_uuid": str(uuid4())},
            expected={"expected_memories": []},
        )
        configs = [
            {"memory_retrieval_limit": 3},
            {"memory_retrieval_limit": 6},
        ]
        runs = run_candidate_matrix(
            configs,
            case_filter={"case_uuids": [case.uuid]},
        )
        assert len(runs) == 2
        assert all(isinstance(r, EvalRun) for r in runs)
        limits = sorted(
            (r.config or {}).get("memory_retrieval_limit")
            for r in runs
        )
        assert limits == [3, 6]
    finally:
        _cleanup(fresh_tag)


def test_run_candidate_matrix_marks_unsupported_keys(app_ctx, fresh_tag):
    """If a candidate config contains keys that the runner can't yet
    apply (e.g. chat_prompt_variant), the EvalRun must record them as
    unsupported, not silently claim they were evaluated."""
    try:
        case = db.create_eval_case(
            name=f"{fresh_tag}: ignored",
            case_type="memory_retrieval",
            split="train",
            status="active",
            input={"query": "test", "agent_uuid": str(uuid4())},
            expected={"expected_memories": []},
        )
        configs = [{
            "memory_retrieval_limit": 4,
            "chat_prompt_variant": "experimental",
        }]
        runs = run_candidate_matrix(
            configs,
            case_filter={"case_uuids": [case.uuid]},
        )
        cfg = runs[0].config or {}
        unsupported = cfg.get("unsupported_config_keys")
        assert unsupported is not None, cfg
        assert "chat_prompt_variant" in unsupported, unsupported
    finally:
        _cleanup(fresh_tag)


def test_default_runner_evalruns_are_cleaned_up_after_test(
    app_ctx, fresh_tag,
):
    """Regression for WP06 Task 4 review fix: EvalRuns created by the
    default runner must be discoverable from the test's fresh_tag prefix
    so cleanup catches them, even though the runner names them
    'optimizer-candidate:...' rather than tagging with the prefix.
    The link is the case_uuids on EvalRun.config."""
    case = db.create_eval_case(
        name=f"{fresh_tag}: leak-check",
        case_type="memory_retrieval",
        split="train",
        status="active",
        input={"query": "x", "agent_uuid": str(uuid4())},
        expected={"expected_memories": []},
    )
    # Snapshot the uuid up-front; the ORM instance becomes expired
    # after _cleanup deletes the row.
    case_uuid = case.uuid
    try:
        # Run the default runner, producing 1 EvalRun named
        # "optimizer-candidate:..." NOT prefixed with fresh_tag.
        runs = run_candidate_matrix(
            [{"memory_retrieval_limit": 3}],
            case_filter={"case_uuids": [case_uuid]},
        )
        assert len(runs) == 1
        run_name = runs[0].name
        assert not run_name.startswith(fresh_tag), (
            f"name must NOT carry fresh_tag for this regression test "
            f"to be meaningful; got {run_name!r}"
        )
    finally:
        _cleanup(fresh_tag)

    # After cleanup, the EvalRun for this case must be gone — i.e.,
    # the cleanup helper found it via the case_uuids link.
    leaked: list = []
    try:
        leaked = db.db.session.query(EvalRun).filter(
            EvalRun.config["case_uuids"].astext.contains(str(case_uuid))
        ).all()
    except Exception:
        # The JSONB-into-text search idiom may not work; fall back to
        # Python-side scan.
        leaked = []
    # The above JSONB-into-text search may need a slightly different
    # idiom; if SQLAlchemy gives trouble, fall back to a Python-side
    # scan:
    if not leaked:
        # Python-side scan as a safety check.
        for r in db.db.session.query(EvalRun).all():
            cfg = r.config or {}
            if str(case_uuid) in (cfg.get("case_uuids") or []):
                leaked.append(r)
    assert not leaked, (
        f"cleanup did not catch {len(leaked)} EvalRun(s) referencing "
        f"the test's case uuid: {[r.name for r in leaked]}"
    )


def test_select_best_candidate_rejects_missing_baseline_cases(
    app_ctx, fresh_tag,
):
    """A candidate that omits a baseline case must be rejected by the
    optimizer even if it scores higher on the cases it did run. This
    is the optimizer-side analogue of the WP06 gate fix."""
    try:
        case_a = _make_case(fresh_tag, "case-a", split="train")
        case_b = _make_case(fresh_tag, "case-b", split="train")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, case_a, score=0.5, passed=True)
        _result(baseline, case_b, score=0.5, passed=True)
        _stamp(baseline, mean=0.5, passed=2, total=2)

        # Candidate only ran case_a — case_b is missing — and scores
        # very well on the subset.
        candidate = _make_run(fresh_tag, "cand-missing-b")
        _result(candidate, case_a, score=1.0, passed=True)
        _stamp(candidate, mean=1.0, passed=1, total=1)

        decision = select_best_candidate(baseline.uuid, [candidate.uuid])
        assert decision.selected_uuid is None, decision
        assert len(decision.rejected_candidates) == 1
        reasons = decision.rejected_candidates[0]["reasons"]
        joined = " ".join(reasons).lower()
        assert "missing" in joined or "baseline" in joined, reasons
        assert str(case_b.uuid) in " ".join(reasons), reasons
    finally:
        _cleanup(fresh_tag)


def test_base_config_does_not_include_secret_by_default():
    """BASE_CONFIG must default to NOT including secret memories.
    Regression for WP07 Finding 2."""
    from evals.optimizer import BASE_CONFIG
    assert BASE_CONFIG.get("memory_include_secret") is False, BASE_CONFIG
    assert "memory_include_private" not in BASE_CONFIG, BASE_CONFIG


def test_select_best_candidate_rejects_candidate_only_cases(
    app_ctx, fresh_tag,
):
    """A candidate that adds extra cases absent from the baseline must
    be rejected by the optimizer, mirroring the missing-baseline-cases
    protection added in WP07."""
    try:
        shared = _make_case(fresh_tag, "shared", split="train")
        extra = _make_case(fresh_tag, "candidate-only-extra", split="train")

        baseline = _make_run(fresh_tag, "baseline")
        _result(baseline, shared, score=0.5, passed=True)
        _stamp(baseline, mean=0.5, passed=1, total=1)

        candidate = _make_run(fresh_tag, "cand-with-extra")
        _result(candidate, shared, score=0.5, passed=True)
        _result(candidate, extra, score=1.0, passed=True)
        _stamp(candidate, mean=0.75, passed=2, total=2)

        decision = select_best_candidate(baseline.uuid, [candidate.uuid])
        assert decision.selected_uuid is None, decision
        assert len(decision.rejected_candidates) == 1
        reasons = decision.rejected_candidates[0]["reasons"]
        joined = " ".join(reasons).lower()
        assert (
            "candidate-only" in joined
            or "unmatched" in joined
            or "extra" in joined
        ), reasons
        assert str(extra.uuid) in " ".join(reasons), reasons
    finally:
        _cleanup(fresh_tag)

