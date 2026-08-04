"""Serve the web app alone for UI verification. Run: `python -m tools.serve_ui`.

For looking at a page in a browser — layout, drag-drop, a flow end to end —
without the agent supervisor and without going near real data. Two deliberate
differences from `main.py`:

- **Port 5055, not 5000**, so this never collides with the operator's own
  instance and neither process has to be stopped to run the other.
- **`rainbox_claude`, not `rainbox_production`** (the CLAUDE.md rule for
  ad-hoc work). Rows created while clicking around are throwaway by
  construction, so a verification pass can't touch the operator's data.

Only the Flask app runs: no supervisor, no agents, no cron firing. Pages that
need a live agent turn (a chat reply, an assistant run) will not complete
here — use `main.py` for those.

`RAINBOX_UI_PORT` and `RAINBOX_UI_DATABASE_URL` override the two defaults.
"""

import os

PORT = int(os.environ.get("RAINBOX_UI_PORT", "5055"))
DATABASE_URL = os.environ.get(
    "RAINBOX_UI_DATABASE_URL", "postgresql+psycopg://localhost/rainbox_claude"
)

# Must precede the webapp import: db builds its engine from DATABASE_URL at
# import time, so setting this afterwards would bind the wrong database.
os.environ["DATABASE_URL"] = DATABASE_URL

from webapp import app  # noqa: E402


def main() -> None:
    print(f"UI-only server on http://127.0.0.1:{PORT}  (database: {DATABASE_URL})")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
