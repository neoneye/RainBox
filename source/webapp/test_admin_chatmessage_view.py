"""The ChatMessage admin list surfaces the message uuid (full + copyable) so an
operator can grab a precise reference to a problematic message."""

from uuid import UUID

import pytest

import db
import webapp  # noqa: F401 — registers admin views on the app
from webapp.core import _fmt_copyable_uuid, app as flask_app


@pytest.fixture
def app_ctx():
    application = db.make_app()
    db.init_db(application)
    ctx = application.app_context()
    ctx.push()
    try:
        yield application
    finally:
        db.db.session.rollback()
        ctx.pop()


class _Row:
    def __init__(self, value):
        self.uuid = value


def test_copyable_uuid_formatter_renders_full_value_and_copy_button():
    u = UUID("795ea3ee-9426-4e03-973a-5d6f6c814b46")
    out = str(_fmt_copyable_uuid(None, None, _Row(u), "uuid"))
    assert "795ea3ee-9426-4e03-973a-5d6f6c814b46" in out  # full, not truncated
    assert "<code>" in out
    assert "Copy uuid" in out
    assert "clipboard.writeText" in out


def test_copyable_uuid_formatter_handles_missing_value():
    assert _fmt_copyable_uuid(None, None, _Row(None), "uuid") == ""


def test_chatmessage_admin_list_has_uuid_column(app_ctx):
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = flask_app.test_client()
    resp = client.get("/admin/chatmessage/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "UUID" in body  # the column header is registered
