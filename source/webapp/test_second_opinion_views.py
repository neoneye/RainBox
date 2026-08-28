"""The /second-opinion overview — the gate's judgments as a working surface.

Answers the two questions the review records exist for: why did a run go wrong
(filter by verdict, follow the link into the trace), and why was one right for
the wrong reasons (approved + identity_mismatch). The operator's assessment of
each review is written from here.
"""

from uuid import uuid4

import pytest

import db
import webapp  # noqa: F401 — registers the views
from db import AssistantRun
from webapp.core import app as flask_app


@pytest.fixture
def app_ctx():
    application = db.make_app()
    db.init_db(application)
    ctx = application.app_context()
    ctx.push()
    try:
        yield application
    finally:
        db.session.rollback()
        ctx.pop()


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


@pytest.fixture
def run(app_ctx):
    r = db.start_assistant_run(
        journal_id=uuid4(), room_uuid=uuid4(), agent_uuid=uuid4())
    try:
        yield r
    finally:
        db.session.rollback()
        db.session.query(AssistantRun).filter(
            AssistantRun.uuid == r.uuid).delete()
        db.session.commit()


def _review(run, **over):
    kwargs = dict(
        run_uuid=run.uuid, step_uuid=None, step_index=0,
        action="python_run", verdict="rejected",
    )
    kwargs.update(over)
    return db.record_second_opinion_review(**kwargs)


def test_the_page_shows_a_review_and_links_into_its_trace(run, client):
    _review(run, verdict="rejected", problems=[
        {"category": "identity_mismatch",
         "text": "the operator profile is metric"}])
    body = client.get("/second-opinion").get_data(as_text=True)
    assert "rejected" in body
    assert "identity_mismatch" in body
    assert "the operator profile is metric" in body
    assert f"/assistant?id={run.uuid}" in body


def test_the_verdict_filter_narrows_the_list(run, client):
    _review(run, verdict="approved")
    _review(run, verdict="rejected", problems=[
        {"category": "logic_error", "text": "wrong constant"}])
    body = client.get("/second-opinion?verdict=approved").get_data(as_text=True)
    assert "wrong constant" not in body


def test_a_skipped_review_is_visible_as_its_own_verdict(run, client):
    """The fail-open cases are the reason the verdict is four-valued; the
    overview has to be able to show 'the gate did not run'."""
    _review(run, verdict="skipped", skip_reason="no_model_group")
    body = client.get("/second-opinion?verdict=skipped").get_data(as_text=True)
    assert "no_model_group" in body


def test_the_right_answer_wrong_reasons_view(run, client):
    """Approved, yet the reviewer flagged the ground the gate exists for."""
    _review(run, verdict="approved", problems=[
        {"category": "identity_mismatch", "text": "assumes US units"}])
    _review(run, verdict="approved", problems=[
        {"category": "logic_error", "text": "wrong constant"}])
    body = client.get(
        "/second-opinion?verdict=approved&category=identity_mismatch"
    ).get_data(as_text=True)
    assert "assumes US units" in body
    assert "wrong constant" not in body


def test_unassessed_filter_supports_working_a_backlog(run, client):
    done = _review(run, verdict="rejected", problems=[
        {"category": "logic_error", "text": "already judged"}])
    _review(run, verdict="rejected", problems=[
        {"category": "logic_error", "text": "still to judge"}])
    db.record_second_opinion_assessment(done.uuid, "agree")
    body = client.get("/second-opinion?assessed=no").get_data(as_text=True)
    assert "still to judge" in body
    assert "already judged" not in body


def test_recording_an_assessment_and_returning_to_the_same_filters(run, client):
    review = _review(run, verdict="approved", problems=[
        {"category": "identity_mismatch", "text": "assumes US units"}])
    resp = client.post(
        "/second-opinion/assess?verdict=approved&category=identity_mismatch",
        data={"review_uuid": str(review.uuid), "assessment": "under_blocked",
              "note": "right number, wrong reasoning"})
    assert resp.status_code == 302
    assert "verdict=approved" in resp.headers["Location"]
    assert "category=identity_mismatch" in resp.headers["Location"]
    got = db.get_second_opinion_assessment(review.uuid)
    assert got.assessment == "under_blocked"
    assert got.note == "right number, wrong reasoning"


def test_an_existing_assessment_is_shown_on_its_row(run, client):
    review = _review(run, verdict="rejected")
    db.record_second_opinion_assessment(
        review.uuid, "over_blocked", note="the bar was too strict here")
    body = client.get("/second-opinion").get_data(as_text=True)
    assert "over_blocked" in body
    assert "the bar was too strict here" in body


def test_an_unknown_assessment_value_is_refused(run, client):
    review = _review(run)
    resp = client.post("/second-opinion/assess", data={
        "review_uuid": str(review.uuid), "assessment": "meh"})
    assert resp.status_code == 400
    assert db.get_second_opinion_assessment(review.uuid) is None


def test_assessing_an_unknown_review_is_refused(run, client):
    resp = client.post("/second-opinion/assess", data={
        "review_uuid": str(uuid4()), "assessment": "agree"})
    assert resp.status_code == 404


def test_bad_filter_values_are_refused(run, client):
    assert client.get("/second-opinion?verdict=nope").status_code == 400
    assert client.get("/second-opinion?category=nope").status_code == 400
    assert client.get("/second-opinion?assessed=nope").status_code == 400


def test_the_empty_state_explains_itself(run, client):
    body = client.get("/second-opinion?verdict=error").get_data(as_text=True)
    assert "No reviews match" in body


def test_the_nav_reaches_the_page(run, client):
    body = client.get("/second-opinion").get_data(as_text=True)
    assert "/second-opinion" in body
    assert "Second opinion" in body
