"""Tests for the live profile-guidance eval harness (evals/profile_guidance):
the runner drives the production prompt-construction seam with a stubbed
model call — variants toggle the blocks, repetitions are recorded with
prompt hashes and model ids, only `reply` decisions score, the global
`profile.current` setting is never touched, and no chat rows are left
behind."""

from uuid import uuid4

import pytest

import db
import evals.profile_guidance as pg
from agents.assistant import AssistantActionName, AssistantStepDecision


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        db.db.session.rollback()
        ctx.pop()


@pytest.fixture
def case(app_ctx):
    germany = pg._template_uuid("Germany")
    c = db.create_eval_case(
        name=f"pg-test-{uuid4().hex[:8]}", case_type="chat_reply",
        status="active",
        input={"message": "Write 31 December 2026 as a short numeric date.",
               "profile_uuid": germany},
        expected={"must_include": ["31.12.2026"],
                  "must_not_include": ["12/31/2026"]},
        rubric={"family": "locale"},
    )
    try:
        yield c
    finally:
        db.db.session.rollback()
        for run in db.db.session.query(db.EvalRun).all():
            if str(c.uuid) in (run.config or {}).get("case_uuids", []):
                db.db.session.delete(run)
        db.db.session.query(db.EvalCase).filter(
            db.EvalCase.uuid == c.uuid).delete()
        db.db.session.commit()


def _stub(reply_text, action=AssistantActionName.REPLY):
    captured = {"prompts": []}

    def fake(self, *, system_prompt, user_prompt, response_model, validator=None):
        captured["prompts"].append((system_prompt, user_prompt))
        # The real ModelGroupAgent usage schema — {"input", "output", "ms"} —
        # so this stub can't mask a key mismatch in the harness again.
        self._last_usage = {"input": 321, "output": 12, "ms": 40}
        self._last_model_uuid = captured.setdefault("model_uuid", uuid4())
        return AssistantStepDecision(
            reason="eval", action=action, args={"message": reply_text})

    return fake, captured


def test_suite_records_repetitions_and_mean(case, monkeypatch):
    fake, captured = _stub("Das Datum ist 31.12.2026.")
    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
    run = pg.run_profile_guidance_suite([case.uuid], variant="formatting_only",
                                        repetitions=3)
    assert (run.config or {})["variant"] == "formatting_only"
    results = db.list_eval_results_for_run(run.uuid)
    assert len(results) == 1
    result = results[0]
    assert result.score == 1.0 and result.passed
    reps = result.details["repetitions"]
    assert len(reps) == 3
    for rep in reps:
        assert rep["output"] == "Das Datum ist 31.12.2026."
        assert rep["score"] == 1.0 and rep["passed"]
        assert rep["input_tokens"] == 321
        assert rep["model_uuid"] == str(captured["model_uuid"])
        assert len(rep["prompt_hash"]) == 16
    assert result.details["family"] == "locale"
    assert run.summary["cases"] == 1 and run.summary["passed"] == 1


def test_variants_toggle_blocks_in_the_real_prompt(case, monkeypatch):
    seen = {}
    for variant in pg.VARIANTS:
        fake, captured = _stub("ok")
        monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
        pg.run_profile_guidance_suite([case.uuid], variant=variant,
                                      repetitions=1)
        seen[variant] = captured["prompts"][0][1]
    assert "<formatting_guide" not in seen["baseline"]
    assert "<knowledge_calibration" not in seen["baseline"]
    assert "<formatting_guide" in seen["formatting_only"]
    assert "<knowledge_calibration" not in seen["formatting_only"]
    assert "<formatting_guide" not in seen["calibration_only"]
    assert "<knowledge_calibration" in seen["calibration_only"]   # Germany seeds rows
    assert "<formatting_guide" in seen["combined"]
    assert "<knowledge_calibration" in seen["combined"]
    # The identity block rides every variant (it is not gated).
    assert all("<operator_identity" in p for p in seen.values())
    # The case message is the current request in the production prompt shape.
    assert all("31 December 2026" in p for p in seen.values())


def test_non_reply_decision_is_a_failed_repetition(case, monkeypatch):
    fake, _ = _stub("31.12.2026", action=AssistantActionName.MEMORY_QUERY)
    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
    run = pg.run_profile_guidance_suite([case.uuid], variant="baseline",
                                        repetitions=2)
    result = db.list_eval_results_for_run(run.uuid)[0]
    assert result.score == 0.0 and not result.passed
    assert all("only reply is accepted" in r["error"]
               for r in result.details["repetitions"])


def test_model_failure_is_a_scored_zero_not_a_crash(case, monkeypatch):
    def boom(self, **kwargs):
        raise RuntimeError("no model reachable")

    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", boom)
    run = pg.run_profile_guidance_suite([case.uuid], repetitions=1)
    result = db.list_eval_results_for_run(run.uuid)[0]
    assert result.score == 0.0
    assert "no model reachable" in result.details["repetitions"][0]["error"]


def test_runner_touches_no_settings_and_leaves_no_chat_rows(case, monkeypatch):
    fake, _ = _stub("31.12.2026")
    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)

    def forbidden(key, value):
        raise AssertionError(f"runner must not write settings ({key})")

    monkeypatch.setattr(db, "set_setting", forbidden)
    monkeypatch.setattr(db, "set_current_profile", forbidden)
    before_msgs = db.db.session.query(db.ChatMessage).count()
    before_rooms = db.db.session.query(db.Chatroom).count()
    current_before = db.db.session.query(db.AppSetting).filter_by(
        key="profile.current").one_or_none()
    value_before = current_before.value if current_before else None

    pg.run_profile_guidance_suite([case.uuid], variant="combined",
                                  repetitions=2)

    assert db.db.session.query(db.ChatMessage).count() == before_msgs
    assert db.db.session.query(db.Chatroom).count() == before_rooms
    current_after = db.db.session.query(db.AppSetting).filter_by(
        key="profile.current").one_or_none()
    assert (current_after.value if current_after else None) == value_before


