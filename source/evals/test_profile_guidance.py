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
        if action is AssistantActionName.REPLY:
            args = {"1_message": reply_text, "2_audit": "OK"}
        else:
            args = {"message": reply_text}
        return AssistantStepDecision(reason="eval", action=action, args=args)

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
    assert all("<user_settings_json" in p for p in seen.values())
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
        assert "Prefer mi and lb" in captured["prompts"][0][1]
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
            # The calibration family is a forced divergence: one neutral
            # message, two inline profiles, OPPOSING bounds — a single
            # baseline answer can satisfy at most one of the pair.
            teach = next(c for c in created
                         if "teach depth divergence" in c.name)
            concise = next(c for c in created
                           if "concise depth divergence" in c.name)
            assert teach.input["message"] == concise.input["message"]
            assert teach.expected["min_words"] > concise.expected["max_words"]
            assert teach.rubric["pair"] == concise.rubric["pair"]
            # The profiles are IDENTICAL except depth: same uuid, same
            # visible name, same row identity and level — so the baseline
            # prompts cannot differ through the identity block.
            tp, cp = teach.input["profile"], concise.input["profile"]
            assert tp["uuid"] == cp["uuid"] and tp["name"] == cp["name"]
            teach_row = tp["data"]["calibration"]["topics"][0]
            concise_row = cp["data"]["calibration"]["topics"][0]
            assert teach_row["level"] == concise_row["level"]
            assert teach_row["depth"] == "teach"
            assert concise_row["depth"] == "concise"
            assert {k: v for k, v in teach_row.items() if k != "depth"} == \
                   {k: v for k, v in concise_row.items() if k != "depth"}
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

        # An operator-owned case (edited: no marker, no legacy fingerprint)
        # is never touched — taking ownership orphans its seed id, so the
        # seeder recreates the canonical definition as a fresh candidate
        # alongside it instead of overwriting the operator's version.
        owned = next(c for c in db.list_eval_cases(case_type="chat_reply")
                     if "German date order" in c.name)
        owned.rubric = {"family": "locale"}               # marker stripped
        owned.expected = {"must_include": ["operator edit"]}
        db.db.session.commit()
        recreated = pg.seed_profile_guidance_cases()
        assert [c.rubric["seed_id"] for c in recreated] == ["locale.date_order.de"]
        assert recreated[0].uuid != owned.uuid
        assert recreated[0].status == "candidate"
        assert db.get_eval_case(owned.uuid).expected == {
            "must_include": ["operator edit"]}            # untouched
    finally:
        db.db.session.rollback()
        for c in db.list_eval_cases(case_type="chat_reply"):
            if c.name.startswith("pg "):
                db.db.session.delete(c)
        db.db.session.commit()


