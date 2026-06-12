"""Session-wide test configuration.

SAFETY: force every test onto a dedicated database (`rainbox_claude`) so a test
can never read or mutate the operator's real data in `rainbox_production`. This
runs at conftest import — before pytest collects/imports any test module, and
before `webapp`/`db` build their app from `DATABASE_URL` — so even running
`pytest` with a production `DATABASE_URL` in the environment is safe.

Override the test DB with `RAINBOX_TEST_DATABASE_URL` if needed (e.g. a
throwaway DB in CI). The database must already exist:

    createdb rainbox_claude
"""
import os

os.environ["DATABASE_URL"] = os.environ.get(
    "RAINBOX_TEST_DATABASE_URL",
    "postgresql+psycopg://localhost/rainbox_claude",
)