def test_inline_profile_override(app_ctx, monkeypatch):
    c = db.create_eval_case(
        name=f"pg-inline-{uuid4().hex[:8]}", case_type="chat_reply",
        status="active",
        input={"message": "hi",
               "profile": {"uuid": str(uuid4()), "name": "Inline",
                           "data": {"units": "imperial"}}},
        expected={"must_include": ["ok"]}, rubric={"family": "injection"},
    )
    try:
        fake, captured = _stub("ok")
        monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
        run = pg.run_profile_guidance_suite([c.uuid], variant="formatting_only",
                                            repetitions=1)
        assert db.list_eval_results_for_run(run.uuid)[0].score == 1.0
        assert "mi, lb, and °F" in captured["prompts"][0][1]
    finally:
        db.db.session.rollback()
        for run in db.db.session.query(db.EvalRun).all():
            if str(c.uuid) in (run.config or {}).get("case_uuids", []):
                db.db.session.delete(run)
        db.db.session.query(db.EvalCase).filter(
            db.EvalCase.uuid == c.uuid).delete()
        db.db.session.commit()


def test_unresolvable_profile_scores_zero(app_ctx, monkeypatch):
    c = db.create_eval_case(
        name=f"pg-missing-{uuid4().hex[:8]}", case_type="chat_reply",
        status="active",
        input={"message": "hi", "profile_uuid": str(uuid4())},
        expected={"must_include": ["ok"]},
    )
    try:
        fake, _ = _stub("ok")
        monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
        run = pg.run_profile_guidance_suite([c.uuid], repetitions=1)
        result = db.list_eval_results_for_run(run.uuid)[0]
        assert result.score == 0.0
        assert "did not resolve" in result.details["repetitions"][0]["error"]
    finally:
        db.db.session.rollback()
        for run in db.db.session.query(db.EvalRun).all():
            if str(c.uuid) in (run.config or {}).get("case_uuids", []):
                db.db.session.delete(run)
        db.db.session.query(db.EvalCase).filter(
            db.EvalCase.uuid == c.uuid).delete()
        db.db.session.commit()


def test_seed_cases_create_update_and_respect_ownership(app_ctx):
    created = pg.seed_profile_guidance_cases()
    try:
        assert pg.seed_profile_guidance_cases() == []   # current rev → no-op
        names = {c.name for c in created}
        if created:                            # first run on this database
            assert all(c.status == "candidate" for c in created)
            assert all((c.rubric or {}).get("seed") == "profile_guidance"
                       for c in created)
            assert any("injection" in n for n in names)
            injection = next(c for c in created if "injection" in c.name)
            assert "profile" in injection.input          # inline hostile note
            assert injection.expected.get("must_include")  # empty reply fails
            exact = next(c for c in created if "code snippet" in c.name)
            assert exact.rubric["threshold"] == 1.0      # hard-zero family
            override = next(c for c in created if "miles and USD" in c.name)
            assert override.expected["must_include_any"]  # labels required
            teach = next(c for c in created
                         if "beginner Python" in c.name)
            assert teach.expected["min_words"] >= 60
            concise = next(c for c in created
                           if "expert Mathematics" in c.name)
            assert concise.expected["max_words"] <= 150
            unlisted = next(c for c in created if "unlisted" in c.name)
            assert unlisted.rubric["family"] == "regression"

        # A pre-fix database: an older-rev seeded case is updated IN PLACE
        # (status preserved), so the release gate never runs old definitions.
        stale = next(c for c in db.list_eval_cases(case_type="chat_reply")
                     if "miles and USD" in c.name)
        stale.rubric = {**stale.rubric, "seed_rev": 1}
        stale.expected = {"must_include": ["62", "22"]}   # the old weak form
        stale.status = "active"
        db.db.session.commit()
        touched = pg.seed_profile_guidance_cases()
        assert [c.name for c in touched] == [stale.name]
        refreshed = db.get_eval_case(stale.uuid)
        assert refreshed.expected["must_include_any"]     # definition fixed
        assert refreshed.status == "active"               # operator state kept
        assert refreshed.rubric["seed_rev"] == pg.SEED_REV

        # An operator-owned case (seed marker removed) is never touched.
        owned = next(c for c in db.list_eval_cases(case_type="chat_reply")
                     if "German date order" in c.name)
        owned.rubric = {"family": "locale"}               # marker stripped
        owned.expected = {"must_include": ["operator edit"]}
        db.db.session.commit()
        assert pg.seed_profile_guidance_cases() == []
        assert db.get_eval_case(owned.uuid).expected == {
            "must_include": ["operator edit"]}
    finally:
        db.db.session.rollback()
        for c in db.list_eval_cases(case_type="chat_reply"):
            if c.name.startswith("pg "):
                db.db.session.delete(c)
        db.db.session.commit()


def test_unknown_variant_rejected(app_ctx):
    with pytest.raises(ValueError, match="unknown variant"):
        pg.run_profile_guidance_suite([], variant="everything")