def test_seed_migrates_markerless_legacy_cases(app_ctx):
    """Databases seeded before the rubric marker existed hold cases with only
    {"family": ...}. A verbatim legacy definition (complete-definition
    fingerprint match) is code-owned and must be migrated onto its stable
    seed id — renames included, history preserved — while a markerless case
    whose input, expected, OR rubric was edited is left alone."""
    legacy_name = "pg calibration: beginner Python teach depth"
    new_name = "pg calibration: teach depth divergence"
    us = pg._template_uuid("US")
    legacy_input = {"message": "What is a Python decorator?",
                    "profile_uuid": us}
    legacy_expected = {"must_include": ["function"]}
    legacy_rubric = {"family": "calibration"}       # pre-marker shape
    assert pg._seed_hash(legacy_input, legacy_expected, legacy_rubric) in \
        pg._LEGACY_SEED_HASHES[legacy_name]

    def _make_legacy(expected=None, rubric=None):
        return db.create_eval_case(
            name=legacy_name, case_type="chat_reply", status="active",
            input=legacy_input, expected=expected or legacy_expected,
            rubric=rubric or dict(legacy_rubric))

    try:
        # Scenario 1: fresh DB state — the legacy case is adopted onto its
        # seed id and renamed onto the new definition in place (same row,
        # status preserved, eval history preserved).
        legacy = _make_legacy()
        run = db.create_eval_run(name="history", agent_role="assistant")
        db.create_eval_result(eval_run_uuid=run.uuid,
                              eval_case_uuid=legacy.uuid,
                              score=0.5, passed=False, details={})
        touched = pg.seed_profile_guidance_cases()
        migrated = db.get_eval_case(legacy.uuid)
        assert migrated is not None
        assert migrated.name == new_name
        assert migrated.status == "active"
        assert migrated.expected["min_words"] > 0     # the new definition
        assert migrated.rubric["seed_id"] == "calibration.teach_divergence"
        assert migrated.rubric["seed_rev"] == pg.SEED_REV
        assert any(c.uuid == legacy.uuid for c in touched)
        assert len(db.list_eval_results_for_run(run.uuid)) == 1  # history kept

        # Scenario 2: a second claimant of the same seed id (a stale legacy
        # copy) is ARCHIVED, never deleted — its results survive.
        stale = _make_legacy()
        run2 = db.create_eval_run(name="history2", agent_role="assistant")
        db.create_eval_result(eval_run_uuid=run2.uuid,
                              eval_case_uuid=stale.uuid,
                              score=0.25, passed=False, details={})
        pg.seed_profile_guidance_cases()
        archived = db.get_eval_case(stale.uuid)
        assert archived is not None                   # NOT deleted
        assert archived.status == "archived"
        assert len(db.list_eval_results_for_run(run2.uuid)) == 1
        active_names = [c.name for c in
                        db.list_eval_cases(case_type="chat_reply",
                                           status="active")]
        assert active_names.count(new_name) == 1

        # Scenario 3: a markerless case with edited EXPECTED does not
        # fingerprint and is never touched, renamed, or archived.
        edited = _make_legacy(expected={"must_include": ["my own criteria"]})
        pg.seed_profile_guidance_cases()
        kept = db.get_eval_case(edited.uuid)
        assert kept is not None and kept.name == legacy_name
        assert kept.status == "active"
        assert kept.expected == {"must_include": ["my own criteria"]}
        db.db.session.delete(kept)
        db.db.session.commit()

        # Scenario 4: a markerless case with an edited RUBRIC (family swap,
        # custom threshold) is operator-owned too — the fingerprint covers
        # the complete definition.
        rubric_edited = _make_legacy(
            rubric={"family": "calibration", "threshold": 0.95})
        pg.seed_profile_guidance_cases()
        kept = db.get_eval_case(rubric_edited.uuid)
        assert kept is not None and kept.name == legacy_name
        assert kept.rubric == {"family": "calibration", "threshold": 0.95}
    finally:
        db.db.session.rollback()
        for c in db.list_eval_cases(case_type="chat_reply"):
            if c.name.startswith("pg "):
                db.db.session.delete(c)
        for r in db.db.session.query(db.EvalRun).filter(
                db.EvalRun.name.in_(["history", "history2"])).all():
            db.db.session.delete(r)      # the runs do not cascade with cases
        db.db.session.commit()


@pytest.fixture
def divergence_pair(app_ctx):
    pg.seed_profile_guidance_cases()
    cases = sorted((c for c in db.list_eval_cases(case_type="chat_reply")
                    if (c.rubric or {}).get("pair") == "depth_divergence"),
                   key=lambda c: c.name)
    assert len(cases) == 2
    try:
        yield cases
    finally:
        db.db.session.rollback()
        for run in db.db.session.query(db.EvalRun).all():
            if any(str(c.uuid) in (run.config or {}).get("case_uuids", [])
                   for c in cases):
                db.db.session.delete(run)
        for c in db.list_eval_cases(case_type="chat_reply"):
            if c.name.startswith("pg "):
                db.db.session.delete(c)
        db.db.session.commit()


def test_pair_baseline_prompts_are_identical(divergence_pair):
    """With the calibration block off there is nothing left to distinguish
    the pair — identity blocks included — so the prompts hash equal; with it
    on, the depth rows diverge them."""
    from agents.assistant import AssistantAgent
    from uuid import uuid4 as u4

    agent = AssistantAgent(agent_uuid=u4(), name="x", send=lambda _: None)
    concise, teach = divergence_pair
    hashes_off = set()
    hashes_on = set()
    for case in (concise, teach):
        profile = pg._resolve_profile(case.input)
        off = pg._build_case_prompts(agent, case, profile,
                                     include_formatting=False,
                                     include_calibration=False)
        on = pg._build_case_prompts(agent, case, profile,
                                    include_formatting=False,
                                    include_calibration=True)
        hashes_off.add(pg._prompt_hash(*off))
        hashes_on.add(pg._prompt_hash(*on))
    assert len(hashes_off) == 1        # baseline: byte-identical prompts
    assert len(hashes_on) == 2         # calibration on: depth diverges them


