"""rainbox web application.

`core` builds the Flask `app` and the Flask-Admin views. The view modules
register their routes against that shared `app` on import, so importing them
here (after core) is what wires up the URL map. `app` is re-exported so
`from webapp import app` keeps working for main.py.
"""

from .core import app  # noqa: F401  (creates the app + admin)

# Importing these registers their @app.route handlers. Order is irrelevant
# between them; each only depends on .core, which is already imported above.
from . import pages  # noqa: F401,E402
from . import models_views  # noqa: F401,E402
from . import model_group_views  # noqa: F401,E402
from . import benchmark_views  # noqa: F401,E402
from . import benchmark_editdocument_views  # noqa: F401,E402
from . import benchmark_kanban_views  # noqa: F401,E402
from . import agent_views  # noqa: F401,E402
from . import chat_views  # noqa: F401,E402
from . import chat_api  # noqa: F401,E402
from . import conversation_api  # noqa: F401,E402
from . import conversation_views  # noqa: F401,E402
from . import tts_kokoro_views  # noqa: F401,E402
from . import stt_whisper_views  # noqa: F401,E402
from . import voice_echo_views  # noqa: F401,E402
from . import cron_views  # noqa: F401,E402
from . import cron_api  # noqa: F401,E402
from . import kanban_views  # noqa: F401,E402
from . import kanban_api  # noqa: F401,E402
from . import git_api  # noqa: F401,E402
from . import settings_views  # noqa: F401,E402

__all__ = ["app"]
