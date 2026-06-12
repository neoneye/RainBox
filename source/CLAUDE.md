# rainbox — agent instructions

## Databases: never touch production by hand

There are two Postgres databases:

- **`rainbox_production`** — the operator's REAL data (app default,
  `DEFAULT_DATABASE_URL`). Treat it as sacred: never run destructive or
  experimental SQL against it, and never let a script reset/seed it.
- **`rainbox_claude`** — the sandbox for experiments, ad-hoc scripts, and
  schema/data pokes. Do whatever you need here.

Tests are already safe automatically: `rainbox/conftest.py` forces every pytest
run onto `rainbox_claude` (overriding even a production `DATABASE_URL`). You do
**not** need to remember anything for the test path.

What you DO need to remember — for **ad-hoc** work (manual `psql`, one-off
`python` scripts, REPL sessions):

- Target `rainbox_claude` explicitly, e.g. `psql -d rainbox_claude` or
  `DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude python …`.
- A bare `python -c "import db; ..."` uses `rainbox_production` by default —
  set `DATABASE_URL` to claude first unless you specifically intend to read
  production.
- Only operate on `rainbox_production` when the user explicitly asks, and never
  destructively (no drop/truncate/mass-update) without confirmation.

Background: tests once ran against the live DB and silently wiped the operator's
real backup settings. The split (production vs claude) + conftest exist so that
can't recur; honoring it for ad-hoc work keeps the guarantee whole.
