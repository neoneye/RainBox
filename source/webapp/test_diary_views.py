"""The /diary/passage page: a diary citation shown in its file, the cited
bytes highlighted (webapp/diary_views.py)."""

import re

import db
from diary.test_retrieval import app_ctx, citations, live, world  # noqa: F401 — fixtures
from webapp.core import app
from webapp.diary_views import diary_passage


def _page(link: str, **params) -> str:
    resp = app.test_client().get("/diary/passage", query_string={"citation": link, **params})
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_a_citation_is_shown_highlighted_in_its_file(live):
    cite = citations(live.q(mode="literal", query="Slept badly"))[0]
    view = diary_passage(cite)
    assert view is not None and view.current and not view.whole_revision
    assert "Slept badly" in view.cited
    assert view.path == "current/1999.txt" and view.source_name == live.src.name
    assert view.entry_date is not None
    body = _page(cite)
    (marked,) = re.findall(r'<mark id="cited">(.*?)</mark>', body, re.S)
    assert "Slept badly" in marked
    assert "current/1999.txt" in body and view.entry_date.isoformat() in body
    assert "scrollIntoView" in body


def test_a_long_file_shows_context_and_offers_the_whole(live):
    cite = citations(live.q(mode="literal", query="Slept badly"))[0]
    short = diary_passage(cite, context_lines=1)
    assert short.lines_hidden_before or short.lines_hidden_after
    whole = diary_passage(cite, whole=True)
    assert not whole.lines_hidden_before and not whole.lines_hidden_after
    raw = (live.root / "current" / "1999.txt").read_bytes().decode()
    assert whole.before + whole.cited + whole.after == raw


def test_a_bare_revision_link_shows_the_file_without_a_highlight(live):
    cite = citations(live.q(mode="literal", query="Slept badly"))[0]
    bare = cite.rsplit(":", 1)[0]
    view = diary_passage(bare)
    assert view is not None and view.whole_revision and view.cited == ""
    body = _page(bare)
    assert "<mark" not in body and "names the whole file revision" in body


def test_a_changed_file_says_its_text_is_gone(live):
    cite = citations(live.q(mode="literal", query="Slept badly"))[0]
    path = live.root / "current" / "1999.txt"
    path.write_bytes(path.read_bytes().replace(b"Slept badly", b"Slept well"))
    live.resync()
    view = diary_passage(cite)
    assert view is not None and not view.current and "Slept badly" in view.cited


def test_unknown_malformed_and_excluded_read_as_not_found(live):
    cite = citations(live.q(mode="literal", query="Slept badly"))[0]
    assert diary_passage("diary:00000000-0000-4000-8000-000000000000:1-5") is None
    assert diary_passage("not a citation") is None
    assert "No diary passage at" in _page("not a citation")
    db.diary_exclude(live.src.uuid, "current/1999.txt")
    assert diary_passage(cite) is None
