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

import pytest


@pytest.fixture(autouse=True)
def _clear_query_embedding_cache():
    """Start every test with an empty query-embedding cache.

    `seed_memory.embed_query` memoizes by query text for the life of the
    process, which is right in production (the embedder is a singleton) and
    wrong across tests: they swap in fake embedders freely, so one test's
    vector for "who is X" would otherwise be served to the next."""
    from memory.seed_memory import _embed_query_cached

    _embed_query_cached.cache_clear()
    yield
    _embed_query_cached.cache_clear()
