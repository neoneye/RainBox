"""Tests for second_opinion_review / second_opinion_assessment persistence:
the pre-execution review gate's judgment as a first-class row, and the
operator's later assessment of that judgment.

See notes/proposals/2026-07-28-second-opinion-review-records.md.
"""

from uuid import uuid4

import pytest
import sqlalchemy as sa

import db
from db import AssistantRun


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
def run(app_ctx):
    r = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4(), step_limit=6
    )
    try:
        yield r
    finally:
        db.db.session.rollback()
        db.db.session.query(AssistantRun).filter(AssistantRun.uuid == r.uuid).delete()
        db.db.session.commit()


def _record(run, **over):
    kwargs = dict(
        run_uuid=run.uuid, step_uuid=None, step_index=0,
        journal_id=run.journal_id, room_uuid=run.room_uuid,
        agent_uuid=run.agent_uuid, action="python_run", verdict="approved",
    )
    kwargs.update(over)
    return db.record_second_opinion_review(**kwargs)


def test_records_an_approved_review_with_its_model_call(run):
    review = _record(
        run, model_uuid=run.agent_uuid, group_from="second_opinion",
        system_prompt="sys", user_prompt="usr", reasoning="thinking",
        response='{"approved": true}',
        input_tokens=120, output_tokens=15, duration_ms=2000,
    )
    assert review.verdict == "approved"
    assert review.action == "python_run"
    assert review.group_from == "second_opinion"
    # The review is a full LLM interaction, recorded like the decide call's.
    assert review.system_prompt == "sys" and review.user_prompt == "usr"
    assert review.reasoning == "thinking"
    assert (review.input_tokens, review.output_tokens) == (120, 15)
    assert review.duration_ms == 2000


def test_categories_are_derived_from_the_problems_not_passed_in(run):
    review = _record(run, problems=[
        {"category": "identity_mismatch", "text": "assumes US units"},
        {"category": "logic_error", "text": "wrong constant"},
        {"category": "identity_mismatch", "text": "assumes AM/PM"},
    ])
    # Distinct, order-independent — a denormalized column purely for indexing.
    assert sorted(review.categories) == ["identity_mismatch", "logic_error"]
    assert len(review.problems) == 3


def test_a_clean_approval_has_no_problems_and_no_categories(run):
    review = _record(run)
    assert review.problems == [] and review.categories == []


def test_fail_open_cases_are_their_own_verdicts_not_approvals(run):
    """The whole point of the four-value verdict: a review that never ran must
    not read as one that approved."""
    skipped = _record(run, verdict="skipped", skip_reason="no_model_group")
    errored = _record(run, verdict="error", error="TimeoutError: boom")
    assert skipped.verdict == "skipped" and skipped.skip_reason == "no_model_group"
    assert errored.verdict == "error" and "TimeoutError" in errored.error


def test_unknown_verdict_is_rejected_by_the_database(run):
    with pytest.raises(sa.exc.IntegrityError):
        _record(run, verdict="probably_fine")
    db.db.session.rollback()


def test_reviews_for_a_run_come_back_in_attempt_order(run):
    """Retries reuse the same step_index, so the attempt chain is derived from
    creation order rather than a stored counter."""
    first = _record(run, verdict="rejected", step_index=0)
    second = _record(run, verdict="rejected", step_index=0)
    third = _record(run, verdict="approved", step_index=0)
    later = _record(run, verdict="approved", step_index=1)
    got = db.list_second_opinion_reviews(run.uuid)
    assert [r.uuid for r in got] == [first.uuid, second.uuid, third.uuid, later.uuid]
    assert [r.verdict for r in got[:3]] == ["rejected", "rejected", "approved"]


def test_reviews_are_scoped_to_their_run(run):
    _record(run)
    assert db.list_second_opinion_reviews(uuid4()) == []


def test_assessment_attaches_to_a_review(run):
    review = _record(run, verdict="approved", problems=[
        {"category": "identity_mismatch", "text": "assumes US units"}])
    db.record_second_opinion_assessment(
        review.uuid, "under_blocked", note="right number, wrong reasoning")
    got = db.get_second_opinion_assessment(review.uuid)
    assert got is not None
    assert got.assessment == "under_blocked"
    assert got.note == "right number, wrong reasoning"