def test_pair_shares_baseline_generation(divergence_pair, monkeypatch):
    """Under variants without the calibration block, the pair generates ONCE
    and both cases score the same outputs — independent stochastic draws
    could otherwise hand the pair opposing lengths by luck and let baseline
    pass both."""
    # ~50 words including the anchor: satisfies concise (<=80), fails teach
    # (needs >=120 and an example).
    text = ("The mean value theorem says a function continuous on a closed "
            "interval and differentiable on its interior attains, at some "
            "interior point, an instantaneous rate of change equal to the "
            "average rate of change across the whole interval, linking local "
            "derivative behavior to global change concisely.")
    calls = {"n": 0}

    def fake(self, *, system_prompt, user_prompt, response_model, validator=None):
        calls["n"] += 1
        self._last_usage = {"input": 100, "output": 10, "ms": 5}
        self._last_model_uuid = None
        from agents.assistant import AssistantActionName, AssistantStepDecision
        return AssistantStepDecision(
            reason="eval", action=AssistantActionName.REPLY,
            args={"1_message": text, "2_audit": "OK"})

    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
    concise, teach = divergence_pair
    run = pg.run_profile_guidance_suite(
        [concise.uuid, teach.uuid], variant="baseline", repetitions=2)
    assert calls["n"] == 2             # ONE generation per repetition, shared
    results = {r.eval_case_uuid: r for r in
               db.list_eval_results_for_run(run.uuid)}
    concise_result = results[concise.uuid]
    teach_result = results[teach.uuid]
    for result in results.values():
        assert all(rep["shared_generation"] for rep in
                   result.details["repetitions"])
        assert all(rep["output"] == text for rep in
                   result.details["repetitions"])
    # The shared output diverges the scores: at most one side of the pair
    # can pass any single baseline answer.
    assert concise_result.score == 1.0
    assert teach_result.score < 0.7

    # With the calibration block on, generation is independent again.
    calls["n"] = 0
    pg.run_profile_guidance_suite(
        [concise.uuid, teach.uuid], variant="calibration_only", repetitions=2)
    assert calls["n"] == 4             # two cases × two repetitions


def test_pair_invariants_fail_closed(divergence_pair, monkeypatch):
    """A solo pair member — or a pair whose prompts diverge with its block
    off — must persist explicit invalid results, never quietly regenerate
    the stochastic-baseline condition sharing exists to prevent."""
    fake, _ = _stub("whatever")
    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
    concise, teach = divergence_pair

    # Solo member under baseline: invalid, zero, no generation.
    run = pg.run_profile_guidance_suite([teach.uuid], variant="baseline",
                                        repetitions=2)
    result = db.list_eval_results_for_run(run.uuid)[0]
    assert result.score == 0.0 and not result.passed
    rep = result.details["repetitions"][0]
    assert rep["invalid"] is True
    assert "exactly two cases" in rep["error"]

    # Broken pair definition (diverged prompts with the block off): both
    # members invalid. Divergence is simulated by giving one member a
    # different profile field.
    teach.input = {**teach.input,
                   "profile": {**teach.input["profile"],
                               "data": {**teach.input["profile"]["data"],
                                        "units": "imperial"}}}
    db.db.session.commit()
    run = pg.run_profile_guidance_suite([concise.uuid, teach.uuid],
                                        variant="baseline", repetitions=2)
    results = db.list_eval_results_for_run(run.uuid)
    assert len(results) == 2
    for result in results:
        assert result.score == 0.0
        assert "diverged" in result.details["repetitions"][0]["error"]

    # Cardinality holds under EVERY variant: a mis-selected solo member is a
    # broken run definition even when the differentiating block is on.
    run = pg.run_profile_guidance_suite([concise.uuid],
                                        variant="calibration_only",
                                        repetitions=1)
    result = db.list_eval_results_for_run(run.uuid)[0]
    rep = result.details["repetitions"][0]
    assert rep["invalid"] is True and "exactly two cases" in rep["error"]


