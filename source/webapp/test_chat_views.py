"""Marker tests for the /chat page shell (webapp/chat_template.py). The page's
JS is inline in the rendered template, so a GET of /chat carries both markup and
behavior — a single body assertion covers either side (same idea as
test_cron_views / test_kanban_views)."""

from webapp.core import app  # noqa: F401  ensure routes register
import webapp  # noqa: F401  registers chat_views on the shared app


def _body() -> str:
    return app.test_client().get("/chat").get_data(as_text=True)


def test_chat_page_renders_with_nav():
    resp = app.test_client().get("/chat")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "pp-nav" in body
    assert "function buildRoomMenu" in body
    assert "function buildFolderMenu" in body


def test_room_and_folder_kebabs_have_copy_id():
    body = _body()
    assert "Copy room id" in body
    assert "Copy folder id" in body
    # both copy via the shared copyText helper (which flashes "Copied")
    assert "copyText(roomUuid, item)" in body
    assert "copyText(folderId, item)" in body