def test_newest_assessment_wins_and_the_older_one_survives(run):
    """Append-only: changing your mind adds a row, it does not edit history."""
    review = _record(run, verdict="rejected")
    db.record_second_opinion_assessment(review.uuid, "agree", note="fair")
    db.record_second_opinion_assessment(review.uuid, "over_blocked", note="too strict")
    assert db.get_second_opinion_assessment(review.uuid).assessment == "over_blocked"
    assert len(db.list_second_opinion_assessments(review.uuid)) == 2


def test_a_review_without_an_assessment_returns_none(run):
    assert db.get_second_opinion_assessment(_record(run).uuid) is None


# --- the overview query -------------------------------------------------------


def test_page_filters_by_verdict_and_reports_counts_for_the_others(run):
    _record(run, verdict="approved")
    _record(run, verdict="rejected")
    _record(run, verdict="rejected")
    _record(run, verdict="skipped", skip_reason="no_model_group")
    rows, total, counts = db.list_second_opinion_reviews_page(
        verdict="rejected", run_uuid=run.uuid)
    assert total == 2
    assert [r.verdict for r in rows] == ["rejected", "rejected"]
    # The chips show what each *other* verdict would give, so counts ignore the
    # verdict filter itself.
    assert counts["approved"] == 1 and counts["rejected"] == 2
    assert counts["skipped"] == 1 and counts["all"] == 4


def test_page_filters_by_category(run):
    _record(run, verdict="rejected", problems=[
        {"category": "identity_mismatch", "text": "metric"}])
    _record(run, verdict="rejected", problems=[
        {"category": "logic_error", "text": "constant"}])
    _record(run, verdict="approved")
    rows, total, _counts = db.list_second_opinion_reviews_page(
        category="identity_mismatch", run_uuid=run.uuid)
    assert total == 1
    assert rows[0].categories == ["identity_mismatch"]


def test_the_right_answer_wrong_reasons_query_is_one_call(run):
    """Approved, yet the reviewer flagged the ground the gate exists for."""
    _record(run, verdict="approved", problems=[
        {"category": "identity_mismatch", "text": "assumes US units"}])
    _record(run, verdict="approved")
    _record(run, verdict="rejected", problems=[
        {"category": "identity_mismatch", "text": "metric"}])
    rows, total, _counts = db.list_second_opinion_reviews_page(
        verdict="approved", category="identity_mismatch", run_uuid=run.uuid)
    assert total == 1
    assert rows[0].verdict == "approved"


def test_page_filters_by_whether_it_has_been_assessed(run):
    a = _record(run, verdict="rejected")
    _record(run, verdict="rejected")
    db.record_second_opinion_assessment(a.uuid, "over_blocked", note="too strict")
    unassessed, total_un, _ = db.list_second_opinion_reviews_page(
        assessed="no", run_uuid=run.uuid)
    assessed, total_as, _ = db.list_second_opinion_reviews_page(
        assessed="yes", run_uuid=run.uuid)
    assert total_un == 1 and unassessed[0].uuid != a.uuid
    assert total_as == 1 and assessed[0].uuid == a.uuid


def test_page_is_newest_first_and_paginated(run):
    made = [_record(run, verdict="approved") for _ in range(5)]
    rows, total, _counts = db.list_second_opinion_reviews_page(
        run_uuid=run.uuid, offset=0, limit=2)
    assert total == 5
    assert [r.uuid for r in rows] == [made[4].uuid, made[3].uuid]
    rows2, _t, _c = db.list_second_opinion_reviews_page(
        run_uuid=run.uuid, offset=4, limit=2)
    assert [r.uuid for r in rows2] == [made[0].uuid]


def test_assessments_for_many_reviews_come_back_in_one_lookup(run):
    a = _record(run, verdict="rejected")
    b = _record(run, verdict="approved")
    c = _record(run, verdict="approved")
    db.record_second_opinion_assessment(a.uuid, "agree")
    db.record_second_opinion_assessment(b.uuid, "unsure")
    db.record_second_opinion_assessment(b.uuid, "under_blocked", note="missed it")
    got = db.second_opinion_assessments_for([a.uuid, b.uuid, c.uuid])
    assert got[a.uuid].assessment == "agree"
    assert got[b.uuid].assessment == "under_blocked"   # newest wins
    assert c.uuid not in got


def test_unknown_assessment_is_rejected_by_the_database(run):
    review = _record(run)
    with pytest.raises(sa.exc.IntegrityError):
        db.record_second_opinion_assessment(review.uuid, "meh")
    db.db.session.rollback()
