from flask import make_response, render_template_string

from .chat_template import CHAT_TEMPLATE
from .core import app


@app.route("/chat")
def chat_page():
    # The page is a single inline-JS document we iterate on often; tell the
    # browser not to cache it so frontend changes show up on a normal reload.
    resp = make_response(render_template_string(CHAT_TEMPLATE))
    resp.headers["Cache-Control"] = "no-store"
    return resp
