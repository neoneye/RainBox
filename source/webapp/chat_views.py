from flask import make_response, render_template_string

from user_profile.identity import current_profile
from user_profile.languages import declared_language_candidates

from .chat_template import CHAT_TEMPLATE
from .core import app
from .stt_gemma4_views import TO_WAV_JS
from .voice_echo_views import _stt_engines


def _dictate_languages() -> list[str]:
    """The language tags the dictate button's menu offers besides "Auto":
    the ones the current profile declares, in its order."""
    return [row["code"] for row in declared_language_candidates(current_profile())]


@app.route("/chat")
def chat_page():
    # The page is a single inline-JS document we iterate on often; tell the
    # browser not to cache it so frontend changes show up on a normal reload.
    resp = make_response(render_template_string(
        CHAT_TEMPLATE, stt_engines=_stt_engines(),
        dictate_languages=_dictate_languages(), to_wav_js=TO_WAV_JS))
    resp.headers["Cache-Control"] = "no-store"
    return resp
