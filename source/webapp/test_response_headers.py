"""Every response carries exactly one `Date` header.

Flask stamps one on static-file responses and the WSGI server stamps its own,
so static assets went out with two — which RFC 9110 forbids. Chrome tolerates
it; Firefox stalls the request, so a page whose body is rendered by an external
script never finishes loading.

These run against a real WSGI server rather than the Flask test client: the
duplicate is created by the serving layer, and the test client never sees it.
"""
import threading
import urllib.request

import pytest
from werkzeug.serving import make_server

from webapp import app


@pytest.fixture(scope="module")
def server():
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _date_headers(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return [v for k, v in r.headers.items() if k.lower() == "date"]


@pytest.mark.parametrize("path", ["/static/git.js", "/static/ui-modal.css"])
def test_static_assets_carry_exactly_one_date_header(server, path):
    # The regression: two Date headers here blanked /git in Firefox.
    assert len(_date_headers(server + path)) == 1


def test_html_page_carries_exactly_one_date_header(server):
    assert len(_date_headers(server + "/git")) == 1
