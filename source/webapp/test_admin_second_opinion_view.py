"""The second-opinion admin lists keep uuid columns narrow.

A raw uuid cell is 36 characters; several of them per row push the columns
that carry the actual finding (verdict, categories, problems) off screen. The
/admin/assistantstep list solves this with `_fmt_short_uuid` — first 6 chars,
full value on hover — and these views follow it.
"""

import db
import webapp  # noqa: F401 — registers all admin views
from webapp.core import admin, app


def _view(model):
    return next(v for v in admin._views if getattr(v, "model", None) is model)


def _rendered_columns(view, path):
    with app.test_request_context(path):
        return [name for name, _label in view.get_list_columns()]


def _uuid_columns(names):
    """Columns whose values are uuids — by name, since that is what the view
    config keys on."""
    return [n for n in names if n.endswith("uuid") or n.endswith("_id")]


def test_every_uuid_column_in_the_review_list_is_formatted():
    """A general invariant rather than a fixed list: a uuid column added later
    cannot silently render full-width."""
    view = _view(db.SecondOpinionReview)
    rendered = _rendered_columns(view, "/admin/secondopinionreview/")
    unformatted = [
        c for c in _uuid_columns(rendered) if c not in (view.column_formatters or {})
    ]
    assert not unformatted, f"unformatted uuid columns: {unformatted}"


def test_every_uuid_column_in_the_assessment_list_is_formatted():
    view = _view(db.SecondOpinionAssessment)
    rendered = _rendered_columns(view, "/admin/secondopinionassessment/")
    unformatted = [
        c for c in _uuid_columns(rendered) if c not in (view.column_formatters or {})
    ]
    assert not unformatted, f"unformatted uuid columns: {unformatted}"


def test_the_review_uuid_cell_links_to_the_run_it_gated():
    """Same affordance as the step list's uuid cell: the identifier doubles as
    the way into the trace it belongs to."""
    from uuid import uuid4
    view = _view(db.SecondOpinionReview)
    review = db.SecondOpinionReview(
        uuid=uuid4(), run_uuid=uuid4(), step_uuid=uuid4(), step_index=0,
        action="python_run", verdict="approved")
    cell = str(view.column_formatters["uuid"](view, None, review, "uuid"))
    assert f"/assistant?id={review.run_uuid}" in cell
    assert f"#step-{review.step_uuid}" in cell
    assert str(review.uuid)[:6] in cell
    assert str(review.uuid) not in cell.split("title=")[0]   # short in the body


def test_problems_render_as_readable_lines_not_a_dict_repr():
    """The findings column is the point of the list; a raw {'text': …,
    'category': …} repr is both unreadable and wider than the sentence."""
    from uuid import uuid4
    view = _view(db.SecondOpinionReview)
    review = db.SecondOpinionReview(
        uuid=uuid4(), run_uuid=uuid4(), step_index=0, action="python_run",
        verdict="rejected",
        problems=[{"category": "identity_mismatch", "text": "assumes US units"},
                  {"category": "logic_error", "text": "wrong constant"}])
    cell = str(view.column_formatters["problems"](view, None, review, "problems"))
    assert "assumes US units" in cell
    assert "wrong constant" in cell
    assert "{" not in cell and "'text'" not in cell
    # The category stays as plain text — <code> is the uuid columns' styling and
    # does not belong on a sentence.
    assert "identity_mismatch" in cell
    assert "<code>" not in cell


def test_an_empty_problems_cell_is_blank():
    from uuid import uuid4
    view = _view(db.SecondOpinionReview)
    review = db.SecondOpinionReview(
        uuid=uuid4(), run_uuid=uuid4(), step_index=0, action="python_run",
        verdict="approved", problems=[])
    assert str(
        view.column_formatters["problems"](view, None, review, "problems")) == ""


def test_a_review_without_a_step_still_links_to_the_run():
    """step_uuid is nullable — a worker that dies between review and dispatch
    leaves a review with no step row, and its cell must not break."""
    from uuid import uuid4
    view = _view(db.SecondOpinionReview)
    review = db.SecondOpinionReview(
        uuid=uuid4(), run_uuid=uuid4(), step_uuid=None, step_index=0,
        action="python_run", verdict="approved")
    cell = str(view.column_formatters["uuid"](view, None, review, "uuid"))
    assert f"/assistant?id={review.run_uuid}" in cell
    assert "#step-None" not in cell
