from flask import Response, abort, make_response, render_template_string, url_for

from user_profile.identity import current_profile
from user_profile.languages import declared_language_candidates

from . import stt_gemma4_views, stt_lfm3_views, stt_whisper_views
from .chat_template import CHAT_TEMPLATE
from .core import app
from .stt_gemma4_views import TO_WAV_JS
from .voice_echo_views import _stt_engines

# What a dictated clip is recorded as on /activity, whichever engine heard it.
DICTATE_CALLER = "chat.dictate"


def _dictate_languages() -> list[str]:
    """The language tags the dictate button's menu offers besides "Auto":
    the ones the current profile declares, in its order."""
    return [row["code"] for row in declared_language_candidates(current_profile())]


def _dictate_engines() -> list[dict]:
    """The speech-to-text engines as the dictate button uses them: the echo
    page's table, with the clip posted to this page's own route so the call
    is recorded as dictation and not as a demo page's."""
    return [{**engine, "transcribe": url_for("chat_dictate", engine=engine["id"])}
            for engine in _stt_engines()]


@app.route("/chat/api/dictate/<engine>", methods=["POST"])
def chat_dictate(engine: str) -> Response | tuple[Response, int]:
    """Transcribe a clip from the composer's dictate button with the named
    engine: multipart `audio`, optional `language` tag -> {"text"} or
    {"error"}. The engines' own modules do the work."""
    if engine == "gemma4":
        return stt_gemma4_views.transcribe(DICTATE_CALLER)
    if engine == "lfm3":
        return stt_lfm3_views.transcribe(DICTATE_CALLER)
    if engine == "whisper":
        return stt_whisper_views.transcribe()
    abort(404)


@app.route("/chat")
def chat_page():
    # The page is a single inline-JS document we iterate on often; tell the
    # browser not to cache it so frontend changes show up on a normal reload.
    resp = make_response(render_template_string(
        CHAT_TEMPLATE, stt_engines=_dictate_engines(),
        dictate_languages=_dictate_languages(), to_wav_js=TO_WAV_JS))
    resp.headers["Cache-Control"] = "no-store"
    return resp