def test_malformed_pair_metadata_fails_closed(divergence_pair, monkeypatch):
    """Unknown pair_block values and members that disagree about their block
    are broken definitions: every member is invalidated before generation,
    under every variant — never silently run independently."""
    fake, _ = _stub("whatever")
    monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
    concise, teach = divergence_pair

    # Unknown / misspelled pair_block.
    teach.rubric = {**teach.rubric, "pair_block": "calibraton"}
    concise.rubric = {**concise.rubric, "pair_block": "calibraton"}
    db.db.session.commit()
    run = pg.run_profile_guidance_suite([concise.uuid, teach.uuid],
                                        variant="calibration_only",
                                        repetitions=1)
    results = db.list_eval_results_for_run(run.uuid)
    assert len(results) == 2
    for result in results:
        rep = result.details["repetitions"][0]
        assert rep["invalid"] is True and "unknown pair_block" in rep["error"]
    assert (db.get_eval_run(run.uuid).summary or {}).get("invalid")

    # Members disagreeing about the block.
    teach.rubric = {**teach.rubric, "pair_block": "calibration"}
    db.db.session.commit()
    run = pg.run_profile_guidance_suite([concise.uuid, teach.uuid],
                                        variant="baseline", repetitions=1)
    for result in db.list_eval_results_for_run(run.uuid):
        rep = result.details["repetitions"][0]
        assert rep["invalid"] is True and "disagree" in rep["error"]


def test_date_format_pair_is_single_field_and_runs_independently(app_ctx, monkeypatch):
    """The date-format counterfactual is a genuine single-field pair —
    identical profiles (uuid and visible name included) except date_format —
    but NOT a shared-generation pair: the differing field rides the
    always-on identity block, so its prompts can never be byte-equal. It
    runs independently in every variant, never invalid, with opposing
    expectations deciding each side."""
    pg.seed_profile_guidance_cases()
    cases = sorted((c for c in db.list_eval_cases(case_type="chat_reply")
                    if (c.rubric or {}).get("pair") == "date_format_counterfactual"),
                   key=lambda c: c.name)
    try:
        assert len(cases) == 2
        a, b = cases
        assert all("pair_block" not in (c.rubric or {}) for c in cases)
        pa, pb = a.input["profile"], b.input["profile"]
        assert pa["uuid"] == pb["uuid"] and pa["name"] == pb["name"]
        assert {k: v for k, v in pa["data"].items() if k != "date_format"} == \
               {k: v for k, v in pb["data"].items() if k != "date_format"}
        assert pa["data"]["date_format"] != pb["data"]["date_format"]

        fake, _ = _stub("The date is 31.12.2026.")
        monkeypatch.setattr(pg.AssistantAgent, "_structured_completion", fake)
        for variant in pg.VARIANTS:
            run = pg.run_profile_guidance_suite(
                [a.uuid, b.uuid], variant=variant, repetitions=1)
            results = {r.eval_case_uuid: r for r in
                       db.list_eval_results_for_run(run.uuid)}
            for result in results.values():
                rep = result.details["repetitions"][0]
                assert not rep.get("shared_generation")
                assert not rep.get("invalid")
            # One fixed answer satisfies exactly one side of the pair.
            assert results[a.uuid].score == 1.0    # expects 31.12.2026
            assert results[b.uuid].score < 0.7     # expects 12/31/2026
    finally:
        db.db.session.rollback()
        for run in db.db.session.query(db.EvalRun).all():
            if any(str(c.uuid) in (run.config or {}).get("case_uuids", [])
                   for c in cases):
                db.db.session.delete(run)
        for c in db.list_eval_cases(case_type="chat_reply"):
            if c.name.startswith("pg "):
                db.db.session.delete(c)
        db.db.session.commit()


def test_duplicate_arbitration_never_prefers_archived(app_ctx):
    """When two rows claim one seed id, a live row must win even against an
    archived row that carries a newer revision — an archived claimant must
    never dethrone the active case."""
    sid = "locale.date_order.de"
    archived = db.create_eval_case(
        name="pg locale: German date order", case_type="chat_reply",
        status="archived", input={"message": "old"}, expected={},
        rubric={"seed": "profile_guidance", "seed_id": sid,
                "seed_rev": pg.SEED_REV, "family": "locale"})
    live = db.create_eval_case(
        name="pg locale: German date order", case_type="chat_reply",
        status="active", input={"message": "current"}, expected={},
        rubric={"seed": "profile_guidance", "seed_id": sid,
                "seed_rev": 1, "family": "locale"})
    try:
        pg.seed_profile_guidance_cases()
        assert db.get_eval_case(live.uuid).status == "active"     # kept
        assert db.get_eval_case(live.uuid).rubric["seed_rev"] == pg.SEED_REV
        assert db.get_eval_case(archived.uuid).status == "archived"
    finally:
        db.db.session.rollback()
        for c in db.list_eval_cases(case_type="chat_reply"):
            if c.name.startswith("pg "):
                db.db.session.delete(c)
        db.db.session.commit()


def test_unknown_variant_rejected(app_ctx):
    with pytest.raises(ValueError, match="unknown variant"):
        pg.run_profile_guidance_suite([], variant="everything")
