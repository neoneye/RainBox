"""Unit tests for main.py supervisor helpers.

Only the pure policy bits are tested here; the full supervisor_loop needs a DB
and child processes and is exercised by hand (Run demo / chat).
"""
from main import IDLE_TICK_TIMEOUT, TICK_TIMEOUT, _select_timeout


def test_idle_backoff_when_no_agents_and_no_work():
    # The whole point: a fully idle supervisor must not poll Postgres every
    # second — it backs off to the longer interval.
    assert _select_timeout(num_agents=0, found_work=False) == IDLE_TICK_TIMEOUT


def test_fast_tick_while_agents_alive():
    assert _select_timeout(num_agents=2, found_work=False) == TICK_TIMEOUT


def test_fast_tick_when_work_found_even_without_agents():
    # Work was just seen (routing/inbox/cron) — stay responsive so the next
    # pass spawns/services it promptly rather than after a backoff.
    assert _select_timeout(num_agents=0, found_work=True) == TICK_TIMEOUT


def test_fast_tick_when_agents_and_work():
    assert _select_timeout(num_agents=3, found_work=True) == TICK_TIMEOUT


def test_idle_backoff_is_longer_than_fast_tick():
    assert IDLE_TICK_TIMEOUT > TICK_TIMEOUT
