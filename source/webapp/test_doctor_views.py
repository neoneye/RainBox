"""The /doctor page renders the health checks + a copy-paste report."""

import pytest

import db
import webapp  # noqa: F401 — registers all views (incl. doctor) on the app
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


def test_doctor_page_renders_checks_and_copy_block(app_ctx, client):
    resp = client.get("/doctor")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Doctor" in body
    # every probe name shows up
    for name in ("capabilities", "model_groups", "embedder", "skills", "mcp"):
        assert name in body
    # the copy-paste plain-text block is present
    assert 'id="pp-doctor-report"' in body
    assert "rainbox doctor" in body


def test_doctor_nav_link_present(app_ctx, client):
    """The nav (shared _nav.html) carries a Doctor link to /doctor."""
    body = client.get("/doctor").get_data(as_text=True)
    assert 'href="/doctor"' in body and ">Doctor<" in body


def test_the_copy_button_reports_what_the_browser_did(app_ctx, client):
    """The doctor report is pasted into bug threads. A button that says
    "Copied" over a clipboard that was never written puts the *previous*
    report in that thread as if it were this one — so the label is read from
    the browser's `copy` event, which fires only on a real copy, and not from
    document.execCommand's return value (an extension can proxy the call,
    return true, and copy nothing)."""
    body = client.get("/doctor").get_data(as_text=True)
    assert "document.addEventListener('copy', witness, true)" in body
    assert "return claimed ? 'blocked' : 'unavailable';" in body
    assert "'Copy failed'" in body
    # Verified path first: writeText resolves identically whether the write
    # landed or was intercepted, so it can confirm nothing.
    assert (body.index("var status = ppVerifiedCopy(t);")
            < body.index("navigator.clipboard.writeText(t)"))
